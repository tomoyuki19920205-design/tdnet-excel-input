#!/usr/bin/env python3
"""backfill_events_tdnet.py — SQLite events → Supabase tdnet_events バックフィル

buyback / forecast_revision / dividend_revision の SQLite events データを
Supabase tdnet_events へ投入する。

Usage:
    python tools/backfill_events_tdnet.py --dry-run
    python tools/backfill_events_tdnet.py --since 2026-04-01 --dry-run
    python tools/backfill_events_tdnet.py --since 2026-04-01
    python tools/backfill_events_tdnet.py --event-type buyback
    python tools/backfill_events_tdnet.py --limit 50 --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.pipeline.db import load_env
from src.events.common_models import EventRecord
from src.events.tdnet_event_store import save_event_to_supabase

logger = logging.getLogger("backfill_events")
JST = timezone(timedelta(hours=9))

# SQLite event_type → Supabase display_category (tdnet_event_store が自動変換するが念のためログ用)
_TYPE_LABEL = {
    "buyback": "buyback",
    "forecast_revision": "forecast",
    "dividend_revision": "dividend",
}

SUPPORTED_TYPES = ("buyback", "forecast_revision", "dividend_revision")


def _sqlite_row_to_event_record(row: dict) -> EventRecord:
    """SQLite events の1行を EventRecord に変換。"""
    return EventRecord(
        event_id=row.get("event_id", ""),
        source_doc_id=row.get("source_doc_id", ""),
        ticker=row.get("ticker", ""),
        company_name=row.get("company_name", ""),
        disclosure_datetime=row.get("disclosure_datetime", ""),
        title=row.get("title", ""),
        doc_url=row.get("doc_url", "") or "",
        event_type=row.get("event_type", ""),
        subtype=row.get("subtype", ""),
        importance=row.get("importance", 50) or 50,
        summary_text=row.get("summary_text", "") or "",
        raw_payload_json=row.get("raw_payload_json", "") or "{}",
        extracted_payload_json=row.get("extracted_payload_json", "") or "{}",
        fingerprint=row.get("fingerprint", ""),
        status=row.get("status", "new"),
        first_seen_at=row.get("first_seen_at", ""),
        last_seen_at=row.get("last_seen_at", ""),
        notified_at=row.get("notified_at"),
        created_at=row.get("created_at", ""),
        updated_at=row.get("updated_at", ""),
    )


def run(
    *,
    since: str = "",
    until: str = "",
    event_types: list[str] | None = None,
    limit: int = 0,
    dry_run: bool = False,
    db_path: str = "",
) -> dict:
    """SQLite events → Supabase tdnet_events バックフィル実行。

    Returns:
        {"total": int, "inserted": int, "dedup_skipped": int, "errors": int, "dry_run": int}
    """
    if not db_path:
        db_path = str(Path(_PROJECT_ROOT) / "decision_db.db")

    # event_type フィルタ
    types_to_process = event_types or list(SUPPORTED_TYPES)
    # サポート外は除外
    types_to_process = [t for t in types_to_process if t in SUPPORTED_TYPES]
    if not types_to_process:
        logger.error(f"有効な event_type が指定されていません: {event_types}")
        return {"total": 0, "inserted": 0, "dedup_skipped": 0, "errors": 0, "dry_run": 0}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # クエリ構築
    where_parts = ["event_type IN ({})".format(",".join("?" for _ in types_to_process))]
    params: list = list(types_to_process)

    if since:
        where_parts.append("first_seen_at >= ?")
        params.append(since)
    if until:
        where_parts.append("first_seen_at < ?")
        params.append(until)

    where_clause = "WHERE " + " AND ".join(where_parts)
    limit_clause = f"LIMIT {limit}" if limit > 0 else ""

    sql = f"""
        SELECT *
        FROM events
        {where_clause}
        ORDER BY first_seen_at ASC
        {limit_clause}
    """

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    total = len(rows)
    counts = {
        "total": total,
        "inserted": 0,
        "dedup_skipped": 0,
        "errors": 0,
        "dry_run": 0,
        "by_type": {t: {"inserted": 0, "dedup_skipped": 0, "errors": 0, "dry_run": 0} for t in SUPPORTED_TYPES},
    }

    logger.info(
        f"[BACKFILL_EVENTS] start: total={total} "
        f"types={types_to_process} since={since!r} dry_run={dry_run}"
    )

    if total == 0:
        logger.info("[BACKFILL_EVENTS] no records found.")
        return counts

    for row in rows:
        row_dict = dict(row)
        ticker_val = row_dict.get("ticker", "?")
        et = row_dict.get("event_type", "?")
        fp = (row_dict.get("fingerprint", "") or "")[:12]

        try:
            record = _sqlite_row_to_event_record(row_dict)
            result = save_event_to_supabase(record, dry_run=dry_run)
            action = result.get("action", "error")

            if action == "inserted":
                counts["inserted"] += 1
                counts["by_type"][et]["inserted"] += 1
                logger.info(
                    f"[BACKFILL_EVENTS] INSERTED: ticker={ticker_val} type={et} "
                    f"subtype={row_dict.get('subtype')} fp={fp}... "
                    f"-> Supabase type={result.get('display_category')} "
                    f"dedupe={result.get('dedupe_key', '')[:12]}..."
                )
            elif action == "dedup_skipped":
                counts["dedup_skipped"] += 1
                counts["by_type"][et]["dedup_skipped"] += 1
                logger.debug(
                    f"[BACKFILL_EVENTS] SKIP(dedup): ticker={ticker_val} type={et} fp={fp}..."
                )
            elif action == "dry_run":
                counts["dry_run"] += 1
                counts["by_type"][et]["dry_run"] += 1
                logger.info(
                    f"[BACKFILL_EVENTS] DRY-RUN: ticker={ticker_val} type={et} "
                    f"-> Supabase type={_TYPE_LABEL.get(et, et)} "
                    f"subtype={row_dict.get('subtype')} "
                    f"date={str(row_dict.get('disclosure_datetime', ''))[:10]}"
                )
            else:
                counts["errors"] += 1
                counts["by_type"][et]["errors"] += 1
                logger.warning(
                    f"[BACKFILL_EVENTS] ERROR: ticker={ticker_val} type={et} fp={fp}... "
                    f"error={result.get('error', 'unknown')}"
                )

        except Exception as e:
            counts["errors"] += 1
            counts["by_type"].get(et, {})
            logger.error(f"[BACKFILL_EVENTS] EXCEPTION: ticker={ticker_val} type={et} fp={fp}...: {e}")

    logger.info(
        f"[BACKFILL_EVENTS] done: total={counts['total']} "
        f"inserted={counts['inserted']} dedup={counts['dedup_skipped']} "
        f"errors={counts['errors']}"
        + (f" dry_run={counts['dry_run']}" if dry_run else "")
    )
    return counts


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="SQLite events (buyback/forecast/dividend) → Supabase tdnet_events バックフィル"
    )
    parser.add_argument("--since", type=str, default="2026-04-01",
                        help="開始日 (first_seen_at >= この値, デフォルト: 2026-04-01)")
    parser.add_argument("--until", type=str, default="",
                        help="終了日 (first_seen_at < この値, デフォルト: 制限なし)")
    parser.add_argument("--event-type", type=str, default=None,
                        help=f"対象 event_type ({', '.join(SUPPORTED_TYPES)}) カンマ区切り複数指定可")
    parser.add_argument("--limit", type=int, default=0,
                        help="最大件数 (0=全件)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Supabaseに書き込まない")
    parser.add_argument("--db-path", type=str, default="",
                        help="decision_db.db パス (省略時は自動)")
    args = parser.parse_args()

    load_env(_PROJECT_ROOT)

    event_types = None
    if args.event_type:
        event_types = [t.strip() for t in args.event_type.split(",")]

    result = run(
        since=args.since,
        until=args.until,
        event_types=event_types,
        limit=args.limit,
        dry_run=args.dry_run,
        db_path=args.db_path,
    )

    print()
    print("=" * 55)
    print("  BACKFILL_EVENTS SUMMARY")
    print("=" * 55)
    print(f"  total        : {result['total']}")
    print(f"  inserted     : {result['inserted']}")
    print(f"  dedup_skipped: {result['dedup_skipped']}")
    print(f"  errors       : {result['errors']}")
    if args.dry_run:
        print(f"  dry_run      : {result['dry_run']}")
    print()
    print("  event_type breakdown:")
    for et, ct in result.get("by_type", {}).items():
        key = "dry_run" if args.dry_run else "inserted"
        val = ct.get(key, 0)
        if val > 0 or ct.get("errors", 0) > 0:
            print(f"    {et}: {key}={val} dedup={ct.get('dedup_skipped',0)} errors={ct.get('errors',0)}")
    print("=" * 55)


if __name__ == "__main__":
    main()
