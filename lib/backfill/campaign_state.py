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

SCHEMA_VERSION = "2"
LEGACY_SCHEMA_VERSION = "1"

FRESH_DOWNLOAD_STATUSES = {
    "NOT_STARTED", "COMPLETE", "FAILED_RETRYABLE", "FAILED_PERMANENT",
    "QUARANTINED", "CONFLICT",
}
FRESH_DOWNLOAD_PLAN_CLASSES = {
    "STANDARD_FRESH_DOWNLOAD", "QUARANTINE_FRESH_RECHECK",
}
STOP_FRESH_STATE_EXISTING_CONFLICT = "STOP_V4_FRESH_STATE_TABLE_EXISTING_CONFLICT"

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

_FRESH_DOWNLOAD_COLUMNS = (
    "campaign_id", "manifest_row_id", "plan_classification", "fresh_status",
    "source_route", "target_zip_path", "target_provenance_path",
    "artifact_zip_sha256", "artifact_internal_document_id", "artifact_ticker",
    "artifact_period", "artifact_quarter", "identity_verdict",
    "auto_ready_allowed", "quarantine_release_required", "attempt_count",
    "last_run_id", "last_journal_path", "last_error_code", "last_error_stage",
    "last_error_message", "prior_identity_status", "prior_cache_status",
    "prior_overall_status", "prior_error_code", "prior_zip_sha256",
    "prior_internal_document_id", "migration_run_id", "migrated_at",
    "created_at", "updated_at", "completed_at",
)

_FILING_IDENTITY_COLUMNS = (
    "campaign_id", "manifest_row_id", "requested_disclosure_no", "company_code",
    "normalized_company_code", "source_url", "normalized_xbrl_url",
    "disclosure_date", "expected_period", "expected_quarter", "document_type",
)


class FreshDownloadCASFailed(RuntimeError):
    """The production fresh-download compare-and-swap contract did not match."""


class FreshDownloadQuarantineCASFailed(RuntimeError):
    """A runtime Fresh quarantine transition failed its fail-closed contract."""


class FreshStateMigrationConflict(RuntimeError):
    """An existing fresh-download schema or seed does not match the migration contract."""


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


def _create_fresh_download_schema(conn: sqlite3.Connection) -> None:
    statuses = ",".join(f"'{value}'" for value in sorted(FRESH_DOWNLOAD_STATUSES))
    classifications = ",".join(
        f"'{value}'" for value in sorted(FRESH_DOWNLOAD_PLAN_CLASSES)
    )
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS campaign_fresh_downloads (
            campaign_id TEXT NOT NULL,
            manifest_row_id TEXT NOT NULL,
            plan_classification TEXT NOT NULL CHECK(plan_classification IN ({classifications})),
            fresh_status TEXT NOT NULL CHECK(fresh_status IN ({statuses})),
            source_route TEXT,
            target_zip_path TEXT NOT NULL,
            target_provenance_path TEXT NOT NULL,
            artifact_zip_sha256 TEXT,
            artifact_internal_document_id TEXT,
            artifact_ticker TEXT,
            artifact_period TEXT,
            artifact_quarter TEXT,
            identity_verdict TEXT,
            auto_ready_allowed INTEGER NOT NULL CHECK(auto_ready_allowed IN (0,1)),
            quarantine_release_required INTEGER NOT NULL CHECK(quarantine_release_required IN (0,1)),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            last_run_id TEXT,
            last_journal_path TEXT,
            last_error_code TEXT,
            last_error_stage TEXT,
            last_error_message TEXT,
            prior_identity_status TEXT NOT NULL,
            prior_cache_status TEXT NOT NULL,
            prior_overall_status TEXT NOT NULL,
            prior_error_code TEXT,
            prior_zip_sha256 TEXT,
            prior_internal_document_id TEXT,
            migration_run_id TEXT NOT NULL,
            migrated_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            PRIMARY KEY (campaign_id, manifest_row_id),
            FOREIGN KEY (campaign_id, manifest_row_id)
                REFERENCES campaign_filings(campaign_id, manifest_row_id)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_campaign_fresh_status "
        "ON campaign_fresh_downloads(campaign_id, fresh_status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_campaign_fresh_classification "
        "ON campaign_fresh_downloads(campaign_id, plan_classification)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_campaign_fresh_status_row "
        "ON campaign_fresh_downloads(campaign_id, fresh_status, manifest_row_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_campaign_fresh_source "
        "ON campaign_fresh_downloads(campaign_id, source_route)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_campaign_fresh_run "
        "ON campaign_fresh_downloads(campaign_id, last_run_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_campaign_fresh_zip_path "
        "ON campaign_fresh_downloads(campaign_id, target_zip_path)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_campaign_fresh_provenance_path "
        "ON campaign_fresh_downloads(campaign_id, target_provenance_path)"
    )


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
        if str(schema_version) == SCHEMA_VERSION:
            _create_fresh_download_schema(conn)


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


