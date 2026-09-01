#!/usr/bin/env python3
"""One-shot polling worker for company_news_v1 inbox payloads.

Windows Task Scheduler invokes this command periodically.  Work payloads are
coordinated through the one-slot bridge; other company_news_v1 payloads continue
to use the existing canonical ingestion and Supabase synchronization adapters.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.news_monitor import NewsValidationError, validate_payload
from lib.ny_market import (
    NYMarketValidationError,
    connect_db as connect_ny_market_db,
    mark_run as mark_ny_market_run,
    upsert_report as upsert_ny_market_report,
    validate_payload as validate_ny_market_payload,
)
from tools.company_news_atomic import atomic_write_json
from tools.company_news_work_bridge import (
    BridgeError,
    BridgePaths,
    WorkPayloadValidationError,
    _pid_is_alive,
    expected_failure_path,
    expected_output_path,
    process_assignment,
    quarantine_work_output,
    record_work_failure,
    validate_assignment,
)
from tools.company_news_queue import (
    QueueError,
    QueuePaths,
    configured_slot_ids,
    increment_queue_metrics,
    reconcile_queue,
)
from tools.ingest_company_news import ingest_file
from tools.sync_company_news import sync as sync_company_news
from tools.sync_ny_market import sync as sync_ny_market

WORK_FILENAME_RE = re.compile(r"^work_(slot\d{2})_([A-Za-z0-9][A-Za-z0-9._-]{0,127})\.json$")
WORK_FAILURE_FILENAME_RE = re.compile(
    r"^work_failure_(slot\d{2})_([A-Za-z0-9][A-Za-z0-9._-]{0,127})\.json$"
)
STATE_SCHEMA = "company_news_inbox_worker_state_v1"
_JST = timezone(timedelta(hours=9))


class WorkerError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerPaths:
    root: Path
    work_dir: Path
    inbox: Path
    db: Path
    state: Path
    log: Path
    lock: Path

    @classmethod
    def from_values(
        cls,
        root: Path = ROOT,
        work_dir: Path | None = None,
        inbox: Path | None = None,
        db: Path | None = None,
    ) -> "WorkerPaths":
        root = root.resolve()
        work_dir = (work_dir or root / "data" / "news_work").resolve()
        inbox = (inbox or root / "data" / "news_inbox").resolve()
        return cls(
            root=root,
            work_dir=work_dir,
            inbox=inbox,
            db=(db or root / "decision_db.db").resolve(),
            state=work_dir / "state" / "inbox_worker.json",
            log=work_dir / "logs" / "inbox_worker.jsonl",
            lock=work_dir / "state" / "inbox_worker.lock",
        )

    def bridge(self, slot_id: str = "slot01") -> BridgePaths:
        return BridgePaths.from_root(self.root, self.work_dir, self.inbox, slot_id=slot_id)


def _now() -> str:
    return datetime.now(_JST).isoformat(timespec="seconds")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_json(path, value)


def _append_log(paths: WorkerPaths, event: str, **details: Any) -> None:
    paths.log.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": _now(), "event": event, **details}
    with paths.log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _write_console(stream: Any, value: str) -> None:
    """Write optional CLI output without requiring a console (pythonw has none)."""
    if stream is not None:
        print(value, file=stream)


def _load_state(paths: WorkerPaths) -> dict[str, Any]:
    if not paths.state.exists():
        return {"schema_version": STATE_SCHEMA, "runs": {}, "updated_at": _now()}
    try:
        state = json.loads(paths.state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerError(f"invalid worker state {paths.state}: {exc}") from exc
    if not isinstance(state, dict) or state.get("schema_version") != STATE_SCHEMA or not isinstance(state.get("runs"), dict):
        raise WorkerError(f"invalid worker state schema: {paths.state}")
    return state


def _save_state(paths: WorkerPaths, state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    _atomic_json(paths.state, state)


@contextmanager
def _worker_lock(paths: WorkerPaths):
    paths.lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    for attempt in range(2):
        try:
            descriptor = os.open(paths.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError as exc:
            try:
                match = re.search(r"pid=(\d+)", paths.lock.read_text(encoding="utf-8"))
                pid = int(match.group(1)) if match else 0
            except (OSError, ValueError):
                pid = 0
            if (not pid or not _pid_is_alive(pid)) and attempt == 0:
                paths.lock.unlink(missing_ok=True)
                continue
            raise WorkerError(f"worker already running ({paths.lock})") from exc
    if descriptor is None:
        raise WorkerError("could not acquire inbox worker lock")
    try:
        os.write(descriptor, f"pid={os.getpid()} at={_now()}\n".encode())
        os.close(descriptor)
        yield
    finally:
        paths.lock.unlink(missing_ok=True)


def _read_assignment(paths: WorkerPaths, slot_id: str = "slot01") -> dict[str, Any] | None:
    bridge = paths.bridge(slot_id)
    if not bridge.assignment.exists():
        return None
    try:
        value = json.loads(bridge.assignment.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerError(f"invalid assignment file {bridge.assignment}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkerError(f"assignment must be an object: {bridge.assignment}")
    return validate_assignment(value, bridge)


def _quarantine_generic(paths: WorkerPaths, path: Path, error: Exception) -> None:
    quarantine = paths.inbox / "quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    target = quarantine / path.name
    if target.exists():
        stamp = datetime.now(_JST).strftime("%Y%m%dT%H%M%S%f")
        target = quarantine / f"{path.stem}.{stamp}{path.suffix}"
    if path.exists():
        shutil.move(str(path), str(target))
    target.with_suffix(target.suffix + ".error.txt").write_text(str(error), encoding="utf-8")
    _append_log(paths, "quarantined", file=path.name, error=str(error))


def _archive_duplicate(paths: WorkerPaths, path: Path) -> None:
    target_dir = paths.inbox / "processed" / "duplicates"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(_JST).strftime("%Y%m%dT%H%M%S%f")
    shutil.move(str(path), str(target_dir / f"{path.stem}.{stamp}{path.suffix}"))


def _process_generic_files(
    paths: WorkerPaths,
    files: list[Path],
    state: dict[str, Any],
    *,
    sync_func: Callable[[Path, bool], dict[str, int]],
    dry_run_sync: bool,
) -> tuple[list[dict[str, Any]], int]:
    results: list[dict[str, Any]] = []
    failures = 0
    runs: dict[str, Any] = state["runs"]

    for path in files:
        _append_log(paths, "detected", file=path.name, payload_type="generic")
        payload: dict[str, Any] | None = None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise NewsValidationError("payload must be an object")
            payload = value
            run = validate_payload(payload)
            run_id = run.scan["scan_run_id"]
            _append_log(paths, "validated", file=path.name, run_id=run_id)
        except (OSError, json.JSONDecodeError, NewsValidationError, ValueError) as exc:
            # Preserve the existing adapter's failed-run ledger behavior when possible.
            if path.exists() and not ingest_file(path, paths.db, paths.inbox / "processed", paths.inbox / "quarantine"):
                _append_log(paths, "quarantined", file=path.name, error=str(exc))
            failures += 1
            results.append({"file": path.name, "status": "quarantined", "error": str(exc)})
            continue

        previous = runs.get(run_id, {})
        if previous.get("phase") == "completed":
            _archive_duplicate(paths, path)
            _append_log(paths, "processed_payload_ignored", file=path.name, run_id=run_id)
            results.append({"file": path.name, "status": "already_completed", "run_id": run_id})
            continue

        if ingest_file(path, paths.db, paths.inbox / "processed", paths.inbox / "quarantine"):
            processed = paths.inbox / "processed" / path.name
            runs[run_id] = {
                "payload_type": "company_news",
                "phase": "ingested",
                "processed_file": str(processed),
                "ingested_at": _now(),
            }
            _save_state(paths, state)
            _append_log(paths, "ingested", file=path.name, run_id=run_id)
            results.append({"file": path.name, "status": "ingested", "run_id": run_id})
        else:
            failures += 1
            _append_log(paths, "quarantined", file=path.name, run_id=run_id, error="ingestion adapter rejected payload")
            results.append({"file": path.name, "status": "quarantined", "run_id": run_id})

    pending = [
        run_id for run_id, value in runs.items()
        if value.get("phase") == "ingested" and value.get("payload_type") != "ny_market_daily"
    ]
    if pending:
        try:
            sync_result = sync_func(paths.db, dry_run_sync)
            for run_id in pending:
                runs[run_id].update({"phase": "completed", "synced_at": _now(), "sync_result": sync_result})
                _append_log(paths, "synced", run_id=run_id, sync_result=sync_result, dry_run_sync=dry_run_sync)
                _append_log(paths, "completed", run_id=run_id, payload_type="generic")
            _save_state(paths, state)
            for item in results:
                if item.get("run_id") in pending and item["status"] == "ingested":
                    item["status"] = "completed"
                    item["sync_result"] = sync_result
        except Exception as exc:
            failures += len(pending)
            for run_id in pending:
                runs[run_id]["last_error"] = str(exc)
                _append_log(paths, "failed", run_id=run_id, phase="sync", error=str(exc))
            _save_state(paths, state)

    return results, failures


def _process_ny_market_files(
    paths: WorkerPaths,
    files: list[Path],
    state: dict[str, Any],
    *,
    sync_func: Callable[[Path, bool], dict[str, int]],
    dry_run_sync: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Ingest NY payloads independently; never fall through to company-news validation."""
    results: list[dict[str, Any]] = []
    failures = 0
    runs: dict[str, Any] = state["runs"]

    for path in files:
        _append_log(paths, "detected", file=path.name, payload_type="ny_market_daily")
        validated = None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            validated = validate_ny_market_payload(payload)
            run = validated.run
            conn = connect_ny_market_db(paths.db)
            try:
                mark_ny_market_run(conn, run, "running", increment=True)
                upsert_ny_market_report(conn, validated)
                mark_ny_market_run(conn, run, "success")
            finally:
                conn.close()
            processed_dir = paths.inbox / "processed"
            processed_dir.mkdir(parents=True, exist_ok=True)
            processed_path = processed_dir / path.name
            if processed_path.exists():
                stamp = datetime.now(_JST).strftime("%Y%m%dT%H%M%S%f")
                processed_path = processed_dir / f"{path.stem}.{stamp}{path.suffix}"
            shutil.move(str(path), str(processed_path))
            stable_key = run["stable_key"]
            runs[stable_key] = {
                "payload_type": "ny_market_daily",
                "phase": "ingested",
                "run_id": run["run_id"],
                "stable_key": stable_key,
                "report_date_jst": run["report_date_jst"],
                "processed_file": str(processed_path),
                "ingested_at": _now(),
            }
            _save_state(paths, state)
            _append_log(paths, "ingested", file=path.name, run_id=stable_key, payload_type="ny_market_daily")
            results.append({"file": path.name, "status": "ingested", "run_id": stable_key})
        except (OSError, json.JSONDecodeError, NYMarketValidationError, ValueError) as exc:
            if validated is not None:
                conn = connect_ny_market_db(paths.db)
                try:
                    mark_ny_market_run(conn, validated.run, "failed", error=exc)
                finally:
                    conn.close()
            if path.exists():
                _quarantine_generic(paths, path, exc)
            failures += 1
            results.append({"file": path.name, "status": "quarantined", "error": str(exc)})

    pending = [
        key for key, value in runs.items()
        if value.get("phase") == "ingested" and value.get("payload_type") == "ny_market_daily"
    ]
    if pending:
        try:
            # A prior sync failure leaves the local run retry_pending. Promote it
            # before building the outbound batch so Supabase receives success.
            conn = connect_ny_market_db(paths.db)
            try:
                for key in pending:
                    mark_ny_market_run(conn, runs[key], "success")
            finally:
                conn.close()
            sync_result = sync_func(paths.db, dry_run_sync)
            conn = connect_ny_market_db(paths.db)
            try:
                for key in pending:
                    value = runs[key]
                    value.update({"phase": "completed", "synced_at": _now(), "sync_result": sync_result})
                    value.pop("last_error", None)
                    _append_log(paths, "synced", run_id=key, payload_type="ny_market_daily", sync_result=sync_result)
                    _append_log(paths, "completed", run_id=key, payload_type="ny_market_daily")
            finally:
                conn.close()
            _save_state(paths, state)
            for item in results:
                if item.get("run_id") in pending and item["status"] == "ingested":
                    item["status"] = "completed"
                    item["sync_result"] = sync_result
        except Exception as exc:
            failures += len(pending)
            conn = connect_ny_market_db(paths.db)
            try:
                for key in pending:
                    mark_ny_market_run(conn, runs[key], "retry_pending", error=exc)
                    runs[key]["last_error"] = str(exc)
                    _append_log(paths, "failed", run_id=key, payload_type="ny_market_daily", phase="sync", error=str(exc))
            finally:
                conn.close()
            _save_state(paths, state)

    return results, failures


