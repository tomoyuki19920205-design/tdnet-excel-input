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
from tools.company_news_work_bridge import (
    BridgeError,
    BridgePaths,
    _pid_is_alive,
    expected_output_path,
    process_assignment,
    quarantine_work_output,
    validate_assignment,
)
from tools.ingest_company_news import ingest_file
from tools.sync_company_news import sync as sync_company_news

WORK_FILENAME_RE = re.compile(r"^work_(slot\d+)_([A-Za-z0-9][A-Za-z0-9._-]{0,127})\.json$")
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

    def bridge(self) -> BridgePaths:
        return BridgePaths.from_root(self.root, self.work_dir, self.inbox)


def _now() -> str:
    return datetime.now(_JST).isoformat(timespec="seconds")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _append_log(paths: WorkerPaths, event: str, **details: Any) -> None:
    paths.log.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": _now(), "event": event, **details}
    with paths.log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


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


def _read_assignment(paths: WorkerPaths) -> dict[str, Any] | None:
    bridge = paths.bridge()
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

    pending = [run_id for run_id, value in runs.items() if value.get("phase") == "ingested"]
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


def run_once(
    paths: WorkerPaths,
    *,
    sync_func: Callable[[Path, bool], dict[str, int]] = sync_company_news,
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
        assignment = _read_assignment(paths)
        candidates = sorted(path for path in paths.inbox.glob("*.json") if path.is_file())
        work_files = [path for path in candidates if path.name.startswith("work_")]
        generic_files = [path for path in candidates if path not in work_files]
        results, failures = _process_generic_files(
            paths, generic_files, state, sync_func=sync_func, dry_run_sync=dry_run_sync
        )

        bridge = paths.bridge()
        expected = expected_output_path(bridge, assignment) if assignment else None
        unattended_candidate = False
        for path in work_files:
            _append_log(paths, "detected", file=path.name, payload_type="work", trigger=trigger)
            if assignment is None or expected is None or path.resolve() != expected.resolve():
                error = BridgeError("Work payload has no matching current assignment")
                quarantine_work_output(bridge, path, error)
                _append_log(paths, "quarantined", file=path.name, error=str(error), payload_type="work")
                results.append({"file": path.name, "status": "quarantined", "error": str(error)})
                failures += 1
                continue
            try:
                result = process_assignment(
                    bridge,
                    paths.db,
                    sync_func=sync_func,
                    dry_run_sync=dry_run_sync,
                    detected_by=trigger,
                )
                result["file"] = path.name
                results.append(result)
                if result["status"] in {"completed", "already_completed"}:
                    _append_log(paths, "completed", file=path.name, assignment_id=assignment["assignment_id"], payload_type="work")
                    unattended_candidate = trigger == "task_scheduler" and result["status"] == "completed"
            except Exception as exc:
                failures += 1
                _append_log(paths, "failed", file=path.name, assignment_id=assignment["assignment_id"], error=str(exc), payload_type="work")
                results.append({"file": path.name, "status": "failed", "error": str(exc)})

        if assignment is not None and expected is not None and expected not in work_files and assignment["status"] != "completed":
            try:
                result = process_assignment(
                    bridge,
                    paths.db,
                    sync_func=sync_func,
                    dry_run_sync=dry_run_sync,
                    detected_by=trigger,
                )
                if result["status"] != "waiting":
                    result["file"] = expected.name
                    results.append(result)
                    _append_log(paths, "completed", file=expected.name, assignment_id=assignment["assignment_id"], payload_type="work_resume")
                    unattended_candidate = trigger == "task_scheduler" and result["status"] == "completed"
            except Exception as exc:
                failures += 1
                _append_log(paths, "failed", file=expected.name, assignment_id=assignment["assignment_id"], error=str(exc), payload_type="work_resume")
                results.append({"file": expected.name, "status": "failed", "error": str(exc)})

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
    try:
        result = run_once(paths, dry_run_sync=args.dry_run_sync, trigger=args.trigger)
    except (WorkerError, BridgeError, NewsValidationError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
