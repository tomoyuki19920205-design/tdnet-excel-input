#!/usr/bin/env python3
# ============================================================
# backfill_missed_date.py — 過去日付の未取得開示バックフィル CLI
# ============================================================
"""
タスク未実行日や既知の取りこぼし日の開示を、通常 ingest フローで再取得する。

Usage:
    python tools/backfill_missed_date.py --date 2026-03-16
    python tools/backfill_missed_date.py --date 2026-03-16 --code 9279
    python tools/backfill_missed_date.py --from 2026-03-14 --to 2026-03-17
    python tools/backfill_missed_date.py --date 2026-03-16 --dry-run
    python tools/backfill_missed_date.py --date 2026-03-16 --title-contains 決算短信
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# プロジェクトルートを sys.path に追加
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.config import load_config, Config
from src.db import StateDB
from src.fetcher import (
    _fetch_via_html,
    classify_disclosure,
    is_instrument_excluded,
)
from src.migration.migration_db import MigrationDB
from src.models import DisclosureType

logger = logging.getLogger("backfill")

JST = timezone(timedelta(hours=9))


# ============================================================
# ユーティリティ
# ============================================================

def _date_range(from_date: str, to_date: str) -> list[str]:
    """YYYY-MM-DD の日付リストを生成。"""
    start = datetime.strptime(from_date, "%Y-%m-%d").date()
    end = datetime.strptime(to_date, "%Y-%m-%d").date()
    if start > end:
        raise ValueError(f"--from ({from_date}) は --to ({to_date}) 以前である必要があります")
    result = []
    current = start
    while current <= end:
        result.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return result


# ============================================================
# バックフィル実行
# ============================================================

def run_backfill(
    *,
    dates: list[str],
    code_filter: str | None = None,
    title_filter: str | None = None,
    dry_run: bool = False,
    verbose: bool = False,
    config: Config | None = None,
) -> dict:
    """
    指定日付の開示をバックフィル取得し、通常 ingest フローで処理する。
    _fetch_via_html を直接使い、自前でフィルタリングする。

    Returns:
        サマリ辞書
    """
    if config is None:
        config = load_config()
    state_db = StateDB(config.state_db_path)
    decision_db = MigrationDB(config.decision_db_path)

    # tdnet_ingest の _process_single を再利用
    from tools.tdnet_ingest import _process_single

    run_id = f"backfill-{uuid.uuid4().hex[:8]}"
    t0 = time.monotonic()

    totals = {
        "scanned": 0,
        "matched": 0,
        "skipped_duplicate": 0,
        "skipped_filter": 0,
        "skipped_instrument": 0,
        "parse_failed": 0,
        "inserted": 0,
        "updated": 0,
        "no_change": 0,
        "quarantined": 0,
        "errors": 0,
        "dry_run": 0,
    }

    try:
        for target_date in dates:
            logger.info(f"{'=' * 55}")
            logger.info(f"  [BACKFILL] target_date={target_date}")
            logger.info(f"{'=' * 55}")

            # HTML スクレイピングで全件取得（fetcherの内部フィルタを回避）
            try:
                all_items = _fetch_via_html(target_date)
            except Exception as e:
                logger.error(f"[BACKFILL] HTML 取得失敗: {e}")
                totals["errors"] += 1
                continue

            totals["scanned"] += len(all_items)
            logger.info(
                f"[BACKFILL] date={target_date} raw_fetched={len(all_items)}"
            )

            # 自前フィルタリング
            target_items = []
            for item in all_items:
                # コードフィルタ
                if code_filter and item.ticker != code_filter:
                    continue

                # タイトルフィルタ
                if title_filter and title_filter not in item.title:
                    continue

                # ETF/投信除外
                if is_instrument_excluded(item.ticker, item.title, item.company_name):
                    totals["skipped_instrument"] += 1
                    continue

                # 決算短信のみ（classify_disclosure で判定）
                dtype = classify_disclosure(item.title)
                if dtype != DisclosureType.FINANCIAL_STATEMENT:
                    totals["skipped_filter"] += 1
                    continue

                # 重複チェック（state_db で既処理か）
                if state_db.is_processed(item.disclosure_id):
                    totals["skipped_duplicate"] += 1
                    if verbose:
                        logger.info(
                            f"[BACKFILL] skip_duplicate: code={item.ticker} "
                            f"title={item.title[:50]}"
                        )
                    continue

                target_items.append(item)

            totals["matched"] += len(target_items)
            logger.info(
                f"[BACKFILL] date={target_date} matched={len(target_items)} "
                f"skipped_dup={totals['skipped_duplicate']} "
                f"skipped_filter={totals['skipped_filter']}"
            )

            if dry_run:
                # dry-run: 対象一覧を表示
                for item in target_items:
                    totals["dry_run"] += 1
                    logger.info(
                        f"[DRY-RUN] code={item.ticker} "
                        f"company={item.company_name} "
                        f"title={item.title[:60]} "
                        f"pub={item.published_at} "
                        f"url={item.doc_url[:80]}"
                    )
                continue

            # 実行: 通常 ingest フローで処理
            for item in target_items:
                try:
                    result = _process_single(
                        item, config, state_db, decision_db, run_id,
                        dry_run=False,
                    )
                    status = result.get("status", "error")
                    if status == "inserted":
                        totals["inserted"] += 1
                    elif status == "updated":
                        totals["updated"] += 1
                    elif status == "no_change":
                        totals["no_change"] += 1
                    elif status == "skipped":
                        totals["skipped_duplicate"] += 1
                    elif status == "error":
                        totals["errors"] += 1
                        logger.warning(
                            f"[BACKFILL] error: code={item.ticker} "
                            f"detail={result.get('detail', '?')[:100]}"
                        )
                    else:
                        totals["inserted"] += 1  # その他の成功系

                    logger.info(
                        f"[BACKFILL] code={item.ticker} "
                        f"status={status} detail={result.get('detail', '')[:80]}"
                    )
                except Exception as e:
                    totals["errors"] += 1
                    logger.error(
                        f"[BACKFILL] exception: code={item.ticker} error={e}"
                    )

    finally:
        decision_db.close()
        state_db.close()

    elapsed = time.monotonic() - t0
    totals["elapsed"] = round(elapsed, 2)
    totals["run_id"] = run_id

    return totals


def print_summary(totals: dict) -> None:
    """バックフィルサマリを表示。"""
    print()
    print("=" * 55)
    print("  BACKFILL SUMMARY")
    print("=" * 55)
    for label, key in [
        ("run_id", "run_id"),
        ("スキャン件数", "scanned"),
        ("対象（決算短信）", "matched"),
        ("重複スキップ", "skipped_duplicate"),
        ("フィルタスキップ", "skipped_filter"),
        ("INSERT", "inserted"),
        ("UPDATE", "updated"),
        ("変更なし", "no_change"),
        ("エラー", "errors"),
        ("dry-run", "dry_run"),
    ]:
        print(f"  {label:18s}: {totals.get(key, 0)}")
    print(f"  {'elapsed':18s}: {totals.get('elapsed', 0):.1f}s")
    print("=" * 55)
    print()

    # grep 用 1行サマリ
    kv = " ".join(
        f"{k}={totals.get(k, 0)}"
        for k in ["scanned", "matched", "skipped_duplicate", "inserted",
                   "updated", "no_change", "errors", "elapsed"]
    )
    summary_line = f"[BACKFILL_SUMMARY] {kv}"
    print(summary_line)
    logger.info(summary_line)


# ============================================================
# CLI
# ============================================================

def main():
    # UTF-8 出力確保
    if sys.stdout and hasattr(sys.stdout, "encoding"):
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace",
            )

    parser = argparse.ArgumentParser(
        description="過去日付の未取得開示をバックフィル取得",
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="対象日付 (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--from", dest="from_date", type=str, default=None,
        help="開始日付 (YYYY-MM-DD) — --to と併用",
    )
    parser.add_argument(
        "--to", dest="to_date", type=str, default=None,
        help="終了日付 (YYYY-MM-DD) — --from と併用",
    )
    parser.add_argument(
        "--code", type=str, default=None,
        help="銘柄コード絞り込み (例: 9279)",
    )
    parser.add_argument(
        "--title-contains", type=str, default=None,
        help="タイトル絞り込み (例: 決算短信)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="DB書き込みなし、対象一覧のみ表示",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="詳細ログ出力",
    )
    args = parser.parse_args()

    # 日付検証
    if args.date and (args.from_date or args.to_date):
        parser.error("--date と --from/--to は排他です")
    if args.date:
        dates = [args.date]
    elif args.from_date and args.to_date:
        dates = _date_range(args.from_date, args.to_date)
    elif args.from_date or args.to_date:
        parser.error("--from と --to は両方指定してください")
    else:
        parser.error("--date または --from/--to を指定してください")

    # ロギング
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # .env ロード
    from lib.pipeline.db import load_env
    load_env(_PROJECT_ROOT)

    print("=" * 55)
    print("  BACKFILL MISSED DATE")
    print("=" * 55)
    print(f"  dates: {dates}")
    print(f"  code: {args.code or 'ALL'}")
    print(f"  title: {args.title_contains or 'ALL'}")
    print(f"  dry_run: {args.dry_run}")
    print()

    totals = run_backfill(
        dates=dates,
        code_filter=args.code,
        title_filter=args.title_contains,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    print_summary(totals)

    # exit code
    if totals.get("errors", 0) > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