def run_once(
    paths: WorkerPaths,
    *,
    sync_func: Callable[[Path, bool], dict[str, int]] = sync_company_news,
    ny_sync_func: Callable[[Path, bool], dict[str, int]] = sync_ny_market,
    dry_run_sync: bool = False,
    trigger: str = "manual",
) -> dict[str, Any]:
    try:
        lock = _worker_lock(paths)
        lock.__enter__()
    except WorkerError as exc:
        _append_log(paths, "concurrent_run_ignored", error=str(exc), trigger=trigger)
        return {"status": "busy", "detected": 0, "completed": 0, "quarantined": 0, "failed": 0}

    try:
        state = _load_state(paths)
        queue_paths = QueuePaths.from_values(paths.root, paths.work_dir)
        slot_ids = configured_slot_ids(queue_paths)
        assignments = {slot_id: _read_assignment(paths, slot_id) for slot_id in slot_ids}
        candidates = sorted(path for path in paths.inbox.glob("*.json") if path.is_file())
        failure_files = [path for path in candidates if path.name.startswith("work_failure_")]
        work_files = [
            path for path in candidates
            if path.name.startswith("work_") and path not in failure_files
        ]
        ny_market_files = [
            path for path in candidates
            if path.name.startswith("ny_market_daily_") and path not in work_files and path not in failure_files
        ]
        generic_files = [
            path for path in candidates
            if path not in work_files and path not in failure_files and path not in ny_market_files
        ]
        ny_results, ny_failures = _process_ny_market_files(
            paths, ny_market_files, state, sync_func=ny_sync_func, dry_run_sync=dry_run_sync
        )
        results, failures = _process_generic_files(
            paths, generic_files, state, sync_func=sync_func, dry_run_sync=dry_run_sync
        )
        results = ny_results + results
        failures += ny_failures

        unattended_candidate = False
        handled_slots: set[str] = set()
        stale_payloads = 0
        validation_failures = 0
        sync_retries = 0
        for path in failure_files:
            match = WORK_FAILURE_FILENAME_RE.fullmatch(path.name)
            slot_id = match.group(1) if match else "slot01"
            bridge = paths.bridge(slot_id)
            assignment = assignments.get(slot_id)
            expected = expected_failure_path(bridge, assignment) if assignment else None
            if (
                match is None
                or slot_id not in assignments
                or assignment is None
                or expected is None
                or path.resolve() != expected.resolve()
            ):
                error = BridgeError("Work failure has no matching current assignment")
                quarantine_work_output(bridge, path, error)
                results.append({"file": path.name, "slot_id": slot_id, "status": "quarantined", "error": str(error)})
                failures += 1
                stale_payloads += 1
                continue
            handled_slots.add(slot_id)
            try:
                result = record_work_failure(bridge, path)
                result.update({"file": path.name, "slot_id": slot_id})
                results.append(result)
                failures += 1
                _append_log(
                    paths,
                    "operational_failure",
                    file=path.name,
                    slot_id=slot_id,
                    assignment_id=assignment["assignment_id"],
                )
            except Exception as exc:
                failures += 1
                validation_failures += 1
                if path.exists():
                    quarantine_work_output(bridge, path, exc)
                results.append({"file": path.name, "slot_id": slot_id, "status": "failed", "error": str(exc)})

        for path in work_files:
            match = WORK_FILENAME_RE.fullmatch(path.name)
            slot_id = match.group(1) if match else "slot01"
            bridge = paths.bridge(slot_id)
            assignment = assignments.get(slot_id)
            expected = expected_output_path(bridge, assignment) if assignment else None
            _append_log(
                paths,
                "detected",
                file=path.name,
                slot_id=slot_id,
                payload_type="work",
                trigger=trigger,
            )
            if (
                match is None
                or slot_id not in assignments
                or assignment is None
                or expected is None
                or path.resolve() != expected.resolve()
            ):
                error = BridgeError("Work payload has no matching current assignment")
                quarantine_work_output(bridge, path, error)
                _append_log(
                    paths,
                    "quarantined",
                    file=path.name,
                    slot_id=slot_id,
                    error=str(error),
                    payload_type="work",
                )
                results.append({"file": path.name, "slot_id": slot_id, "status": "quarantined", "error": str(error)})
                failures += 1
                stale_payloads += 1
                continue
            handled_slots.add(slot_id)
            try:
                result = process_assignment(
                    bridge,
                    paths.db,
                    sync_func=sync_func,
                    dry_run_sync=dry_run_sync,
                    detected_by=trigger,
                )
                result["file"] = path.name
                result["slot_id"] = slot_id
                results.append(result)
                if result["status"] in {"completed", "already_completed"}:
                    _append_log(
                        paths,
                        "completed",
                        file=path.name,
                        slot_id=slot_id,
                        assignment_id=assignment["assignment_id"],
                        payload_type="work",
                    )
                    unattended_candidate = unattended_candidate or (
                        trigger == "task_scheduler" and result["status"] == "completed"
                    )
            except Exception as exc:
                failures += 1
                slot_state_path = bridge.state
                if isinstance(exc, WorkPayloadValidationError):
                    validation_failures += 1
                elif slot_state_path.exists():
                    try:
                        slot_state = json.loads(slot_state_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        slot_state = {}
                    if slot_state.get("phase") in {"ingested", "synced", "completed"}:
                        sync_retries += 1
                _append_log(
                    paths,
                    "failed",
                    file=path.name,
                    slot_id=slot_id,
                    assignment_id=assignment["assignment_id"],
                    error=str(exc),
                    payload_type="work",
                )
                results.append({"file": path.name, "slot_id": slot_id, "status": "failed", "error": str(exc)})

        # A failed Supabase sync has already moved the payload to processed.
        # Resume each affected slot independently without waiting for a new file.
        for slot_id, assignment in assignments.items():
            if assignment is None or slot_id in handled_slots or assignment["status"] == "completed":
                continue
            bridge = paths.bridge(slot_id)
            expected = expected_output_path(bridge, assignment)
            try:
                result = process_assignment(
                    bridge,
                    paths.db,
                    sync_func=sync_func,
                    dry_run_sync=dry_run_sync,
                    detected_by=trigger,
                )
                if result["status"] != "waiting":
                    result.update({"file": expected.name, "slot_id": slot_id})
                    results.append(result)
                    _append_log(
                        paths,
                        "completed",
                        file=expected.name,
                        slot_id=slot_id,
                        assignment_id=assignment["assignment_id"],
                        payload_type="work_resume",
                    )
                    unattended_candidate = unattended_candidate or (
                        trigger == "task_scheduler" and result["status"] == "completed"
                    )
            except Exception as exc:
                failures += 1
                sync_retries += 1
                _append_log(
                    paths,
                    "failed",
                    file=expected.name,
                    slot_id=slot_id,
                    assignment_id=assignment["assignment_id"],
                    error=str(exc),
                    payload_type="work_resume",
                )
                results.append({"file": expected.name, "slot_id": slot_id, "status": "failed", "error": str(exc)})

        queue_result: dict[str, Any] | None = None
        if queue_paths.entries.exists() and queue_paths.state.exists():
            try:
                increment_queue_metrics(
                    queue_paths,
                    stale_payload_count=stale_payloads,
                    validation_failure_count=validation_failures,
                    sync_retry_count=sync_retries,
                )
                queue_result = reconcile_queue(queue_paths, paths.bridge(), paths.db)
                _append_log(paths, "queue_reconciled", queue_result=queue_result.get("status"))
            except (QueueError, BridgeError, OSError, ValueError) as exc:
                failures += 1
                queue_result = {"status": "failed", "error": str(exc)}
                _append_log(paths, "queue_failed", error=str(exc))

        completed = sum(item.get("status") in {"completed", "already_completed"} for item in results)
        quarantined = sum(item.get("status") == "quarantined" for item in results)
        response: dict[str, Any] = {
            "status": "completed_with_errors" if failures else "completed",
            "trigger": trigger,
            "detected": len(candidates),
            "completed": completed,
            "quarantined": quarantined,
            "failed": failures,
            "results": results,
        }
        if queue_result is not None:
            response["queue"] = queue_result
        if unattended_candidate:
            response["unattended_local_write_candidate"] = "UNATTENDED_LOCAL_WRITE_PASS"
            _append_log(paths, "unattended_local_write_candidate", verdict="UNATTENDED_LOCAL_WRITE_PASS")
        return response
    finally:
        lock.__exit__(None, None, None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="scan once and exit (the only v2 mode)")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--inbox", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--dry-run-sync", action="store_true")
    parser.add_argument("--trigger", choices=("manual", "task_scheduler"), default="manual")
    args = parser.parse_args()
    if not args.once:
        parser.error("--once is required; v2 intentionally has no resident watch loop")

    paths = WorkerPaths.from_values(args.root, args.work_dir, args.inbox, args.db)
    _append_log(paths, "worker_started", trigger=args.trigger)
    try:
        result = run_once(paths, dry_run_sync=args.dry_run_sync, trigger=args.trigger)
    except (WorkerError, BridgeError, NewsValidationError, OSError, ValueError) as exc:
        _append_log(paths, "worker_error", trigger=args.trigger, error=str(exc), error_type=type(exc).__name__)
        _append_log(paths, "worker_finished", trigger=args.trigger, exit_status=1, processed_count=0)
        _write_console(sys.stderr, f"ERROR: {exc}")
        return 1
    except Exception as exc:
        _append_log(paths, "worker_error", trigger=args.trigger, error=str(exc), error_type=type(exc).__name__)
        _append_log(paths, "worker_finished", trigger=args.trigger, exit_status=1, processed_count=0)
        raise
    exit_status = 1 if result["failed"] else 0
    processed_count = result["completed"] + result["quarantined"]
    _append_log(
        paths,
        "worker_finished",
        trigger=args.trigger,
        exit_status=exit_status,
        processed_count=processed_count,
        detected=result["detected"],
        completed=result["completed"],
        quarantined=result["quarantined"],
        failed=result["failed"],
    )
    _write_console(sys.stdout, json.dumps(result, ensure_ascii=False, indent=2))
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
