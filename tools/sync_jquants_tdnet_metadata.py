#!/usr/bin/env python3
"""Incrementally cache TDnet titles/items needed to canonicalize forecast revisions."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lib.runtime_paths import runtime_path

from src.jquants.adapter import fetch_tdnet_list_raw

REPUBLISHED_DATE_GAP_DAYS = 7


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS jquants_tdnet_metadata (
          disclosure_id TEXT PRIMARY KEY,
          disclosed_date TEXT NOT NULL,
          disclosed_time TEXT,
          ticker TEXT,
          title TEXT NOT NULL DEFAULT '',
          disc_items_json TEXT NOT NULL DEFAULT '[]',
          rev_no TEXT,
          disc_status TEXT,
          metadata_status TEXT NOT NULL DEFAULT 'verified',
          fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS jquants_tdnet_metadata_dates (
          disclosed_date TEXT PRIMARY KEY,
          item_count INTEGER NOT NULL,
          fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_jquants_tdnet_metadata_date
          ON jquants_tdnet_metadata(disclosed_date);
        """
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(jquants_tdnet_metadata)")
    }
    if "disclosed_time" not in columns:
        connection.execute(
            "ALTER TABLE jquants_tdnet_metadata ADD COLUMN disclosed_time TEXT"
        )
    if "metadata_status" not in columns:
        connection.execute(
            "ALTER TABLE jquants_tdnet_metadata ADD COLUMN metadata_status TEXT "
            "NOT NULL DEFAULT 'verified'"
        )


def _raw_dates(connection: sqlite3.Connection, start: str, end: str) -> list[str]:
    result: set[str] = set()
    for (raw_json,) in connection.execute(
        "SELECT raw_json FROM jquants_financials_normalized WHERE raw_json IS NOT NULL"
    ):
        try:
            raw = json.loads(raw_json)
        except (TypeError, json.JSONDecodeError):
            continue
        disclosed_date = str(raw.get("DiscDate") or "")
        if start <= disclosed_date <= end:
            result.add(disclosed_date)
        disclosure_id = str(raw.get("DiscNo") or "")
        try:
            encoded = date.fromisoformat(
                f"{disclosure_id[:4]}-{disclosure_id[4:6]}-{disclosure_id[6:8]}"
            )
            reported = date.fromisoformat(disclosed_date)
        except ValueError:
            continue
        gap = (reported - encoded).days
        if 0 <= gap <= 90:
            for offset in range(gap + 1):
                candidate = (encoded + timedelta(days=offset)).isoformat()
                if start <= candidate <= end:
                    result.add(candidate)
    return sorted(result)


