"""Isolated V4 campaign state SQLite schema and access helpers.

This module is deliberately independent from the legacy backfill state store.
It never discovers a database path and has no import-time filesystem effects.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

SCHEMA_VERSION = "1"

CAMPAIGN_STATUSES = {
    "CREATED", "REGISTERING", "READY", "RUNNING", "COMPLETED", "FAILED", "CLOSED",
}

_CAMPAIGN_COLUMNS = (
    "campaign_id", "campaign_name", "manifest_path", "manifest_sha256",
    "manifest_record_count", "code_sha", "worker_version", "status",
    "created_at", "updated_at",
)

_FILING_COLUMNS = (
    "campaign_id", "manifest_row_id", "state_filing_id", "requested_disclosure_no",
    "company_code", "normalized_company_code", "source_url", "normalized_xbrl_url",
    "disclosure_date", "expected_period", "expected_quarter", "document_type",
    "internal_document_id", "zip_sha256", "zip_internal_ticker", "zip_internal_period",
    "zip_internal_quarter", "run_id", "worker_version", "extractor_version",
    "extractor_route", "code_sha", "registration_status", "identity_status",
    "cache_status", "extraction_status", "sqlite_save_status", "canonical_save_status",
    "supabase_save_status", "overall_status", "error_code", "error_stage",
    "error_message", "retryable", "created_at", "updated_at", "started_at",
    "completed_at",
)

FRESH_DOWNLOAD_MUTABLE_COLUMNS = (
    "internal_document_id", "zip_sha256", "zip_internal_ticker",
    "zip_internal_period", "zip_internal_quarter", "identity_status",
    "cache_status", "overall_status", "error_code", "error_stage",
    "error_message", "retryable", "updated_at",
)

FRESH_DOWNLOAD_SUCCESS_CONSTANTS = {
    "identity_status": "VERIFIED",
    "cache_status": "READY",
    "overall_status": "IDENTITY_VERIFIED",
    "error_code": None,
    "error_stage": None,
    "error_message": None,
    "retryable": 1,
}


class FreshDownloadCASFailed(RuntimeError):
    """The production fresh-download compare-and-swap contract did not match."""


def _non_target_digest(conn: sqlite3.Connection, campaign_id: str, target_ids: set[str]) -> str:
    digest = hashlib.sha256()
    for row in conn.execute(
        "SELECT * FROM campaign_filings WHERE campaign_id=? ORDER BY manifest_row_id", (campaign_id,)
    ):
        current = dict(row)
        if str(current["manifest_row_id"]) in target_ids:
            continue
        encoded = (json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        digest.update(encoded)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect_db(path: str | Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Open an explicitly supplied SQLite path with safe connection settings."""
    conn = sqlite3.connect(str(path), timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run an explicit transaction and rollback on any exception."""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def initialize_schema(conn: sqlite3.Connection, *, schema_version: str = SCHEMA_VERSION) -> None:
    """Create the campaign schema idempotently; reject version mismatches."""
    conn.execute("PRAGMA foreign_keys = ON")
    with transaction(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS campaign_schema_metadata ("
            "schema_version TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        row = conn.execute(
            "SELECT schema_version FROM campaign_schema_metadata LIMIT 1"
        ).fetchone()
        if row is not None and str(row[0]) != str(schema_version):
            raise RuntimeError(
                f"campaign schema version mismatch: expected {schema_version}, found {row[0]}"
            )
        now = _now()
        if row is None:
            conn.execute(
                "INSERT INTO campaign_schema_metadata(schema_version, created_at, updated_at) VALUES (?, ?, ?)",
                (str(schema_version), now, now),
            )

        conn.execute(
            """CREATE TABLE IF NOT EXISTS campaigns (
                campaign_id TEXT PRIMARY KEY,
                campaign_name TEXT NOT NULL,
                manifest_path TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                manifest_record_count INTEGER NOT NULL,
                code_sha TEXT NOT NULL,
                worker_version TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS campaign_filings (
                campaign_id TEXT NOT NULL,
                manifest_row_id TEXT NOT NULL,
                state_filing_id TEXT,
                requested_disclosure_no TEXT,
                company_code TEXT,
                normalized_company_code TEXT,
                source_url TEXT,
                normalized_xbrl_url TEXT,
                disclosure_date TEXT,
                expected_period TEXT,
                expected_quarter TEXT,
                document_type TEXT,
                internal_document_id TEXT,
                zip_sha256 TEXT,
                zip_internal_ticker TEXT,
                zip_internal_period TEXT,
                zip_internal_quarter TEXT,
                run_id TEXT,
                worker_version TEXT,
                extractor_version TEXT,
                extractor_route TEXT,
                code_sha TEXT,
                registration_status TEXT NOT NULL DEFAULT 'REGISTERED',
                identity_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
                cache_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                extraction_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
                sqlite_save_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
                canonical_save_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
                supabase_save_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
                overall_status TEXT NOT NULL DEFAULT 'REGISTERED',
                error_code TEXT,
                error_stage TEXT,
                error_message TEXT,
                retryable INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                PRIMARY KEY (campaign_id, manifest_row_id),
                FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id)
            )"""
        )
        indexes = {
            "ix_campaign_filings_requested": "requested_disclosure_no",
            "ix_campaign_filings_company": "normalized_company_code",
            "ix_campaign_filings_url": "normalized_xbrl_url",
            "ix_campaign_filings_status": "overall_status",
            "ix_campaign_filings_cache": "cache_status",
            "ix_campaign_filings_extraction": "extraction_status",
        }
        for name, column in indexes.items():
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON campaign_filings(campaign_id, {column})")


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone()
    return row is not None


def get_schema_version(conn: sqlite3.Connection) -> str | None:
    if not table_exists(conn, "campaign_schema_metadata"):
        return None
    row = conn.execute("SELECT schema_version FROM campaign_schema_metadata LIMIT 1").fetchone()
    return None if row is None else str(row[0])


def create_campaign(conn: sqlite3.Connection, values: Mapping[str, object]) -> None:
    now = str(values.get("created_at") or _now())
    updated = str(values.get("updated_at") or now)
    payload = dict(values)
    payload.setdefault("created_at", now)
    payload.setdefault("updated_at", updated)
    status = str(payload.get("status") or "CREATED")
    if status not in CAMPAIGN_STATUSES:
        raise ValueError(f"invalid campaign status: {status}")
    payload["status"] = status
    columns = ", ".join(_CAMPAIGN_COLUMNS)
    placeholders = ", ".join("?" for _ in _CAMPAIGN_COLUMNS)
    conn.execute(
        f"INSERT INTO campaigns ({columns}) VALUES ({placeholders})",
        [payload.get(column) for column in _CAMPAIGN_COLUMNS],
    )


def create_campaign_filing(conn: sqlite3.Connection, values: Mapping[str, object]) -> None:
    payload = dict(values)
    now = str(payload.get("created_at") or _now())
    payload.setdefault("created_at", now)
    payload.setdefault("updated_at", now)
    payload.setdefault("registration_status", "REGISTERED")
    payload.setdefault("identity_status", "UNVERIFIED")
    payload.setdefault("cache_status", "UNKNOWN")
    payload.setdefault("extraction_status", "NOT_STARTED")
    payload.setdefault("sqlite_save_status", "NOT_STARTED")
    payload.setdefault("canonical_save_status", "NOT_STARTED")
    payload.setdefault("supabase_save_status", "NOT_STARTED")
    payload.setdefault("overall_status", "REGISTERED")
    payload.setdefault("retryable", 1)
    columns = ", ".join(_FILING_COLUMNS)
    placeholders = ", ".join("?" for _ in _FILING_COLUMNS)
    conn.execute(
        f"INSERT INTO campaign_filings ({columns}) VALUES ({placeholders})",
        [payload.get(column) for column in _FILING_COLUMNS],
    )


def create_campaign_filings(conn: sqlite3.Connection, values_list: list[Mapping[str, object]]) -> None:
    """Insert campaign filings in bulk; caller owns the transaction boundary."""
    payloads: list[list[object]] = []
    now = _now()
    for values in values_list:
        payload = dict(values)
        payload.setdefault("created_at", now)
        payload.setdefault("updated_at", now)
        payload.setdefault("registration_status", "REGISTERED")
        payload.setdefault("identity_status", "UNVERIFIED")
        payload.setdefault("cache_status", "UNKNOWN")
        payload.setdefault("extraction_status", "NOT_STARTED")
        payload.setdefault("sqlite_save_status", "NOT_STARTED")
        payload.setdefault("canonical_save_status", "NOT_STARTED")
        payload.setdefault("supabase_save_status", "NOT_STARTED")
        payload.setdefault("overall_status", "REGISTERED")
        payload.setdefault("retryable", 1)
        payloads.append([payload.get(column) for column in _FILING_COLUMNS])
    if not payloads:
        return
    columns = ", ".join(_FILING_COLUMNS)
    placeholders = ", ".join("?" for _ in _FILING_COLUMNS)
    conn.executemany(
        f"INSERT INTO campaign_filings ({columns}) VALUES ({placeholders})", payloads
    )


def apply_fresh_download_successes(
    conn: sqlite3.Connection,
    *,
    campaign_id: str,
    before_rows: Sequence[Mapping[str, object]],
    verified_results: Sequence[Mapping[str, object]],
    expected_count: int = 5,
    updated_at: str | None = None,
    after_update: Callable[[int, sqlite3.Connection], None] | None = None,
) -> list[dict[str, object]]:
    """Atomically mark exactly ``expected_count`` verified downloads ready.

    Every column from the read-only starting snapshot participates in the CAS
    predicate.  Only the audited fresh-download mutable columns may change.
    The callback is a test-only fault-injection seam; production callers omit it.
    """
    if expected_count != 5 or len(before_rows) != expected_count or len(verified_results) != expected_count:
        raise FreshDownloadCASFailed("fresh download update requires exactly five rows")
    before_by_id = {str(row.get("manifest_row_id") or ""): dict(row) for row in before_rows}
    result_by_id = {str(row.get("manifest_row_id") or ""): dict(row) for row in verified_results}
    if "" in before_by_id or set(before_by_id) != set(result_by_id) or len(before_by_id) != expected_count:
        raise FreshDownloadCASFailed("fresh download target identity mismatch")
    if any(str(row.get("campaign_id") or "") != campaign_id for row in before_by_id.values()):
        raise FreshDownloadCASFailed("fresh download campaign identity mismatch")

    timestamp = str(updated_at or _now())
    assignments = ", ".join(f"{column}=?" for column in FRESH_DOWNLOAD_MUTABLE_COLUMNS)
    cas = " AND ".join(f"{column} IS ?" for column in _FILING_COLUMNS)
    protected = tuple(column for column in _FILING_COLUMNS if column not in FRESH_DOWNLOAD_MUTABLE_COLUMNS)
    readbacks: list[dict[str, object]] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        non_target_before = _non_target_digest(conn, campaign_id, set(before_by_id))
        for index, row_id in enumerate(sorted(before_by_id)):
            before = before_by_id[row_id]
            verified = result_by_id[row_id]
            desired = {
                "internal_document_id": verified.get("internal_document_id"),
                "zip_sha256": verified.get("zip_sha256"),
                "zip_internal_ticker": verified.get("ticker"),
                "zip_internal_period": verified.get("period"),
                "zip_internal_quarter": verified.get("quarter"),
                **FRESH_DOWNLOAD_SUCCESS_CONSTANTS,
                "updated_at": timestamp,
            }
            required = (
                desired["internal_document_id"], desired["zip_sha256"],
                desired["zip_internal_ticker"], desired["zip_internal_period"],
                desired["zip_internal_quarter"],
            )
            if any(value in {None, ""} for value in required):
                raise FreshDownloadCASFailed("verified artifact metadata is incomplete")
            if (
                str(before.get("expected_period") or "") != str(desired["zip_internal_period"])
                or str(before.get("expected_quarter") or "") != str(desired["zip_internal_quarter"])
                or str(before.get("normalized_company_code") or "") != str(desired["zip_internal_ticker"])
            ):
                raise FreshDownloadCASFailed("verified artifact metadata does not match campaign row")
            values = [desired[column] for column in FRESH_DOWNLOAD_MUTABLE_COLUMNS]
            values.extend(before.get(column) for column in _FILING_COLUMNS)
            cursor = conn.execute(
                f"UPDATE campaign_filings SET {assignments} WHERE {cas}", values
            )
            if cursor.rowcount != 1:
                raise FreshDownloadCASFailed(f"fresh download CAS failed for {row_id}")
            if after_update is not None:
                after_update(index, conn)
            current_row = conn.execute(
                "SELECT * FROM campaign_filings WHERE campaign_id=? AND manifest_row_id=?",
                (campaign_id, row_id),
            ).fetchone()
            if current_row is None:
                raise FreshDownloadCASFailed(f"fresh download readback missing for {row_id}")
            current = dict(current_row)
            if any(current.get(column) != before.get(column) for column in protected):
                raise FreshDownloadCASFailed(f"protected field changed for {row_id}")
            if any(current.get(column) != desired[column] for column in FRESH_DOWNLOAD_MUTABLE_COLUMNS):
                raise FreshDownloadCASFailed(f"fresh download readback mismatch for {row_id}")
            readbacks.append(current)
        if _non_target_digest(conn, campaign_id, set(before_by_id)) != non_target_before:
            raise FreshDownloadCASFailed("non-target campaign row changed")
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    return readbacks
