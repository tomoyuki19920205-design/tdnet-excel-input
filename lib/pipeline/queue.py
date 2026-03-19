"""lib/pipeline/queue.py -- job_queue / rebuild_queue 操作"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from .db import supabase_select, supabase_upsert, supabase_update, get_supabase_config

logger = logging.getLogger("pipeline.queue")
JST = timezone(timedelta(hours=9))


# ============================================================
# job_queue
# ============================================================

def enqueue_job(
    job_type: str,
    target_type: str,
    target_id: str,
    *,
    payload: dict | None = None,
    priority: int = 5,
    config: dict | None = None,
) -> dict | None:
    """job_queue に pending ジョブを追加 (重複防止付き)。

    同一 (job_type, target_type, target_id) で pending/running が
    存在する場合は追加しない。
    """
    cfg = config or get_supabase_config()

    # 重複チェック
    existing = supabase_select(
        "job_queue",
        params={
            "job_type": f"eq.{job_type}",
            "target_type": f"eq.{target_type}",
            "target_id": f"eq.{target_id}",
            "status": "in.(pending,running)",
            "select": "id,status",
            "limit": "1",
        },
        config=cfg,
    )
    if existing:
        logger.debug(
            f"[queue] job already exists: {job_type}/{target_type}/{target_id} "
            f"status={existing[0].get('status')}"
        )
        return None

    row = {
        "job_type": job_type,
        "target_type": target_type,
        "target_id": target_id,
        "payload_json": payload,
        "priority": priority,
        "status": "pending",
    }
    result = supabase_upsert("job_queue", row, config=cfg)
    if result["ok"]:
        logger.info(f"[queue] enqueued job: {job_type}/{target_type}/{target_id}")
    return result


def take_pending_jobs(
    job_type: str,
    *,
    limit: int = 50,
    config: dict | None = None,
) -> list[dict]:
    """pending ジョブを取得して running に変更。"""
    cfg = config or get_supabase_config()
    jobs = supabase_select(
        "job_queue",
        params={
            "job_type": f"eq.{job_type}",
            "status": "eq.pending",
            "select": "*",
            "order": "priority.asc,created_at.asc",
            "limit": str(limit),
        },
        config=cfg,
    )
    now = datetime.now(JST).isoformat()
    for job in jobs:
        supabase_update(
            "job_queue",
            {"status": "running", "started_at": now, "attempts": job.get("attempts", 0) + 1},
            params={"id": f"eq.{job['id']}"},
            config=cfg,
        )
    return jobs


def complete_job(
    job_id: int,
    status: str = "done",
    *,
    error_message: str | None = None,
    config: dict | None = None,
) -> None:
    """ジョブを完了/失敗に更新。"""
    cfg = config or get_supabase_config()
    now = datetime.now(JST).isoformat()
    payload: dict[str, Any] = {"status": status, "finished_at": now}
    if error_message:
        payload["error_message"] = error_message[:2000]
    supabase_update(
        "job_queue",
        payload,
        params={"id": f"eq.{job_id}"},
        config=cfg,
    )


# ============================================================
# rebuild_queue
# ============================================================

def enqueue_rebuild(
    ticker: str,
    reason: str = "new_filing",
    *,
    source_job_id: int | None = None,
    config: dict | None = None,
) -> dict | None:
    """rebuild_queue に ticker を追加 (重複防止付き)。

    同一 ticker で pending/running が存在する場合は追加しない。
    """
    cfg = config or get_supabase_config()

    existing = supabase_select(
        "rebuild_queue",
        params={
            "ticker": f"eq.{ticker}",
            "status": "in.(pending,running)",
            "select": "id,status",
            "limit": "1",
        },
        config=cfg,
    )
    if existing:
        logger.debug(f"[rebuild] already pending: {ticker}")
        return None

    row: dict[str, Any] = {
        "ticker": ticker,
        "reason": reason,
        "status": "pending",
    }
    if source_job_id:
        row["source_job_id"] = source_job_id
    result = supabase_upsert("rebuild_queue", row, config=cfg)
    if result["ok"]:
        logger.info(f"[rebuild] enqueued: {ticker} ({reason})")
    return result


def take_pending_rebuilds(
    *,
    limit: int = 100,
    config: dict | None = None,
) -> list[dict]:
    """pending rebuild を取得して running に変更。"""
    cfg = config or get_supabase_config()
    rows = supabase_select(
        "rebuild_queue",
        params={
            "status": "eq.pending",
            "select": "*",
            "order": "created_at.asc",
            "limit": str(limit),
        },
        config=cfg,
    )
    now = datetime.now(JST).isoformat()
    for row in rows:
        ok = supabase_update(
            "rebuild_queue",
            {"status": "running", "started_at": now},
            params={"id": f"eq.{row['id']}"},
            config=cfg,
        )
        if not ok:
            logger.warning(
                f"[rebuild] failed to mark running: "
                f"id={row['id']} ticker={row.get('ticker')}"
            )
    return rows


def complete_rebuild(
    rebuild_id: int,
    status: str = "done",
    *,
    config: dict | None = None,
) -> bool:
    """rebuild を完了/失敗に更新。

    Returns:
        True if update succeeded.
    """
    cfg = config or get_supabase_config()
    now = datetime.now(JST).isoformat()
    ok = supabase_update(
        "rebuild_queue",
        {"status": status, "finished_at": now},
        params={"id": f"eq.{rebuild_id}"},
        config=cfg,
    )
    if not ok:
        logger.warning(
            f"[rebuild] queue update FAILED: id={rebuild_id} "
            f"target_status={status}"
        )
    else:
        logger.info(
            f"[rebuild] queue updated: id={rebuild_id} → {status}"
        )
    return ok