def _upsert_date(
    connection: sqlite3.Connection, disclosed_date: str, items: Iterable[dict[str, Any]]
) -> int:
    rows = list(items)
    connection.execute("BEGIN IMMEDIATE")
    try:
        for item in rows:
            disclosure_id = str(item.get("DiscNo") or "")
            if not disclosure_id:
                continue
            connection.execute(
                """
                INSERT INTO jquants_tdnet_metadata(
                  disclosure_id,disclosed_date,disclosed_time,ticker,title,disc_items_json,
                  rev_no,disc_status,metadata_status,fetched_at
                ) VALUES (?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(disclosure_id) DO UPDATE SET
                  disclosed_date=excluded.disclosed_date,
                  disclosed_time=excluded.disclosed_time,
                  ticker=excluded.ticker,
                  title=excluded.title,
                  disc_items_json=excluded.disc_items_json,
                  rev_no=excluded.rev_no,
                  disc_status=excluded.disc_status,
                  metadata_status='verified',
                  fetched_at=CURRENT_TIMESTAMP
                """,
                (
                    disclosure_id,
                    disclosed_date,
                    str(item.get("DiscTime") or ""),
                    str(item.get("Code") or ""),
                    str(item.get("Title") or ""),
                    json.dumps(item.get("DiscItems") or [], ensure_ascii=False),
                    None if item.get("RevNo") is None else str(item.get("RevNo")),
                    None if item.get("DiscStatus") is None else str(item.get("DiscStatus")),
                    "verified",
                ),
            )
        connection.execute(
            """
            INSERT INTO jquants_tdnet_metadata_dates(disclosed_date,item_count,fetched_at)
            VALUES (?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(disclosed_date) DO UPDATE SET
              item_count=excluded.item_count,fetched_at=CURRENT_TIMESTAMP
            """,
            (disclosed_date, len(rows)),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return len(rows)


def _upsert_statements_only_fallbacks(
    connection: sqlite3.Connection, start: str, end: str
) -> int:
    """Represent statement rows absent from `/td/list` without guessing a role."""
    verified = {
        str(row[0]) for row in connection.execute(
            "SELECT disclosure_id,metadata_status FROM jquants_tdnet_metadata"
        ) if row[1] == "verified"
    }
    fallbacks: dict[str, tuple[str, str, str, str]] = {}
    for (raw_json,) in connection.execute(
        "SELECT raw_json FROM jquants_financials_normalized WHERE raw_json IS NOT NULL"
    ):
        try:
            raw = json.loads(raw_json)
        except (TypeError, json.JSONDecodeError):
            continue
        disclosure_id = str(raw.get("DiscNo") or "")
        disclosed_date = str(raw.get("DiscDate") or "")
        if (
            not disclosure_id
            or disclosure_id in verified
            or not start <= disclosed_date <= end
        ):
            continue
        economic_date, status = _unmatched_economic_date(disclosure_id, disclosed_date)
        fallbacks[disclosure_id] = (
            economic_date,
            str(raw.get("DiscTime") or ""),
            str(raw.get("Code") or ""),
            status,
        )
    if not fallbacks:
        return 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.executemany(
            """
            INSERT INTO jquants_tdnet_metadata(
              disclosure_id,disclosed_date,disclosed_time,ticker,title,
              disc_items_json,rev_no,disc_status,metadata_status,fetched_at
            ) VALUES (?,?,?,?,?,'[]',NULL,NULL,?,CURRENT_TIMESTAMP)
            ON CONFLICT(disclosure_id) DO UPDATE SET
              disclosed_date=excluded.disclosed_date,
              disclosed_time=excluded.disclosed_time,
              ticker=excluded.ticker,
              metadata_status=excluded.metadata_status,
              fetched_at=CURRENT_TIMESTAMP
            WHERE jquants_tdnet_metadata.metadata_status <> 'verified'
            """,
            [
                (disclosure_id, disclosed_date, disclosed_time, ticker, "", status)
                for disclosure_id, (disclosed_date, disclosed_time, ticker, status)
                in fallbacks.items()
            ],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return len(fallbacks)


def _unmatched_economic_date(disclosure_id: str, reported_date: str) -> tuple[str, str]:
    """Restore the original date for delayed historical re-publications.

    Disclosure numbers encode the source submission date. Small gaps are normal
    scheduled/publication delays; a gap of seven days or more in a record absent
    from `/td/list` is treated as historical re-publication rather than a new
    economic disclosure.
    """
    try:
        encoded = date.fromisoformat(
            f"{disclosure_id[:4]}-{disclosure_id[4:6]}-{disclosure_id[6:8]}"
        )
        reported = date.fromisoformat(reported_date)
    except ValueError:
        return reported_date, "statements_only"
    gap = (reported - encoded).days
    if gap >= REPUBLISHED_DATE_GAP_DAYS:
        return encoded.isoformat(), "historical_republication"
    return reported_date, "statements_only"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(runtime_path(ROOT / "data" / "jquants.db")))
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--payload-cache",
        help="optional read-only SQLite cache with date_payloads(disclosed_date,payload_json)",
    )
    parser.add_argument("--sleep", type=float, default=0.7)
    args = parser.parse_args()

    connection = sqlite3.connect(args.db, timeout=60)
    universe_row = connection.execute(
        "SELECT MAX(date) FROM market_data_universe"
    ).fetchone()
    as_of = date.fromisoformat(str(universe_row[0])) if universe_row and universe_row[0] else date.today()
    end = args.to_date or as_of.isoformat()
    start = args.from_date or as_of.replace(year=as_of.year - 3).isoformat()
    dates = _raw_dates(connection, start, end)
    if args.apply:
        _schema(connection)
        connection.commit()
    existing = set()
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='jquants_tdnet_metadata_dates'"
    ).fetchone()
    if table:
        existing = {
            row[0] for row in connection.execute(
                "SELECT disclosed_date FROM jquants_tdnet_metadata_dates "
                "WHERE disclosed_date BETWEEN ? AND ?", (start, end)
            )
        }
    missing = [value for value in dates if value not in existing]
    print(json.dumps({
        "mode": "apply" if args.apply else "read-only",
        "from_date": start,
        "to_date": end,
        "raw_dates": len(dates),
        "cached_dates": len(existing & set(dates)),
        "missing_dates": len(missing),
    }, ensure_ascii=False))
    if not args.apply:
        connection.close()
        return 0 if not missing else 2

    _load_dotenv()
    if not os.environ.get("JQUANTS_API_KEY"):
        if not args.payload_cache:
            raise RuntimeError("JQUANTS_API_KEY is required")
    payload_cache = (
        sqlite3.connect(f"file:{Path(args.payload_cache).resolve().as_posix()}?mode=ro", uri=True)
        if args.payload_cache else None
    )
    for index, disclosed_date in enumerate(missing, 1):
        cached = payload_cache.execute(
            "SELECT payload_json FROM date_payloads WHERE disclosed_date=?",
            (disclosed_date,),
        ).fetchone() if payload_cache else None
        items = (
            json.loads(cached[0]) if cached
            else fetch_tdnet_list_raw(disclosed_date.replace("-", ""))
        )
        count = _upsert_date(connection, disclosed_date, items)
        if index % 25 == 0 or index == len(missing):
            print(f"cached {index}/{len(missing)} dates; last_items={count}", flush=True)
        time.sleep(args.sleep)
    fallback_count = _upsert_statements_only_fallbacks(connection, start, end)
    print(f"statements_only_fallbacks={fallback_count}", flush=True)
    if payload_cache:
        payload_cache.close()
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
