#!/usr/bin/env python3
"""Sync daily NY market canonical tables from SQLite to Supabase."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.ny_market import connect_db, rows_for_sync
from lib.pipeline.db import get_supabase_write_config, load_env, supabase_upsert


def sync(db_path: Path, dry_run: bool = False) -> dict[str, int]:
    conn = connect_db(db_path)
    try:
        tables = ("canonical_ny_market_reports", "canonical_ny_market_report_runs")
        batches = {table: rows_for_sync(conn, table) for table in tables}
    finally:
        conn.close()
    if dry_run:
        return {table: len(rows) for table, rows in batches.items()}
    load_env()
    config = get_supabase_write_config()
    if not config:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY is not configured")
    conflicts = {"canonical_ny_market_reports": "stable_key", "canonical_ny_market_report_runs": "run_id"}
    result: dict[str, int] = {}
    for table, rows in batches.items():
        if not rows:
            result[table] = 0
            continue
        response = supabase_upsert(table, rows, on_conflict=conflicts[table], config=config)
        if not response.get("ok"):
            raise RuntimeError(f"{table} sync failed: {response.get('error', 'unknown')}")
        result[table] = int(response.get("count", 0))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "decision_db.db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(sync(args.db, args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
