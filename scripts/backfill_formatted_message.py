#!/usr/bin/env python3
"""backfill_formatted_message.py — 既存データの display_title / display_summary / formatted_message を更新

Usage:
    python scripts/backfill_formatted_message.py          # dry-run
    python scripts/backfill_formatted_message.py --apply   # 実適用
"""
from __future__ import annotations

import json
import os
import sys
import io
from pathlib import Path

# Windows の CP932 で emoji が出力できない問題を回避
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# プロジェクトルートを path に追加
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# .env 読み込み
_env_file = PROJECT_ROOT / ".env"
if _env_file.exists():
    for line in _env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from src.events.common_models import EventRecord, EventType
from src.events.common_notify import (
    build_display_title,
    build_display_summary,
    build_formatted_message,
)


def _get_supabase():
    try:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not url or not key:
            print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set")
            sys.exit(1)
        return create_client(url, key)
    except ImportError:
        print("ERROR: pip install supabase")
        sys.exit(1)


def _row_to_event_record(row: dict) -> EventRecord:
    """DB行 → EventRecord 再構築"""
    raw_payload = row.get("raw_payload") or {}
    if isinstance(raw_payload, str):
        try:
            raw_payload = json.loads(raw_payload)
        except (json.JSONDecodeError, TypeError):
            raw_payload = {}

    # original_event_type を復元 (event_type は display_category に正規化済み)
    original_event_type = raw_payload.get("original_event_type", "")
    # display_category → EventType マッピング
    event_type_map = {
        "buyback": EventType.BUYBACK,
        "forecast": EventType.FORECAST_REVISION,
        "dividend": EventType.DIVIDEND_REVISION,
    }
    event_type = event_type_map.get(row.get("event_type", ""), original_event_type)

    extracted = raw_payload.get("extracted", {})

    return EventRecord(
        ticker=row.get("ticker", ""),
        company_name=row.get("company_name", ""),
        event_type=event_type,
        subtype=row.get("event_subtype", ""),
        title=row.get("headline", ""),
        extracted_payload_json=json.dumps(extracted, ensure_ascii=False) if extracted else "",
    )


def main():
    apply = "--apply" in sys.argv
    client = _get_supabase()

    print("Fetching all active events...")
    resp = client.table("tdnet_events").select("*").eq("status", "active").execute()
    rows = resp.data or []
    print(f"  Found {len(rows)} events")

    updated = 0
    for row in rows:
        ev = _row_to_event_record(row)
        new_title = build_display_title(ev)
        new_summary = build_display_summary(ev)
        new_formatted = build_formatted_message(ev)

        old_title = row.get("display_title", "")
        old_formatted = row.get("formatted_message", "")

        if new_title == old_title and new_formatted == old_formatted:
            continue  # 変更なし

        updated += 1
        if not apply:
            print(f"  [DRY-RUN] {row['ticker']} {row.get('company_name', '')}")
            print(f"    OLD title: {old_title[:80]}")
            print(f"    NEW title: {new_title[:80]}")
            print(f"    NEW fmt:   {new_formatted[:80]}")
            print()
        else:
            client.table("tdnet_events").update({
                "display_title": new_title,
                "display_summary": new_summary,
                "formatted_message": new_formatted,
            }).eq("id", row["id"]).execute()

    mode = "APPLIED" if apply else "DRY-RUN"
    print(f"\n[{mode}] {updated}/{len(rows)} events updated")
    if not apply and updated > 0:
        print("実適用するには: python scripts/backfill_formatted_message.py --apply")


if __name__ == "__main__":
    main()