def create_fresh_download(conn: sqlite3.Connection, values: Mapping[str, object]) -> None:
    payload = dict(values)
    status = str(payload.get("fresh_status") or "")
    classification = str(payload.get("plan_classification") or "")
    if status not in FRESH_DOWNLOAD_STATUSES:
        raise ValueError(f"invalid fresh download status: {status}")
    if classification not in FRESH_DOWNLOAD_PLAN_CLASSES:
        raise ValueError(f"invalid fresh download plan classification: {classification}")
    now = str(payload.get("created_at") or _now())
    payload.setdefault("migrated_at", now)
    payload.setdefault("created_at", now)
    payload.setdefault("updated_at", now)
    payload.setdefault("attempt_count", 0)
    columns = ", ".join(_FRESH_DOWNLOAD_COLUMNS)
    placeholders = ", ".join("?" for _ in _FRESH_DOWNLOAD_COLUMNS)
    conn.execute(
        f"INSERT INTO campaign_fresh_downloads ({columns}) VALUES ({placeholders})",
        [payload.get(column) for column in _FRESH_DOWNLOAD_COLUMNS],
    )


def _fresh_seed_payloads(
    *, campaign_id: str, plan_rows: Sequence[Mapping[str, object]],
    prior_rows: Mapping[str, Mapping[str, object]],
    complete_artifacts: Mapping[str, Mapping[str, object]],
    migration_run_id: str, migrated_at: str, journal_path: str | None,
) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in plan_rows:
        row_id = str(item.get("manifest_row_id") or "")
        classification = str(item.get("plan_classification") or "")
        if not row_id or row_id in seen or classification not in FRESH_DOWNLOAD_PLAN_CLASSES:
            raise FreshStateMigrationConflict("invalid or duplicate fresh download plan row")
        seen.add(row_id)
        prior = prior_rows.get(row_id)
        if prior is None or str(prior.get("campaign_id") or "") != campaign_id:
            raise FreshStateMigrationConflict("fresh download plan does not match campaign filings")
        artifact = complete_artifacts.get(row_id)
        if artifact is not None:
            if classification != "STANDARD_FRESH_DOWNLOAD":
                raise FreshStateMigrationConflict("quarantine row cannot be seeded complete")
            status, source_route, attempts = "COMPLETE", "JQUANTS_TD_FILES", 1
            auto_ready, release = 1, 0
        elif classification == "QUARANTINE_FRESH_RECHECK":
            status, source_route, attempts = "QUARANTINED", None, 0
            auto_ready, release = 0, 1
        else:
            status, source_route, attempts = "NOT_STARTED", None, 0
            auto_ready, release = 1, 0
        payload = {
            "campaign_id": campaign_id, "manifest_row_id": row_id,
            "plan_classification": classification, "fresh_status": status,
            "source_route": source_route,
            "target_zip_path": item.get("target_zip_path"),
            "target_provenance_path": item.get("target_provenance_path"),
            "artifact_zip_sha256": None if artifact is None else artifact.get("zip_sha256"),
            "artifact_internal_document_id": None if artifact is None else artifact.get("internal_document_id"),
            "artifact_ticker": None if artifact is None else artifact.get("zip_internal_ticker"),
            "artifact_period": None if artifact is None else artifact.get("zip_internal_period"),
            "artifact_quarter": None if artifact is None else artifact.get("zip_internal_quarter"),
            "identity_verdict": None if artifact is None else artifact.get("identity_verdict"),
            "auto_ready_allowed": auto_ready,
            "quarantine_release_required": release, "attempt_count": attempts,
            "last_run_id": None if artifact is None else artifact.get("run_id"),
            "last_journal_path": journal_path if artifact is not None else None,
            "last_error_code": None, "last_error_stage": None, "last_error_message": None,
            "prior_identity_status": prior.get("identity_status"),
            "prior_cache_status": prior.get("cache_status"),
            "prior_overall_status": prior.get("overall_status"),
            "prior_error_code": prior.get("error_code"),
            "prior_zip_sha256": prior.get("zip_sha256"),
            "prior_internal_document_id": prior.get("internal_document_id"),
            "migration_run_id": migration_run_id, "migrated_at": migrated_at,
            "created_at": migrated_at, "updated_at": migrated_at,
            "completed_at": None if artifact is None else (
                artifact.get("downloaded_at_utc") or artifact.get("downloaded_at")
            ),
        }
        required = (
            payload["target_zip_path"], payload["target_provenance_path"],
            payload["prior_identity_status"], payload["prior_cache_status"],
            payload["prior_overall_status"],
        )
        if any(value in {None, ""} for value in required):
            raise FreshStateMigrationConflict("fresh download seed is incomplete")
        if status == "COMPLETE" and any(payload[column] in {None, ""} for column in (
            "artifact_zip_sha256", "artifact_internal_document_id", "artifact_ticker",
            "artifact_period", "artifact_quarter", "identity_verdict", "last_run_id",
            "completed_at",
        )):
            raise FreshStateMigrationConflict("complete fresh download seed is incomplete")
        payloads.append(payload)
    if set(complete_artifacts) - seen:
        raise FreshStateMigrationConflict("complete artifact is outside the plan")
    return payloads


