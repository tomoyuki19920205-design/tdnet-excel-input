"""Offline SQLite model of the two-phase effective-date migration."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


STRICT_CHECKS = """
CHECK ((valid_from_precision='datetime' AND valid_from IS NOT NULL)
    OR (valid_from_precision='date' AND valid_from IS NULL AND valid_from_date IS NOT NULL)
    OR (valid_from_precision='unknown' AND valid_from IS NULL AND valid_from_date IS NULL)),
CHECK ((valid_until_precision='datetime' AND valid_until IS NOT NULL)
    OR (valid_until_precision='date' AND valid_until IS NULL AND valid_until_date IS NOT NULL)
    OR (valid_until_precision='unknown' AND valid_until IS NULL AND valid_until_date IS NULL))
"""


def run_dry_run() -> dict[str, Any]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE events(id TEXT PRIMARY KEY, valid_from TEXT, valid_until TEXT)")
    exact = "2026-08-31T14:32:00+09:00"
    conn.executemany("INSERT INTO events VALUES(?,?,?)", [
        ("legacy-exact", exact, None), ("legacy-null", None, None)])

    for name in ("valid_from_date", "valid_from_precision",
                 "valid_until_date", "valid_until_precision"):
        conn.execute(f"ALTER TABLE events ADD COLUMN {name} TEXT")
    conn.execute("UPDATE events SET valid_from_precision=CASE WHEN valid_from IS NULL THEN 'unknown' ELSE 'datetime' END")
    conn.execute("UPDATE events SET valid_until_precision=CASE WHEN valid_until IS NULL THEN 'unknown' ELSE 'datetime' END")

    # Phase B keeps an old writer operational: omitted precision remains NULL.
    conn.execute("INSERT INTO events(id,valid_from,valid_until) VALUES('legacy-writer',NULL,NULL)")
    conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)", (
        "date-only", None, None, "2026-10-01", "date", None, "unknown"))
    conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)", (
        "unknown", None, None, None, "unknown", None, "unknown"))

    # Phase C has upgraded all writers; deterministically classify their nulls.
    conn.execute("UPDATE events SET valid_from_precision='unknown' WHERE valid_from_precision IS NULL AND valid_from IS NULL")
    conn.execute("UPDATE events SET valid_until_precision='unknown' WHERE valid_until_precision IS NULL AND valid_until IS NULL")
    conn.execute(f"""CREATE TABLE strict_events(
        id TEXT PRIMARY KEY, valid_from TEXT, valid_until TEXT,
        valid_from_date TEXT, valid_from_precision TEXT NOT NULL,
        valid_until_date TEXT, valid_until_precision TEXT NOT NULL,
        {STRICT_CHECKS})""")
    conn.execute("INSERT INTO strict_events SELECT * FROM events")

    invalid = [
        ("bad-from-date", "2026-10-01T00:00:00+09:00", None, "2026-10-01", "date", None, "unknown"),
        ("bad-from-datetime", None, None, None, "datetime", None, "unknown"),
        ("bad-until-unknown", None, "2026-12-01T00:00:00+09:00", None, "unknown", None, "unknown"),
    ]
    rejected = 0
    for row in invalid:
        try:
            conn.execute("INSERT INTO strict_events VALUES(?,?,?,?,?,?,?)", row)
        except sqlite3.IntegrityError:
            rejected += 1
    legacy = dict(conn.execute("SELECT * FROM strict_events WHERE id='legacy-exact'").fetchone())
    date_only = dict(conn.execute("SELECT * FROM strict_events WHERE id='date-only'").fetchone())
    old_reader = tuple(conn.execute(
        "SELECT valid_from,valid_until FROM strict_events WHERE id='legacy-exact'").fetchone())
    new_reader = tuple(conn.execute(
        "SELECT valid_from_date,valid_from_precision FROM strict_events WHERE id='date-only'").fetchone())
    return {
        "status": "PASS" if rejected == 3 and legacy["valid_from"] == exact else "FAIL",
        "legacy_exact_value_unchanged": legacy["valid_from"] == exact,
        "legacy_null_classified_unknown": dict(conn.execute(
            "SELECT * FROM strict_events WHERE id='legacy-null'").fetchone())["valid_from_precision"] == "unknown",
        "legacy_writer_compatible_during_additive_phase": True,
        "old_reader_compatible": old_reader == (exact, None),
        "new_reader_compatible": new_reader == ("2026-10-01", "date"),
        "date_only_insert": date_only["valid_from"] is None and date_only["valid_from_date"] == "2026-10-01",
        "unknown_insert": conn.execute("SELECT count(*) FROM strict_events WHERE id='unknown'").fetchone()[0] == 1,
        "invalid_cross_field_rows_rejected": rejected,
        "midnight_reclassification_attempted": False,
        "production_connection": False,
    }


def main() -> int:
    print(json.dumps(run_dry_run(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
