#!/usr/bin/env python3
# ============================================================
# daily_reconcile.py — 深夜整合性チェック (Phase 1 最小実装)
# ============================================================
"""
軽量チェックを実行し、異常を data_quality_issues に記録。

Usage:
    python tools/daily_reconcile.py
    python tools/daily_reconcile.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.pipeline.db import load_env, get_supabase_config, supabase_select, supabase_upsert, supabase_update
from lib.pipeline.logging_utils import PipelineRun

logger = logging.getLogger("pipeline.reconcile")
JST = timezone(timedelta(hours=9))


def _record_issue(
    check_name: str,
    detail: str,
    *,
    ticker: str | None = None,
    severity: str = "warn",
    config: dict,
    dry_run: bool = False,
) -> None:
    """data_quality_issues に記録。"""
    logger.warning(f"[reconcile] {check_name}: {detail}")
    if dry_run:
        return
    supabase_upsert(
        "data_quality_issues",
        {
            "check_name": check_name,
            "ticker": ticker,
            "detail": detail[:2000],
            "severity": severity,
            "status": "open",
        },
        config=config,
    )


def check_stuck_jobs(config: dict, dry_run: bool) -> int:
    """running が1時間以上続いている job_queue を検出し、pending に戻す。"""
    cutoff = (datetime.now(JST) - timedelta(hours=1)).isoformat()
    rows = supabase_select(
        "job_queue",
        params={
            "status": "eq.running",
            "started_at": f"lt.{cutoff}",
            "select": "id,job_type,target_id,started_at,attempts",
        },
        config=config,
    )
    requeued = 0
    for row in rows:
        attempts = row.get("attempts", 0)
        _record_issue(
            "stuck_job",
            f"job_queue id={row['id']} type={row.get('job_type')} "
            f"target={row.get('target_id')} started={row.get('started_at')}",
            severity="error",
            config=config,
            dry_run=dry_run,
        )
        # retry_count <= 3 なら pending に戻す
        if attempts <= 3 and not dry_run:
            try:
                supabase_update(
                    "job_queue",
                    {"status": "pending", "started_at": None, "finished_at": None},
                    params={"id": f"eq.{row['id']}"},
                    config=config,
                )
                requeued += 1
                logger.info(
                    f"[reconcile] stuck job requeued: id={row['id']} "
                    f"attempts={attempts}"
                )
            except Exception as e:
                logger.error(f"[reconcile] stuck job requeue failed: {e}")
        elif attempts > 3:
            logger.warning(
                f"[reconcile] stuck job id={row['id']} exceeded max retries "
                f"(attempts={attempts}), needs manual review"
            )
    if requeued:
        logger.info(f"[reconcile] requeued {requeued} stuck jobs")
    return len(rows)


def check_failed_jobs(config: dict, dry_run: bool) -> int:
    """failed ジョブを検出し、retry_count <= 3 なら pending に戻す。"""
    rows = supabase_select(
        "job_queue",
        params={
            "status": "eq.failed",
            "select": "id,job_type,target_id,error_message,attempts,finished_at",
            "order": "created_at.desc",
            "limit": "100",
        },
        config=config,
    )
    requeued = 0
    permanent_failures = 0
    for row in rows:
        attempts = row.get("attempts", 0)
        if attempts <= 3:
            if not dry_run:
                try:
                    supabase_update(
                        "job_queue",
                        {
                            "status": "pending",
                            "started_at": None,
                            "finished_at": None,
                            "error_message": None,
                        },
                        params={"id": f"eq.{row['id']}"},
                        config=config,
                    )
                    requeued += 1
                    logger.info(
                        f"[reconcile] failed job requeued: id={row['id']} "
                        f"type={row.get('job_type')} target={row.get('target_id')} "
                        f"attempts={attempts}"
                    )
                except Exception as e:
                    logger.error(f"[reconcile] failed job requeue failed: {e}")
            else:
                requeued += 1  # dry-run でもカウント
        else:
            permanent_failures += 1
            _record_issue(
                "permanent_failure",
                f"job_queue id={row['id']} type={row.get('job_type')} "
                f"target={row.get('target_id')} attempts={attempts} "
                f"error={row.get('error_message', '')[:200]}",
                severity="error",
                config=config,
                dry_run=dry_run,
            )
    if requeued:
        logger.info(
            f"[reconcile] requeued {requeued} failed jobs "
            f"(permanent_failures={permanent_failures})"
        )
    return len(rows)


def check_rebuild_backlog(config: dict, dry_run: bool) -> int:
    """6時間以上前の pending rebuild を検出。"""
    cutoff = (datetime.now(JST) - timedelta(hours=6)).isoformat()
    rows = supabase_select(
        "rebuild_queue",
        params={
            "status": "eq.pending",
            "created_at": f"lt.{cutoff}",
            "select": "id,ticker,created_at",
        },
        config=config,
    )
    if rows:
        tickers = [r.get("ticker", "?") for r in rows]
        _record_issue(
            "rebuild_backlog",
            f"{len(rows)} pending rebuilds older than 6h: {tickers[:10]}",
            severity="warn",
            config=config,
            dry_run=dry_run,
        )
    return len(rows)


def check_financials_duplicates(config: dict, dry_run: bool) -> int:
    """financials で (ticker, period, quarter) が重複する行を検出。"""
    # Supabase REST では GROUP BY が使えないため、
    # 最大 2000 行取得してアプリ側で checker
    rows = supabase_select(
        "financials",
        params={
            "select": "ticker,period,quarter",
            "limit": "2000",
        },
        config=config,
    )
    seen: dict[tuple, int] = {}
    for row in rows:
        key = (row.get("ticker"), row.get("period"), row.get("quarter"))
        seen[key] = seen.get(key, 0) + 1
    dups = {k: v for k, v in seen.items() if v > 1}
    for key, count in list(dups.items())[:10]:
        _record_issue(
            "financials_duplicate",
            f"ticker={key[0]} period={key[1]} quarter={key[2]} count={count}",
            ticker=key[0],
            severity="error",
            config=config,
            dry_run=dry_run,
        )
    return len(dups)


def check_quarantine_spike(config: dict, dry_run: bool) -> int:
    """当日の quarantine 件数が異常に多くないかチェック。"""
    today = datetime.now(JST).strftime("%Y-%m-%d")
    rows = supabase_select(
        "quarantine_items",
        params={
            "created_at": f"gte.{today}T00:00:00",
            "select": "id",
            "limit": "200",
        },
        config=config,
    )
    count = len(rows)
    if count >= 20:
        _record_issue(
            "quarantine_spike",
            f"Today's quarantine count: {count} (threshold: 20)",
            severity="warn" if count < 50 else "error",
            config=config,
            dry_run=dry_run,
        )
    return count


def run(*, dry_run: bool = False) -> dict:
    """daily_reconcile メイン。"""
    load_env(_PROJECT_ROOT)
    config = get_supabase_config()

    results = {}
    issues_total = 0

    logger.info("[reconcile] starting daily reconcile checks")

    # 1. stuck jobs 検出 + 自動再投入
    n = check_stuck_jobs(config, dry_run)
    results["stuck_jobs"] = n
    issues_total += n

    # 2. failed jobs 検出 + 自動再投入
    n = check_failed_jobs(config, dry_run)
    results["failed_jobs"] = n
    issues_total += n

    # 3. rebuild backlog
    n = check_rebuild_backlog(config, dry_run)
    results["rebuild_backlog"] = n
    issues_total += n

    # 4. financials duplicates
    n = check_financials_duplicates(config, dry_run)
    results["financials_duplicates"] = n
    issues_total += n

    # 5. quarantine spike
    n = check_quarantine_spike(config, dry_run)
    results["quarantine_today"] = n

    logger.info(f"[reconcile] done: issues_total={issues_total} checks={results}")

    return {
        "status": "done",
        "issues_total": issues_total,
        "checks": results,
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Daily reconcile checks")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = run(dry_run=args.dry_run)
    print(f"\nReconcile: {result['status']} issues={result['issues_total']}")