def migrate_fresh_download_state(
    conn: sqlite3.Connection, *, backup_conn: sqlite3.Connection, campaign_id: str,
    plan_rows: Sequence[Mapping[str, object]],
    complete_artifacts: Mapping[str, Mapping[str, object]], migration_run_id: str,
    migrated_at: str | None = None, journal_path: str | None = None,
) -> dict[str, object]:
    """Explicitly migrate one campaign from schema v1 to v2.

    The legacy filing snapshot comes from ``backup_conn``.  Any post-download
    changes in the current copy are restored using the backup's exact values;
    the fresh artifact state is retained only in ``campaign_fresh_downloads``.
    """
    timestamp = str(migrated_at or _now())
    current_version = get_schema_version(conn)
    backup_version = get_schema_version(backup_conn)
    if backup_version != LEGACY_SCHEMA_VERSION:
        raise FreshStateMigrationConflict("backup campaign schema is not version 1")
    backup_rows = {
        str(row["manifest_row_id"]): dict(row) for row in backup_conn.execute(
            "SELECT * FROM campaign_filings WHERE campaign_id=? ORDER BY manifest_row_id",
            (campaign_id,),
        )
    }
    current_rows = {
        str(row["manifest_row_id"]): dict(row) for row in conn.execute(
            "SELECT * FROM campaign_filings WHERE campaign_id=? ORDER BY manifest_row_id",
            (campaign_id,),
        )
    }
    if not backup_rows or set(backup_rows) != set(current_rows) or len(plan_rows) != len(backup_rows):
        raise FreshStateMigrationConflict("current, backup, and plan populations differ")
    expected = _fresh_seed_payloads(
        campaign_id=campaign_id, plan_rows=plan_rows, prior_rows=backup_rows,
        complete_artifacts=complete_artifacts, migration_run_id=migration_run_id,
        migrated_at=timestamp, journal_path=journal_path,
    )
    semantic_columns = tuple(
        column for column in _FRESH_DOWNLOAD_COLUMNS
        if column not in {"migrated_at", "created_at", "updated_at"}
    )
    if current_version == SCHEMA_VERSION:
        if not table_exists(conn, "campaign_fresh_downloads"):
            raise FreshStateMigrationConflict("version 2 is missing fresh download table")
        existing = {
            str(row["manifest_row_id"]): dict(row) for row in conn.execute(
                "SELECT * FROM campaign_fresh_downloads WHERE campaign_id=? ORDER BY manifest_row_id",
                (campaign_id,),
            )
        }
        expected_by_id = {str(row["manifest_row_id"]): row for row in expected}
        if set(existing) != set(expected_by_id) or any(
            any(existing[row_id].get(column) != expected_by_id[row_id].get(column)
                for column in semantic_columns)
            for row_id in existing
        ):
            raise FreshStateMigrationConflict(STOP_FRESH_STATE_EXISTING_CONFLICT)
        return {"status": "ALREADY_MIGRATED", "rows": len(existing), "restored_rows": 0}
    if current_version != LEGACY_SCHEMA_VERSION or table_exists(conn, "campaign_fresh_downloads"):
        raise FreshStateMigrationConflict("only a clean version 1 database can be migrated")

    for row_id, artifact in complete_artifacts.items():
        current = current_rows.get(row_id)
        if current is None or any((
            current.get("internal_document_id") != artifact.get("internal_document_id"),
            current.get("zip_sha256") != artifact.get("zip_sha256"),
            current.get("zip_internal_ticker") != artifact.get("zip_internal_ticker"),
            current.get("zip_internal_period") != artifact.get("zip_internal_period"),
            current.get("zip_internal_quarter") != artifact.get("zip_internal_quarter"),
            current.get("identity_status") != "VERIFIED",
            current.get("cache_status") != "READY",
            current.get("overall_status") != "IDENTITY_VERIFIED",
        )):
            raise FreshStateMigrationConflict("complete artifact does not match current campaign row")

    changed_ids = {
        row_id for row_id in current_rows if current_rows[row_id] != backup_rows[row_id]
    }
    if changed_ids != set(complete_artifacts):
        raise FreshStateMigrationConflict("post-download changes do not match complete artifacts")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = [str(row[1]) for row in conn.execute("PRAGMA table_info(campaign_filings)")]
        mutable = [column for column in columns if column not in {"campaign_id", "manifest_row_id"}]
        for row_id in sorted(changed_ids):
            current, prior = current_rows[row_id], backup_rows[row_id]
            changed = [column for column in mutable if current.get(column) != prior.get(column)]
            if changed:
                assignments = ",".join(f"{column}=?" for column in changed)
                conn.execute(
                    f"UPDATE campaign_filings SET {assignments} WHERE campaign_id=? AND manifest_row_id=?",
                    [prior[column] for column in changed] + [campaign_id, row_id],
                )
        restored = {
            str(row["manifest_row_id"]): dict(row) for row in conn.execute(
                "SELECT * FROM campaign_filings WHERE campaign_id=? ORDER BY manifest_row_id",
                (campaign_id,),
            )
        }
        if restored != backup_rows:
            raise FreshStateMigrationConflict("legacy campaign filings were not restored exactly")
        _create_fresh_download_schema(conn)
        for payload in expected:
            create_fresh_download(conn, payload)
        conn.execute(
            "UPDATE campaign_schema_metadata SET schema_version=?,updated_at=?",
            (SCHEMA_VERSION, timestamp),
        )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    return {"status": "MIGRATED", "rows": len(expected), "restored_rows": len(changed_ids)}


