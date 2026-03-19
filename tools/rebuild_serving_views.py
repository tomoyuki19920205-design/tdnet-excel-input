#!/usr/bin/env python3
# ============================================================
# rebuild_serving_views.py — Serving テーブル再構築
# ============================================================
"""
rebuild_queue pending を読み、ticker 単位で viewer 用テーブルを再構築。
Phase 1: financials / segment_canonical を ticker 単位で整合性更新。

--ticker 直指定時: rebuild_queue は消化しない（直接対象 ticker のみ処理）。
--ticker なし: rebuild_queue の pending 行を取得し、running → done/failed に更新する。

いずれの場合も pipeline_runs には記録される（pipeline_run.py 経由の場合）。

Usage:
    python tools/rebuild_serving_views.py --dry-run
    python tools/rebuild_serving_views.py --apply
    python tools/rebuild_serving_views.py --apply --ticker 6750
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.pipeline.db import load_env, get_supabase_config, supabase_select
from lib.pipeline.queue import take_pending_rebuilds, complete_rebuild
from lib.pipeline.logging_utils import PipelineRun

logger = logging.getLogger("pipeline.serving")


def rebuild_ticker_financials(
    ticker: str,
    *,
    config: dict,
    dry_run: bool = False,
) -> dict:
    """ticker の financials をチェック・整合性確認。

    Phase 1 では financials は sqlite_to_supabase / sync_financials が
    直接更新するためここでは重複チェックのみ。
    将来の canonical → serving rebuild はここに追加する。
    """
    rows = supabase_select(
        "financials",
        params={
            "ticker": f"eq.{ticker}",
            "select": "ticker,period,quarter",
        },
        config=config,
    )
    # 重複チェック: (ticker, period, quarter) が重複していないか
    seen: set[tuple] = set()
    dups = 0
    for row in rows:
        key = (row["ticker"], row["period"], row["quarter"])
        if key in seen:
            dups += 1
        seen.add(key)

    return {"ticker": ticker, "rows": len(rows), "unique": len(seen), "dups": dups}


def rebuild_ticker_segments(
    ticker: str,
    *,
    config: dict,
    dry_run: bool = False,
) -> dict:
    """ticker の segment_canonical をチェック・整合性確認。"""
    rows = supabase_select(
        "segment_canonical",
        params={
            "ticker": f"eq.{ticker}",
            "select": "ticker,period,quarter,segment_name",
        },
        config=config,
    )
    seen: set[tuple] = set()
    dups = 0
    for row in rows:
        key = (row["ticker"], row["period"], row["quarter"], row["segment_name"])
        if key in seen:
            dups += 1
        seen.add(key)

    return {"ticker": ticker, "rows": len(rows), "unique": len(seen), "dups": dups}


def rebuild_ticker(
    ticker: str,
    *,
    config: dict,
    dry_run: bool = False,
) -> dict:
    """ticker 単位の rebuild。"""
    fin = rebuild_ticker_financials(ticker, config=config, dry_run=dry_run)
    seg = rebuild_ticker_segments(ticker, config=config, dry_run=dry_run)
    issues = []
    if fin["dups"] > 0:
        issues.append(f"financials: {fin['dups']} duplicates")
    if seg["dups"] > 0:
        issues.append(f"segments: {seg['dups']} duplicates")
    return {
        "ticker": ticker,
        "financials": fin,
        "segments": seg,
        "issues": issues,
        "status": "warning" if issues else "ok",
    }


def run(*, dry_run: bool = False, ticker: str | None = None) -> dict:
    """rebuild_serving_views のメイン。

    Args:
        ticker: 指定時は直接対象 ticker のみ rebuild (queue は消化しない)。
                未指定時は rebuild_queue の pending を処理する。
    """
    load_env(_PROJECT_ROOT)
    config = get_supabase_config()

    results: list[dict] = []
    total = 0
    success = 0
    failed = 0

    if ticker:
        # 直指定 — rebuild_queue は消化しない
        tickers = [ticker]
        queue_items = []
        logger.info(f"[rebuild] ticker={ticker} 直指定 (rebuild_queue は消化しない)")
    else:
        # rebuild_queue から取得 — pending → running に更新される
        queue_items = take_pending_rebuilds(config=config)
        tickers = [item["ticker"] for item in queue_items]
        if queue_items:
            logger.info(
                f"[rebuild] rebuild_queue から {len(queue_items)} 件取得: "
                f"{[item['ticker'] for item in queue_items[:10]]}"
            )

    if not tickers:
        logger.info("[rebuild] no pending rebuilds")
        return {"status": "skipped", "reason": "no_pending_rebuilds", "results": [], "total": 0}

    logger.info(f"[rebuild] processing {len(tickers)} tickers")

    for i, t in enumerate(tickers):
        total += 1
        try:
            result = rebuild_ticker(t, config=config, dry_run=dry_run)
            results.append(result)
            if result["status"] == "ok":
                success += 1
            else:
                logger.warning(f"[rebuild] {t}: issues={result['issues']}")
                success += 1  # warning は成功扱い
            logger.info(
                f"[rebuild] [{i+1}/{len(tickers)}] {t}: "
                f"fin={result['financials']['rows']} seg={result['segments']['rows']} "
                f"status={result['status']}"
            )
        except Exception as e:
            failed += 1
            logger.error(f"[rebuild] {t}: error={e}")
            results.append({"ticker": t, "status": "error", "error": str(e)})

    # queue_items の完了処理 (--ticker 直指定時は queue_items は空)
    for item in queue_items:
        item_ticker = item["ticker"]
        matching = [r for r in results if r["ticker"] == item_ticker]
        new_status = "done"
        if not matching or matching[0].get("status") == "error":
            new_status = "failed"
        try:
            complete_rebuild(item["id"], new_status, config=config)
            logger.info(
                f"[rebuild] queue id={item['id']} ticker={item_ticker} → {new_status}"
            )
        except Exception as e:
            logger.warning(
                f"[rebuild] queue update FAILED: id={item['id']} "
                f"ticker={item_ticker} target_status={new_status} error={e}"
            )

    return {
        "status": "success" if failed == 0 else "partial",
        "total": total,
        "success": success,
        "failed": failed,
        "results": results,
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Rebuild serving views")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ticker", type=str, help="特定 ticker のみ rebuild")
    args = parser.parse_args()
    dry_run = not args.apply or args.dry_run
    result = run(dry_run=dry_run, ticker=args.ticker)
    print(f"\nResult: {result['status']} total={result['total']} success={result.get('success', 0)} failed={result.get('failed', 0)}")
