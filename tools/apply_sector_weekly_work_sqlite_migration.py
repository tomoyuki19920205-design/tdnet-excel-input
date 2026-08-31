#!/usr/bin/env python3
"""Inspect, dry-run, or explicitly apply Sector Weekly SQLite migration 018."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.sector_weekly_sqlite import (
    MIGRATION_HISTORY_TABLE,
    MIGRATION_ID,
    MIGRATION_PATH,
    RUNNER_VERSION,
    WORK_TABLE,
    MigrationRequiredError,
    SchemaMismatchError,
    inspect_sector_schema,
    migration_checksum,
    migration_sql,
    validate_history_schema,
    validate_work_schema,
)


PRODUCTION_DB = (ROOT / "decision_db.db").resolve()
PROTECTED_TABLES = (
    "canonical_sector_reports",
    "canonical_sector_report_runs",
    "canonical_news_events",
    "canonical_news_scan_runs",
)


class SectorSQLiteMigrationError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _split_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise SectorSQLiteMigrationError("migration SQL ends with an incomplete statement")
    return statements


def _open(path: Path, mode: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _checks(conn: sqlite3.Connection) -> dict[str, Any]:
    integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
    foreign_keys = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
    if integrity != ["ok"]:
        raise SectorSQLiteMigrationError(f"integrity_check failed: {integrity}")
    if foreign_keys:
        raise SectorSQLiteMigrationError(f"foreign_key_check failed: {foreign_keys}")
    return {"integrity_check": integrity[0], "foreign_key_check": foreign_keys}


def _protected_snapshot(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for table in PROTECTED_TABLES:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
        ).fetchone()
        snapshot[table] = {
            "exists": row is not None,
            "sql": str(row[0]) if row else None,
            "count": int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]) if row else None,
        }
    return snapshot


def _validate_preexisting_objects(conn: sqlite3.Connection, checksum: str) -> str:
    history_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (MIGRATION_HISTORY_TABLE,),
    ).fetchone()
    work_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (WORK_TABLE,),
    ).fetchone()
    if history_sql:
        validate_history_schema(conn)
        row = conn.execute(
            f"SELECT checksum_sha256 FROM {MIGRATION_HISTORY_TABLE} WHERE migration_id=?", (MIGRATION_ID,),
        ).fetchone()
        if row:
            if str(row[0]) != checksum:
                raise SchemaMismatchError("recorded migration checksum differs from reviewed migration")
            validate_work_schema(conn)
            return "already_applied"
    if work_sql:
        raise SchemaMismatchError("queue table exists without a matching migration history record")
    return "pending"


def create_sqlite_backup(source_path: Path, backup_dir: Path) -> dict[str, Any]:
    source_path = source_path.resolve()
    backup_dir = backup_dir.resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"{source_path.stem}.before_{MIGRATION_ID}.{stamp}.db"
    if backup_path.exists():
        raise SectorSQLiteMigrationError(f"refusing to overwrite backup: {backup_path}")
    source = _open(source_path, "ro")
    destination = sqlite3.connect(str(backup_path))
    try:
        source.backup(destination)
        destination.commit()
        checks = _checks(destination)
    except Exception:
        destination.close()
        source.close()
        backup_path.unlink(missing_ok=True)
        raise
    destination.close()
    source.close()
    if not backup_path.is_file() or backup_path.stat().st_size <= 0:
        raise SectorSQLiteMigrationError("SQLite backup was not created correctly")
    restore_probe = _open(backup_path, "ro")
    try:
        restore_tables = int(restore_probe.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'",
        ).fetchone()[0])
    finally:
        restore_probe.close()
    return {
        "path": str(backup_path), "size": backup_path.stat().st_size,
        "restore_probe_tables": restore_tables, **checks,
    }


def apply_sqlite_migration(
    db_path: Path,
    *,
    expected_db_path: Path,
    backup_dir: Path,
    allow_create_empty: bool = False,
    sql_path: Path = MIGRATION_PATH,
    after_statement: Callable[[int, sqlite3.Connection], None] | None = None,
) -> dict[str, Any]:
    db_path = db_path.expanduser().resolve()
    expected_db_path = expected_db_path.expanduser().resolve()
    if db_path != expected_db_path:
        raise SectorSQLiteMigrationError(
            f"unsafe DB path: expected {expected_db_path}, received {db_path}"
        )
    if not db_path.exists():
        if not allow_create_empty:
            raise SectorSQLiteMigrationError(f"target DB does not exist: {db_path}")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(str(db_path)).close()
    if not db_path.is_file() or db_path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        raise SectorSQLiteMigrationError(f"target is not an allowed SQLite file: {db_path}")
    checksum = migration_checksum(sql_path)
    conn = _open(db_path, "rw")
    try:
        state = _validate_preexisting_objects(conn, checksum)
        checks_before = _checks(conn)
        protected_before = _protected_snapshot(conn)
    finally:
        conn.close()
    if state == "already_applied":
        return {
            "status": "already_applied", "migration_id": MIGRATION_ID,
            "db_path": str(db_path), "checksum_sha256": checksum,
            "backup": None, **checks_before,
        }
    backup = create_sqlite_backup(db_path, backup_dir)
    statements = _split_statements(migration_sql(sql_path))
    conn = _open(db_path, "rw")
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        for number, statement in enumerate(statements, start=1):
            conn.execute(statement)
            if after_statement:
                after_statement(number, conn)
        conn.execute(
            f"INSERT INTO {MIGRATION_HISTORY_TABLE} "
            "(migration_id,checksum_sha256,applied_at,runner_version) VALUES(?,?,?,?)",
            (MIGRATION_ID, checksum, _utc_now(), RUNNER_VERSION),
        )
        validate_work_schema(conn)
        if _protected_snapshot(conn) != protected_before:
            raise SectorSQLiteMigrationError("protected canonical/company-news tables changed")
        checks_after = _checks(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    verify = _open(db_path, "ro")
    try:
        validate_work_schema(verify)
        if _protected_snapshot(verify) != protected_before:
            raise SectorSQLiteMigrationError("protected tables changed after commit")
        count = int(verify.execute(f"SELECT count(*) FROM {WORK_TABLE}").fetchone()[0])
        final_checks = _checks(verify)
    finally:
        verify.close()
    return {
        "status": "applied", "migration_id": MIGRATION_ID, "db_path": str(db_path),
        "checksum_sha256": checksum, "assignment_count": count,
        "backup": backup, "checks_during_transaction": checks_after, **final_checks,
    }


def dry_run(db_path: Path) -> dict[str, Any]:
    db_path = db_path.expanduser().resolve()
    if not db_path.is_file():
        raise SectorSQLiteMigrationError(f"target DB does not exist: {db_path}")
    with tempfile.TemporaryDirectory(prefix="sector-weekly-sqlite-018-") as temp_text:
        temp = Path(temp_text)
        copy_path = temp / "dry_run.db"
        source = _open(db_path, "ro")
        copy = sqlite3.connect(str(copy_path))
        try:
            source.backup(copy)
        finally:
            copy.close()
            source.close()
        result = apply_sqlite_migration(
            copy_path, expected_db_path=copy_path, backup_dir=temp / "backups",
        )
        result["status"] = "dry_run_ok"
        result["source_db_path"] = str(db_path)
        result["db_path"] = str(copy_path)
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True, help="explicit SQLite target path")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--inspect", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--backup-dir", type=Path, default=ROOT / "backup" / "sector_weekly_sqlite")
    args = parser.parse_args()
    try:
        if args.apply:
            if args.confirm != MIGRATION_ID:
                raise SectorSQLiteMigrationError(f"--confirm {MIGRATION_ID} is required")
            result = apply_sqlite_migration(
                args.db, expected_db_path=PRODUCTION_DB, backup_dir=args.backup_dir,
            )
        elif args.dry_run:
            result = dry_run(args.db)
        else:
            result = inspect_sector_schema(args.db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, sqlite3.Error, SectorSQLiteMigrationError, MigrationRequiredError, SchemaMismatchError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
