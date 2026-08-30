#!/usr/bin/env python3
"""One-slot Sector Weekly bridge for a ChatGPT Scheduled Task worker."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.sector_weekly import (
    WeeklyWindow,
    connect_sector_db,
    now_jst,
    sector_name,
    upsert_report,
    validate_report,
)
from lib.sector_weekly_work import (
    DEFAULT_LEASE_SECONDS,
    SectorWorkError,
    claim_next,
    complete_assignment,
    fail_assignment,
    get_assignment,
    mark_running,
    payload_hash,
    prepare_submission,
    recover_expired_leases,
)
from tools.company_news_atomic import atomic_write_json, replace_with_retry
from tools.sector_weekly_scheduler import assemble_payload, build_prompt
from tools.sync_sector_weekly import sync as sync_sector_weekly

RESULT_SCHEMA = "sector_weekly_work_result_v1"
DEFAULT_OWNER = "sector-weekly-worker"
DEFAULT_WORK_ROOT = ROOT / "data" / "sector_weekly_work"


class SectorBridgeError(RuntimeError):
    pass


def _parse_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise SectorBridgeError(f"{field} must be an ISO-8601 string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SectorBridgeError(f"{field} is not valid ISO-8601") from exc
    if result.tzinfo is None:
        raise SectorBridgeError(f"{field} must include a timezone")
    return result


def _window(row: dict[str, Any]) -> WeeklyWindow:
    start = _parse_datetime(row["period_start"], "period_start")
    end = _parse_datetime(row["period_end"], "period_end")
    return WeeklyWindow(period_start=start, period_end=end, week_key=end.date().isoformat())


def _assignment_contract(row: dict[str, Any], work_root: Path) -> dict[str, Any]:
    assignment_id = str(row["assignment_id"])
    return {
        "schema_version": row["schema_version"],
        "assignment_id": assignment_id,
        "stable_key": row["stable_key"],
        "sector_code": int(row["sector_code"]),
        "sector_name": row["sector_name"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "status": row["status"],
        "attempt_count": int(row["attempt_count"]),
        "claim_owner": row["claim_owner"],
        "claimed_at": row["claimed_at"],
        "lease_expires_at": row["lease_expires_at"],
        "research_prompt": build_prompt(int(row["sector_code"]), _window(row)),
        "submit_path": str((work_root / "inbox" / f"{assignment_id}.json").resolve()),
        "result_schema_version": RESULT_SCHEMA,
    }


def claim_one(
    db_path: Path,
    owner: str,
    *,
    work_root: Path = DEFAULT_WORK_ROOT,
    at: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict[str, Any]:
    conn = connect_sector_db(db_path)
    try:
        row = claim_next(conn, owner, now=at, lease_seconds=lease_seconds)
    finally:
        conn.close()
    if row is None:
        return {"status": "no_work", "claim_owner": owner}
    contract = _assignment_contract(row, work_root)
    atomic_write_json(work_root / "active" / f"{row['assignment_id']}.json", contract)
    return {"status": "claimed", "assignment": contract}


def start_one(db_path: Path, assignment_id: str, owner: str, *, at: datetime | None = None) -> dict[str, Any]:
    conn = connect_sector_db(db_path)
    try:
        row = mark_running(conn, assignment_id, owner, now=at)
    finally:
        conn.close()
    return {"status": row["status"], "assignment_id": assignment_id, "claim_owner": owner}


def _read_envelope(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SectorBridgeError("submitted payload is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise SectorBridgeError("submitted payload must be a JSON object")
    required = {
        "schema_version", "assignment_id", "claim_owner", "sector_code", "sector_name",
        "period_start", "period_end", "report",
    }
    if set(value) != required:
        missing = sorted(required - set(value))
        extra = sorted(set(value) - required)
        raise SectorBridgeError(f"submitted envelope keys mismatch: missing={missing}, extra={extra}")
    if value["schema_version"] != RESULT_SCHEMA:
        raise SectorBridgeError("submitted result schema_version is not supported")
    if not isinstance(value["report"], dict):
        raise SectorBridgeError("report must be a JSON object")
    return value


def _validate_ownership(envelope: dict[str, Any], row: dict[str, Any], assignment_id: str, owner: str) -> None:
    expected = {
        "assignment_id": assignment_id,
        "claim_owner": owner,
        "sector_code": int(row["sector_code"]),
        "sector_name": row["sector_name"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
    }
    for field, expected_value in expected.items():
        if envelope.get(field) != expected_value:
            raise SectorBridgeError(f"submitted {field} does not match the claimed assignment")
    if row["claim_owner"] != owner and row["status"] != "success":
        raise SectorBridgeError("claim owner does not own this assignment")
    if row["status"] not in {"claimed", "running", "success"}:
        raise SectorBridgeError("assignment is not active or completed")


def _quarantine(work_root: Path, assignment_id: str, source: Path) -> None:
    target = work_root / "quarantine" / f"{assignment_id}.json"
    try:
        body = source.read_text(encoding="utf-8", errors="replace")
    except OSError:
        body = ""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(body, encoding="utf-8")
    replace_with_retry(temporary, target)


def submit_one(
    db_path: Path,
    assignment_id: str,
    owner: str,
    payload_path: Path,
    *,
    work_root: Path = DEFAULT_WORK_ROOT,
    at: datetime | None = None,
    sync_func: Callable[[Path, bool], dict[str, int]] = sync_sector_weekly,
    dry_run_sync: bool = False,
) -> dict[str, Any]:
    timestamp = at or now_jst()
    conn = connect_sector_db(db_path)
    try:
        row = get_assignment(conn, assignment_id)
        if row is None:
            raise SectorBridgeError("assignment not found")
        try:
            envelope = _read_envelope(payload_path)
            _validate_ownership(envelope, row, assignment_id, owner)
            digest = payload_hash(envelope)
            if row["status"] == "success":
                if row["submitted_payload_hash"] != digest:
                    raise SectorBridgeError("completed assignment received a conflicting payload")
                return {"status": "already_success", "assignment_id": assignment_id, "payload_hash": digest}
            window = _window(row)
            payload = assemble_payload(envelope["report"], int(row["sector_code"]), window, generated_at=timestamp)
            validated = validate_report(payload, expected_code=int(row["sector_code"]), expected_window=window)
            inbox_path = work_root / "inbox" / f"{assignment_id}.json"
            atomic_write_json(inbox_path, envelope)
            upsert_report(conn, validated)
            prepare_submission(conn, assignment_id, owner, digest, now=timestamp)
            sync_result = sync_func(db_path, dry_run_sync)
            completed = complete_assignment(conn, assignment_id, owner, digest, now=timestamp)
            processed = work_root / "processed" / f"{assignment_id}.json"
            processed.parent.mkdir(parents=True, exist_ok=True)
            replace_with_retry(inbox_path, processed)
            (work_root / "active" / f"{assignment_id}.json").unlink(missing_ok=True)
            return {
                "status": completed["status"], "assignment_id": assignment_id,
                "payload_hash": digest, "sync_result": sync_result, "processed_path": str(processed.resolve()),
            }
        except Exception as exc:
            if isinstance(exc, SectorBridgeError) or row["status"] != "success":
                _quarantine(work_root, assignment_id, payload_path)
            current = get_assignment(conn, assignment_id)
            if current and current["status"] in {"claimed", "running"} and current["claim_owner"] == owner:
                fail_assignment(conn, assignment_id, owner, exc, now=timestamp)
            raise
    finally:
        conn.close()


def fail_one(db_path: Path, assignment_id: str, owner: str, message: str, *, at: datetime | None = None) -> dict[str, Any]:
    conn = connect_sector_db(db_path)
    try:
        row = fail_assignment(conn, assignment_id, owner, SectorBridgeError(message), now=at)
    finally:
        conn.close()
    return {"status": row["status"], "assignment_id": assignment_id}


def recover(db_path: Path, *, at: datetime | None = None) -> dict[str, Any]:
    conn = connect_sector_db(db_path)
    try:
        count = recover_expired_leases(conn, now=at)
    finally:
        conn.close()
    return {"status": "recovered", "count": count}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "decision_db.db")
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)
    claim_parser = subparsers.add_parser("claim")
    claim_parser.add_argument("--owner", default=DEFAULT_OWNER)
    claim_parser.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--assignment-id", required=True)
    start_parser.add_argument("--owner", default=DEFAULT_OWNER)
    submit_parser = subparsers.add_parser("submit")
    submit_parser.add_argument("--assignment-id", required=True)
    submit_parser.add_argument("--owner", default=DEFAULT_OWNER)
    submit_parser.add_argument("--payload", type=Path, required=True)
    submit_parser.add_argument("--dry-run-sync", action="store_true")
    fail_parser = subparsers.add_parser("fail")
    fail_parser.add_argument("--assignment-id", required=True)
    fail_parser.add_argument("--owner", default=DEFAULT_OWNER)
    fail_parser.add_argument("--message", required=True)
    subparsers.add_parser("recover")
    args = parser.parse_args()
    try:
        if args.command == "claim":
            result = claim_one(args.db, args.owner, work_root=args.work_root, lease_seconds=args.lease_seconds)
        elif args.command == "start":
            result = start_one(args.db, args.assignment_id, args.owner)
        elif args.command == "submit":
            result = submit_one(
                args.db, args.assignment_id, args.owner, args.payload,
                work_root=args.work_root, dry_run_sync=args.dry_run_sync,
            )
        elif args.command == "fail":
            result = fail_one(args.db, args.assignment_id, args.owner, args.message)
        else:
            result = recover(args.db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