def load_fresh_download_rows(
    conn: sqlite3.Connection, campaign_id: str, row_ids: Sequence[str],
) -> list[dict[str, object]]:
    if get_schema_version(conn) != SCHEMA_VERSION or not table_exists(conn, "campaign_fresh_downloads"):
        raise FreshStateMigrationConflict("fresh download schema version 2 is required")
    if not row_ids:
        return []
    placeholders = ",".join("?" for _ in row_ids)
    rows = conn.execute(
        f"SELECT * FROM campaign_fresh_downloads WHERE campaign_id=? "
        f"AND manifest_row_id IN ({placeholders}) ORDER BY manifest_row_id",
        [campaign_id, *row_ids],
    ).fetchall()
    if len(rows) != len(set(row_ids)):
        raise FreshStateMigrationConflict("fresh download target rows are incomplete")
    return [dict(row) for row in rows]


def select_next_fresh_downloads(
    conn: sqlite3.Connection, campaign_id: str, *, limit: int = 100,
) -> list[dict[str, object]]:
    if not 1 <= limit <= 100:
        raise ValueError("fresh download selection limit must be between 1 and 100")
    return [dict(row) for row in conn.execute(
        "SELECT * FROM campaign_fresh_downloads WHERE campaign_id=? "
        "AND plan_classification='STANDARD_FRESH_DOWNLOAD' "
        "AND fresh_status='NOT_STARTED' ORDER BY manifest_row_id LIMIT ?",
        (campaign_id, limit),
    )]


