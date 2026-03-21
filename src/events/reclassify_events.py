#!/usr/bin/env python3
"""reclassify_events.py — 既存 Supabase tdnet_events の event_type を再分類する

一回限りの backfill スクリプト。
既存レコードの event_type / headline を使って _normalize_display_category() で
再判定し、更新が必要なレコードのみ UPDATE する。

Usage:
    python -m src.events.reclassify_events           # dry-run (変更なし)
    python -m src.events.reclassify_events --apply    # 実際に更新
"""
from __future__ import annotations

import json
import logging
import os
import sys
from collections import Counter

# プロジェクトルートをパスに追加
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.events.tdnet_event_store import (
    _normalize_display_category,
    _EVENT_TYPE_TO_CATEGORY,
    _KEYWORD_RULES,
    DISPLAY_OTHER,
)
from src.events.common_models import EventRecord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("reclassify_events")


def _get_supabase():
    """Supabase client を初期化して返す。"""
    try:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not url or not key:
            logger.error("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set")
            return None
        return create_client(url, key)
    except ImportError:
        logger.error("supabase package not installed")
        return None


def reclassify(apply: bool = False) -> dict:
    """既存レコードを再分類する。

    Returns:
        {
            "total": int,
            "updated": int,
            "unchanged": int,
            "before": Counter,
            "after": Counter,
            "transitions": Counter,  # "old -> new" のカウント
        }
    """
    client = _get_supabase()
    if client is None:
        return {"error": "supabase_not_available"}

    # 全レコード取得
    resp = (
        client.table("tdnet_events")
        .select("id, event_type, event_subtype, headline, summary, raw_payload")
        .order("detected_at", desc=True)
        .limit(1000)
        .execute()
    )
    rows = resp.data or []
    logger.info(f"[RECLASSIFY] fetched {len(rows)} rows")

    before_counts: Counter = Counter()
    after_counts: Counter = Counter()
    transitions: Counter = Counter()
    updated = 0
    unchanged = 0

    for row in rows:
        old_type = row.get("event_type", "")
        headline = row.get("headline", "")
        summary = row.get("summary", "")

        # EventRecord を模擬して再判定
        mock_event = EventRecord(
            event_type=old_type,
            title=headline,
            summary_text=summary,
        )
        new_type = _normalize_display_category(mock_event)

        before_counts[old_type] += 1
        after_counts[new_type] += 1

        if old_type != new_type:
            transitions[f"{old_type} -> {new_type}"] += 1

            if apply:
                # raw_payload に original_event_type を保存
                raw_payload = row.get("raw_payload", {})
                if isinstance(raw_payload, str):
                    try:
                        raw_payload = json.loads(raw_payload)
                    except (json.JSONDecodeError, TypeError):
                        raw_payload = {}
                if isinstance(raw_payload, dict):
                    raw_payload["original_event_type"] = old_type
                else:
                    raw_payload = {"original_event_type": old_type}

                client.table("tdnet_events").update({
                    "event_type": new_type,
                    "raw_payload": json.dumps(raw_payload, ensure_ascii=False, default=str),
                }).eq("id", row["id"]).execute()

            updated += 1
            logger.info(
                f"  {'UPDATED' if apply else 'WOULD UPDATE'}: "
                f"id={row['id'][:8]}... {old_type} -> {new_type} "
                f"headline={headline[:50]}"
            )
        else:
            unchanged += 1

    result = {
        "total": len(rows),
        "updated": updated,
        "unchanged": unchanged,
        "before": dict(before_counts),
        "after": dict(after_counts),
        "transitions": dict(transitions),
    }

    # サマリー出力
    logger.info(f"\n{'='*60}")
    logger.info(f"[RECLASSIFY] {'APPLIED' if apply else 'DRY-RUN'} Summary:")
    logger.info(f"  Total rows: {len(rows)}")
    logger.info(f"  Updated: {updated}")
    logger.info(f"  Unchanged: {unchanged}")
    logger.info(f"\n  BEFORE distribution:")
    for k, v in sorted(before_counts.items(), key=lambda x: -x[1]):
        logger.info(f"    {k}: {v}")
    logger.info(f"\n  AFTER distribution:")
    for k, v in sorted(after_counts.items(), key=lambda x: -x[1]):
        logger.info(f"    {k}: {v}")
    if transitions:
        logger.info(f"\n  Transitions:")
        for k, v in sorted(transitions.items(), key=lambda x: -x[1]):
            logger.info(f"    {k}: {v}")
    logger.info(f"{'='*60}")

    return result


def main():
    apply = "--apply" in sys.argv
    if apply:
        logger.info("[RECLASSIFY] APPLY mode — will update Supabase records")
    else:
        logger.info("[RECLASSIFY] DRY-RUN mode — no changes will be made (use --apply to apply)")

    result = reclassify(apply=apply)
    if "error" in result:
        logger.error(f"[RECLASSIFY] Failed: {result['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
