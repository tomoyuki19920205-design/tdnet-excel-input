"""Isolated V4 campaign state SQLite schema and access helpers.

This module is deliberately independent from the legacy backfill state store.
It never discovers a database path and has no import-time filesystem effects.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping

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
