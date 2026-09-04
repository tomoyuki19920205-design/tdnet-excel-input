#!/usr/bin/env python3
"""Sync canonical news tables from SQLite to Supabase via existing REST helper."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib.runtime_paths import runtime_path
from lib.news_monitor import connect_news_db, rows_for_sync
from lib.pipeline.db import get_supabase_write_config, load_env, supabase_upsert


def sync(db_path: Path, dry_run: bool = False) -> dict[str, int]:
    conn = connect_news_db(db_path)
    try:
        batches = {table: rows_for_sync(conn, table) for table in ("canonical_news_events", "canonical_news_scan_runs")}
    finally:
        conn.close()
    if dry_run:
        return {table: len(rows) for table, rows in batches.items()}
    load_env()
    config = get_supabase_write_config()
    if not config:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY is not configured")
    result: dict[str, int] = {}
    for table, rows in batches.items():
        if not rows:
            result[table] = 0
            continue
        response = supabase_upsert(table, rows, on_conflict="dedupe_key" if table.endswith("events") else "scan_run_id", config=config)
        if not response.get("ok"):
            raise RuntimeError(f"{table} sync failed: {response.get('error', 'unknown')}")
        result[table] = int(response.get("count", 0))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=runtime_path(ROOT / "decision_db.db"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(sync(args.db, args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