def _table_digest(
    conn: sqlite3.Connection, table: str, campaign_id: str, excluded_ids: set[str],
) -> str:
    digest = hashlib.sha256()
    for row in conn.execute(
        f"SELECT * FROM {table} WHERE campaign_id=? ORDER BY manifest_row_id", (campaign_id,)
    ):
        current = dict(row)
        if str(current["manifest_row_id"]) in excluded_ids:
            continue
        digest.update((json.dumps(
            current, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) + "\n").encode("utf-8"))
    return digest.hexdigest()


def apply_fresh_download_successes(
    conn: sqlite3.Connection, *, campaign_id: str,
    before_rows: Sequence[Mapping[str, object]],
    verified_results: Sequence[Mapping[str, object]], expected_count: int,
    run_id: str, journal_path: str, updated_at: str | None = None,
    attempt_increments: Sequence[int] | None = None,
    after_update: Callable[[int, sqlite3.Connection], None] | None = None,
) -> list[dict[str, object]]:
    """CAS verified artifacts into the dedicated fresh table only."""
    if not 1 <= expected_count <= 100 or len(before_rows) != expected_count or len(verified_results) != expected_count:
        raise FreshDownloadCASFailed("fresh download update count is outside the audited range")
    increments = list(attempt_increments or [1] * expected_count)
    if len(increments) != expected_count or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in increments
    ):
        raise FreshDownloadCASFailed("fresh download attempt increments are invalid")
    before_by_id = {str(row.get("manifest_row_id") or ""): dict(row) for row in before_rows}
    result_by_id = {str(row.get("manifest_row_id") or ""): dict(row) for row in verified_results}
    if "" in before_by_id or set(before_by_id) != set(result_by_id) or len(before_by_id) != expected_count:
        raise FreshDownloadCASFailed("fresh download target identity mismatch")
    timestamp = str(updated_at or _now())
    conn.execute("BEGIN IMMEDIATE")
    readbacks: list[dict[str, object]] = []
    try:
        filing_digest = _table_digest(conn, "campaign_filings", campaign_id, set())
        non_target_before = _table_digest(
            conn, "campaign_fresh_downloads", campaign_id, set(before_by_id)
        )
        for index, row_id in enumerate(sorted(before_by_id)):
            before, verified = before_by_id[row_id], result_by_id[row_id]
            if before.get("fresh_status") not in {"NOT_STARTED", "FAILED_RETRYABLE"}:
                raise FreshDownloadCASFailed(f"fresh download start state rejected for {row_id}")
            filing = conn.execute(
                "SELECT * FROM campaign_filings WHERE campaign_id=? AND manifest_row_id=?",
                (campaign_id, row_id),
            ).fetchone()
            if filing is None or any(
                dict(filing).get(column) != before.get(column)
                for column in _FILING_IDENTITY_COLUMNS
            ):
                raise FreshDownloadCASFailed(f"campaign filing identity changed for {row_id}")
            desired = {
                "fresh_status": "COMPLETE", "source_route": "JQUANTS_TD_FILES",
                "artifact_zip_sha256": verified.get("zip_sha256"),
                "artifact_internal_document_id": verified.get("internal_document_id"),
                "artifact_ticker": verified.get("ticker"),
                "artifact_period": verified.get("period"),
                "artifact_quarter": verified.get("quarter"),
                "identity_verdict": verified.get("identity_verdict"),
                "auto_ready_allowed": 1, "quarantine_release_required": 0,
                "attempt_count": int(before.get("attempt_count") or 0) + increments[index],
                "last_run_id": run_id, "last_journal_path": journal_path,
                "last_error_code": None, "last_error_stage": None,
                "last_error_message": None, "updated_at": timestamp,
                "completed_at": timestamp,
            }
            if any(desired[column] in {None, ""} for column in (
                "artifact_zip_sha256", "artifact_ticker",
                "artifact_period", "artifact_quarter", "identity_verdict",
            )):
                raise FreshDownloadCASFailed("verified artifact metadata is incomplete")
            internal_id = desired["artifact_internal_document_id"]
            verdict = desired["identity_verdict"]
            if (
                (verdict == "official_linked_xbrl_match_without_internal_id" and internal_id is not None)
                or (verdict != "official_linked_xbrl_match_without_internal_id" and internal_id in {None, ""})
            ):
                raise FreshDownloadCASFailed("verified artifact internal identity is inconsistent")
            if (
                str(before.get("expected_period") or "") != str(desired["artifact_period"])
                or str(before.get("expected_quarter") or "") != str(desired["artifact_quarter"])
                or str(before.get("normalized_company_code") or "") != str(desired["artifact_ticker"])
            ):
                raise FreshDownloadCASFailed("verified artifact metadata does not match campaign row")
            mutable = tuple(desired)
            assignments = ",".join(f"{column}=?" for column in mutable)
            cas = " AND ".join(f"{column} IS ?" for column in _FRESH_DOWNLOAD_COLUMNS)
            values = [desired[column] for column in mutable]
            values.extend(before.get(column) for column in _FRESH_DOWNLOAD_COLUMNS)
            cursor = conn.execute(
                f"UPDATE campaign_fresh_downloads SET {assignments} WHERE {cas}", values
            )
            if cursor.rowcount != 1:
                raise FreshDownloadCASFailed(f"fresh download CAS failed for {row_id}")
            if after_update is not None:
                after_update(index, conn)
            current = conn.execute(
                "SELECT * FROM campaign_fresh_downloads WHERE campaign_id=? AND manifest_row_id=?",
                (campaign_id, row_id),
            ).fetchone()
            if current is None or any(dict(current).get(column) != value for column, value in desired.items()):
                raise FreshDownloadCASFailed(f"fresh download readback mismatch for {row_id}")
            readbacks.append(dict(current))
        if _table_digest(conn, "campaign_filings", campaign_id, set()) != filing_digest:
            raise FreshDownloadCASFailed("campaign filings changed during fresh download")
        if _table_digest(conn, "campaign_fresh_downloads", campaign_id, set(before_by_id)) != non_target_before:
            raise FreshDownloadCASFailed("non-target fresh download row changed")
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    return readbacks


