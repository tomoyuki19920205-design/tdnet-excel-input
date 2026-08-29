#!/usr/bin/env python3
"""One-slot Desktop ChatGPT Work -> Company News Monitor bridge.

This coordinator never trusts Work output as a database write. It validates the
existing ``company_news_v1`` contract, delegates local ingestion to the existing
adapter, then delegates Supabase synchronization to the existing sync module.
There is intentionally no watch loop or automatic next-company advance in v1.
"""
from __future__ import annotations

import argparse
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

from lib.news_monitor import NewsValidationError, normalize_ticker, validate_payload
from tools.ingest_company_news import ingest_file
from tools.sync_company_news import sync as sync_company_news

ASSIGNMENT_SCHEMA = "company_news_assignment_v1"
SLOT_ID = "slot01"
ASSIGNMENT_STATUSES = frozenset({"ready", "completed", "failed"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_JST = timezone(timedelta(hours=9))


class BridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class BridgePaths:
    root: Path
    work_dir: Path
    inbox: Path
    assignment: Path
    state: Path
    log: Path
    lock: Path

    @classmethod
    def from_root(cls, root: Path = ROOT, work_dir: Path | None = None, inbox: Path | None = None) -> "BridgePaths":
        root = root.resolve()
        work_dir = (work_dir or root / "data" / "news_work").resolve()
        inbox = (inbox or root / "data" / "news_inbox").resolve()
        return cls(
            root=root,
            work_dir=work_dir,
            inbox=inbox,
            assignment=work_dir / "slots" / SLOT_ID / "assignment.json",
            state=work_dir / "state" / f"{SLOT_ID}.json",
            log=work_dir / "logs" / f"{SLOT_ID}.jsonl",
            lock=work_dir / "state" / f"{SLOT_ID}.lock",
        )


def _now() -> str:
    return datetime.now(_JST).isoformat(timespec="seconds")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _append_log(paths: BridgePaths, event: str, **details: Any) -> None:
    paths.log.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": _now(), "slot_id": SLOT_ID, "event": event, **details}
    with paths.log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


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
                if lock_pid:
                    os.kill(lock_pid, 0)
            except (OSError, ValueError):
                paths.lock.unlink(missing_ok=True)
                if attempt == 0:
                    continue
            raise BridgeError(f"{SLOT_ID} is already being processed ({paths.lock})") from exc
    if descriptor is None:
        raise BridgeError(f"could not acquire {SLOT_ID} lock")
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
    if value["slot_id"] != SLOT_ID:
        raise BridgeError(f"slot_id must be {SLOT_ID}")
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
    output = (paths.root / value["output_directory"]).resolve()
    if output != paths.inbox:
        raise BridgeError(f"output_directory must resolve to {paths.inbox}")
    return value


def _load_state(paths: BridgePaths, assignment_id: str) -> dict[str, Any]:
    if not paths.state.exists():
        return {"schema_version": "company_news_bridge_state_v1", "slot_id": SLOT_ID, "assignment_id": assignment_id, "phase": "waiting", "updated_at": _now()}
    state = _read_json(paths.state)
    if state.get("assignment_id") != assignment_id:
        return {"schema_version": "company_news_bridge_state_v1", "slot_id": SLOT_ID, "assignment_id": assignment_id, "phase": "waiting", "updated_at": _now()}
    return state


def _save_state(paths: BridgePaths, state: dict[str, Any], phase: str, **details: Any) -> None:
    state.update({"phase": phase, "updated_at": _now(), **details})
    _atomic_json(paths.state, state)


def _set_assignment_status(paths: BridgePaths, assignment: dict[str, Any], status: str, error: str | None = None) -> None:
    assignment["status"] = status
    assignment["updated_at"] = _now()
    if error:
        assignment["error_message"] = error[:1000]
    else:
        assignment.pop("error_message", None)
    _atomic_json(paths.assignment, assignment)


def expected_output_path(paths: BridgePaths, assignment: dict[str, Any]) -> Path:
    return paths.inbox / f"work_{SLOT_ID}_{assignment['assignment_id']}.json"


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


def _validate_result(path: Path, assignment: dict[str, Any]) -> None:
    payload = _read_json(path)
    run = validate_payload(payload)
    if payload.get("run_id") != assignment["assignment_id"]:
        raise BridgeError("Work output run_id must equal assignment_id")
    if run.scan["ticker"] != assignment["ticker"]:
        raise BridgeError("Work output ticker does not match assignment ticker")
    search_from = date.fromisoformat(assignment["search_from"])
    search_to = date.fromisoformat(assignment["search_to"])
    for index, event in enumerate(run.events):
        published = datetime.fromisoformat(event["published_at"]).date()
        if published < search_from or published > search_to:
            raise BridgeError(f"items[{index}].published_at is outside assignment search range")


def _quarantine_result(paths: BridgePaths, path: Path, error: Exception) -> None:
    quarantine = paths.inbox / "quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    target = quarantine / path.name
    if path.exists():
        shutil.move(str(path), str(target))
    target.with_suffix(target.suffix + ".error.txt").write_text(str(error), encoding="utf-8")


def bridge_status(paths: BridgePaths, db_path: Path) -> dict[str, Any]:
    assignment = validate_assignment(_read_json(paths.assignment), paths)
    output = expected_output_path(paths, assignment)
    processed = _processed_output_path(paths, assignment)
    state = _load_state(paths, assignment["assignment_id"])
    return {
        "slot_id": SLOT_ID,
        "assignment_id": assignment["assignment_id"],
        "assignment_status": assignment["status"],
        "bridge_phase": state.get("phase", "waiting"),
        "output_expected": str(output),
        "output_present": output.exists(),
        "processed_present": processed.exists(),
        "canonical_run_present": _run_exists(db_path, assignment["assignment_id"]),
    }


def process_assignment(
    paths: BridgePaths,
    db_path: Path,
    *,
    sync_func: Callable[[Path, bool], dict[str, int]] = sync_company_news,
    dry_run_sync: bool = False,
) -> dict[str, Any]:
    with _slot_lock(paths):
        assignment = validate_assignment(_read_json(paths.assignment), paths)
        assignment_id = assignment["assignment_id"]
        state = _load_state(paths, assignment_id)
        if assignment["status"] == "completed":
            _append_log(paths, "already_completed", assignment_id=assignment_id)
            return {"status": "already_completed", "assignment_id": assignment_id}

        output = expected_output_path(paths, assignment)
        processed = _processed_output_path(paths, assignment)
        if not output.exists() and not (processed.exists() and _run_exists(db_path, assignment_id)):
            _append_log(paths, "waiting_for_output", assignment_id=assignment_id, expected=str(output))
            return {"status": "waiting", "assignment_id": assignment_id, "expected_output": str(output)}

        try:
            result_path = output if output.exists() else processed
            _validate_result(result_path, assignment)
            if state.get("phase") != "ingested" and not _run_exists(db_path, assignment_id):
                if not output.exists():
                    raise BridgeError("processed output exists but canonical run is missing; manual review required")
                if not ingest_file(output, db_path, paths.inbox / "processed", paths.inbox / "quarantine"):
                    raise BridgeError("existing ingestion adapter quarantined Work output")
                _save_state(paths, state, "ingested", processed_file=str(processed))
                _append_log(paths, "ingested", assignment_id=assignment_id)
            else:
                _save_state(paths, state, "ingested", processed_file=str(processed), resumed=True)

            sync_result = sync_func(db_path, dry_run_sync)
            _save_state(paths, state, "synced", sync_result=sync_result, dry_run_sync=dry_run_sync)
            _set_assignment_status(paths, assignment, "completed")
            _append_log(paths, "completed", assignment_id=assignment_id, sync_result=sync_result, dry_run_sync=dry_run_sync)
            return {"status": "completed", "assignment_id": assignment_id, "sync_result": sync_result, "dry_run_sync": dry_run_sync}
        except Exception as exc:
            if output.exists() and not _run_exists(db_path, assignment_id):
                _quarantine_result(paths, output, exc)
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
        "slot_id": SLOT_ID,
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
    parser.add_argument("--db", type=Path, default=ROOT / "decision_db.db")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--master-db", type=Path, default=ROOT / "data" / "jquants.db")
    create.add_argument("--assignment-id", required=True)
    create.add_argument("--ticker")
    create.add_argument("--search-from", type=date.fromisoformat, required=True)
    create.add_argument("--search-to", type=date.fromisoformat, required=True)
    subparsers.add_parser("status")
    process = subparsers.add_parser("process")
    process.add_argument("--dry-run-sync", action="store_true")
    args = parser.parse_args()

    paths = BridgePaths.from_root(args.root, args.work_dir, args.inbox)
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
