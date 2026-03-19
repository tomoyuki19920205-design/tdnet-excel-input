#!/usr/bin/env python3
# ============================================================
# retry_failed_jobs.py — failed/quarantined ジョブの再投入
# ============================================================
"""
失敗ジョブを pending に戻して再実行する。

Usage:
    python tools/retry_failed_jobs.py
    python tools/retry_failed_jobs.py --dry-run
    python tools/retry_failed_jobs.py --target-id 6750    # 特定 target
    python tools/retry_failed_jobs.py --max-attempts 5
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

from lib.pipeline.db import load_env, get_supabase_config, supabase_select, supabase_update

logger = logging.getLogger("pipeline.retry")

DEFAULT_MAX_ATTEMPTS = 3


def run(
    *,
    dry_run: bool = False,
    target_id: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict:
    """failed/quarantined ジョブを pending に戻す。"""
    load_env(_PROJECT_ROOT)
    config = get_supabase_config()

    params: dict[str, str] = {
        "status": "in.(failed,quarantined)",
        "select": "id,job_type,target_type,target_id,attempts,error_message",
        "order": "created_at.desc",
        "limit": "100",
    }
    if target_id:
        params["target_id"] = f"eq.{target_id}"

    rows = supabase_select("job_queue", params=params, config=config)

    retried = 0
    skipped = 0
    over_limit = 0

    for row in rows:
        attempts = row.get("attempts", 0)
        if attempts >= max_attempts:
            over_limit += 1
            logger.info(
                f"[retry] skip (over limit): id={row['id']} "
                f"target={row.get('target_id')} attempts={attempts}/{max_attempts}"
            )
            continue

        if dry_run:
            retried += 1
            logger.info(
                f"[retry] would retry: id={row['id']} "
                f"type={row.get('job_type')} target={row.get('target_id')}"
            )
            continue

        ok = supabase_update(
            "job_queue",
            {"status": "pending", "error_message": None, "started_at": None, "finished_at": None},
            params={"id": f"eq.{row['id']}"},
            config=config,
        )
        if ok:
            retried += 1
            logger.info(
                f"[retry] retried: id={row['id']} "
                f"type={row.get('job_type')} target={row.get('target_id')} "
                f"attempts={attempts}"
            )
        else:
            skipped += 1

    logger.info(f"[retry] done: retried={retried} skipped={skipped} over_limit={over_limit}")

    return {
        "status": "done",
        "retried": retried,
        "skipped": skipped,
        "over_limit": over_limit,
        "total_candidates": len(rows),
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Retry failed jobs")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--target-id", type=str)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    args = parser.parse_args()
    result = run(dry_run=args.dry_run, target_id=args.target_id, max_attempts=args.max_attempts)
    print(f"\nRetry: retried={result['retried']} over_limit={result['over_limit']}")