def apply_fresh_download_quarantine(
    conn: sqlite3.Connection, *, campaign_id: str, manifest_row_id: str,
    requested_document_id: str, expected_status: str,
    expected_attempt_count: int, reason_code: str, failure_stage: str,
    source_route: str, http_status: int, evidence_path: str,
    evidence_sha256: str, run_id: str, updated_at: str | None = None,
    after_update: Callable[[sqlite3.Connection], None] | None = None,
) -> dict[str, object]:
    """CAS one untouched STANDARD Fresh row into evidence-backed quarantine.

    This deliberately supports only the exact J-Quants TD Files 404 contract
    and the formal ZIP-internal identity-conflict contract.
    It does not count the external diagnostic as a production download attempt.
    """
    contract = (reason_code, failure_stage, source_route, http_status)
    allowed_contracts = {
        ("TD_FILES_DISCNO_NOT_FOUND", "STAGE_A", "JQUANTS_TD_FILES", 404),
        ("ZIP_INTERNAL_IDENTITY_CONFLICT", "ZIP_IDENTITY", "JQUANTS_TD_FILES", 200),
    }
    allowed = (
        expected_status == "NOT_STARTED"
        and expected_attempt_count == 0
        and contract in allowed_contracts
        and bool(campaign_id and manifest_row_id and requested_document_id)
        and bool(evidence_path and run_id)
        and len(evidence_sha256) == 64
        and all(character in "0123456789abcdefABCDEF" for character in evidence_sha256)
    )
    if not allowed:
        raise FreshDownloadQuarantineCASFailed("runtime quarantine contract is not allowed")
    if get_schema_version(conn) != SCHEMA_VERSION or not table_exists(
        conn, "campaign_fresh_downloads"
    ):
        raise FreshDownloadQuarantineCASFailed("fresh download schema version 2 is required")

    timestamp = updated_at or _now()
    target_ids = {manifest_row_id}
    conn.execute("BEGIN IMMEDIATE")
    try:
        fresh_row = conn.execute(
            "SELECT * FROM campaign_fresh_downloads "
            "WHERE campaign_id=? AND manifest_row_id=?",
            (campaign_id, manifest_row_id),
        ).fetchone()
        filing_row = conn.execute(
            "SELECT * FROM campaign_filings "
            "WHERE campaign_id=? AND manifest_row_id=?",
            (campaign_id, manifest_row_id),
        ).fetchone()
        if fresh_row is None or filing_row is None:
            raise FreshDownloadQuarantineCASFailed("runtime quarantine target is missing")
        before = dict(fresh_row)
        filing = dict(filing_row)
        if (
            filing.get("requested_disclosure_no") != requested_document_id
            or before.get("plan_classification") != "STANDARD_FRESH_DOWNLOAD"
            or before.get("fresh_status") != expected_status
            or before.get("attempt_count") != expected_attempt_count
            or any(before.get(column) is not None for column in (
                "artifact_zip_sha256", "artifact_internal_document_id", "artifact_ticker",
                "artifact_period", "artifact_quarter", "identity_verdict",
            ))
            or before.get("completed_at") is not None
        ):
            raise FreshDownloadQuarantineCASFailed("runtime quarantine precondition changed")

        filing_digest = _table_digest(conn, "campaign_filings", campaign_id, set())
        non_target_digest = _table_digest(
            conn, "campaign_fresh_downloads", campaign_id, target_ids
        )
        schema_before = tuple(conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall())
        fresh_count = int(conn.execute(
            "SELECT COUNT(*) FROM campaign_fresh_downloads WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()[0])
        error_detail = json.dumps({
            "evidence_sha256": evidence_sha256.lower(),
            "failure_stage": failure_stage,
            "http_status": http_status,
            "reason_code": reason_code,
            "requested_document_id": requested_document_id,
            "source_route": source_route,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        desired = {
            "fresh_status": "QUARANTINED",
            "source_route": source_route,
            "auto_ready_allowed": 0,
            "quarantine_release_required": 1,
            "attempt_count": 0,
            "last_run_id": run_id,
            "last_journal_path": evidence_path,
            "last_error_code": reason_code,
            "last_error_stage": failure_stage,
            "last_error_message": error_detail,
            "updated_at": timestamp,
            "completed_at": None,
        }
        mutable = tuple(desired)
        assignments = ",".join(f"{column}=?" for column in mutable)
        cas = " AND ".join(f"{column} IS ?" for column in _FRESH_DOWNLOAD_COLUMNS)
        values = [desired[column] for column in mutable]
        values.extend(before.get(column) for column in _FRESH_DOWNLOAD_COLUMNS)
        cursor = conn.execute(
            f"UPDATE campaign_fresh_downloads SET {assignments} WHERE {cas}", values
        )
        if cursor.rowcount != 1:
            raise FreshDownloadQuarantineCASFailed("runtime quarantine CAS rowcount mismatch")
        if after_update is not None:
            after_update(conn)
        current = conn.execute(
            "SELECT * FROM campaign_fresh_downloads "
            "WHERE campaign_id=? AND manifest_row_id=?",
            (campaign_id, manifest_row_id),
        ).fetchone()
        if current is None or any(
            dict(current).get(column) != value for column, value in desired.items()
        ):
            raise FreshDownloadQuarantineCASFailed("runtime quarantine readback mismatch")
        if any(dict(current).get(column) is not None for column in (
            "artifact_zip_sha256", "artifact_internal_document_id", "artifact_ticker",
            "artifact_period", "artifact_quarter", "identity_verdict", "completed_at",
        )):
            raise FreshDownloadQuarantineCASFailed("runtime quarantine created artifact state")
        if (
            _table_digest(conn, "campaign_filings", campaign_id, set()) != filing_digest
            or _table_digest(conn, "campaign_fresh_downloads", campaign_id, target_ids)
            != non_target_digest
            or int(conn.execute(
                "SELECT COUNT(*) FROM campaign_fresh_downloads WHERE campaign_id=?",
                (campaign_id,),
            ).fetchone()[0]) != fresh_count
            or tuple(conn.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            ).fetchall()) != schema_before
            or str(conn.execute("PRAGMA integrity_check").fetchone()[0]) != "ok"
            or conn.execute("PRAGMA foreign_key_check").fetchone() is not None
        ):
            raise FreshDownloadQuarantineCASFailed(
                "runtime quarantine invariant failed"
            )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    return {"before": before, "after": dict(current), "invariants": {
        "campaign_filings_unchanged": True,
        "non_target_fresh_unchanged": True,
        "schema_unchanged": True,
        "fresh_count_unchanged": True,
        "integrity_check": "ok",
        "foreign_key_check": 0,
    }}
