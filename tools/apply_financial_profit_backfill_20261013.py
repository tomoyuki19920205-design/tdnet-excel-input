"""Apply the reviewed, exact financial-profit migration in one transaction.

The default is a rollback rehearsal. The script never prints credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

import psycopg2


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "021_financial_profit_backfill_20261013.sql"
)
APPROVED_SHA256 = "d05f94c05fd3ce60d9c8e61b35c031c36adaa629dfd3e2621a7ae596c6cb7756"
DEFAULT_SETTINGS = Path.home() / "tdnet-production" / "settings" / ".env.local"


def _postgres_url() -> str:
    value = os.environ.get("SUPABASE_POSTGRES_URL")
    if value:
        return value
    for line in DEFAULT_SETTINGS.read_text(encoding="utf-8").splitlines():
        if line.startswith("SUPABASE_POSTGRES_URL="):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("SUPABASE_POSTGRES_URL is unavailable")


def _migration_body() -> str:
    payload = MIGRATION.read_bytes()
    # Git for Windows may write CRLF even though the reviewed SQL uses LF.
    normalized = payload.replace(b"\r\n", b"\n")
    if hashlib.sha256(normalized).hexdigest() != APPROVED_SHA256:
        raise RuntimeError("Reviewed migration checksum changed")
    sql = payload.decode("utf-8-sig").rstrip()
    if not sql.endswith("COMMIT;"):
        raise RuntimeError("Expected a final COMMIT statement")
    return sql[: -len("COMMIT;")]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit the reviewed migration")
    args = parser.parse_args()

    conn = psycopg2.connect(_postgres_url(), connect_timeout=15)
    try:
        with conn.cursor() as cursor:
            cursor.execute(_migration_body())
        if args.apply:
            conn.commit()
            print("APPLIED: 16 ordinary-profit rows, 9 false canonical OP rows, 7 legacy OP fields")
        else:
            conn.rollback()
            print("DRY RUN PASSED: all exact before-image and count guards; transaction rolled back")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
