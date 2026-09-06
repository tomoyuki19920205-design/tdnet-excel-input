#!/usr/bin/env python3
"""One-slot Sector Weekly bridge for a ChatGPT Scheduled Task worker."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
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
)
from lib.sector_weekly_work import (
    DEFAULT_LEASE_SECONDS,
    MAX_ATTEMPTS,
    SectorWorkError,
    abandon_assignment,
    claim_next,
    completion_target_window,
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
from lib.sector_weekly_sqlite import MigrationRequiredError, validate_work_schema
from tools.company_news_atomic import atomic_write_json, atomic_write_text, replace_with_retry
from tools.sector_weekly_scheduler import assemble_payload, build_prompt

LEGACY_RESULT_SCHEMA = "sector_weekly_work_result_v1"
RESULT_SCHEMA = "sector_weekly_work_result_v2"
DEFAULT_OWNER = "sector-weekly-worker"
DEFAULT_WORK_ROOT = ROOT / "data" / "sector_weekly_work"
QUALITY_REOPEN_CONFIRMATION = "REOPEN_SECTOR_WEEKLY_QUALITY"
_RFC3339_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SectorBridgeError(RuntimeError):
    pass


class SectorContractError(SectorBridgeError):
    """A contract or system invariant failure that must not be retried automatically."""


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


def _contract_identity(value: dict[str, Any]) -> dict[str, Any]:
    try:
        identity = {
            "assignment_id": str(value["assignment_id"]),
            "sector_code": int(value["sector_code"]),
            "sector_name": value["sector_name"],
            "period_start": value["period_start"],
            "period_end": value["period_end"],
            "attempt_count": int(value["attempt_count"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise SectorContractError("claim contract identity is incomplete or invalid") from exc
    if not isinstance(identity["sector_name"], str) or not identity["sector_name"].strip():
        raise SectorContractError("claim contract sector_name is empty or invalid")
    expected_name = sector_name(identity["sector_code"])
    if identity["sector_name"] != expected_name:
        raise SectorContractError("claim contract sector_code and sector_name do not match the canonical mapping")
    for field in ("period_start", "period_end"):
        _parse_datetime(identity[field], field)
    return identity


def _contract_hash(value: dict[str, Any]) -> str:
    body = json.dumps(
        _contract_identity(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(body).hexdigest()


def _active_contract_path(work_root: Path, value: dict[str, Any]) -> Path:
    identity = _contract_identity(value)
    return work_root / "active" / (
        f"{identity['assignment_id']}.attempt-{identity['attempt_count']}.{_contract_hash(identity)[:16]}.json"
    )


def _assignment_contract(row: dict[str, Any], work_root: Path) -> dict[str, Any]:
    assignment_id = str(row["assignment_id"])
    identity = _contract_identity(row)
    contract_hash = _contract_hash(identity)
    attempt_count = identity["attempt_count"]
    active_contract_path = _active_contract_path(work_root, identity)
    previous_failure = None
    if row.get("last_error_type") or row.get("last_error_message"):
        previous_failure = {
            "attempt_count": max(0, attempt_count - 1),
            "error_type": row.get("last_error_type"),
            "message": row.get("last_error_message"),
        }
    return {
        "schema_version": row["schema_version"],
        "assignment_id": assignment_id,
        "stable_key": row["stable_key"],
        "sector_code": identity["sector_code"],
        "sector_name": identity["sector_name"],
        "period_start": identity["period_start"],
        "period_end": identity["period_end"],
        "status": row["status"],
        "attempt_count": attempt_count,
        "contract_hash": contract_hash,
        "claim_owner": row["claim_owner"],
        "claimed_at": row["claimed_at"],
        "lease_expires_at": row["lease_expires_at"],
        "previous_failure": previous_failure,
        "research_prompt": build_prompt(int(row["sector_code"]), _window(row)),
        "active_contract_path": str(active_contract_path.resolve()),
        "submit_path": str((
            work_root / "drafts" / f"{assignment_id}.attempt-{attempt_count}.{contract_hash[:16]}.json"
        ).resolve()),
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
    conn = connect_sector_db(db_path)
    try:
        target_window = window or completion_target_window(conn, timestamp)
        row = claim_next(conn, owner, now=timestamp, lease_seconds=lease_seconds, window=target_window)
    finally:
        conn.close()
    if row is None:
        return {"status": "no_work", "claim_owner": owner}
    contract = _assignment_contract(row, work_root)
    active_path = Path(contract["active_contract_path"])
    if active_path.exists():
        raise SectorBridgeError("attempt-specific active claim contract already exists")
    atomic_write_json(active_path, contract)
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
    _active_contract_path(work_root, row).unlink(missing_ok=True)
    return {
        "status": row["status"], "assignment_id": assignment_id,
        "attempt_count": int(row["attempt_count"]), "released": True,
    }


def _read_envelope(path: Path, *, require_contract: bool = True) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SectorBridgeError("submitted payload is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise SectorBridgeError("submitted payload must be a JSON object")
    legacy_required = {
        "schema_version", "assignment_id", "claim_owner", "sector_code", "sector_name",
        "period_start", "period_end", "report",
    }
    required = legacy_required | ({"attempt_count", "contract_hash"} if require_contract else set())
    permitted = legacy_required | {"attempt_count", "contract_hash"}
    if not required.issubset(value) or not set(value).issubset(permitted):
        missing = sorted(required - set(value))
        extra = sorted(set(value) - permitted)
        raise SectorBridgeError(f"submitted envelope keys mismatch: missing={missing}, extra={extra}")
    allowed_schemas = {RESULT_SCHEMA} if require_contract else {LEGACY_RESULT_SCHEMA, RESULT_SCHEMA}
    if value["schema_version"] not in allowed_schemas:
        raise SectorBridgeError("submitted result schema_version is not supported")
    if not isinstance(value["report"], dict):
        raise SectorBridgeError("report must be a JSON object")
    return value


def _validate_ownership(envelope: dict[str, Any], row: dict[str, Any], assignment_id: str, owner: str) -> None:
    transport_waiting = row["status"] == "retry_pending" and bool(row["submitted_payload_hash"])
    expected = {
        "assignment_id": assignment_id,
        "claim_owner": owner,
        "sector_code": int(row["sector_code"]),
        "sector_name": row["sector_name"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
    }
    if not transport_waiting:
        expected.update({
            "attempt_count": int(row["attempt_count"]),
            "contract_hash": _contract_hash(row),
        })
    for field, expected_value in expected.items():
        if envelope.get(field) != expected_value:
            raise SectorContractError(f"submitted {field} does not match the claimed assignment")
    if row["claim_owner"] != owner and row["status"] != "success" and not transport_waiting:
        raise SectorBridgeError("claim owner does not own this assignment")
    if row["status"] not in {"claimed", "running", "success"} and not transport_waiting:
        raise SectorBridgeError("assignment is not active, staged, or completed")


def _quarantine(
    work_root: Path,
    assignment_id: str,
    source: Path,
    *,
    attempt_count: int | None = None,
    contract_hash: str | None = None,
) -> None:
    suffix = f".attempt-{attempt_count}" if attempt_count is not None else ".attempt-unknown"
    if contract_hash:
        suffix += f".{contract_hash[:16]}"
    target = work_root / "quarantine" / f"{assignment_id}{suffix}.json"
    try:
        body = source.read_text(encoding="utf-8", errors="replace")
    except OSError:
        body = ""
    target.parent.mkdir(parents=True, exist_ok=True)
    sequence = 1
    while target.exists():
        target = target.with_name(f"{target.stem}.{sequence}.json")
        sequence += 1
    atomic_write_text(target, body)


def _read_active_contract(work_root: Path, row: dict[str, Any]) -> dict[str, Any]:
    path = _active_contract_path(work_root, row)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SectorContractError("active claim contract is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise SectorContractError("active claim contract must be a JSON object")
    return value


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise MigrationRequiredError(f"Sector Weekly SQLite DB does not exist or is not migrated: {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        validate_work_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


def verify_claim_one(
    db_path: Path,
    assignment_id: str,
    owner: str,
    contract_hash: str,
    *,
    work_root: Path = DEFAULT_WORK_ROOT,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Read-only verification of the immutable DB and active-file claim contract."""
    conn = _connect_readonly(db_path)
    try:
        row = get_assignment(conn, assignment_id)
    finally:
        conn.close()
    if row is None:
        raise SectorBridgeError("assignment not found")
    if row["status"] not in {"claimed", "running"} or row["claim_owner"] != owner:
        raise SectorBridgeError("assignment is not actively owned by this worker")
    timestamp = at or now_jst()
    if _parse_datetime(row["lease_expires_at"], "lease_expires_at") <= timestamp:
        raise SectorBridgeError("claim lease has expired")
    active = _read_active_contract(work_root, row)
    expected = _contract_identity(row)
    actual = _contract_identity(active)
    if actual != expected:
        raise SectorContractError("active claim contract does not match the assignment database")
    expected_hash = _contract_hash(expected)
    if active.get("contract_hash") != expected_hash or contract_hash != expected_hash:
        raise SectorContractError("claim contract hash does not match")
    if active.get("claim_owner") != owner:
        raise SectorContractError("active claim contract owner does not match")
    regenerated = _assignment_contract(row, work_root)
    for field in (
        "schema_version", "stable_key", "research_prompt", "submit_path",
        "active_contract_path", "result_schema_version", "previous_failure",
    ):
        if active.get(field) != regenerated[field]:
            raise SectorContractError(f"active claim contract {field} does not match")
    return {
        "status": "verified", "verified": True, "contract_hash": expected_hash,
        "research_prompt": regenerated["research_prompt"],
        "submit_path": regenerated["submit_path"],
        "active_contract_path": regenerated["active_contract_path"],
        **expected,
    }


