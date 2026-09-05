#!/usr/bin/env python3
"""Local one-shot transport worker for staged Sector Weekly research payloads."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.sector_weekly import connect_sector_db, now_jst, upsert_report, validate_report
from lib.sector_weekly_work import (
    complete_staged_assignment,
    get_assignment,
    is_quality_revision,
    mark_staged_sync_error,
    payload_hash,
    prepare_staged_sync,
    reject_staged_payload,
)
from tools.company_news_atomic import replace_with_retry
from tools.company_news_work_bridge import _pid_is_alive
from tools.sector_weekly_scheduler import assemble_payload
from tools.sector_weekly_work_bridge import (
    DEFAULT_WORK_ROOT,
    SectorBridgeError,
    _read_envelope,
    _validate_ownership,
    _window,
)
from tools.sync_sector_weekly import sync_one as sync_sector_weekly

ASSIGNMENT_FILE_RE = re.compile(
    r"^(?P<assignment>[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\.json$"
)


class SectorInboxWorkerError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerPaths:
    root: Path
    db: Path
    work_root: Path
    inbox: Path
    processed: Path
    quarantine: Path
    log: Path
    lock: Path

    @classmethod
    def from_values(
        cls,
        root: Path = ROOT,
        db: Path | None = None,
        work_root: Path | None = None,
    ) -> "WorkerPaths":
        root = root.resolve()
        work = (work_root or DEFAULT_WORK_ROOT).resolve()
        return cls(
            root=root,
            db=(db or root / "decision_db.db").resolve(),
            work_root=work,
            inbox=work / "inbox",
            processed=work / "processed",
            quarantine=work / "quarantine",
            log=work / "logs" / "inbox_worker.jsonl",
            lock=work / "state" / "inbox_worker.lock",
        )


def _now_text() -> str:
    return now_jst().isoformat(timespec="seconds")


def _safe_error(error: Exception, *, transport: bool = False) -> str:
    if transport:
        return f"external sector sync failed ({type(error).__name__})"
    return f"{type(error).__name__}: {str(error)[:500]}"


def _append_log(paths: WorkerPaths, event: str, **details: Any) -> None:
    paths.log.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": _now_text(), "event": event, **details}
    with paths.log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


@contextmanager
def _worker_lock(paths: WorkerPaths) -> Iterator[None]:
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
            raise SectorInboxWorkerError("Sector Weekly inbox worker is already running") from exc
    if descriptor is None:
        raise SectorInboxWorkerError("could not acquire Sector Weekly inbox worker lock")
    try:
        os.write(descriptor, f"pid={os.getpid()} at={_now_text()}\n".encode("utf-8"))
        os.close(descriptor)
        yield
    finally:
        paths.lock.unlink(missing_ok=True)


def _move_atomic(source: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / source.name
    if target.exists():
        target = directory / f"{source.stem}.{datetime.now().strftime('%Y%m%dT%H%M%S%f')}{source.suffix}"
    replace_with_retry(source, target)
    return target


def _replace_processed(source: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / source.name
    replace_with_retry(source, target)
    return target


def _revision_sync_database(paths: WorkerPaths, validated: Any, assignment_id: str, digest: str) -> Path:
    """Create an isolated snapshot so failed transport cannot replace local canonical data."""
    state = paths.work_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    target = state / f"quality-revision-{assignment_id}-{digest[:12]}-{uuid.uuid4().hex}.sqlite"
    source = sqlite3.connect(paths.db)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    try:
        temp_conn = connect_sector_db(target)
        try:
            upsert_report(temp_conn, validated)
            prepare_staged_sync(temp_conn, assignment_id, digest)
        finally:
            temp_conn.close()
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def _remove_identical_quarantine(paths: WorkerPaths, assignment_id: str, digest: str) -> bool:
    candidate = paths.quarantine / f"{assignment_id}.json"
    if not candidate.exists():
        return False
    try:
        envelope = _read_envelope(candidate)
    except Exception:
        return False
    if payload_hash(envelope) != digest:
        return False
    candidate.unlink()
    return True


def process_one(
    paths: WorkerPaths,
    path: Path,
    *,
    sync_func: Callable[[Path, str, str, bool], dict[str, int]] = sync_sector_weekly,
    dry_run_sync: bool = False,
) -> dict[str, Any]:
    match = ASSIGNMENT_FILE_RE.fullmatch(path.name)
    if match is None:
        raise SectorInboxWorkerError("inbox filename is not an assignment UUID")
    assignment_id = match.group("assignment")
    conn = connect_sector_db(paths.db)
    digest: str | None = None
    try:
        row = get_assignment(conn, assignment_id)
        if row is None:
            raise SectorBridgeError("assignment not found")
        try:
            envelope = _read_envelope(path)
            _validate_ownership(envelope, row, assignment_id, str(envelope.get("claim_owner", "")))
            if envelope["assignment_id"] != assignment_id:
                raise SectorBridgeError("filename assignment ID does not match payload")
            digest = payload_hash(envelope)
            if row["submitted_payload_hash"] != digest:
                raise SectorBridgeError("logical payload hash does not match staged assignment")
            if row["status"] == "success":
                revision_archive = paths.work_root / "revisions" / assignment_id
                processed = (
                    _replace_processed(path, paths.processed)
                    if revision_archive.is_dir() else _move_atomic(path, paths.processed)
                )
                return {
                    "status": "already_success", "assignment_id": assignment_id,
                    "payload_hash": digest, "processed_path": str(processed),
                }
            if row["status"] != "retry_pending" or not row["submitted_payload_hash"]:
                raise SectorBridgeError("assignment is not staged for transport")
            window = _window(row)
            payload = assemble_payload(
                envelope["report"], int(row["sector_code"]), window, generated_at=now_jst(),
            )
            validated = validate_report(
                payload, expected_code=int(row["sector_code"]), expected_window=window,
            )
        except Exception as exc:
            quarantined = _move_atomic(path, paths.quarantine)
            if row["status"] == "retry_pending" and row["submitted_payload_hash"]:
                reject_staged_payload(conn, assignment_id, exc)
            return {
                "status": "quarantined", "assignment_id": assignment_id,
                "error": _safe_error(exc), "quarantine_path": str(quarantined),
            }

        revision = is_quality_revision(row)
        report = validated.report
        dedupe_key = report.get("dedupe_key")
        run_id = report.get("run_id")
        if not isinstance(dedupe_key, str) or not dedupe_key.strip():
            raise SectorInboxWorkerError("validated report is missing dedupe_key")
        if not isinstance(run_id, str) or not run_id.strip():
            raise SectorInboxWorkerError("validated report is missing run_id")
        sync_db = paths.db
        revision_db: Path | None = None
        if revision:
            revision_db = _revision_sync_database(paths, validated, assignment_id, digest)
            sync_db = revision_db
        else:
            upsert_report(conn, validated)
            prepare_staged_sync(conn, assignment_id, digest)
        try:
            sync_result = sync_func(sync_db, dedupe_key, run_id, dry_run_sync)
        except Exception as exc:
            summary = _safe_error(exc, transport=True)
            mark_staged_sync_error(conn, assignment_id, digest, RuntimeError(summary))
            if revision_db is not None:
                revision_db.unlink(missing_ok=True)
            return {
                "status": "sync_error", "assignment_id": assignment_id,
                "payload_hash": digest, "error": summary,
            }
        if revision:
            upsert_report(conn, validated)
        completed = complete_staged_assignment(conn, assignment_id, digest)
        if revision_db is not None:
            revision_db.unlink(missing_ok=True)
        processed = _replace_processed(path, paths.processed) if revision else _move_atomic(path, paths.processed)
        duplicate_removed = _remove_identical_quarantine(paths, assignment_id, digest)
        return {
            "status": "success", "assignment_id": assignment_id,
            "attempt_count": int(completed["attempt_count"]), "payload_hash": digest,
            "sync_result": sync_result, "processed_path": str(processed),
            "identical_quarantine_removed": duplicate_removed,
            "sector_name": report["sector_name"],
            "period_start": report["period_start"], "period_end": report["period_end"],
            "dedupe_key": dedupe_key, "run_id": run_id,
        }
    finally:
        conn.close()


def run_once(
    paths: WorkerPaths,
    *,
    sync_func: Callable[[Path, str, str, bool], dict[str, int]] = sync_sector_weekly,
    dry_run_sync: bool = False,
    trigger: str = "manual",
) -> dict[str, Any]:
    try:
        lock = _worker_lock(paths)
        lock.__enter__()
    except SectorInboxWorkerError as exc:
        _append_log(paths, "concurrent_run_ignored", trigger=trigger, error=_safe_error(exc))
        return {
            "status": "busy", "trigger": trigger,
            "detected": 0, "success": 0, "failed": 0, "results": [],
        }
    try:
        candidates = sorted(path for path in paths.inbox.glob("*.json") if path.is_file())
        results: list[dict[str, Any]] = []
        failures = 0
        for path in candidates:
            _append_log(paths, "payload_detected", trigger=trigger, file=path.name)
            try:
                result = process_one(paths, path, sync_func=sync_func, dry_run_sync=dry_run_sync)
            except Exception as exc:
                quarantined = _move_atomic(path, paths.quarantine) if path.exists() else None
                result = {
                    "status": "quarantined", "file": path.name, "error": _safe_error(exc),
                    "quarantine_path": str(quarantined) if quarantined else None,
                }
            result.setdefault("file", path.name)
            results.append(result)
            if result["status"] not in {"success", "already_success"}:
                failures += 1
            _append_log(paths, "payload_finished", trigger=trigger, **result)
        return {
            "status": (
                "no_work" if not candidates
                else "completed_with_errors" if failures
                else "completed"
            ),
            "trigger": trigger, "detected": len(candidates),
            "success": sum(item["status"] in {"success", "already_success"} for item in results),
            "failed": failures, "results": results,
        }
    finally:
        lock.__exit__(None, None, None)


def main() -> int:
    started_at = monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--dry-run-sync", action="store_true")
    parser.add_argument("--trigger", choices=("manual", "task_scheduler"), default="manual")
    args = parser.parse_args()
    if not args.once:
        parser.error("--once is required")
    paths = WorkerPaths.from_values(args.root, args.db, args.work_root)
    _append_log(paths, "worker_started", trigger=args.trigger)
    try:
        result = run_once(paths, dry_run_sync=args.dry_run_sync, trigger=args.trigger)
    except Exception as exc:
        error = _safe_error(exc)
        _append_log(paths, "worker_error", trigger=args.trigger, error=error)
        _append_log(
            paths,
            "worker_finished",
            trigger=args.trigger,
            status="error",
            detected=0,
            success=0,
            failed=1,
            exit_status=1,
            duration_seconds=round(monotonic() - started_at, 3),
            error=error,
        )
        if sys.stderr is not None:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    exit_status = 1 if result["failed"] else 0
    _append_log(
        paths,
        "worker_finished",
        exit_status=exit_status,
        duration_seconds=round(monotonic() - started_at, 3),
        **result,
    )
    if sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
