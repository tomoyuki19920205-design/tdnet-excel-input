"""In-memory SQLite rehearsal of the PostgreSQL date-precision migration."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


EXACT = "2026-08-29T09:00:00+09:00"


def run_dry_run() -> dict[str, object]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE legacy_events(event_id TEXT PRIMARY KEY, published_at TEXT NOT NULL)")
    connection.execute("INSERT INTO legacy_events VALUES('legacy-exact', ?)", (EXACT,))
    connection.execute("""
        CREATE TABLE canonical_news_events(
          event_id TEXT PRIMARY KEY,
          published_at TEXT,
          published_date TEXT,
          published_precision TEXT NOT NULL DEFAULT 'datetime'
            CHECK (published_precision IN ('datetime','date','unknown')),
          CHECK (
            (published_precision='datetime' AND published_at IS NOT NULL)
            OR (published_precision='date' AND published_at IS NULL AND published_date IS NOT NULL)
            OR (published_precision='unknown' AND published_at IS NULL AND published_date IS NULL)
          )
        )
    """)
    connection.execute("""
        INSERT INTO canonical_news_events(event_id, published_at, published_date, published_precision)
        SELECT event_id, published_at, NULL, 'datetime' FROM legacy_events
    """)
    connection.execute("INSERT INTO canonical_news_events VALUES('date-only', NULL, '2026-08-31', 'date')")
    connection.execute("INSERT INTO canonical_news_events VALUES('unknown', NULL, NULL, 'unknown')")
    invalid_rejected = 0
    for values in (
        ("bad-date", EXACT, "2026-08-31", "date"),
        ("bad-datetime", None, None, "datetime"),
        ("bad-unknown", None, "2026-08-31", "unknown"),
    ):
        try:
            connection.execute("INSERT INTO canonical_news_events VALUES(?,?,?,?)", values)
        except sqlite3.IntegrityError:
            invalid_rejected += 1
    legacy = dict(connection.execute(
        "SELECT event_id, published_at FROM canonical_news_events WHERE event_id='legacy-exact'").fetchone())
    rows = [dict(row) for row in connection.execute(
        "SELECT event_id, published_at, published_date, published_precision FROM canonical_news_events ORDER BY event_id")]
    result = {
        "schema_version": "company_news_date_precision_migration_dry_run_v1",
        "database": "sqlite_in_memory_fixture",
        "production_connection": False,
        "legacy_exact_value_unchanged": legacy["published_at"] == EXACT,
        "legacy_reader_compatible": set(legacy) == {"event_id", "published_at"},
        "new_reader_compatible": all(set(row) == {"event_id", "published_at", "published_date", "published_precision"}
                                     for row in rows),
        "date_only_insert": any(row["published_precision"] == "date" and row["published_at"] is None
                                and row["published_date"] == "2026-08-31" for row in rows),
        "unknown_insert": any(row["published_precision"] == "unknown" and row["published_at"] is None
                              and row["published_date"] is None for row in rows),
        "invalid_cross_field_rows_rejected": invalid_rejected,
        "published_date_backfilled_for_legacy": False,
        "rows": rows,
    }
    result["status"] = "PASS" if all((
        result["legacy_exact_value_unchanged"], result["legacy_reader_compatible"],
        result["new_reader_compatible"], result["date_only_insert"], result["unknown_insert"],
        invalid_rejected == 3,
    )) else "FAIL"
    connection.close()
    return result


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_dry_run()
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
