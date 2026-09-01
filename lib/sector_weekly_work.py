"""Dedicated local assignment queue for ChatGPT-driven Sector Weekly research."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from lib.sector_weekly import (
    JST,
    REPORT_TYPE,
    WeeklyWindow,
    dedupe_key,
    ensure_week_runs,
    iso_seconds,
    now_jst,
    sector_name,
)

ASSIGNMENT_SCHEMA = "sector_weekly_assignment_v1"
ASSIGNMENT_STATUSES = frozenset({
    "pending", "ready", "claimed", "running", "success", "retry_pending", "failed",
})
ACTIVE_STATUSES = frozenset({"claimed", "running"})
MAX_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 15 * 60
MAX_LEASE_LIFETIME_SECONDS = 55 * 60
TRANSPORT_ERROR_TYPES = frozenset({"sync_pending", "sync_error"})
_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class SectorWorkError(RuntimeError):
    pass


def _now(value: datetime | None = None) -> datetime:
    result = value or now_jst()
    if result.tzinfo is None:
        raise SectorWorkError("work timestamps must include a timezone")
    return result.astimezone(JST)


def _parse(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise SectorWorkError("assignment contains an invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise SectorWorkError("assignment timestamp must include a timezone")
    return parsed.astimezone(JST)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise SectorWorkError("work timestamps must include a timezone")
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_owner(owner: str) -> str:
    if not isinstance(owner, str) or not _OWNER_RE.fullmatch(owner):
        raise SectorWorkError("claim owner must match the safe owner pattern")
    return owner


def assignment_id_for(stable_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tse-sector-weekly-work:{stable_key}"))


def payload_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _row(value: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(value) if value is not None else None


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def get_assignment(conn: sqlite3.Connection, assignment_id: str) -> dict[str, Any] | None:
    return _row(conn.execute(
        "SELECT * FROM sector_weekly_work_assignments WHERE assignment_id=?", (assignment_id,),
    ).fetchone())


def get_assignment_by_key(conn: sqlite3.Connection, stable_key: str) -> dict[str, Any] | None:
    return _row(conn.execute(
        "SELECT * FROM sector_weekly_work_assignments WHERE stable_key=?", (stable_key,),
    ).fetchone())


def enqueue_assignment(
    conn: sqlite3.Connection,
    code: int,
    window: WeeklyWindow,
    *,
    now: datetime | None = None,
    available_at: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    timestamp = _now(now)
    available = _now(available_at or timestamp)
    ensure_week_runs(conn, window)
    key = dedupe_key(window, code)
    assignment_id = assignment_id_for(key)
    initial_status = "ready" if available <= timestamp else "pending"
    created = False
    with _immediate(conn):
        existing = get_assignment_by_key(conn, key)
        if existing is None:
            conn.execute(
                "INSERT INTO sector_weekly_work_assignments "
                "(assignment_id,schema_version,stable_key,sector_code,sector_name,period_start,period_end,status,"
                "attempt_count,available_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    assignment_id, ASSIGNMENT_SCHEMA, key, code, sector_name(code),
                    _utc_text(window.period_start), _utc_text(window.period_end), initial_status, 0,
                    _utc_text(available), _utc_text(timestamp), _utc_text(timestamp),
                ),
            )
            created = True
        elif (
            existing["status"] in {"pending", "retry_pending"}
            and existing["last_error_type"] not in TRANSPORT_ERROR_TYPES
            and available <= timestamp
        ):
            conn.execute(
                "UPDATE sector_weekly_work_assignments SET status='ready',available_at=?,updated_at=? "
                "WHERE assignment_id=?",
                (_utc_text(available), _utc_text(timestamp), existing["assignment_id"]),
            )
        result = get_assignment(conn, assignment_id)
    if result is None:
        raise SectorWorkError("failed to read back enqueued assignment")
    return result, created


def _set_run_state(
    conn: sqlite3.Connection,
    stable_key: str,
    status: str,
    timestamp: datetime,
    *,
    attempt_count: int | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> None:
    started_at = iso_seconds(timestamp) if status == "running" else None
    completed_at = iso_seconds(timestamp) if status in {"success", "failed"} else None
    fields = [
        "status=?", "last_error_type=?", "last_error_message=?",
        "started_at=COALESCE(?,started_at)", "completed_at=?", "updated_at=?",
    ]
    values: list[Any] = [status, error_type, error_message, started_at, completed_at, iso_seconds(timestamp)]
    if attempt_count is not None:
        fields.append("attempt_count=?")
        values.append(attempt_count)
    values.append(stable_key)
    conn.execute(
        f"UPDATE canonical_sector_report_runs SET {','.join(fields)} WHERE run_id=?", values,
    )


def _recover_expired_in_transaction(conn: sqlite3.Connection, timestamp: datetime) -> int:
    rows = conn.execute(
        "SELECT assignment_id,stable_key,attempt_count FROM sector_weekly_work_assignments "
        "WHERE status IN ('claimed','running') AND lease_expires_at IS NOT NULL AND lease_expires_at<=?",
        (_utc_text(timestamp),),
    ).fetchall()
    for row in rows:
        terminal = int(row["attempt_count"]) >= MAX_ATTEMPTS
        status = "failed" if terminal else "retry_pending"
        conn.execute(
            "UPDATE sector_weekly_work_assignments SET status=?,available_at=?,claim_owner=NULL,claimed_at=NULL,"
            "lease_expires_at=NULL,last_error_type='LeaseExpired',last_error_message='worker lease expired before submit',"
            "updated_at=? WHERE assignment_id=?",
            (status, _utc_text(timestamp), _utc_text(timestamp), row["assignment_id"]),
        )
        _set_run_state(
            conn, row["stable_key"], status if status == "failed" else "retry_pending", timestamp,
            attempt_count=int(row["attempt_count"]), error_type="LeaseExpired",
            error_message="worker lease expired before submit",
        )
    return len(rows)


def recover_expired_leases(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    timestamp = _now(now)
    with _immediate(conn):
        return _recover_expired_in_transaction(conn, timestamp)


def claim_next(
    conn: sqlite3.Connection,
    owner: str,
    *,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    window: WeeklyWindow | None = None,
) -> dict[str, Any] | None:
    owner = _validate_owner(owner)
    if not isinstance(lease_seconds, int) or not 60 <= lease_seconds <= 60 * 60:
        raise SectorWorkError("lease_seconds must be between 60 and 3600")
    timestamp = _now(now)
    lease_expires = timestamp + timedelta(seconds=lease_seconds)
    period_filter = ""
    period_values: tuple[Any, ...] = ()
    if window is not None:
        period_filter = " AND period_start=? AND period_end=?"
        period_values = (_utc_text(window.period_start), _utc_text(window.period_end))
    actionable = conn.execute(
        "SELECT 1 FROM sector_weekly_work_assignments WHERE "
        "((status='ready' AND attempt_count<? AND available_at<=?) OR "
        "(status IN ('pending','retry_pending') AND available_at<=? "
        "AND submitted_payload_hash IS NULL "
        "AND (last_error_type IS NULL OR last_error_type NOT IN ('sync_pending','sync_error'))) OR "
        "(status IN ('claimed','running') AND lease_expires_at IS NOT NULL AND lease_expires_at<=?))"
        f"{period_filter} LIMIT 1",
        (MAX_ATTEMPTS, _utc_text(timestamp), _utc_text(timestamp), _utc_text(timestamp), *period_values),
    ).fetchone()
    active = conn.execute(
        "SELECT 1 FROM sector_weekly_work_assignments WHERE status IN ('claimed','running') "
        f"AND lease_expires_at>?{period_filter} LIMIT 1", (_utc_text(timestamp), *period_values),
    ).fetchone()
    if active is not None or actionable is None:
        return None
    with _immediate(conn):
        _recover_expired_in_transaction(conn, timestamp)
        active = conn.execute(
            "SELECT assignment_id FROM sector_weekly_work_assignments "
            f"WHERE status IN ('claimed','running') AND lease_expires_at>?{period_filter} LIMIT 1",
            (_utc_text(timestamp), *period_values),
        ).fetchone()
        if active is not None:
            return None
        conn.execute(
            "UPDATE sector_weekly_work_assignments SET status='ready',updated_at=? "
            "WHERE status IN ('pending','retry_pending') AND available_at<=? "
            "AND submitted_payload_hash IS NULL "
            "AND (last_error_type IS NULL OR last_error_type NOT IN ('sync_pending','sync_error'))"
            f"{period_filter}",
            (_utc_text(timestamp), _utc_text(timestamp), *period_values),
        )
        selected = conn.execute(
            "SELECT * FROM sector_weekly_work_assignments WHERE status='ready' AND attempt_count<? AND available_at<=? "
            f"{period_filter} ORDER BY CASE WHEN attempt_count=0 THEN 0 ELSE 1 END,sector_code LIMIT 1",
            (MAX_ATTEMPTS, _utc_text(timestamp), *period_values),
        ).fetchone()
        if selected is None:
            return None
        attempts = int(selected["attempt_count"]) + 1
        cursor = conn.execute(
            "UPDATE sector_weekly_work_assignments SET status='claimed',attempt_count=?,claim_owner=?,claimed_at=?,"
            "lease_expires_at=?,started_at=NULL,last_error_type=NULL,last_error_message=NULL,"
            "submitted_payload_hash=NULL,updated_at=? "
            "WHERE assignment_id=? AND status='ready'",
            (
                attempts, owner, _utc_text(timestamp), _utc_text(lease_expires),
                _utc_text(timestamp), selected["assignment_id"],
            ),
        )
        if cursor.rowcount != 1:
            raise SectorWorkError("assignment claim lost an atomic race")
        _set_run_state(conn, selected["stable_key"], "running", timestamp, attempt_count=attempts)
        return get_assignment(conn, selected["assignment_id"])


def heartbeat_assignment(
    conn: sqlite3.Connection,
    assignment_id: str,
    owner: str,
    *,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_lifetime_seconds: int = MAX_LEASE_LIFETIME_SECONDS,
) -> dict[str, Any]:
    """Atomically renew an active lease without exceeding its total lifetime."""
    owner = _validate_owner(owner)
    if not isinstance(lease_seconds, int) or not 60 <= lease_seconds <= 60 * 60:
        raise SectorWorkError("lease_seconds must be between 60 and 3600")
    if not isinstance(max_lifetime_seconds, int) or not lease_seconds <= max_lifetime_seconds <= 4 * 60 * 60:
        raise SectorWorkError("max_lifetime_seconds must be between lease_seconds and 14400")
    timestamp = _now(now)
    with _immediate(conn):
        row = get_assignment(conn, assignment_id)
        if row is None:
            raise SectorWorkError("assignment not found")
        if row["status"] not in ACTIVE_STATUSES or row["claim_owner"] != owner:
            raise SectorWorkError("heartbeat owner does not match active claim")
        if _parse(row["lease_expires_at"]) <= timestamp:
            raise SectorWorkError("assignment lease has expired")
        claimed_at = _parse(row["claimed_at"])
        maximum = claimed_at + timedelta(seconds=max_lifetime_seconds)
        renewed_until = min(timestamp + timedelta(seconds=lease_seconds), maximum)
        if renewed_until <= timestamp or renewed_until <= _parse(row["lease_expires_at"]):
            raise SectorWorkError("assignment maximum lease lifetime has been reached")
        cursor = conn.execute(
            "UPDATE sector_weekly_work_assignments SET lease_expires_at=?,updated_at=? "
            "WHERE assignment_id=? AND claim_owner=? AND status IN ('claimed','running') AND lease_expires_at>?",
            (_utc_text(renewed_until), _utc_text(timestamp), assignment_id, owner, _utc_text(timestamp)),
        )
        if cursor.rowcount != 1:
            raise SectorWorkError("heartbeat lost an atomic ownership race")
        return get_assignment(conn, assignment_id) or row


def mark_running(
    conn: sqlite3.Connection,
    assignment_id: str,
    owner: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    owner = _validate_owner(owner)
    timestamp = _now(now)
    with _immediate(conn):
        row = get_assignment(conn, assignment_id)
        if row is None:
            raise SectorWorkError("assignment not found")
        if row["status"] == "running" and row["claim_owner"] == owner:
            return row
        if row["status"] != "claimed" or row["claim_owner"] != owner:
            raise SectorWorkError("assignment is not claimed by this owner")
        if _parse(row["lease_expires_at"]) <= timestamp:
            raise SectorWorkError("assignment lease has expired")
        conn.execute(
            "UPDATE sector_weekly_work_assignments SET status='running',started_at=?,updated_at=? WHERE assignment_id=?",
            (_utc_text(timestamp), _utc_text(timestamp), assignment_id),
        )
        return get_assignment(conn, assignment_id) or row


def fail_assignment(
    conn: sqlite3.Connection,
    assignment_id: str,
    owner: str,
    error: Exception,
    *,
    now: datetime | None = None,
    retry_delay_seconds: int = 0,
) -> dict[str, Any]:
    owner = _validate_owner(owner)
    timestamp = _now(now)
    available = timestamp + timedelta(seconds=max(0, retry_delay_seconds))
    with _immediate(conn):
        row = get_assignment(conn, assignment_id)
        if row is None:
            raise SectorWorkError("assignment not found")
        if row["status"] == "success":
            return row
        if row["claim_owner"] != owner or row["status"] not in ACTIVE_STATUSES:
            raise SectorWorkError("assignment failure owner does not match active claim")
        terminal = int(row["attempt_count"]) >= MAX_ATTEMPTS
        status = "failed" if terminal else "retry_pending"
        error_type = type(error).__name__
        message = str(error)[:2000]
        conn.execute(
            "UPDATE sector_weekly_work_assignments SET status=?,available_at=?,claim_owner=NULL,claimed_at=NULL,"
            "lease_expires_at=NULL,last_error_type=?,last_error_message=?,updated_at=? WHERE assignment_id=?",
            (status, _utc_text(available), error_type, message, _utc_text(timestamp), assignment_id),
        )
        _set_run_state(
            conn, row["stable_key"], status if status == "failed" else "retry_pending", timestamp,
            attempt_count=int(row["attempt_count"]), error_type=error_type, error_message=message,
        )
        return get_assignment(conn, assignment_id) or row


def abandon_assignment(
    conn: sqlite3.Connection,
    assignment_id: str,
    owner: str,
    *,
    now: datetime | None = None,
    reason: str = "worker hard time budget reached",
) -> dict[str, Any]:
    """Atomically release an owned, unexpired claim for a later attempt."""
    owner = _validate_owner(owner)
    timestamp = _now(now)
    message = str(reason).strip()[:2000] or "worker hard time budget reached"
    with _immediate(conn):
        row = get_assignment(conn, assignment_id)
        if row is None:
            raise SectorWorkError("assignment not found")
        if row["claim_owner"] != owner or row["status"] not in ACTIVE_STATUSES:
            raise SectorWorkError("abandon owner does not match active claim")
        if _parse(row["lease_expires_at"]) <= timestamp:
            raise SectorWorkError("assignment lease has expired")
        terminal = int(row["attempt_count"]) >= MAX_ATTEMPTS
        status = "failed" if terminal else "retry_pending"
        cursor = conn.execute(
            "UPDATE sector_weekly_work_assignments SET status=?,available_at=?,claim_owner=NULL,claimed_at=NULL,"
            "lease_expires_at=NULL,started_at=NULL,last_error_type='WorkerAbandoned',last_error_message=?,updated_at=? "
            "WHERE assignment_id=? AND claim_owner=? AND status IN ('claimed','running') AND lease_expires_at>?",
            (
                status, _utc_text(timestamp), message, _utc_text(timestamp), assignment_id, owner,
                _utc_text(timestamp),
            ),
        )
        if cursor.rowcount != 1:
            raise SectorWorkError("abandon lost an atomic ownership race")
        _set_run_state(
            conn, row["stable_key"], status if terminal else "retry_pending", timestamp,
            attempt_count=int(row["attempt_count"]), error_type="WorkerAbandoned", error_message=message,
        )
        return get_assignment(conn, assignment_id) or row


def complete_assignment(
    conn: sqlite3.Connection,
    assignment_id: str,
    owner: str,
    submitted_hash: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    owner = _validate_owner(owner)
    if not re.fullmatch(r"[0-9a-f]{64}", submitted_hash):
        raise SectorWorkError("submitted payload hash must be SHA-256 hex")
    timestamp = _now(now)
    with _immediate(conn):
        row = get_assignment(conn, assignment_id)
        if row is None:
            raise SectorWorkError("assignment not found")
        if row["status"] == "success":
            if row["submitted_payload_hash"] != submitted_hash:
                raise SectorWorkError("completed assignment received a conflicting payload")
            return row
        if row["claim_owner"] != owner or row["status"] not in ACTIVE_STATUSES:
            raise SectorWorkError("submit owner does not match active claim")
        if _parse(row["lease_expires_at"]) <= timestamp:
            raise SectorWorkError("assignment lease expired before submit")
        conn.execute(
            "UPDATE sector_weekly_work_assignments SET status='success',completed_at=?,submitted_payload_hash=?,"
            "lease_expires_at=NULL,last_error_type=NULL,last_error_message=NULL,updated_at=? WHERE assignment_id=?",
            (_utc_text(timestamp), submitted_hash, _utc_text(timestamp), assignment_id),
        )
        _set_run_state(
            conn, row["stable_key"], "success", timestamp, attempt_count=int(row["attempt_count"]),
        )
        return get_assignment(conn, assignment_id) or row


def stage_assignment(
    conn: sqlite3.Connection,
    assignment_id: str,
    owner: str,
    submitted_hash: str,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """Atomically hand a validated research payload to the local transport worker.

    This transition deliberately does not touch canonical report data and does
    not increment the research attempt count.  The returned boolean is false
    for an idempotent restage of the same payload.
    """
    owner = _validate_owner(owner)
    if not re.fullmatch(r"[0-9a-f]{64}", submitted_hash):
        raise SectorWorkError("submitted payload hash must be SHA-256 hex")
    timestamp = _now(now)
    with _immediate(conn):
        row = get_assignment(conn, assignment_id)
        if row is None:
            raise SectorWorkError("assignment not found")
        if row["status"] == "success" or row["last_error_type"] in TRANSPORT_ERROR_TYPES:
            if row["submitted_payload_hash"] != submitted_hash:
                raise SectorWorkError("staged assignment received a conflicting payload")
            return row, False
        if row["claim_owner"] != owner or row["status"] not in ACTIVE_STATUSES:
            raise SectorWorkError("stage owner does not match active claim")
        if _parse(row["lease_expires_at"]) <= timestamp:
            raise SectorWorkError("assignment lease expired before stage")
        cursor = conn.execute(
            "UPDATE sector_weekly_work_assignments SET status='retry_pending',available_at=?,"
            "claim_owner=NULL,claimed_at=NULL,lease_expires_at=NULL,submitted_payload_hash=?,"
            "last_error_type='sync_pending',last_error_message='validated payload staged for local sync',"
            "updated_at=? WHERE assignment_id=? AND claim_owner=? "
            "AND status IN ('claimed','running') AND lease_expires_at>?",
            (
                _utc_text(timestamp), submitted_hash, _utc_text(timestamp), assignment_id,
                owner, _utc_text(timestamp),
            ),
        )
        if cursor.rowcount != 1:
            raise SectorWorkError("stage lost an atomic ownership race")
        _set_run_state(
            conn, row["stable_key"], "retry_pending", timestamp,
            attempt_count=int(row["attempt_count"]), error_type="sync_pending",
            error_message="validated payload staged for local sync",
        )
        return get_assignment(conn, assignment_id) or row, True


def prepare_staged_sync(
    conn: sqlite3.Connection,
    assignment_id: str,
    submitted_hash: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Expose an idempotently-upserted local report to the outbound sync batch."""
    timestamp = _now(now)
    with _immediate(conn):
        row = get_assignment(conn, assignment_id)
        if row is None:
            raise SectorWorkError("assignment not found")
        if row["status"] == "success" and row["submitted_payload_hash"] == submitted_hash:
            return row
        if row["status"] != "retry_pending" or not row["submitted_payload_hash"]:
            raise SectorWorkError("assignment is not waiting for transport sync")
        if row["submitted_payload_hash"] != submitted_hash:
            raise SectorWorkError("staged payload hash does not match assignment")
        _set_run_state(
            conn, row["stable_key"], "success", timestamp,
            attempt_count=int(row["attempt_count"]),
        )
        return row


