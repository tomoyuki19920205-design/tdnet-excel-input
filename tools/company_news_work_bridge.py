#!/usr/bin/env python3
"""One-slot Desktop ChatGPT Work -> Company News Monitor bridge.

This coordinator never trusts Work output as a database write. It validates the
existing ``company_news_v1`` contract, delegates local ingestion to the existing
adapter, then delegates Supabase synchronization to the existing sync module.
There is intentionally no watch loop or automatic next-company advance in v1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.runtime_paths import runtime_path
from lib.news_monitor import NewsValidationError, normalize_ticker, validate_payload
from tools.company_news_atomic import atomic_write_json
from tools.ingest_company_news import ingest_file
from tools.sync_company_news import sync as sync_company_news

ASSIGNMENT_SCHEMA = "company_news_assignment_v1"
WORK_FAILURE_SCHEMA = "company_news_work_failure_v1"
DEFAULT_SLOT_ID = "slot01"
# Backward-compatible import used by existing callers/tests.
SLOT_ID = DEFAULT_SLOT_ID
ASSIGNMENT_STATUSES = frozenset({"ready", "completed", "failed"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SLOT_ID_RE = re.compile(r"^slot\d{2}$")
_JST = timezone(timedelta(hours=9))


class BridgeError(RuntimeError):
    pass


class WorkPayloadValidationError(BridgeError):
    """A Work payload violates the canonical or assignment contract."""

    pass


@dataclass(frozen=True)
class BridgePaths:
    root: Path
    work_dir: Path
    inbox: Path
    slot_id: str
    assignment: Path
    state: Path
    log: Path
    lock: Path

    @classmethod
    def from_root(
        cls,
        root: Path = ROOT,
        work_dir: Path | None = None,
        inbox: Path | None = None,
        slot_id: str = DEFAULT_SLOT_ID,
    ) -> "BridgePaths":
        root = root.resolve()
        work_dir = (work_dir or runtime_path(root / "data" / "news_work", code_root=root)).resolve()
        inbox = (inbox or runtime_path(root / "data" / "news_inbox", code_root=root)).resolve()
        if not _SLOT_ID_RE.fullmatch(slot_id):
            raise BridgeError(f"invalid slot_id: {slot_id}")
        return cls(
            root=root,
            work_dir=work_dir,
            inbox=inbox,
            slot_id=slot_id,
            assignment=work_dir / "slots" / slot_id / "assignment.json",
            state=work_dir / "state" / f"{slot_id}.json",
            log=work_dir / "logs" / f"{slot_id}.jsonl",
            lock=work_dir / "state" / f"{slot_id}.lock",
        )


def _now() -> str:
    return datetime.now(_JST).isoformat(timespec="seconds")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_json(path, value)


def _append_log(paths: BridgePaths, event: str, **details: Any) -> None:
    paths.log.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": _now(), "slot_id": paths.slot_id, "event": event, **details}
    with paths.log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _pid_is_alive(pid: int) -> bool:
    """Return whether *pid* is running without signalling it on Windows."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    # On Windows, os.kill(pid, 0) calls TerminateProcess rather than performing
    # the non-destructive existence probe provided by POSIX.  Open a waitable
    # process handle instead and poll it with a zero timeout.
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    error_access_denied = 5
    error_invalid_parameter = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_for_single_object.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    handle = open_process(synchronize, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            return False
        if error == error_access_denied:
            return True
        # An indeterminate probe must not make an active lock look stale.
        return True
    try:
        status = wait_for_single_object(handle, 0)
        if status == wait_timeout:
            return True
        if status == wait_object_0:
            return False
        return True
    finally:
        close_handle(handle)


@contextmanager
def _slot_lock(paths: BridgePaths):
    paths.lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = None
    for attempt in range(2):
        try:
            descriptor = os.open(paths.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError as exc:
            try:
                match = re.search(r"pid=(\d+)", paths.lock.read_text(encoding="utf-8"))
                lock_pid = int(match.group(1)) if match else None
                if lock_pid and not _pid_is_alive(lock_pid):
                    raise ProcessLookupError(lock_pid)
            except (OSError, ValueError):
                paths.lock.unlink(missing_ok=True)
                if attempt == 0:
                    continue
            raise BridgeError(f"{paths.slot_id} is already being processed ({paths.lock})") from exc
    if descriptor is None:
        raise BridgeError(f"could not acquire {paths.slot_id} lock")
    try:
        os.write(descriptor, f"pid={os.getpid()} at={_now()}\n".encode())
        os.close(descriptor)
        yield
    finally:
        paths.lock.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BridgeError(f"{path} must contain a JSON object")
    return value


def validate_assignment(value: dict[str, Any], paths: BridgePaths) -> dict[str, Any]:
    required = {"schema_version", "slot_id", "assignment_id", "ticker", "company_name", "search_from", "search_to", "status", "output_directory", "created_at"}
    missing = sorted(required.difference(value))
    if missing:
        raise BridgeError(f"assignment missing field(s): {', '.join(missing)}")
    if value["schema_version"] != ASSIGNMENT_SCHEMA:
        raise BridgeError(f"schema_version must be {ASSIGNMENT_SCHEMA}")
    if value["slot_id"] != paths.slot_id:
        raise BridgeError(f"slot_id must be {paths.slot_id}")
    assignment_id = value["assignment_id"]
    if not isinstance(assignment_id, str) or not _ID_RE.fullmatch(assignment_id):
        raise BridgeError("assignment_id contains unsupported characters")
    try:
        value["ticker"] = normalize_ticker(value["ticker"])
    except NewsValidationError as exc:
        raise BridgeError(str(exc)) from exc
    if not isinstance(value["company_name"], str) or not value["company_name"].strip():
        raise BridgeError("company_name must be a non-empty string")
    try:
        search_from = date.fromisoformat(value["search_from"])
        search_to = date.fromisoformat(value["search_to"])
        created_at = datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BridgeError("assignment dates must use ISO-8601") from exc
    if search_from > search_to:
        raise BridgeError("search_from must not be after search_to")
    if created_at.tzinfo is None:
        raise BridgeError("created_at must include a timezone")
    if value["status"] not in ASSIGNMENT_STATUSES:
        raise BridgeError(f"unsupported assignment status: {value['status']}")
    output = runtime_path(paths.root / value["output_directory"], code_root=paths.root).resolve()
    if output != paths.inbox:
        raise BridgeError(f"output_directory must resolve to {paths.inbox}")
    return value


def _load_state(paths: BridgePaths, assignment_id: str) -> dict[str, Any]:
    if not paths.state.exists():
        return {"schema_version": "company_news_bridge_state_v1", "slot_id": paths.slot_id, "assignment_id": assignment_id, "phase": "waiting", "updated_at": _now()}
    state = _read_json(paths.state)
    if state.get("assignment_id") != assignment_id:
        return {"schema_version": "company_news_bridge_state_v1", "slot_id": paths.slot_id, "assignment_id": assignment_id, "phase": "waiting", "updated_at": _now()}
    return state


def _save_state(paths: BridgePaths, state: dict[str, Any], phase: str, **details: Any) -> None:
    state.update({"phase": phase, "updated_at": _now(), **details})
    _atomic_json(paths.state, state)


def _set_assignment_status(
    paths: BridgePaths,
    assignment: dict[str, Any],
    status: str,
    error: str | None = None,
    *,
    news_item_count: int | None = None,
) -> None:
    assignment["status"] = status
    assignment["updated_at"] = _now()
    if error:
        assignment["error_message"] = error[:1000]
    else:
        assignment.pop("error_message", None)
    if news_item_count is not None:
        assignment["news_item_count"] = news_item_count
    _atomic_json(paths.assignment, assignment)


def expected_output_path(paths: BridgePaths, assignment: dict[str, Any]) -> Path:
    return paths.inbox / f"work_{paths.slot_id}_{assignment['assignment_id']}.json"


def expected_failure_path(paths: BridgePaths, assignment: dict[str, Any]) -> Path:
    return paths.inbox / f"work_failure_{paths.slot_id}_{assignment['assignment_id']}.json"


def _processed_output_path(paths: BridgePaths, assignment: dict[str, Any]) -> Path:
    return paths.inbox / "processed" / expected_output_path(paths, assignment).name


def _run_exists(db_path: Path, assignment_id: str) -> bool:
    if not db_path.exists():
        return False
    connection = sqlite3.connect(db_path)
    try:
        table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_news_scan_runs'").fetchone()
        return bool(table and connection.execute("SELECT 1 FROM canonical_news_scan_runs WHERE scan_run_id=?", (assignment_id,)).fetchone())
    finally:
        connection.close()


def _validate_result(path: Path, assignment: dict[str, Any]) -> int:
    try:
        payload = _read_json(path)
        run = validate_payload(payload)
    except (BridgeError, NewsValidationError) as exc:
        raise WorkPayloadValidationError(str(exc)) from exc
    if payload.get("run_id") != assignment["assignment_id"]:
        raise WorkPayloadValidationError("Work output run_id must equal assignment_id")
    if run.scan["ticker"] != assignment["ticker"]:
        raise WorkPayloadValidationError("Work output ticker does not match assignment ticker")
    search_from = date.fromisoformat(assignment["search_from"])
    search_to = date.fromisoformat(assignment["search_to"])
    for index, event in enumerate(run.events):
        published = datetime.fromisoformat(event["published_at"]).date()
        if published < search_from or published > search_to:
            raise WorkPayloadValidationError(f"items[{index}].published_at is outside assignment search range")
    return len(run.events)


def record_work_failure(paths: BridgePaths, path: Path) -> dict[str, Any]:
    with _slot_lock(paths):
        assignment = validate_assignment(_read_json(paths.assignment), paths)
        value = _read_json(path)
        required = {
            "schema_version", "task_id", "slot_id", "assignment_id", "ticker", "queue_id",
            "error_type", "error_message", "sources_attempted", "created_at",
        }
        missing = sorted(required.difference(value))
        if missing:
            raise BridgeError(f"Work failure missing field(s): {', '.join(missing)}")
        if value["schema_version"] != WORK_FAILURE_SCHEMA:
            raise BridgeError(f"schema_version must be {WORK_FAILURE_SCHEMA}")
        for field in ("slot_id", "assignment_id", "ticker", "queue_id"):
            if value[field] != assignment.get(field):
                raise BridgeError(f"Work failure {field} does not match current assignment")
        if value["task_id"] != assignment.get("scheduled_task_id", value["task_id"]):
            raise BridgeError("Work failure task_id does not own current assignment")
        if not isinstance(value["error_type"], str) or not value["error_type"].strip():
            raise BridgeError("Work failure error_type must be a non-empty string")
        if not isinstance(value["error_message"], str) or not value["error_message"].strip():
            raise BridgeError("Work failure error_message must be a non-empty string")
        if not isinstance(value["sources_attempted"], list) or any(
            not isinstance(source, str) or not source.strip() for source in value["sources_attempted"]
        ):
            raise BridgeError("Work failure sources_attempted must contain only non-empty strings")
        try:
            created_at = datetime.fromisoformat(str(value["created_at"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise BridgeError("Work failure created_at must use ISO-8601") from exc
        if created_at.tzinfo is None:
            raise BridgeError("Work failure created_at must include a timezone")
        message = f"{value['error_type']}: {value['error_message']}"
        _set_assignment_status(paths, assignment, "failed", message)
        state = _load_state(paths, assignment["assignment_id"])
        _save_state(paths, state, state.get("phase", "waiting"), last_error=message, failure=value)
        target_dir = paths.inbox / "processed" / "failures"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        if target.exists():
            if target.read_bytes() != path.read_bytes():
                raise BridgeError("conflicting duplicate Work failure sidecar")
            path.unlink()
        else:
            shutil.move(str(path), str(target))
        _append_log(paths, "operational_failure", assignment_id=assignment["assignment_id"], error=message)
        return {"status": "failure_recorded", "assignment_id": assignment["assignment_id"], "error": message}


def _quarantine_result(paths: BridgePaths, path: Path, error: Exception) -> None:
    quarantine = paths.inbox / "quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    target = quarantine / path.name
    if target.exists():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12] if path.exists() else "missing"
        target = quarantine / f"{path.stem}.{digest}{path.suffix}"
    if path.exists():
        shutil.move(str(path), str(target))
    target.with_suffix(target.suffix + ".error.txt").write_text(str(error), encoding="utf-8")


def quarantine_work_output(paths: BridgePaths, path: Path, error: Exception) -> Path:
    """Quarantine one Work payload without aborting an outer inbox scan."""
    _quarantine_result(paths, path, error)
    target = paths.inbox / "quarantine" / path.name
    if not target.exists():
        matches = sorted((paths.inbox / "quarantine").glob(f"{path.stem}.*{path.suffix}"))
        target = matches[-1] if matches else target
    _append_log(paths, "quarantined", file=path.name, error=str(error))
    return target


def _archive_completed_duplicate(paths: BridgePaths, output: Path, processed: Path) -> None:
    if not output.exists():
        return
    if processed.exists() and output.read_bytes() != processed.read_bytes():
        raise BridgeError("completed assignment received a conflicting payload")
    duplicates = paths.inbox / "processed" / "duplicates"
    duplicates.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()[:12]
    target = duplicates / f"{output.stem}.{digest}{output.suffix}"
    if target.exists():
        output.unlink()
    else:
        shutil.move(str(output), str(target))


def _record_output_arrival(
    paths: BridgePaths,
    assignment: dict[str, Any],
    state: dict[str, Any],
    output: Path,
    *,
    detected_by: str | None = None,
) -> None:
    details: dict[str, Any] = {
        "expected_output": str(expected_output_path(paths, assignment)),
        "assignment_created_at": assignment["created_at"],
    }
    if output.exists():
        details["output_arrived_at"] = datetime.fromtimestamp(output.stat().st_mtime, _JST).isoformat(timespec="seconds")
    if detected_by:
        details["output_detected_by"] = detected_by
    _save_state(paths, state, state.get("phase", "waiting"), **details)


def bridge_status(paths: BridgePaths, db_path: Path) -> dict[str, Any]:
    assignment = validate_assignment(_read_json(paths.assignment), paths)
    output = expected_output_path(paths, assignment)
    processed = _processed_output_path(paths, assignment)
    state = _load_state(paths, assignment["assignment_id"])
    return {
        "slot_id": paths.slot_id,
        "assignment_id": assignment["assignment_id"],
        "assignment_status": assignment["status"],
        "bridge_phase": state.get("phase", "waiting"),
        "output_expected": str(output),
        "output_present": output.exists(),
        "processed_present": processed.exists(),
        "canonical_run_present": _run_exists(db_path, assignment["assignment_id"]),
        "assignment_created_at": assignment["created_at"],
        "output_arrived_at": state.get("output_arrived_at"),
        "output_detected_by": state.get("output_detected_by"),
    }


def process_assignment(
    paths: BridgePaths,
    db_path: Path,
    *,
    sync_func: Callable[[Path, bool], dict[str, int]] = sync_company_news,
    dry_run_sync: bool = False,
    detected_by: str | None = None,
) -> dict[str, Any]:
    with _slot_lock(paths):
        assignment = validate_assignment(_read_json(paths.assignment), paths)
        assignment_id = assignment["assignment_id"]
        state = _load_state(paths, assignment_id)
        output = expected_output_path(paths, assignment)
        processed = _processed_output_path(paths, assignment)
        if output.exists():
            _record_output_arrival(paths, assignment, state, output, detected_by=detected_by)
            state = _load_state(paths, assignment_id)
        if assignment["status"] == "completed":
            try:
                _archive_completed_duplicate(paths, output, processed)
            except Exception as exc:
                quarantine_work_output(paths, output, exc)
                raise
            _append_log(paths, "already_completed", assignment_id=assignment_id)
            return {"status": "already_completed", "assignment_id": assignment_id}

        if not output.exists() and not (processed.exists() and _run_exists(db_path, assignment_id)):
            _append_log(paths, "waiting_for_output", assignment_id=assignment_id, expected=str(output))
            return {"status": "waiting", "assignment_id": assignment_id, "expected_output": str(output)}

        try:
            result_path = output if output.exists() else processed
            news_item_count = _validate_result(result_path, assignment)
            _append_log(paths, "validated", assignment_id=assignment_id, file=result_path.name)
            if state.get("phase") != "ingested" and not _run_exists(db_path, assignment_id):
                if not output.exists():
                    raise BridgeError("processed output exists but canonical run is missing; manual review required")
                if not ingest_file(output, db_path, paths.inbox / "processed", paths.inbox / "quarantine"):
                    raise BridgeError("existing ingestion adapter quarantined Work output")
                _save_state(paths, state, "ingested", processed_file=str(processed))
                _append_log(paths, "ingested", assignment_id=assignment_id)
            elif state.get("phase") not in {"synced", "completed"}:
                _save_state(paths, state, "ingested", processed_file=str(processed), resumed=True)

            state = _load_state(paths, assignment_id)
            if state.get("phase") in {"synced", "completed"}:
                sync_result = state.get("sync_result", {})
            else:
                sync_result = sync_func(db_path, dry_run_sync)
                _save_state(paths, state, "synced", sync_result=sync_result, dry_run_sync=dry_run_sync)
                _append_log(paths, "synced", assignment_id=assignment_id, sync_result=sync_result, dry_run_sync=dry_run_sync)
            _set_assignment_status(paths, assignment, "completed", news_item_count=news_item_count)
            state = _load_state(paths, assignment_id)
            _save_state(paths, state, "completed", sync_result=sync_result, dry_run_sync=dry_run_sync)
            _append_log(paths, "completed", assignment_id=assignment_id, sync_result=sync_result, dry_run_sync=dry_run_sync)
            return {"status": "completed", "assignment_id": assignment_id, "sync_result": sync_result, "dry_run_sync": dry_run_sync}
        except Exception as exc:
            if output.exists() and not _run_exists(db_path, assignment_id):
                quarantine_work_output(paths, output, exc)
            durable_assignment = validate_assignment(_read_json(paths.assignment), paths)
            durable_completed = durable_assignment["status"] == "completed" and _run_exists(db_path, assignment_id)
            if not durable_completed:
                _set_assignment_status(paths, assignment, "failed", str(exc))
                _save_state(paths, state, state.get("phase", "waiting"), last_error=str(exc))
            _append_log(paths, "failed", assignment_id=assignment_id, error=str(exc))
            raise


def _company_from_master(master_db: Path, requested_ticker: str | None) -> tuple[str, str]:
    connection = sqlite3.connect(master_db)
    connection.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "companies" in tables:
            query = "SELECT ticker_code AS ticker,name_ja AS company_name FROM companies WHERE name_ja IS NOT NULL"
            params: tuple[Any, ...] = ()
            if requested_ticker:
                query += " AND ticker_code=?"; params = (requested_ticker,)
            query += " ORDER BY ticker_code LIMIT 1"
        elif "market_data_universe" in tables:
            query = "SELECT ticker,company_name FROM market_data_universe WHERE company_name IS NOT NULL"
            params = ()
            if requested_ticker:
                query += " AND ticker IN (?,?)"; params = (requested_ticker, requested_ticker + "0")
            query += " ORDER BY date DESC,ticker LIMIT 1"
        else:
            raise BridgeError(f"no supported company master table in {master_db}")
        row = connection.execute(query, params).fetchone()
        if not row:
            raise BridgeError("no matching company found in company master")
        ticker = str(row["ticker"]).upper()
        if len(ticker) == 5 and ticker.endswith("0"):
            ticker = ticker[:4]
        return normalize_ticker(ticker), str(row["company_name"]).strip()
    finally:
        connection.close()


def create_assignment(
    paths: BridgePaths,
    master_db: Path,
    *,
    assignment_id: str,
    ticker: str | None = None,
    search_from: date,
    search_to: date,
) -> dict[str, Any]:
    if not _ID_RE.fullmatch(assignment_id):
        raise BridgeError("assignment_id contains unsupported characters")
    selected_ticker, company_name = _company_from_master(master_db, normalize_ticker(ticker) if ticker else None)
    assignment = {
        "schema_version": ASSIGNMENT_SCHEMA,
        "slot_id": paths.slot_id,
        "assignment_id": assignment_id,
        "ticker": selected_ticker,
        "company_name": company_name,
        "search_from": search_from.isoformat(),
        "search_to": search_to.isoformat(),
        "status": "ready",
        "output_directory": "data/news_inbox",
        "created_at": _now(),
    }
    if paths.assignment.exists():
        existing = validate_assignment(_read_json(paths.assignment), paths)
        if existing == assignment or existing["assignment_id"] == assignment_id:
            return existing
        if existing["status"] != "completed":
            raise BridgeError(f"unfinished assignment already exists: {existing['assignment_id']}")
        raise BridgeError("v1 does not auto-advance; archive the completed assignment before creating another")
    _atomic_json(paths.assignment, assignment)
    _save_state(paths, _load_state(paths, assignment_id), "waiting")
    _append_log(paths, "assignment_created", assignment_id=assignment_id, ticker=selected_ticker)
    return assignment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--inbox", type=Path)
    parser.add_argument("--slot-id", default=DEFAULT_SLOT_ID)
    parser.add_argument("--db", type=Path, default=runtime_path(ROOT / "decision_db.db", code_root=ROOT))
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--master-db", type=Path, default=runtime_path(ROOT / "data" / "jquants.db", code_root=ROOT))
    create.add_argument("--assignment-id", required=True)
    create.add_argument("--ticker")
    create.add_argument("--search-from", type=date.fromisoformat, required=True)
    create.add_argument("--search-to", type=date.fromisoformat, required=True)
    subparsers.add_parser("status")
    process = subparsers.add_parser("process")
    process.add_argument("--dry-run-sync", action="store_true")
    args = parser.parse_args()

    paths = BridgePaths.from_root(args.root, args.work_dir, args.inbox, slot_id=args.slot_id)
    try:
        if args.command == "create":
            result = create_assignment(paths, args.master_db, assignment_id=args.assignment_id, ticker=args.ticker, search_from=args.search_from, search_to=args.search_to)
        elif args.command == "status":
            result = bridge_status(paths, args.db)
        else:
            result = process_assignment(paths, args.db, dry_run_sync=args.dry_run_sync)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (BridgeError, NewsValidationError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
