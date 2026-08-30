#!/usr/bin/env python3
"""Apply and verify the additive sector-weekly Supabase migration."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "017_sector_weekly_reports.sql"


def load_env() -> None:
    for path in (ROOT / ".env.local", ROOT / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    load_env()
    url = os.environ.get("SUPABASE_POSTGRES_URL")
    if not url:
        raise RuntimeError("SUPABASE_POSTGRES_URL is required")
    with psycopg2.connect(url, connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout='30000ms'")
            cursor.execute("SELECT count(*) FROM api_latest_news_events")
            company_before = int(cursor.fetchone()[0])
            cursor.execute(MIGRATION.read_text(encoding="utf-8"))
            cursor.execute("SELECT count(*) FROM api_latest_news_events")
            company_after = int(cursor.fetchone()[0])
            if company_before != company_after:
                raise RuntimeError("existing company news read model changed")
            cursor.execute(
                "SELECT to_regclass('public.canonical_sector_reports'), "
                "to_regclass('public.canonical_sector_report_runs'), to_regclass('public.api_latest_news_stream')"
            )
            if not all(cursor.fetchone()):
                raise RuntimeError("sector weekly migration verification failed")
    print(f"sector migration applied; company news rows unchanged: {company_after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