def verify_payload_one(
    db_path: Path,
    assignment_id: str,
    owner: str,
    contract_hash: str,
    payload_path: Path,
    *,
    work_root: Path = DEFAULT_WORK_ROOT,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Read-only pre-stage verification; it never creates inbox or canonical rows."""
    verified = verify_claim_one(
        db_path, assignment_id, owner, contract_hash, work_root=work_root, at=at,
    )
    conn = _connect_readonly(db_path)
    try:
        row = get_assignment(conn, assignment_id)
    finally:
        conn.close()
    if row is None:
        raise SectorBridgeError("assignment not found")
    envelope = _read_envelope(payload_path)
    _validate_ownership(envelope, row, assignment_id, owner)
    return {
        "status": "payload_verified", "verified": True,
        "assignment_id": assignment_id, "attempt_count": int(row["attempt_count"]),
        "contract_hash": contract_hash, "payload_hash": payload_hash(envelope),
    }


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
    envelope = _read_envelope(processed, require_contract=False)
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
        if (work_root / "inbox" / f"{assignment_id}.json").exists() or _active_contract_path(
            work_root, row,
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
    expected_contract_hash: str | None = None,
) -> dict[str, Any]:
    timestamp = at or now_jst()
    conn = connect_sector_db(db_path)
    staging_path: Path | None = None
    published_inbox = False
    try:
        row = get_assignment(conn, assignment_id)
        if row is None:
            raise SectorBridgeError("assignment not found")
        try:
            envelope = _read_envelope(payload_path)
            _validate_ownership(envelope, row, assignment_id, owner)
            if expected_contract_hash is not None and envelope["contract_hash"] != expected_contract_hash:
                raise SectorContractError("submitted contract_hash does not match the command contract")
            if row["status"] in {"claimed", "running"}:
                verify_claim_one(
                    db_path, assignment_id, owner,
                    expected_contract_hash or str(envelope["contract_hash"]),
                    work_root=work_root, at=timestamp,
                )
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
            validate_report(
                payload,
                expected_code=int(row["sector_code"]),
                expected_window=window,
                require_new_markdown_style=True,
            )
            inbox_path = work_root / "inbox" / f"{assignment_id}.json"
            existing_inbox = False
            if inbox_path.exists():
                existing = _read_envelope(inbox_path)
                if payload_hash(existing) != digest:
                    raise SectorContractError("inbox contains a conflicting payload")
                existing_inbox = True
            if not existing_inbox:
                staging_path = work_root / "staging" / f"{assignment_id}.{digest[:16]}.json"
                staging_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(staging_path, envelope)

            def publish() -> None:
                nonlocal published_inbox
                if staging_path is None:
                    return
                inbox_path.parent.mkdir(parents=True, exist_ok=True)
                replace_with_retry(staging_path, inbox_path)
                published_inbox = True

            try:
                staged, changed = stage_assignment(
                    conn, assignment_id, owner, digest, now=timestamp,
                    publish=publish if not existing_inbox else None,
                )
            except Exception:
                if published_inbox:
                    inbox_path.unlink(missing_ok=True)
                raise
            try:
                _active_contract_path(work_root, row).unlink(missing_ok=True)
            except OSError:
                pass
            return {
                "status": "handoff_pending" if changed else "already_staged",
                "assignment_status": staged["status"], "assignment_id": assignment_id,
                "payload_hash": digest, "inbox_path": str(inbox_path.resolve()),
            }
        except Exception as exc:
            if staging_path is not None:
                staging_path.unlink(missing_ok=True)
            if isinstance(exc, SectorBridgeError) or row["status"] != "success":
                _quarantine(
                    work_root, assignment_id, payload_path,
                    attempt_count=int(row["attempt_count"]),
                    contract_hash=str(envelope.get("contract_hash", "")) if "envelope" in locals() else None,
                )
            current = get_assignment(conn, assignment_id)
            if current and current["status"] in {"claimed", "running"} and current["claim_owner"] == owner:
                fail_assignment(
                    conn, assignment_id, owner, exc, now=timestamp,
                    retryable=not isinstance(exc, SectorContractError),
                )
            raise
    finally:
        conn.close()


def validate_and_stage_one(
    db_path: Path,
    assignment_id: str,
    owner: str,
    contract_hash: str,
    payload_path: Path,
    *,
    work_root: Path = DEFAULT_WORK_ROOT,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Single Worker entry point; stage_one remains the only save decision."""
    return stage_one(
        db_path, assignment_id, owner, payload_path,
        work_root=work_root, at=at, expected_contract_hash=contract_hash,
    )


def fail_one(
    db_path: Path,
    assignment_id: str,
    owner: str,
    message: str,
    *,
    error_type: str = "WorkerReportedError",
    needs_review: bool = False,
    at: datetime | None = None,
) -> dict[str, Any]:
    automatic_review = (
        error_type in {"SectorBridgeError", "SectorContractError", "SystemInvariantError"}
        or error_type.endswith("ContractError")
        or error_type.endswith("InvariantError")
    )
    conn = connect_sector_db(db_path)
    try:
        row = fail_assignment(
            conn, assignment_id, owner, RuntimeError(message), now=at,
            error_type=error_type, retryable=not (needs_review or automatic_review),
        )
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
    conn = connect_sector_db(db_path)
    try:
        target_window = completion_target_window(conn, timestamp)
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
    verify_claim_parser = subparsers.add_parser("verify-claim")
    verify_claim_parser.add_argument("--assignment-id", required=True)
    verify_claim_parser.add_argument("--owner", default=DEFAULT_OWNER)
    verify_claim_parser.add_argument("--contract-hash", required=True)
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
    validate_stage_parser = subparsers.add_parser("validate-and-stage")
    validate_stage_parser.add_argument("--assignment-id", required=True)
    validate_stage_parser.add_argument("--owner", default=DEFAULT_OWNER)
    validate_stage_parser.add_argument("--contract-hash", required=True)
    validate_stage_parser.add_argument("--payload", type=Path, required=True)
    verify_payload_parser = subparsers.add_parser("verify-payload")
    verify_payload_parser.add_argument("--assignment-id", required=True)
    verify_payload_parser.add_argument("--owner", default=DEFAULT_OWNER)
    verify_payload_parser.add_argument("--contract-hash", required=True)
    verify_payload_parser.add_argument("--payload", type=Path, required=True)
    fail_parser = subparsers.add_parser("fail")
    fail_parser.add_argument("--assignment-id", required=True)
    fail_parser.add_argument("--owner", default=DEFAULT_OWNER)
    fail_parser.add_argument("--message", required=True)
    fail_parser.add_argument("--error-type", default="WorkerReportedError")
    fail_parser.add_argument("--needs-review", action="store_true")
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
        elif args.command == "verify-claim":
            result = verify_claim_one(
                args.db, args.assignment_id, args.owner, args.contract_hash,
                work_root=args.work_root, at=at,
            )
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
        elif args.command == "validate-and-stage":
            result = validate_and_stage_one(
                args.db, args.assignment_id, args.owner, args.contract_hash, args.payload,
                work_root=args.work_root, at=at,
            )
        elif args.command == "verify-payload":
            result = verify_payload_one(
                args.db, args.assignment_id, args.owner, args.contract_hash, args.payload,
                work_root=args.work_root, at=at,
            )
        elif args.command == "fail":
            result = fail_one(
                args.db, args.assignment_id, args.owner, args.message,
                error_type=args.error_type, needs_review=args.needs_review, at=at,
            )
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
                print(_status_table(result), file=sys.stderr)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return int(result.get("exit_code", 0))
    except (RuntimeError, ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
