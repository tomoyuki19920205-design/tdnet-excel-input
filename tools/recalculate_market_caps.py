#!/usr/bin/env python3
"""Audit or apply point-in-time market-cap recalculation in SQLite."""
import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.fetch_jquants_prices import recalculate_market_caps


DEFAULT_DB = ROOT / "data" / "jquants.db"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        stats = recalculate_market_caps(conn, apply=args.apply)
    finally:
        conn.close()

    result = {"mode": "apply" if args.apply else "dry-run", **asdict(stats)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if stats.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
