#!/usr/bin/env python3
"""One-slot Sector Weekly bridge for a ChatGPT Scheduled Task worker."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.sector_weekly import (
    JST, WeeklyWindow,
    connect_sector_db,
    now_jst,
    sector_name,
    validate_report,
    weekly_window,
)
from lib.sector_weekly_work import (
    DEFAULT_LEASE_SECONDS,
    MAX_ATTEMPTS,
    SectorWorkError,
    abandon_assignment,
    claim_next,
    completion_status,
    fail_assignment,
    get_assignment,
    heartbeat_assignment,
    is_transport_waiting,
    mark_running,
    payload_hash,
    recover_expired_leases,
    reopen_quality_revision,
    stage_assignment,
)
from lib.pipeline.db import get_supabase_read_config, load_env, supabase_select
from tools.company_news_atomic import atomic_write_json, replace_with_retry
from tools.sector_weekly_scheduler import assemble_payload, build_prompt, in_worker_window

RESULT_SCHEMA = "sector_weekly_work_result_v1"
DEFAULT_OWNER = "sector-weekly-worker"
DEFAULT_WORK_ROOT = ROOT / "data" / "sector_weekly_work"
QUALITY_REOPEN_CONFIRMATION = "REOPEN_SECTOR_WEEKLY_QUALITY"
_RFC3339_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
    return WeeklyWindow(
        period_start=start,
        period_end=end,
        week_key=end.astimezone(JST).date().isoformat(),
    )


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
        "submit_path": str((work_root / "drafts" / f"{assignment_id}.json").resolve()),
        "result_schema_version": RESULT_SCHEMA,
    }


def claim_one(
    db_path: Path,
    owner: str,
    *,
    work_root: Path = DEFAULT_WORK_ROOT,
    at: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    window: WeeklyWindow | None = None,
) -> dict[str, Any]:
    timestamp = at or now_jst()
    if not in_worker_window(timestamp):
        return {"status": "no_work", "claim_owner": owner, "reason": "outside_worker_window"}
    target_window = window or weekly_window(timestamp)
    conn = connect_sector_db(db_path)
    try:
        row = claim_next(conn, owner, now=timestamp, lease_seconds=lease_seconds, window=target_window)
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


def heartbeat_one(
    db_path: Path,
    assignment_id: str,
    owner: str,
    *,
    at: datetime | None = None,
) -> dict[str, Any]:
    conn = connect_sector_db(db_path)
    try:
        row = heartbeat_assignment(conn, assignment_id, owner, now=at)
    finally:
        conn.close()
    return {
        "status": "lease_renewed", "assignment_id": assignment_id,
        "claim_owner": owner, "lease_expires_at": row["lease_expires_at"],
    }


def abandon_one(
    db_path: Path,
    assignment_id: str,
    owner: str,
    *,
    at: datetime | None = None,
    reason: str = "worker hard time budget reached",
    work_root: Path = DEFAULT_WORK_ROOT,
) -> dict[str, Any]:
    conn = connect_sector_db(db_path)
    try:
        row = abandon_assignment(conn, assignment_id, owner, now=at, reason=reason)
    finally:
        conn.close()
    (work_root / "active" / f"{assignment_id}.json").unlink(missing_ok=True)
    return {
        "status": row["status"], "assignment_id": assignment_id,
        "attempt_count": int(row["attempt_count"]), "released": True,
    }


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
    transport_waiting = row["status"] == "retry_pending" and bool(row["submitted_payload_hash"])
    if row["claim_owner"] != owner and row["status"] != "success" and not transport_waiting:
        raise SectorBridgeError("claim owner does not own this assignment")
    if row["status"] not in {"claimed", "running", "success"} and not transport_waiting:
        raise SectorBridgeError("assignment is not active, staged, or completed")


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


def _comparison_datetime(value: Any, field: str, *, allow_date_only: bool = False) -> Any:
    """Return a canonical comparison value without changing stored payload data."""
    if value is None and allow_date_only:
        return None
    if allow_date_only and isinstance(value, str) and _DATE_ONLY_RE.fullmatch(value):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise SectorBridgeError(f"{field} is not a valid calendar date") from exc
        return value
    if not isinstance(value, str) or not _RFC3339_TIMESTAMP_RE.fullmatch(value):
        raise SectorBridgeError(f"{field} must be an RFC3339 timestamp with a timezone")
    parsed = _parse_datetime(value, field)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _comparison_sources(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise SectorBridgeError("sources must be a list for canonical comparison")
    result: list[dict[str, Any]] = []
    for index, source in enumerate(value):
        if not isinstance(source, dict):
            raise SectorBridgeError(f"sources[{index}] must be an object for canonical comparison")
        copied = dict(source)
        copied["published_at"] = _comparison_datetime(
            source.get("published_at"), f"sources[{index}].published_at", allow_date_only=True,
        )
        result.append(copied)
    return result


def _normalized_report_row(row: dict[str, Any]) -> dict[str, Any]:
    """Build a non-mutating semantic comparison row for canonical verification only."""
    json_fields = {
        "summary_bullets", "watchlist_companies", "next_week_watchpoints",
        "missed_candidates", "sources",
    }
    fields = {
        "schema_version", "report_type", "sector_code", "sector_name", "period_start", "period_end",
        "generated_at", "importance", "direction", "summary_bullets", "full_report_md",
        "watchlist_companies", "next_week_watchpoints", "missed_candidates", "sources", "run_id", "dedupe_key",
    }
    result: dict[str, Any] = {}
    for field in fields:
        value = row.get(field)
        if field in json_fields and isinstance(value, str):
            value = json.loads(value)
        if field in {"period_start", "period_end", "generated_at"}:
            value = _comparison_datetime(value, field)
        elif field == "sources":
            value = _comparison_sources(value)
        result[field] = value
    return result


def _read_supabase_reports(stable_key: str) -> list[dict[str, Any]]:
    load_env(str(ROOT))
    config = get_supabase_read_config()
    return supabase_select(
        "canonical_sector_reports",
        params={"dedupe_key": f"eq.{stable_key}", "select": "*"},
        config=config,
    )


def reopen_quality_one(
    db_path: Path,
    assignment_id: str,
    stable_key: str,
    expected_hash: str,
    reason: str,
    confirmation: str,
    *,
    work_root: Path = DEFAULT_WORK_ROOT,
    at: datetime | None = None,
    supabase_reader: Any = _read_supabase_reports,
) -> dict[str, Any]:
    """Archive and reopen one verified success; canonical rows remain untouched."""
    if confirmation != QUALITY_REOPEN_CONFIRMATION:
        raise SectorBridgeError("exact quality-reopen confirmation text is required")
    processed = work_root / "processed" / f"{assignment_id}.json"
    envelope = _read_envelope(processed)
    digest = payload_hash(envelope)
    if digest != expected_hash:
        raise SectorBridgeError("processed payload hash does not match expected hash")
    timestamp = at or now_jst()
    conn = connect_sector_db(db_path)
    try:
        row = get_assignment(conn, assignment_id)
        if row is None:
            raise SectorBridgeError("assignment not found")
        if row["stable_key"] != stable_key or envelope["assignment_id"] != assignment_id:
            raise SectorBridgeError("assignment, stable key, and processed payload do not agree")
        if row["status"] != "success":
            raise SectorBridgeError("quality revision requires a successful assignment")
        if row["claim_owner"] or row["lease_expires_at"]:
            raise SectorBridgeError("quality revision requires no claim owner or lease")
        if int(row["attempt_count"]) >= MAX_ATTEMPTS:
            raise SectorBridgeError("quality revision attempt limit has been reached")
        if row["last_error_type"] or row["last_error_message"]:
            raise SectorBridgeError("quality revision requires no pending transport or error state")
        if (work_root / "inbox" / f"{assignment_id}.json").exists() or (
            work_root / "active" / f"{assignment_id}.json"
        ).exists():
            raise SectorBridgeError("quality revision requires no active transport files")
        if row["submitted_payload_hash"] != expected_hash:
            raise SectorBridgeError("assignment payload hash does not match expected hash")
        reports = [dict(item) for item in conn.execute(
            "SELECT * FROM canonical_sector_reports WHERE dedupe_key=?", (stable_key,),
        ).fetchall()]
        if len(reports) != 1:
            raise SectorBridgeError("local canonical report count must be exactly one")
        runs = conn.execute(
            "SELECT status FROM canonical_sector_report_runs WHERE dedupe_key=?", (stable_key,),
        ).fetchall()
        if len(runs) != 1 or runs[0]["status"] != "success":
            raise SectorBridgeError("local canonical run must be exactly one successful row")
        window = _window(row)
        old_payload = assemble_payload(
            envelope["report"], int(row["sector_code"]), window,
            generated_at=_parse_datetime(reports[0]["generated_at"], "generated_at"),
        )
        if _normalized_report_row(old_payload) != _normalized_report_row(reports[0]):
            raise SectorBridgeError("local canonical report does not semantically match processed payload")
        remote = supabase_reader(stable_key)
        if len(remote) != 1 or _normalized_report_row(remote[0]) != _normalized_report_row(reports[0]):
            raise SectorBridgeError("Supabase canonical report must be exactly one semantic match")
        archive = (
            work_root / "revisions" / assignment_id /
            f"{timestamp.strftime('%Y%m%dT%H%M%S%f')}.{expected_hash[:16]}.json"
        )
        if archive.exists():
            raise SectorBridgeError("quality revision archive already exists")
        archive.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(archive, {
            "schema_version": "sector_weekly_quality_revision_archive_v1",
            "assignment_id": assignment_id, "stable_key": stable_key,
            "old_payload_hash": expected_hash, "revision_started_at": timestamp.isoformat(timespec="seconds"),
            "reason": reason, "original_payload": envelope,
        })
        try:
            reopened = reopen_quality_revision(
                conn, assignment_id, stable_key, expected_hash, reason, now=timestamp,
            )
        except Exception:
            # The archive belongs to this failed operation and no state transition
            # occurred, so remove it to keep a later explicit retry possible.
            archive.unlink(missing_ok=True)
            raise
        return {
            "status": "quality_revision", "assignment_id": assignment_id, "stable_key": stable_key,
            "attempt_count": int(reopened["attempt_count"]), "expected_next_attempt": int(reopened["attempt_count"]) + 1,
            "payload_hash": expected_hash,
            "assignment_payload_hash": expected_hash,
            "processed_payload_hash": digest,
            # These are logical payload hashes attested only after complete semantic
            # comparison; storage may spell equivalent UTC offsets as Z or +00:00.
            "local_canonical_payload_hash": expected_hash,
            "supabase_canonical_payload_hash": expected_hash,
            "archive_path": str(archive.resolve()),
            "canonical_rows_changed": 0, "supabase_writes": 0,
        }
    finally:
        conn.close()


def stage_one(
    db_path: Path,
    assignment_id: str,
    owner: str,
    payload_path: Path,
    *,
    work_root: Path = DEFAULT_WORK_ROOT,
    at: datetime | None = None,
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
            if is_transport_waiting(row):
                if row["submitted_payload_hash"] != digest:
                    raise SectorBridgeError("staged assignment received a conflicting payload")
            window = _window(row)
            payload = assemble_payload(envelope["report"], int(row["sector_code"]), window, generated_at=timestamp)
            validate_report(payload, expected_code=int(row["sector_code"]), expected_window=window)
            inbox_path = work_root / "inbox" / f"{assignment_id}.json"
            atomic_write_json(inbox_path, envelope)
            staged, changed = stage_assignment(conn, assignment_id, owner, digest, now=timestamp)
            (work_root / "active" / f"{assignment_id}.json").unlink(missing_ok=True)
            return {
                "status": "handoff_pending" if changed else "already_staged",
                "assignment_status": staged["status"], "assignment_id": assignment_id,
                "payload_hash": digest, "inbox_path": str(inbox_path.resolve()),
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


def status_one(db_path: Path, *, at: datetime | None = None) -> dict[str, Any]:
    timestamp = at or now_jst()
    target_window = weekly_window(timestamp)
    conn = connect_sector_db(db_path)
    try:
        return completion_status(conn, target_window)
    finally:
        conn.close()


def _status_table(result: dict[str, Any]) -> str:
    rows = [
        ("state", result["state"]), ("period", f"{result['period_start']} .. {result['period_end']}"),
        ("assignments", f"{result['assignments']}/33"), ("success", result["success"]),
        ("ready", result["ready"]), ("claimed", result["claimed"]), ("running", result["running"]),
        ("retry_pending", result["retry_pending"]), ("failed", result["failed"]),
        ("missing", result["missing_count"]), ("stale", result["stale_count"]),
        ("duplicate", result["duplicate_count"]), ("attempts", result["attempts"]),
        ("last_success_at", result["last_success_at"] or "-"),
    ]
    return "\n".join(f"{name:18} {value}" for name, value in rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "decision_db.db")
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument("--at", help="timezone-aware ISO-8601 clock override for isolated tests and audits")
    subparsers = parser.add_subparsers(dest="command", required=True)
    claim_parser = subparsers.add_parser("claim")
    claim_parser.add_argument("--owner", default=DEFAULT_OWNER)
    claim_parser.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--assignment-id", required=True)
    start_parser.add_argument("--owner", default=DEFAULT_OWNER)
    heartbeat_parser = subparsers.add_parser("heartbeat")
    heartbeat_parser.add_argument("--assignment-id", required=True)
    heartbeat_parser.add_argument("--owner", default=DEFAULT_OWNER)
    abandon_parser = subparsers.add_parser("abandon")
    abandon_parser.add_argument("--assignment-id", required=True)
    abandon_parser.add_argument("--owner", default=DEFAULT_OWNER)
    abandon_parser.add_argument("--reason", default="worker hard time budget reached")
    stage_parser = subparsers.add_parser("stage")
    stage_parser.add_argument("--assignment-id", required=True)
    stage_parser.add_argument("--owner", default=DEFAULT_OWNER)
    stage_parser.add_argument("--payload", type=Path, required=True)
    fail_parser = subparsers.add_parser("fail")
    fail_parser.add_argument("--assignment-id", required=True)
    fail_parser.add_argument("--owner", default=DEFAULT_OWNER)
    fail_parser.add_argument("--message", required=True)
    reopen_parser = subparsers.add_parser("reopen-quality")
    reopen_parser.add_argument("--assignment-id", required=True)
    reopen_parser.add_argument("--stable-key", required=True)
    reopen_parser.add_argument("--expected-hash", required=True)
    reopen_parser.add_argument("--reason", required=True)
    reopen_parser.add_argument("--confirm", required=True)
    subparsers.add_parser("recover")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        at = _parse_datetime(args.at, "--at") if args.at else None
        if args.command == "claim":
            result = claim_one(args.db, args.owner, work_root=args.work_root, lease_seconds=args.lease_seconds, at=at)
        elif args.command == "start":
            result = start_one(args.db, args.assignment_id, args.owner, at=at)
        elif args.command == "heartbeat":
            result = heartbeat_one(args.db, args.assignment_id, args.owner, at=at)
        elif args.command == "abandon":
            result = abandon_one(
                args.db, args.assignment_id, args.owner, at=at,
                reason=args.reason, work_root=args.work_root,
            )
        elif args.command == "stage":
            result = stage_one(
                args.db, args.assignment_id, args.owner, args.payload,
                work_root=args.work_root, at=at,
            )
        elif args.command == "fail":
            result = fail_one(args.db, args.assignment_id, args.owner, args.message, at=at)
        elif args.command == "reopen-quality":
            result = reopen_quality_one(
                args.db, args.assignment_id, args.stable_key, args.expected_hash,
                args.reason, args.confirm, work_root=args.work_root, at=at,
            )
        elif args.command == "recover":
            result = recover(args.db, at=at)
        else:
            result = status_one(args.db, at=at)
            if not args.json:
                print(_status_table(result))
                return int(result["exit_code"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(result.get("exit_code", 0))
    except (RuntimeError, ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
