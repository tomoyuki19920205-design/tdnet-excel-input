"""lib/pipeline/logging_utils.py -- pipeline_runs 書き込み + 同時実行防止

すべてのサブコマンドは PipelineRun context を通して pipeline_runs にログを残す。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from .db import supabase_insert, supabase_upsert, supabase_update, supabase_select, get_supabase_write_config, get_supabase_read_config

logger = logging.getLogger("pipeline.log")
JST = timezone(timedelta(hours=9))


class PipelineRun:
    """pipeline_runs テーブルへの書き込みをラップするコンテキストマネージャ。

    すべてのサブコマンド (ingest/process/rebuild/notify/reconcile/retry/backfill)
    および全体実行 (full_pipeline) で使用する。

    Usage::

        with PipelineRun("rebuild", trigger_type="manual") as run:
            # ... do work ...
            run.update(processed=10, success=8, failed=2)
        # 自動的に finished_at, duration_sec, status=done が書き込まれる
        # 例外時は status=failed が書き込まれる
    """

    def __init__(
        self,
        job_type: str,
        *,
        trigger_type: str = "manual",
        config: dict | None = None,
    ) -> None:
        self.job_type = job_type
        self.trigger_type = trigger_type
        self.config = config or get_supabase_write_config() or {}
        if not self.config:
            logger.warning("[pipeline_run] write config unavailable (service role key missing)")
        self.run_id: int | None = None
        self.t0 = 0.0
        self._stats: dict[str, Any] = {
            "processed_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "quarantined_count": 0,
            "skipped_count": 0,
        }

    def __enter__(self) -> "PipelineRun":
        self.t0 = time.monotonic()
        now = datetime.now(JST).isoformat()
        row = {
            "job_type": self.job_type,
            "trigger_type": self.trigger_type,
            "started_at": now,
            "status": "running",
        }
        # INSERT with return=representation で run_id を直接取得
        result = supabase_insert("pipeline_runs", row, config=self.config)
        if result["ok"] and result["rows"]:
            self.run_id = result["rows"][0].get("id")
            logger.info(
                f"[pipeline_run] started: {self.job_type} "
                f"(id={self.run_id}, trigger={self.trigger_type})"
            )
        else:
            # INSERT 失敗 — warning を出すが処理は続行
            logger.warning(
                f"[pipeline_run] INSERT pipeline_runs FAILED: "
                f"{self.job_type} error={result.get('error')}"
            )
            logger.info(
                f"[pipeline_run] started: {self.job_type} "
                f"(id=NONE — logging degraded)"
            )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        elapsed = time.monotonic() - self.t0
        status = "failed" if exc_type else "done"
        now = datetime.now(JST).isoformat()
        message = str(exc_val)[:1000] if exc_val else None

        if self.run_id:
            ok = supabase_update(
                "pipeline_runs",
                {
                    "status": status,
                    "finished_at": now,
                    "duration_sec": round(elapsed, 2),
                    "message": message,
                    **self._stats,
                },
                params={"id": f"eq.{self.run_id}"},
                config=self.config,
            )
            if not ok:
                logger.warning(
                    f"[pipeline_run] UPDATE pipeline_runs FAILED: "
                    f"id={self.run_id} target_status={status}"
                )
        else:
            logger.warning(
                f"[pipeline_run] skipping UPDATE (no run_id): "
                f"{self.job_type} status={status}"
            )

        logger.info(
            f"[pipeline_run] finished: {self.job_type} "
            f"status={status} elapsed={elapsed:.1f}s "
            f"id={self.run_id} "
            f"processed={self._stats['processed_count']} "
            f"success={self._stats['success_count']} "
            f"failed={self._stats['failed_count']}"
        )
        return False  # 例外を再送出

    def update(self, **kwargs: int) -> None:
        """カウンタを更新。processed, success, failed, quarantined, skipped を指定可能。"""
        for key in ("processed", "success", "failed", "quarantined", "skipped"):
            if key in kwargs:
                self._stats[f"{key}_count"] = kwargs[key]

    def mark_done(self, **kwargs: int) -> None:
        """明示的に completed 状態にする (通常は __exit__ が自動処理)。"""
        self.update(**kwargs)

    def mark_failed(self, error: str = "", **kwargs: int) -> None:
        """明示的に failed 状態にする (通常は __exit__ が例外経由で自動処理)。"""
        self.update(**kwargs)
        # __exit__ で status=failed になるよう例外を投げる
        raise RuntimeError(error or "marked as failed")


def check_concurrent_run(
    job_type: str,
    *,
    max_age_hours: int = 1,
    config: dict | None = None,
) -> bool:
    """同一 job_type の running 行が存在するか (同時実行防止)。

    Returns:
        True if a concurrent run exists (should skip).
    """
    cfg = config or get_supabase_read_config()
    cutoff = datetime.now(JST) - timedelta(hours=max_age_hours)
    rows = supabase_select(
        "pipeline_runs",
        params={
            "job_type": f"eq.{job_type}",
            "status": "eq.running",
            "started_at": f"gt.{cutoff.isoformat()}",
            "select": "id,started_at",
            "limit": "1",
        },
        config=cfg,
    )
    if rows:
        logger.warning(
            f"[pipeline_run] concurrent run detected: {job_type} "
            f"(id={rows[0].get('id')}). Skipping."
        )
        return True
    return False


def cleanup_stale_runs(
    job_type: str | None = None,
    *,
    max_age_hours: int = 2,
    config: dict | None = None,
) -> int:
    """stale running 行を failed に落とす。起動時の衛生管理。

    Returns:
        クリーンアップした行数。
    """
    cfg = config or get_supabase_write_config()
    if not cfg:
        return 0
    cutoff = datetime.now(JST) - timedelta(hours=max_age_hours)
    params: dict[str, str] = {
        "status": "eq.running",
        "started_at": f"lt.{cutoff.isoformat()}",
        "select": "id,job_type,started_at",
    }
    if job_type:
        params["job_type"] = f"eq.{job_type}"

    read_cfg = config or get_supabase_read_config()
    rows = supabase_select("pipeline_runs", params=params, config=read_cfg)
    if not rows:
        return 0

    cleaned = 0
    for row in rows:
        ok = supabase_update(
            "pipeline_runs",
            {
                "status": "failed",
                "finished_at": datetime.now(JST).isoformat(),
                "message": f"auto-cleanup: stale>{max_age_hours}h",
            },
            params={"id": f"eq.{row['id']}"},
            config=cfg,
        )
        if ok:
            cleaned += 1

    logger.info(
        f"[pipeline_run] cleanup_stale_runs: {cleaned}/{len(rows)} rows cleaned "
        f"({job_type or 'all'})"
    )
    return cleaned


def check_ingest_running(
    *,
    max_age_minutes: int = 10,
    config: dict | None = None,
) -> int | None:
    """ingest が現在 running かどうかを確認する。

    Returns:
        running 中の ingest の run_id (int)。なければ None。
    """
    cfg = config or get_supabase_read_config()
    cutoff = datetime.now(JST) - timedelta(minutes=max_age_minutes)
    rows = supabase_select(
        "pipeline_runs",
        params={
            "job_type": "eq.ingest",
            "status": "eq.running",
            "started_at": f"gt.{cutoff.isoformat()}",
            "select": "id,started_at",
            "limit": "1",
        },
        config=cfg,
    )
    if rows:
        run_id = rows[0].get("id")
        logger.info(
            f"[pipeline_run] ingest running detected: id={run_id} "
            f"started={rows[0].get('started_at')}"
        )
        return run_id
    return None
