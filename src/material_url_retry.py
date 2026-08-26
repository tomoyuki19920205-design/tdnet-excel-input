"""Durable validation and retry state for Viewer material URLs.

The feed can lead the public TDNET CDN by a few minutes.  A first 404 is
therefore evidence to retry, not evidence that a disclosure is permanently
invalid.  This module keeps the original disclosure identity and timestamp,
leases due work across Realtime/Nightly, and only finalizes repeated 404/410
responses after both an attempt and an age threshold have been reached.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

import requests

STATUS_VALID = "valid"
STATUS_PENDING = "pending_retry"
STATUS_INVALID = "invalid_url"
STATUS_ARCHIVED = "archived"

NOT_FOUND_MAX_ATTEMPTS = 6
NOT_FOUND_MIN_AGE = timedelta(hours=6)
TRANSIENT_ARCHIVE_ATTEMPTS = 100
TRANSIENT_ARCHIVE_AGE = timedelta(days=30)
RETRY_DELAYS_SECONDS = (300, 900, 1800, 3600, 10800, 21600, 43200, 86400)

SERVICE_USER_AGENT = "TDnetExcelInput/1.0"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | str | None) -> datetime:
    if value is None:
        return _utc_now()
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ValidationResult:
    classification: str
    reason: str
    http_status: int | None = None
    final_url: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.classification == STATUS_VALID

    @property
    def is_not_found(self) -> bool:
        return self.http_status in (404, 410)


@dataclass(frozen=True)
class RetryCandidate:
    source_key: str
    source: str
    ticker: str
    company_name: str
    title: str
    document_url: str
    disclosure_datetime: str
    disclosure_type: str
    source_doc_id: str
    source_page_url: str = ""


def validate_material_url(
    url: str,
    *,
    session: requests.Session | None = None,
    timeout_sec: float = 20.0,
) -> ValidationResult:
    """Classify URL validation without turning temporary failures permanent."""
    value = (url or "").strip()
    if not re.match(r"^https?://", value, re.IGNORECASE):
        return ValidationResult(STATUS_INVALID, "malformed_or_internal_url")
    client = session or requests
    response = None
    try:
        response = client.get(
            value,
            headers={"User-Agent": SERVICE_USER_AGENT, "Range": "bytes=0-31"},
            timeout=timeout_sec,
            stream=True,
            allow_redirects=True,
        )
        if response.status_code == 403:
            response.close()
            response = client.get(
                value,
                headers={"User-Agent": BROWSER_USER_AGENT, "Range": "bytes=0-31"},
                timeout=timeout_sec,
                stream=True,
                allow_redirects=True,
            )
        status = int(response.status_code)
        final_url = getattr(response, "url", None) or value
        if status in (404, 410):
            return ValidationResult(STATUS_PENDING, "not_found", status, final_url)
        if status in (403, 408, 425, 429) or 500 <= status <= 599:
            return ValidationResult(STATUS_PENDING, "transient_http", status, final_url)
        if status not in (200, 206):
            return ValidationResult(STATUS_INVALID, "permanent_http", status, final_url)
        first = next(response.iter_content(32), b"")
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "pdf" in content_type or first.startswith(b"%PDF"):
            return ValidationResult(STATUS_VALID, "pdf_verified", status, final_url)
        return ValidationResult(STATUS_INVALID, "successful_non_pdf", status, final_url)
    except (requests.Timeout, requests.ConnectionError) as exc:
        return ValidationResult(STATUS_PENDING, type(exc).__name__)
    except requests.RequestException as exc:
        return ValidationResult(STATUS_PENDING, type(exc).__name__)
    finally:
        if response is not None:
            response.close()


def connect_retry_db(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    init_retry_db(conn)
    return conn


def init_retry_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS material_url_retries (
            source_key TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            ticker TEXT NOT NULL,
            company_name TEXT NOT NULL,
            title TEXT NOT NULL,
            document_url TEXT NOT NULL,
            disclosure_datetime TEXT NOT NULL,
            disclosure_type TEXT NOT NULL,
            source_doc_id TEXT NOT NULL,
            source_page_url TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN
                ('valid','pending_retry','invalid_url','archived')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            not_found_count INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_attempt_at TEXT,
            next_retry_at TEXT,
            validated_at TEXT,
            invalidated_at TEXT,
            archived_at TEXT,
            last_http_status INTEGER,
            last_failure_reason TEXT,
            lease_token TEXT,
            lease_until TEXT,
            notification_published_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_material_url_retries_due
          ON material_url_retries(status,next_retry_at,lease_until);
        CREATE TABLE IF NOT EXISTS material_url_retry_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL,
            runner TEXT NOT NULL,
            pending_count INTEGER NOT NULL,
            valid_count INTEGER NOT NULL,
            invalid_count INTEGER NOT NULL,
            archived_count INTEGER NOT NULL,
            claimed_count INTEGER NOT NULL,
            recovered_count INTEGER NOT NULL,
            finalized_invalid_count INTEGER NOT NULL,
            publish_failed_count INTEGER NOT NULL,
            details_json TEXT NOT NULL
        );
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(material_url_retries)")}
    if "source_page_url" not in columns:
        conn.execute(
            "ALTER TABLE material_url_retries ADD COLUMN source_page_url TEXT NOT NULL DEFAULT ''"
        )
    conn.commit()


def _next_retry(now: datetime, attempt_count: int) -> str:
    delay = RETRY_DELAYS_SECONDS[min(max(attempt_count - 1, 0), len(RETRY_DELAYS_SECONDS) - 1)]
    return (now + timedelta(seconds=delay)).isoformat()


def record_failed_candidate(
    conn: sqlite3.Connection,
    candidate: RetryCandidate,
    result: ValidationResult,
    *,
    now: datetime | str | None = None,
) -> str:
    """Insert/update failed validation while preserving original metadata."""
    moment = _as_utc(now)
    now_iso = moment.isoformat()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT * FROM material_url_retries WHERE source_key=?", (candidate.source_key,)
    ).fetchone()
    if row and row["status"] == STATUS_VALID:
        conn.commit()
        return str(row["status"])
    attempts = (int(row["attempt_count"]) if row else 0) + 1
    not_found = (int(row["not_found_count"]) if row else 0) + int(result.is_not_found)
    first_seen = _as_utc(row["first_seen_at"] if row else moment)
    age = moment - first_seen
    status = result.classification
    if result.is_not_found:
        status = (
            STATUS_INVALID
            if not_found >= NOT_FOUND_MAX_ATTEMPTS and age >= NOT_FOUND_MIN_AGE
            else STATUS_PENDING
        )
    elif result.classification == STATUS_PENDING:
        status = (
            STATUS_ARCHIVED
            if attempts >= TRANSIENT_ARCHIVE_ATTEMPTS and age >= TRANSIENT_ARCHIVE_AGE
            else STATUS_PENDING
        )
    next_retry = (
        _next_retry(moment, attempts) if status == STATUS_PENDING
        else (moment + timedelta(days=7)).isoformat() if status == STATUS_ARCHIVED
        else None
    )
    values = asdict(candidate)
    conn.execute("""
        INSERT INTO material_url_retries (
          source_key,source,ticker,company_name,title,document_url,
          disclosure_datetime,disclosure_type,source_doc_id,source_page_url,status,
          attempt_count,not_found_count,first_seen_at,last_attempt_at,next_retry_at,
          invalidated_at,archived_at,last_http_status,last_failure_reason,
          created_at,updated_at
        ) VALUES (
          :source_key,:source,:ticker,:company_name,:title,:document_url,
          :disclosure_datetime,:disclosure_type,:source_doc_id,:source_page_url,:status,
          :attempt_count,:not_found_count,:first_seen_at,:last_attempt_at,:next_retry_at,
          :invalidated_at,:archived_at,:last_http_status,:last_failure_reason,
          :created_at,:updated_at
        ) ON CONFLICT(source_key) DO UPDATE SET
          status=excluded.status,attempt_count=excluded.attempt_count,
          not_found_count=excluded.not_found_count,last_attempt_at=excluded.last_attempt_at,
          next_retry_at=excluded.next_retry_at,invalidated_at=excluded.invalidated_at,
          archived_at=excluded.archived_at,last_http_status=excluded.last_http_status,
          last_failure_reason=excluded.last_failure_reason,lease_token=NULL,lease_until=NULL,
          updated_at=excluded.updated_at
    """, {
        **values,
        "status": status,
        "attempt_count": attempts,
        "not_found_count": not_found,
        "first_seen_at": first_seen.isoformat(),
        "last_attempt_at": now_iso,
        "next_retry_at": next_retry,
        "invalidated_at": now_iso if status == STATUS_INVALID else None,
        "archived_at": now_iso if status == STATUS_ARCHIVED else None,
        "last_http_status": result.http_status,
        "last_failure_reason": result.reason,
        "created_at": row["created_at"] if row else now_iso,
        "updated_at": now_iso,
    })
    conn.commit()
    return status


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {status: 0 for status in (STATUS_PENDING, STATUS_VALID, STATUS_INVALID, STATUS_ARCHIVED)}
    for row in conn.execute("SELECT status,count(*) AS n FROM material_url_retries GROUP BY status"):
        counts[str(row["status"])] = int(row["n"])
    return counts


def run_due_retries(
    conn: sqlite3.Connection,
    *,
    publish: Callable[[RetryCandidate], bool],
    validator: Callable[[str], ValidationResult] = validate_material_url,
    now: datetime | str | None = None,
    runner: str = "manual",
    limit: int = 200,
    lease_seconds: int = 600,
) -> dict[str, int]:
    """Lease due candidates, validate and publish each recovered item once."""
    moment = _as_utc(now)
    now_iso = moment.isoformat()
    lease_until = (moment + timedelta(seconds=lease_seconds)).isoformat()
    rows = conn.execute("""
        SELECT source_key FROM material_url_retries
        WHERE status IN ('pending_retry','archived') AND next_retry_at<=?
          AND (lease_until IS NULL OR lease_until<?)
        ORDER BY next_retry_at,first_seen_at LIMIT ?
    """, (now_iso, now_iso, limit)).fetchall()
    claimed: list[tuple[str, str]] = []
    for row in rows:
        token = uuid.uuid4().hex
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute("""
            UPDATE material_url_retries SET lease_token=?,lease_until=?,updated_at=?
            WHERE source_key=? AND status IN ('pending_retry','archived')
              AND next_retry_at<=? AND (lease_until IS NULL OR lease_until<?)
        """, (token, lease_until, now_iso, row["source_key"], now_iso, now_iso)).rowcount
        conn.commit()
        if updated:
            claimed.append((str(row["source_key"]), token))

    recovered = finalized = publish_failed = 0
    details: list[dict[str, object]] = []
    for source_key, token in claimed:
        row = conn.execute(
            "SELECT * FROM material_url_retries WHERE source_key=? AND lease_token=?",
            (source_key, token),
        ).fetchone()
        if row is None:
            continue
        candidate = RetryCandidate(**{field: row[field] for field in RetryCandidate.__dataclass_fields__})
        result = validator(candidate.document_url)
        if result.is_valid:
            published = False
            try:
                published = bool(publish(candidate))
            except Exception:
                published = False
            if published:
                conn.execute("""
                    UPDATE material_url_retries SET status='valid',attempt_count=attempt_count+1,
                      last_attempt_at=?,next_retry_at=NULL,validated_at=?,last_http_status=?,
                      last_failure_reason=NULL,lease_token=NULL,lease_until=NULL,
                      notification_published_at=?,updated_at=?
                    WHERE source_key=? AND lease_token=?
                """, (now_iso, now_iso, result.http_status, now_iso, now_iso, source_key, token))
                conn.commit()
                recovered += 1
                details.append({"source_key": source_key, "result": STATUS_VALID})
                continue
            publish_failed += 1
            result = ValidationResult(STATUS_PENDING, "publish_failed", result.http_status, result.final_url)

        previous = str(row["status"])
        status = record_failed_candidate(conn, candidate, result, now=moment)
        finalized += int(previous != STATUS_INVALID and status == STATUS_INVALID)
        details.append({
            "source_key": source_key, "result": status,
            "reason": result.reason, "http_status": result.http_status,
        })

    counts = _counts(conn)
    conn.execute("""
        INSERT INTO material_url_retry_runs (
          run_at,runner,pending_count,valid_count,invalid_count,archived_count,
          claimed_count,recovered_count,finalized_invalid_count,publish_failed_count,details_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        now_iso, runner, counts[STATUS_PENDING], counts[STATUS_VALID],
        counts[STATUS_INVALID], counts[STATUS_ARCHIVED], len(claimed), recovered,
        finalized, publish_failed, json.dumps(details, ensure_ascii=False, sort_keys=True),
    ))
    conn.commit()
    return {
        "pending": counts[STATUS_PENDING], "valid": counts[STATUS_VALID],
        "invalid_url": counts[STATUS_INVALID], "archived": counts[STATUS_ARCHIVED],
        "claimed": len(claimed), "recovered": recovered,
        "finalized_invalid": finalized, "publish_failed": publish_failed,
    }


def list_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return _counts(conn)
