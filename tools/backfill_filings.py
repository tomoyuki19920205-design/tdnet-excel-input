#!/usr/bin/env python3
# ============================================================
# backfill_filings.py — 日付ループ方式バックフィル
# ============================================================
"""
期間ループではなく、日付一覧生成 → 単日処理 方式。
失敗日は記録し、最後にサマリ表示。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.config import Config
from tools.tdnet_ingest import run_ingest

logger = logging.getLogger("pipeline.backfill")


def backfill_dates(start: str, end: str) -> list[str]:
    """
    start〜end (YYYY-MM-DD) の日付リストを生成。
    """
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")

    dates: list[str] = []
    current = start_dt
    while current <= end_dt:
        dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)

    return dates


def run_ingest_for_date(date_str: str, *, dry_run: bool = False) -> dict:
    """
    単日の ingest を実行。

    Note: 現在の tdnet_ingest.run_ingest は当日分のみ取得するため、
    バックフィルには TDnet API の日付指定対応が必要。
    この関数は将来の拡張ポイントとして用意する。
    """
    config = Config()
    config.start_date = date_str
    try:
        result = run_ingest(config, dry_run=dry_run)
        return {
            "date": date_str,
            "status": "success",
            "total": result.get("total", 0),
        }
    except Exception as e:
        return {
            "date": date_str,
            "status": "error",
            "error": str(e),
        }


def run(
    *,
    start: str,
    end: str,
    dry_run: bool = False,
) -> dict:
    """
    バックフィル実行。

    Returns:
        {"dates_total": int, "succeeded": int, "failed": int,
         "failed_dates": list[str]}
    """
    dates = backfill_dates(start, end)
    logger.info(
        f"[backfill] {start} → {end}: {len(dates)} days"
    )

    succeeded = 0
    failed = 0
    failed_dates: list[str] = []

    for i, date_str in enumerate(dates, 1):
        logger.info(f"[backfill] [{i}/{len(dates)}] {date_str}")
        result = run_ingest_for_date(date_str, dry_run=dry_run)

        if result["status"] == "success":
            succeeded += 1
        else:
            failed += 1
            failed_dates.append(date_str)
            logger.warning(
                f"[backfill] {date_str} FAILED: {result.get('error', '?')}"
            )

    # サマリ表示
    print()
    print("=" * 50)
    print("  BACKFILL SUMMARY")
    print("=" * 50)
    print(f"  期間        : {start} → {end}")
    print(f"  対象日数    : {len(dates)}")
    print(f"  成功        : {succeeded}")
    print(f"  失敗        : {failed}")
    if failed_dates:
        print(f"  失敗日      : {', '.join(failed_dates[:20])}")
    print("=" * 50)

    return {
        "dates_total": len(dates),
        "succeeded": succeeded,
        "failed": failed,
        "failed_dates": failed_dates,
    }


def main():
    parser = argparse.ArgumentParser(
        description="日付ループ方式バックフィル",
    )
    parser.add_argument(
        "--from", dest="start", required=True,
        help="開始日 (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--to", dest="end", required=True,
        help="終了日 (YYYY-MM-DD)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    result = run(start=args.start, end=args.end, dry_run=args.dry_run)
    sys.exit(1 if result["failed"] > 0 else 0)


if __name__ == "__main__":
    main()
