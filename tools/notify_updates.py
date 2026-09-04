#!/usr/bin/env python3
# ============================================================
# notify_updates.py — Discord 通知ラッパー
# ============================================================
"""
discord_alerts.run_alerts() をラップ。
失敗は warning 扱いで pipeline 全体を失敗にしない。
"""
from __future__ import annotations
from lib.runtime_paths import runtime_path

import json
import logging
import os
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger("pipeline.notify")

_TICKERS_FILE = os.path.join(_PROJECT_ROOT, "logs", "last_ingested_tickers.json")


def _load_env_value(key: str) -> str:
    val = os.environ.get(key, "")
    if val:
        return val
    env_path = os.path.join(_PROJECT_ROOT, ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip()
    return ""


def _load_ticker_items() -> list[dict]:
    """last_ingested_tickers.json からアラート対象を読み込む"""
    if not os.path.exists(str(runtime_path(_TICKERS_FILE))):
        return []
    try:
        with open(str(runtime_path(_TICKERS_FILE)), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def run(*, dry_run: bool = False) -> dict:
    """
    Discord 通知を実行。

    Returns:
        {"status": "success"|"skipped"|"error", ...}
    """
    logger.info("[notify_updates] start")

    items = _load_ticker_items()
    if not items:
        logger.info("[notify_updates] no ticker items to notify")
        return {"status": "skipped", "reason": "no_items"}

    webhook_url = _load_env_value("DISCORD_WEBHOOK_URL")
    supabase_url = _load_env_value("SUPABASE_URL")
    supabase_key = _load_env_value("SUPABASE_ANON_KEY")

    if not webhook_url:
        logger.warning("[notify_updates] DISCORD_WEBHOOK_URL not set")
        return {"status": "skipped", "reason": "no_webhook"}

    if not supabase_url or not supabase_key:
        logger.warning("[notify_updates] Supabase credentials missing")
        return {"status": "skipped", "reason": "no_supabase"}

    if dry_run:
        logger.info(
            f"[notify_updates] dry-run: would notify {len(items)} items"
        )
        return {"status": "dry_run", "items": len(items)}

    try:
        from tools.discord_alerts import run_alerts

        result = run_alerts(
            items=items,
            webhook_url=webhook_url,
            supabase_url=supabase_url,
            supabase_key=supabase_key,
        )
        logger.info(f"[notify_updates] done: {result}")
        return {"status": "success", **result}
    except Exception as e:
        logger.warning(f"[notify_updates] failed: {e}")
        return {"status": "error", "error": str(e)}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    run()
