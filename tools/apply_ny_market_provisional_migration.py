#!/usr/bin/env python3
"""Apply the NY-only provisional schema migration with row-count verification."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "020_ny_market_provisional.sql"


def _load_env(root: Path) -> None:
    for path in (root / ".env.local", root / ".env"):
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line and not line.lstrip().startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-root", type=Path, required=True)
    args = parser.parse_args()
    _load_env(args.production_root)
    url = os.environ.get("SUPABASE_POSTGRES_URL")
    if not url:
        raise RuntimeError("SUPABASE_POSTGRES_URL is required")
    with psycopg2.connect(url, connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout='30000ms'")
            cursor.execute("SELECT (SELECT count(*) FROM canonical_news_events), (SELECT count(*) FROM canonical_sector_reports), (SELECT count(*) FROM canonical_ny_market_reports)")
            before = cursor.fetchone()
            cursor.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='canonical_ny_market_reports' AND column_name='quality_status')")
            already = bool(cursor.fetchone()[0])
            if not already:
                cursor.execute(MIGRATION.read_text(encoding="utf-8"))
            cursor.execute("SELECT (SELECT count(*) FROM canonical_news_events), (SELECT count(*) FROM canonical_sector_reports), (SELECT count(*) FROM canonical_ny_market_reports)")
            after = cursor.fetchone()
            if before != after:
                raise RuntimeError("migration changed existing report row counts")
            cursor.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='api_latest_news_stream' AND column_name='quality_status')")
            if not cursor.fetchone()[0]:
                raise RuntimeError("Viewer API quality_status column is missing")
            cursor.execute("SELECT count(*) FROM canonical_ny_market_reports WHERE quality_status <> 'complete' OR jsonb_array_length(unresolved_items) <> 0")
            if cursor.fetchone()[0] != 0:
                raise RuntimeError("migration changed pre-existing NY quality states")
    print({"status": "already_applied" if already else "applied", "company_rows": after[0],
           "sector_rows": after[1], "ny_rows": after[2]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
