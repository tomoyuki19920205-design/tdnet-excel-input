#!/usr/bin/env python3
"""CLI entry point for the nightly company IR monitor."""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.company_ir_monitor import import_sources_csv, init_db, notifications_enabled, run_monitor


def main() -> int:
    parser = argparse.ArgumentParser(description="Nightly company IR material/video monitor")
    parser.add_argument("--db", default="data/company_ir_monitor.db")
    parser.add_argument("--sources", default="config/company_ir_sources.csv")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--baseline-only", action="store_true",
        help="Persist every newly seen asset as baseline and never publish events",
    )
    parser.add_argument(
        "--require-discovery-complete", action="store_true",
        help="Skip until no TSE company remains pending/official-only",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    db_path = ROOT / args.db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        imported = import_sources_csv(conn, ROOT / args.sources)
        if args.require_discovery_complete:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_ir_companies'"
            ).fetchone()
            unfinished = -1 if not table else conn.execute(
                "SELECT COUNT(*) FROM company_ir_companies WHERE discovery_status IN ('pending','official_only')"
            ).fetchone()[0]
            if unfinished != 0:
                print("COMPANY_IR_NIGHTLY " + json.dumps({
                    "status": "skipped_discovery_incomplete",
                    "unfinished_companies": unfinished,
                    "sources_imported": imported,
                    "notifications_enabled": False,
                    "notified": 0,
                }, ensure_ascii=False, sort_keys=True))
                return 0
        gate = notifications_enabled(conn)
        stats = run_monitor(
            conn, dry_run=args.dry_run, baseline_only=args.baseline_only,
            allow_notifications=gate,
        )
        result = {
            "sources_imported": imported,
            "baseline_only": args.baseline_only or not gate,
            "notifications_enabled": gate,
            **stats.__dict__,
        }
        print("COMPANY_IR_NIGHTLY " + json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1 if stats.publish_failed else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
