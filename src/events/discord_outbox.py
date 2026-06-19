import sqlite3
import json
import hashlib
from datetime import datetime
from typing import List, Dict, Any, Tuple, Optional

def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def verify_outbox_schema(db_path: str) -> bool:
    """Checks if the discord_chunk_outbox table exists."""
    try:
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='discord_chunk_outbox'"
            ).fetchone()
            return row is not None
    except Exception:
        return False

def compute_chunk_id(run_id: str, payload_hash: str, dedupe_keys: List[str]) -> str:
    """Computes a stable chunk_id."""
    data = f"{run_id}|{payload_hash}|{json.dumps(dedupe_keys)}"
    return hashlib.sha256(data.encode('utf-8')).hexdigest()

def _now() -> str:
    # return UTC ISO or local time depending on existing convention, the migration uses datetime('now', 'localtime')
    # we will just use python's local time to match SQLite
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def create_prepared_chunk(
    db_path: str,
    chunk_id: str,
    run_id: str,
    payload_hash: str,
    content_length: int,
    message_count: int,
    dedupe_keys: List[str],
    tickers: List[str],
    webhook_hash: str
) -> None:
    now = _now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO discord_chunk_outbox (
                chunk_id, run_id, payload_hash, content_length, message_count,
                dedupe_keys_json, tickers_json, webhook_hash, status, created_at, updated_at, prepared_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?)
            """,
            (
                chunk_id, run_id, payload_hash, content_length, message_count,
                json.dumps(dedupe_keys), json.dumps(tickers), webhook_hash,
                now, now, now
            )
        )
        conn.commit()

def mark_posting(db_path: str, chunk_id: str) -> None:
    now = _now()
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE discord_chunk_outbox SET status='posting', posting_at=?, updated_at=? WHERE chunk_id=?",
            (now, now, chunk_id)
        )
        conn.commit()

def mark_sent_http_204(db_path: str, chunk_id: str, http_status: int = 204) -> None:
    now = _now()
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE discord_chunk_outbox SET status='sent_http_204', sent_at=?, updated_at=?, http_status=? WHERE chunk_id=?",
            (now, now, http_status, chunk_id)
        )
        conn.commit()

def mark_state_update_started(db_path: str, chunk_id: str) -> None:
    now = _now()
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE discord_chunk_outbox SET status='state_update_started', state_update_started_at=?, updated_at=? WHERE chunk_id=?",
            (now, now, chunk_id)
        )
        conn.commit()

def mark_state_update_completed(db_path: str, chunk_id: str, state_update_result: Dict[str, Any]) -> None:
    now = _now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE discord_chunk_outbox 
            SET status='state_update_completed', state_updated_at=?, updated_at=?, state_update_result_json=? 
            WHERE chunk_id=?
            """,
            (now, now, json.dumps(state_update_result), chunk_id)
        )
        conn.commit()

def mark_manual_review_required(db_path: str, chunk_id: str, reason: str) -> None:
    now = _now()
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE discord_chunk_outbox SET status='manual_review_required', error_message=?, updated_at=? WHERE chunk_id=?",
            (reason, now, chunk_id)
        )
        conn.commit()

def mark_failed_before_send(db_path: str, chunk_id: str, reason: str) -> None:
    now = _now()
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE discord_chunk_outbox SET status='failed_before_send', error_message=?, updated_at=? WHERE chunk_id=?",
            (reason, now, chunk_id)
        )
        conn.commit()

def mark_failed_after_send(db_path: str, chunk_id: str, reason: str, http_status: Optional[int] = None) -> None:
    now = _now()
    with get_connection(db_path) as conn:
        if http_status:
            conn.execute(
                "UPDATE discord_chunk_outbox SET status='failed_after_send', error_message=?, updated_at=?, http_status=? WHERE chunk_id=?",
                (reason, now, http_status, chunk_id)
            )
        else:
            conn.execute(
                "UPDATE discord_chunk_outbox SET status='failed_after_send', error_message=?, updated_at=? WHERE chunk_id=?",
                (reason, now, chunk_id)
            )
        conn.commit()

def get_chunk(db_path: str, chunk_id: str) -> Optional[sqlite3.Row]:
    with get_connection(db_path) as conn:
        return conn.execute("SELECT * FROM discord_chunk_outbox WHERE chunk_id=?", (chunk_id,)).fetchone()

def scan_outbox_blockers(db_path: str) -> List[sqlite3.Row]:
    """Returns chunks that block further aggregation pipeline execution."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM discord_chunk_outbox WHERE status IN ('posting', 'manual_review_required')"
        ).fetchall()
        return rows

def assert_no_outbox_blockers(db_path: str) -> None:
    """Raises an exception if there are blockers."""
    blockers = scan_outbox_blockers(db_path)
    if blockers:
        blocker_ids = [b['chunk_id'] for b in blockers]
        raise RuntimeError(f"Outbox blockers found: {blocker_ids}")

def classify_recovery_action(status: str) -> str:
    """Classifies what recovery action is safe based on status."""
    if status == 'prepared':
        return "SAFE_TO_RETRY_SEND"
    elif status == 'posting':
        return "MANUAL_REVIEW_REQUIRED"
    elif status in ('sent_http_204', 'state_update_started'):
        return "SAFE_TO_RETRY_STATE_UPDATE"
    elif status == 'state_update_completed':
        return "COMPLETED"
    elif status == 'manual_review_required':
        return "BLOCKED"
    elif status == 'failed_before_send':
        return "SAFE_TO_RETRY_SEND"
    elif status == 'failed_after_send':
        return "MANUAL_REVIEW_REQUIRED"
    return "UNKNOWN"
