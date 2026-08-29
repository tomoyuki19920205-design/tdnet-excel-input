#!/usr/bin/env python3
"""Apply and verify the additive Company News Supabase migration."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "016_company_news_monitor.sql"


def load_env() -> None:
    for path in (ROOT / ".env.local", ROOT / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def relation_count(cursor, relation: str) -> int:
    cursor.execute(f"SELECT count(*) FROM public.{relation}")
    return int(cursor.fetchone()[0])


def main() -> int:
    load_env()
    url = os.environ.get("SUPABASE_POSTGRES_URL")
    if not url:
        raise RuntimeError("SUPABASE_POSTGRES_URL is required")
    with psycopg2.connect(url, connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout='30000ms'")
            before = {name: relation_count(cursor, name) for name in ("api_latest_financials", "api_latest_segments")}
            cursor.execute(MIGRATION.read_text(encoding="utf-8"))
            after = {name: relation_count(cursor, name) for name in before}
            if before != after:
                raise RuntimeError(f"existing read model counts changed: before={before}, after={after}")
            cursor.execute("SELECT to_regclass('public.canonical_news_events'), to_regclass('public.canonical_news_scan_runs'), to_regclass('public.api_latest_news_events'), to_regclass('public.api_latest_news_scan_runs')")
            relations = cursor.fetchone()
            if not all(relations):
                raise RuntimeError(f"news migration verification failed: {relations}")
    print(f"migration applied; existing views unchanged: {after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