def mark_staged_sync_error(
    conn: sqlite3.Connection,
    assignment_id: str,
    submitted_hash: str,
    error: Exception,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record a transport-only retry without consuming a research attempt."""
    timestamp = _now(now)
    message = str(error)[:2000]
    with _immediate(conn):
        row = get_assignment(conn, assignment_id)
        if row is None:
            raise SectorWorkError("assignment not found")
        if row["status"] != "retry_pending" or not row["submitted_payload_hash"]:
            raise SectorWorkError("assignment is not waiting for transport sync")
        if row["submitted_payload_hash"] != submitted_hash:
            raise SectorWorkError("staged payload hash does not match assignment")
        conn.execute(
            "UPDATE sector_weekly_work_assignments SET last_error_type='sync_error',"
            "last_error_message=?,updated_at=? WHERE assignment_id=?",
            (message, _utc_text(timestamp), assignment_id),
        )
        _set_run_state(
            conn, row["stable_key"], "retry_pending", timestamp,
            attempt_count=int(row["attempt_count"]), error_type="sync_error", error_message=message,
        )
        return get_assignment(conn, assignment_id) or row


def reject_staged_payload(
    conn: sqlite3.Connection,
    assignment_id: str,
    error: Exception,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return invalid staged research to the research retry lane, or fail at 3/3."""
    timestamp = _now(now)
    message = str(error)[:2000]
    error_type = f"validation_{type(error).__name__}"
    with _immediate(conn):
        row = get_assignment(conn, assignment_id)
        if row is None:
            raise SectorWorkError("assignment not found")
        if row["status"] != "retry_pending" or not row["submitted_payload_hash"]:
            raise SectorWorkError("assignment is not waiting for staged validation")
        terminal = int(row["attempt_count"]) >= MAX_ATTEMPTS
        status = "failed" if terminal else "retry_pending"
        conn.execute(
            "UPDATE sector_weekly_work_assignments SET status=?,available_at=?,submitted_payload_hash=NULL,"
            "last_error_type=?,last_error_message=?,updated_at=? WHERE assignment_id=?",
            (status, _utc_text(timestamp), error_type, message, _utc_text(timestamp), assignment_id),
        )
        _set_run_state(
            conn, row["stable_key"], status, timestamp,
            attempt_count=int(row["attempt_count"]), error_type=error_type, error_message=message,
        )
        return get_assignment(conn, assignment_id) or row


def complete_staged_assignment(
    conn: sqlite3.Connection,
    assignment_id: str,
    submitted_hash: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Complete a transport-owned handoff after its external sync succeeds."""
    if not re.fullmatch(r"[0-9a-f]{64}", submitted_hash):
        raise SectorWorkError("submitted payload hash must be SHA-256 hex")
    timestamp = _now(now)
    with _immediate(conn):
        row = get_assignment(conn, assignment_id)
        if row is None:
            raise SectorWorkError("assignment not found")
        if row["status"] == "success":
            if row["submitted_payload_hash"] != submitted_hash:
                raise SectorWorkError("completed assignment received a conflicting payload")
            return row
        if row["status"] != "retry_pending" or not row["submitted_payload_hash"]:
            raise SectorWorkError("assignment is not waiting for transport sync")
        if row["submitted_payload_hash"] != submitted_hash:
            raise SectorWorkError("staged payload hash does not match assignment")
        conn.execute(
            "UPDATE sector_weekly_work_assignments SET status='success',completed_at=?,claim_owner=NULL,"
            "claimed_at=NULL,lease_expires_at=NULL,last_error_type=NULL,last_error_message=NULL,updated_at=? "
            "WHERE assignment_id=?",
            (_utc_text(timestamp), _utc_text(timestamp), assignment_id),
        )
        _set_run_state(
            conn, row["stable_key"], "success", timestamp,
            attempt_count=int(row["attempt_count"]),
        )
        return get_assignment(conn, assignment_id) or row


def enqueue_retry_candidate(
    conn: sqlite3.Connection,
    window: WeeklyWindow,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    timestamp = _now(now)
    recover_expired_leases(conn, now=timestamp)
    ensure_week_runs(conn, window)
    for code in range(1, 34):
        key = dedupe_key(window, code)
        row = get_assignment_by_key(conn, key)
        if row is None:
            return enqueue_assignment(conn, code, window, now=timestamp)
        if row["status"] == "success":
            continue
        if row["status"] in ACTIVE_STATUSES:
            continue
        if row["last_error_type"] in TRANSPORT_ERROR_TYPES or row["submitted_payload_hash"]:
            continue
        if int(row["attempt_count"]) >= MAX_ATTEMPTS:
            continue
        if row["status"] == "ready":
            continue
        with _immediate(conn):
            conn.execute(
                "UPDATE sector_weekly_work_assignments SET status='ready',available_at=?,claim_owner=NULL,"
                "claimed_at=NULL,lease_expires_at=NULL,updated_at=? WHERE assignment_id=?",
                (_utc_text(timestamp), _utc_text(timestamp), row["assignment_id"]),
            )
            _set_run_state(
                conn, key, "retry_pending" if int(row["attempt_count"]) else "pending", timestamp,
                attempt_count=int(row["attempt_count"]),
            )
            refreshed = get_assignment(conn, row["assignment_id"])
        return refreshed, False
    return None, False


STATUS_EXIT_CODES = {
    "COMPLETE_33_OF_33": 0,
    "IN_PROGRESS": 10,
    "INCOMPLETE_RETRYABLE": 11,
    "FAILED_FINAL": 12,
    "STALE_PREVIOUS_PERIOD": 13,
    "DATA_INCONSISTENT": 20,
}


def completion_status(conn: sqlite3.Connection, window: WeeklyWindow) -> dict[str, Any]:
    """Summarize one target week without exposing report payloads or secrets."""
    start, end = _utc_text(window.period_start), _utc_text(window.period_end)
    rows = [dict(row) for row in conn.execute(
        "SELECT assignment_id,stable_key,sector_code,sector_name,status,attempt_count,completed_at "
        "FROM sector_weekly_work_assignments WHERE period_start=? AND period_end=? ORDER BY sector_code",
        (start, end),
    ).fetchall()]
    counts = {status: 0 for status in ASSIGNMENT_STATUSES}
    sectors: dict[int, list[dict[str, Any]]] = {}
    attempts = 0
    completed: list[str] = []
    inconsistent = 0
    for row in rows:
        status = str(row["status"])
        if status not in counts:
            inconsistent += 1
            continue
        counts[status] += 1
        attempts += int(row["attempt_count"])
        code = int(row["sector_code"])
        sectors.setdefault(code, []).append(row)
        if row["sector_name"] != sector_name(code) or row["stable_key"] != dedupe_key(window, code):
            inconsistent += 1
        if row["completed_at"]:
            completed.append(str(row["completed_at"]))
    missing = [code for code in range(1, 34) if code not in sectors]
    duplicates = sum(max(0, len(items) - 1) for items in sectors.values())
    stale_rows = conn.execute(
        "SELECT assignment_id,stable_key,sector_code,sector_name,status,attempt_count,period_start,period_end,last_error_type "
        "FROM sector_weekly_work_assignments WHERE period_end<? AND status<>'success' ORDER BY period_end,sector_code",
        (end,),
    ).fetchall()
    stale = [dict(row) for row in stale_rows]
    active = counts["claimed"] + counts["running"]
    retryable = counts["pending"] + counts["ready"] + counts["retry_pending"]
    if inconsistent or duplicates or len(rows) > 33:
        state = "DATA_INCONSISTENT"
    elif counts["failed"]:
        state = "FAILED_FINAL"
    elif counts["success"] == 33 and not missing:
        state = "COMPLETE_33_OF_33"
    elif active:
        state = "IN_PROGRESS"
    elif not rows and stale:
        state = "STALE_PREVIOUS_PERIOD"
    elif rows or missing:
        state = "INCOMPLETE_RETRYABLE"
    else:
        state = "INCOMPLETE_RETRYABLE"
    conditions = [state]
    if stale and "STALE_PREVIOUS_PERIOD" not in conditions:
        conditions.append("STALE_PREVIOUS_PERIOD")
    event_key = f"sector_weekly_completion:{window.week_key}" if state == "COMPLETE_33_OF_33" else None
    return {
        "state": state,
        "conditions": conditions,
        "exit_code": STATUS_EXIT_CODES[state],
        "period_start": iso_seconds(window.period_start),
        "period_end": iso_seconds(window.period_end),
        "total_sectors": 33,
        "assignments": len(rows),
        **{status: counts[status] for status in sorted(counts)},
        "missing_count": len(missing),
        "missing_sectors": [{"sector_code": code, "sector_name": sector_name(code)} for code in missing],
        "stale_count": len(stale),
        "stale_assignments": stale,
        "duplicate_count": duplicates,
        "inconsistency_count": inconsistent,
        "retryable_count": retryable,
        "attempts": attempts,
        "last_success_at": max(completed) if completed else None,
        "complete": state == "COMPLETE_33_OF_33",
        "completion_event": ({"event": "sector_weekly_33_of_33_complete", "event_key": event_key} if event_key else None),
    }
