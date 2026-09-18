#!/usr/bin/env python3
"""Apply the additive IPO analysis report migration and verify isolation."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "020_ipo_analysis_reports.sql"


def load_env() -> None:
    for base in (ROOT, Path(os.environ.get("TDNET_SETTINGS_ROOT", ROOT))):
        for name in (".env.local", ".env"):
            path = base / name
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
            cursor.execute("SELECT count(*) FROM tdnet_events")
            events_before = int(cursor.fetchone()[0])
            cursor.execute("SELECT count(*) FROM canonical_financials")
            financials_before = int(cursor.fetchone()[0])
            cursor.execute(MIGRATION.read_text(encoding="utf-8"))
            cursor.execute("SELECT count(*) FROM tdnet_events")
            events_after = int(cursor.fetchone()[0])
            cursor.execute("SELECT count(*) FROM canonical_financials")
            financials_after = int(cursor.fetchone()[0])
            cursor.execute("SELECT to_regclass('public.ipo_analysis_reports')")
            if cursor.fetchone()[0] is None:
                raise RuntimeError("IPO analysis table was not created")
            if (events_before, financials_before) != (events_after, financials_after):
                raise RuntimeError("migration changed existing event or PL row counts")
    print(f"IPO analysis migration applied; events={events_after} financials={financials_after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
