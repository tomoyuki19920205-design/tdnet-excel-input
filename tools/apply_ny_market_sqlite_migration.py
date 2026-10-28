#!/usr/bin/env python3
"""Safely inspect, dry-run, or apply SQLite migration 019 for NY reports."""
from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_ID = "019_ny_market_daily"
MIGRATION = ROOT / "migrations" / "sqlite" / f"{MIGRATION_ID}.sql"
PRODUCTION_DB = (ROOT / "decision_db.db").resolve()
TABLES = ("canonical_ny_market_reports", "canonical_ny_market_report_runs")
PROTECTED_TABLES = (
    "canonical_news_events", "canonical_news_scan_runs",
    "canonical_sector_reports", "canonical_sector_report_runs",
)


class NYMarketSQLiteMigrationError(RuntimeError):
    pass


def _open(path: Path, mode: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for table in PROTECTED_TABLES:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        result[table] = {"sql": row[0] if row else None, "count": conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] if row else None}
    return result


def inspect(db_path: Path) -> dict[str, Any]:
    if not db_path.is_file():
        return {"status": "missing_db", "db_path": str(db_path)}
    conn = _open(db_path, "ro")
    try:
        existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        present = sorted(set(TABLES).intersection(existing))
        status = "applied" if len(present) == len(TABLES) else "partial" if present else "pending"
        return {"status": status, "db_path": str(db_path), "tables": present}
    finally:
        conn.close()


def _backup(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"{db_path.stem}.before_{MIGRATION_ID}.{stamp}.db"
    source = _open(db_path, "ro")
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    probe = _open(target, "ro")
    try:
        if probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise NYMarketSQLiteMigrationError("backup integrity_check failed")
    finally:
        probe.close()
    return target


def _statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise NYMarketSQLiteMigrationError("incomplete migration SQL")
    return statements


def apply(db_path: Path, *, expected: Path, backup_dir: Path) -> dict[str, Any]:
    db_path = db_path.resolve()
    if db_path != expected.resolve():
        raise NYMarketSQLiteMigrationError(f"unsafe DB path: expected {expected.resolve()}, received {db_path}")
    if not db_path.is_file():
        raise NYMarketSQLiteMigrationError(f"database does not exist: {db_path}")
    state = inspect(db_path)
    if state["status"] == "applied":
        return state
    if state["status"] == "partial":
        raise NYMarketSQLiteMigrationError("partial NY market schema exists")
    conn = _open(db_path, "ro")
    try:
        before = _snapshot(conn)
    finally:
        conn.close()
    backup = _backup(db_path, backup_dir)
    conn = _open(db_path, "rw")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _statements(MIGRATION.read_text(encoding="utf-8")):
            conn.execute(statement)
        if _snapshot(conn) != before:
            raise NYMarketSQLiteMigrationError("protected table changed")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    result = inspect(db_path)
    if result["status"] != "applied":
        raise NYMarketSQLiteMigrationError("post-apply verification failed")
    return {**result, "backup": str(backup)}


def dry_run(db_path: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ny-market-sqlite-") as temp_text:
        copy = Path(temp_text) / "dry-run.db"
        source = _open(db_path.resolve(), "ro")
        target = sqlite3.connect(str(copy))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        result = apply(copy, expected=copy, backup_dir=Path(temp_text) / "backup")
        return {**result, "status": "dry_run_ok", "source_db_path": str(db_path.resolve())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--inspect", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--backup-dir", type=Path, default=ROOT / "backup" / "ny_market_sqlite")
    args = parser.parse_args()
    try:
        if args.apply:
            if args.confirm != MIGRATION_ID:
                raise NYMarketSQLiteMigrationError(f"--confirm {MIGRATION_ID} is required")
            result = apply(args.db, expected=PRODUCTION_DB, backup_dir=args.backup_dir)
        elif args.dry_run:
            result = dry_run(args.db)
        else:
            result = inspect(args.db.resolve())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, sqlite3.Error, NYMarketSQLiteMigrationError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
