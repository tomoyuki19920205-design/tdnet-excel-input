#!/usr/bin/env python3
"""notify_discord_events.py — 未通知イベントの Discord 送信 CLI

Usage:
    python -m src.events.notify_discord_events --since 2026-03-20
    python -m src.events.notify_discord_events --dry-run
    python -m src.events.notify_discord_events --event-type buyback
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.events.common_storage import ensure_events_table, get_unnotified_events, mark_notified, mark_filtered
from src.events.common_notify import send_event_discord
from src.events.notify_rules import filter_and_sort_events
from src.events.tdnet_event_store import update_discord_sent_at_supabase

logger = logging.getLogger("event_notify")


def _load_env():
    env_path = Path(_PROJECT_ROOT) / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


def main():
    if sys.stdout and hasattr(sys.stdout, "encoding"):
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace",
            )

    _load_env()

    parser = argparse.ArgumentParser(description="未通知イベントの Discord 送信")
    parser.add_argument("--since", type=str, default=None, help="対象開始日時")
    parser.add_argument("--event-type", type=str, default=None, help="イベント種別")
    parser.add_argument("--dry-run", action="store_true", help="送信せずプレビュー")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")

    db_path = os.path.join(_PROJECT_ROOT, "decision_db.db")
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")

    if not webhook_url and not args.dry_run:
        print("[ERROR] DISCORD_WEBHOOK_URL not set")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_events_table(conn)
        events = get_unnotified_events(conn, event_type=args.event_type, since=args.since)
        print(f"[NOTIFY] {len(events)} unnotified events (before filter)")

        # フィルタ + ソート
        notifiable, filtered_events = filter_and_sort_events(events)
        print(f"[NOTIFY] {len(notifiable)} notifiable, {len(filtered_events)} filtered")

        # 非通知対象を filtered ステータスに更新
        if not args.dry_run:
            for ev in filtered_events:
                mark_filtered(conn, ev.event_id)

        sent = 0
        supabase_updated = 0
        for ev in notifiable:
            ok = send_event_discord(webhook_url, ev, dry_run=args.dry_run)
            if ok and not args.dry_run:
                mark_notified(conn, ev.event_id)
                # Supabase の discord_sent_at も更新 (best-effort)
                sb_ok = update_discord_sent_at_supabase(ev, dry_run=False)
                if sb_ok:
                    supabase_updated += 1
                sent += 1
            elif ok:
                sent += 1

        print(f"[NOTIFY] sent={sent} supabase_updated={supabase_updated}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
