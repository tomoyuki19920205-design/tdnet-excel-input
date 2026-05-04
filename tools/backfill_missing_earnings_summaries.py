#!/usr/bin/env python3
"""backfill_missing_earnings_summaries.py — TDNET決算短信 → earnings_summaries + tdnet_events バックフィル

4月以降に欠損している earnings_summaries を日付範囲指定で再解析し、
run_earnings_production() を通じて earnings_summaries と tdnet_events の両方を補完する。

Usage:
    python tools/backfill_missing_earnings_summaries.py --date-from 2026-04-01 --date-to 2026-04-30
    python tools/backfill_missing_earnings_summaries.py --date-from 2026-04-01 --date-to 2026-04-02 --dry-run
    python tools/backfill_missing_earnings_summaries.py --date-from 2026-04-01 --date-to 2026-04-02 --limit 5
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.pipeline.db import load_env
from src.events.earnings_production_pipeline import run_earnings_production

logger = logging.getLogger("backfill_earnings_summaries")


# ============================================================
# 日付レンジ生成
# ============================================================
def _date_range(date_from: str, date_to: str) -> list[str]:
    """YYYY-MM-DD の連続日付リストを返す"""
    d_from = date.fromisoformat(date_from)
    d_to   = date.fromisoformat(date_to)
    result = []
    cur = d_from
    while cur <= d_to:
        result.append(cur.isoformat())
        cur += timedelta(days=1)
    return result


# ============================================================
# メイン処理
# ============================================================
def run(
    *,
    date_from: str,
    date_to: str,
    limit: int = 0,
    dry_run: bool = False,
    db_path: str = "",
) -> dict:
    if not db_path:
        db_path = str(Path(_PROJECT_ROOT) / "decision_db.db")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    totals = {
        "dates_processed": 0,
        "dates_failed": 0,
        "total_disclosures": 0,
        "tanshin_count": 0,
        "saved_count": 0,
        "already_exists_count": 0,
        "notified_count": 0,
        "errors": 0,
    }

    target_dates = _date_range(date_from, date_to)
    logger.info(
        f"[BACKFILL_ES] start: date_from={date_from} date_to={date_to} "
        f"dates={len(target_dates)} limit={limit} dry_run={dry_run}"
    )

    for target_date in target_dates:
        logger.info(f"[BACKFILL_ES] === {target_date} ===")
        try:
            from src.fetcher import fetch_new_disclosures
            docs = fetch_new_disclosures(target_date=target_date)

            if not docs:
                logger.info(f"[BACKFILL_ES] {target_date}: no disclosures found")
                totals["dates_processed"] += 1
                continue

            # --limit がある場合は先頭N件に絞る
            if limit > 0:
                docs = docs[:limit]

            logger.info(f"[BACKFILL_ES] {target_date}: fetched {len(docs)} docs")

            result = run_earnings_production(
                docs=docs,
                conn=conn,
                webhook_url="",   # Discord通知を完全に回避
                dry_run=dry_run,
            )

            totals["dates_processed"]     += 1
            totals["total_disclosures"]   += result.total_disclosures
            totals["tanshin_count"]       += result.tanshin_count
            totals["saved_count"]         += result.saved_count
            totals["already_exists_count"]+= result.already_exists_count
            totals["notified_count"]      += result.notified_count
            totals["errors"]              += len(result.errors)

            logger.info(
                f"[BACKFILL_ES] {target_date}: "
                f"tanshin={result.tanshin_count} saved={result.saved_count} "
                f"exists={result.already_exists_count} errors={len(result.errors)}"
            )
            if result.errors:
                for err in result.errors:
                    logger.warning(f"[BACKFILL_ES] {target_date} error: {err}")

        except Exception as e:
            totals["dates_failed"] += 1
            logger.error(f"[BACKFILL_ES] {target_date} FAILED (continuing): {e}")

    conn.close()

    logger.info(
        f"[BACKFILL_ES] done: "
        f"dates_processed={totals['dates_processed']} "
        f"dates_failed={totals['dates_failed']} "
        f"total_disclosures={totals['total_disclosures']} "
        f"tanshin={totals['tanshin_count']} "
        f"saved={totals['saved_count']} "
        f"already_exists={totals['already_exists_count']} "
        f"errors={totals['errors']}"
    )
    return totals


# ============================================================
# CLI エントリポイント
# ============================================================
def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="TDNET決算短信 → earnings_summaries + tdnet_events バックフィル"
    )
    parser.add_argument("--date-from", required=True, metavar="YYYY-MM-DD",
                        help="開始日（例: 2026-04-01）")
    parser.add_argument("--date-to", required=True, metavar="YYYY-MM-DD",
                        help="終了日（例: 2026-04-30）")
    parser.add_argument("--limit", type=int, default=0,
                        help="1日あたりの最大 disclosure 件数（0=全件）")
    parser.add_argument("--dry-run", action="store_true",
                        help="DB書き込みをスキップ")
    parser.add_argument("--db-path", type=str, default="",
                        help="decision_db.db パス（省略時は自動）")
    args = parser.parse_args()

    load_env(_PROJECT_ROOT)

    result = run(
        date_from=args.date_from,
        date_to=args.date_to,
        limit=args.limit,
        dry_run=args.dry_run,
        db_path=args.db_path,
    )

    print()
    print("=" * 55)
    print("  BACKFILL EARNINGS SUMMARIES SUMMARY")
    print("=" * 55)
    print(f"  dates_processed   : {result['dates_processed']}")
    print(f"  dates_failed      : {result['dates_failed']}")
    print(f"  total_disclosures : {result['total_disclosures']}")
    print(f"  tanshin_count     : {result['tanshin_count']}")
    print(f"  saved_count       : {result['saved_count']}")
    print(f"  already_exists    : {result['already_exists_count']}")
    print(f"  errors            : {result['errors']}")
    if args.dry_run:
        print("  [DRY-RUN] no writes performed")
    print("=" * 55)


if __name__ == "__main__":
    main()
