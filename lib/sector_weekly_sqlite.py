"""Fail-closed SQLite schema access for the Sector Weekly work queue."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_ID = "018_sector_weekly_work_assignments"
MIGRATION_PATH = ROOT / "migrations" / "sqlite" / f"{MIGRATION_ID}.sql"
MIGRATION_HISTORY_TABLE = "sector_weekly_sqlite_migrations"
WORK_TABLE = "sector_weekly_work_assignments"
RUNNER_VERSION = "sector_weekly_sqlite_migration_v1"
MAX_ATTEMPTS = 3

EXPECTED_COLUMNS = (
    "assignment_id", "schema_version", "stable_key", "sector_code", "sector_name",
    "period_start", "period_end", "status", "attempt_count", "available_at",
    "claim_owner", "claimed_at", "lease_expires_at", "started_at", "completed_at",
    "last_error_type", "last_error_message", "submitted_payload_hash", "created_at", "updated_at",
)
EXPECTED_STATUS_VALUES = (
    "pending", "ready", "claimed", "running", "success", "retry_pending", "failed",
)


class SectorSQLiteSchemaError(RuntimeError):
    """Base class for queue schema failures."""


class MigrationRequiredError(SectorSQLiteSchemaError):
    """The dedicated SQLite migration has not been applied."""


class SchemaMismatchError(SectorSQLiteSchemaError):
    """Existing objects do not match the reviewed migration."""


def migration_sql(path: Path = MIGRATION_PATH) -> str:
    return path.read_text(encoding="utf-8")


def migration_checksum(path: Path = MIGRATION_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalized(sql: str | None) -> str:
    return re.sub(r"\s+", "", (sql or "").lower().replace('"', ""))


def _table_sql(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone()
    return str(row[0]) if row else None


def _index_details(conn: sqlite3.Connection, table: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in conn.execute(f"PRAGMA index_list({table})"):
        name = str(row[1])
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,),
        ).fetchone()
        result[name] = {
            "unique": bool(row[2]),
            "partial": bool(row[4]),
            "columns": tuple(str(info[2]) for info in conn.execute(f"PRAGMA index_info({name})")),
            "sql": str(sql_row[0]) if sql_row and sql_row[0] else "",
        }
    return result


def validate_history_schema(conn: sqlite3.Connection, *, required: bool = True) -> None:
    sql = _table_sql(conn, MIGRATION_HISTORY_TABLE)
    if sql is None:
        if required:
            raise MigrationRequiredError(f"SQLite migration {MIGRATION_ID} is required")
        return
    columns = tuple(str(row[1]) for row in conn.execute(f"PRAGMA table_info({MIGRATION_HISTORY_TABLE})"))
    if columns != ("migration_id", "checksum_sha256", "applied_at", "runner_version"):
        raise SchemaMismatchError("Sector Weekly SQLite migration history schema does not match")
    normalized = _normalized(sql)
    for fragment in (
        "migration_idtextprimarykeynotnull",
        "length(checksum_sha256)=64",
        "checksum_sha256notglob'*[^0-9a-f]*'",
    ):
        if fragment not in normalized:
            raise SchemaMismatchError(f"migration history constraint missing: {fragment}")
    history_info = list(conn.execute(f"PRAGMA table_info({MIGRATION_HISTORY_TABLE})"))
    if [int(row[5]) for row in history_info] != [1, 0, 0, 0]:
        raise SchemaMismatchError("migration history primary key does not match")


def validate_work_schema(conn: sqlite3.Connection, *, require_history: bool = True) -> None:
    validate_history_schema(conn, required=require_history)
    sql = _table_sql(conn, WORK_TABLE)
    if sql is None:
        raise MigrationRequiredError(f"SQLite migration {MIGRATION_ID} is required")
    info = list(conn.execute(f"PRAGMA table_info({WORK_TABLE})"))
    columns = tuple(str(row[1]) for row in info)
    if columns != EXPECTED_COLUMNS:
        raise SchemaMismatchError(f"queue columns do not match: {columns!r}")
    types = {str(row[1]): str(row[2]).upper() for row in info}
    if any(types[name] != ("INTEGER" if name in {"sector_code", "attempt_count"} else "TEXT") for name in columns):
        raise SchemaMismatchError("queue column types do not match")
    not_null = {str(row[1]): bool(row[3]) for row in info}
    required = {
        "assignment_id", "schema_version", "stable_key", "sector_code", "sector_name",
        "period_start", "period_end", "status", "attempt_count", "available_at", "created_at", "updated_at",
    }
    if any(not_null[name] != (name in required) for name in columns):
        raise SchemaMismatchError("queue NULLability does not match")
    primary_keys = {str(row[1]): int(row[5]) for row in info}
    if primary_keys["assignment_id"] != 1 or any(
        value for name, value in primary_keys.items() if name != "assignment_id"
    ):
        raise SchemaMismatchError("queue primary key does not match")
    defaults = {str(row[1]): row[4] for row in info}
    if str(defaults["attempt_count"]) != "0":
        raise SchemaMismatchError("attempt_count default does not match")
    normalized = _normalized(sql)
    status_list = ",".join(f"'{value}'" for value in EXPECTED_STATUS_VALUES)
    for fragment in (
        "schema_version='sector_weekly_assignment_v1'",
        "typeof(sector_code)='integer'",
        "sector_codebetween1and33",
        "length(trim(sector_name))>0",
        "length(period_start)=20",
        "length(period_end)=20",
        "period_end>period_start",
        f"statusin({status_list})",
        "typeof(attempt_count)='integer'",
        f"attempt_countbetween0and{MAX_ATTEMPTS}",
        "length(available_at)=20",
        "claim_ownerisnullorlength(trim(claim_owner))>0",
        "length(submitted_payload_hash)=64",
        "submitted_payload_hashnotglob'*[^0-9a-f]*'",
        "length(created_at)=20",
        "length(updated_at)=20",
        "statusnotin('claimed','running')or(claim_ownerisnotnullandclaimed_atisnotnullandlease_expires_atisnotnull)",
    ):
        if fragment not in normalized:
            raise SchemaMismatchError(f"queue constraint missing: {fragment}")
    indexes = _index_details(conn, WORK_TABLE)
    ready = indexes.get("ix_sector_weekly_work_ready")
    if not ready or ready["columns"] != ("status", "available_at", "sector_code") or ready["partial"]:
        raise SchemaMismatchError("ready assignment index does not match")
    lease = indexes.get("ix_sector_weekly_work_lease")
    if not lease or lease["columns"] != ("lease_expires_at",) or not lease["partial"]:
        raise SchemaMismatchError("active lease partial index does not match")
    if "wherestatusin('claimed','running')" not in _normalized(lease["sql"]):
        raise SchemaMismatchError("active lease partial index predicate does not match")
    unique_stable_key = any(
        details["unique"] and details["columns"] == ("stable_key",)
        for details in indexes.values()
    )
    if not unique_stable_key:
        raise SchemaMismatchError("stable_key UNIQUE index is missing")
    if require_history:
        row = conn.execute(
            f"SELECT checksum_sha256 FROM {MIGRATION_HISTORY_TABLE} WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if row is None:
            raise MigrationRequiredError(f"SQLite migration history has no {MIGRATION_ID} record")
        if str(row[0]) != migration_checksum():
            raise SchemaMismatchError("SQLite migration checksum does not match reviewed SQL")


def connect_sector_db(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise MigrationRequiredError(f"Sector Weekly SQLite DB does not exist or is not migrated: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        validate_work_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


def inspect_sector_schema(db_path: str | Path) -> dict[str, Any]:
    path = Path(db_path).expanduser().resolve()
    result: dict[str, Any] = {"db_path": str(path), "migration_id": MIGRATION_ID}
    if not path.is_file():
        return {**result, "status": "missing_db"}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        validate_work_schema(conn)
    except MigrationRequiredError as exc:
        return {**result, "status": "migration_required", "error": str(exc)}
    except SchemaMismatchError as exc:
        return {**result, "status": "schema_mismatch", "error": str(exc)}
    finally:
        conn.close()
    return {**result, "status": "applied", "checksum_sha256": migration_checksum()}
