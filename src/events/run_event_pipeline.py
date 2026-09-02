#!/usr/bin/env python3
"""run_event_pipeline.py — イベント検知 CLI

Usage:
    python -m src.events.run_event_pipeline --date 2026-03-20
    python -m src.events.run_event_pipeline --event-type buyback
    python -m src.events.run_event_pipeline --dry-run
    python -m src.events.run_event_pipeline --doc-id XXXXX
    python -m src.events.run_event_pipeline --list
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.events.common_models import DocumentMeta, EventType
from src.events.common_storage import ensure_events_table, list_events
from src.events.event_pipeline import process_documents

logger = logging.getLogger("event_pipeline")

JST = timezone(timedelta(hours=9))


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


def _get_db_path() -> str:
    return os.path.join(_PROJECT_ROOT, "decision_db.db")


def _get_webhook_url() -> str:
    return os.environ.get("DISCORD_WEBHOOK_URL", "")


def _fetch_docs_for_date(target_date: str | None = None) -> list[DocumentMeta]:
    """指定日のTDNET開示をDocumentMetaリストとして取得"""
    from src.fetcher import fetch_new_disclosures

    items = fetch_new_disclosures(target_date=target_date)
    docs = []
    for item in items:
        docs.append(DocumentMeta(
            doc_id=item.disclosure_id,
            ticker=item.ticker,
            company_name=item.company_name,
            title=item.title,
            disclosure_datetime=item.published_at,
            doc_url=item.doc_url,
            source_doc_id=item.source_doc_id or item.disclosure_id,
            link_validated=getattr(item, "link_validated", None),
        ))
    return docs


def _fetch_single_doc(doc_id: str) -> list[DocumentMeta]:
    """doc_id 指定で1文書を処理用に取得"""
    # DB から source 情報を取得するか、最小限のメタで返す
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM events WHERE event_id = ? OR source_doc_id = ? LIMIT 1",
            (doc_id, doc_id),
        ).fetchone()
        if row:
            return [DocumentMeta(
                doc_id=row["source_doc_id"],
                ticker=row["ticker"] or "",
                company_name=row["company_name"] or "",
                title=row["title"] or "",
                disclosure_datetime=row["disclosure_datetime"] or "",
                doc_url=dict(row).get("doc_url", "") or "",
            )]
    except Exception:
        pass
    finally:
        conn.close()

    # 見つからない場合は最小限で返す
    return [DocumentMeta(doc_id=doc_id, ticker="", title="")]


def _print_list(event_type: str | None = None, since: str | None = None):
    db_path = _get_db_path()
    if not os.path.exists(db_path):
        print("[INFO] DB not found")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_events_table(conn)
        events = list_events(conn, event_type=event_type, since=since)
        if not events:
            print("[INFO] No events found")
            return

        print(f"{'ID':12s} {'Type':20s} {'Subtype':15s} {'Ticker':8s} {'Imp':4s} {'Status':10s} {'First Seen':20s} {'Title':40s}")
        print("-" * 130)
        for ev in events:
            print(
                f"{ev.event_id[:12]:12s} {ev.event_type:20s} {ev.subtype:15s} "
                f"{ev.ticker:8s} {ev.importance:4d} {ev.status:10s} "
                f"{(ev.first_seen_at or '')[:20]:20s} {(ev.title or '')[:40]:40s}"
            )
    finally:
        conn.close()


def main():
    if sys.stdout and hasattr(sys.stdout, "encoding"):
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace",
            )

    _load_env()

    parser = argparse.ArgumentParser(
        description="TDNET イベント検知パイプライン CLI",
    )
    parser.add_argument("--date", type=str, default=None, help="対象日付 (YYYY-MM-DD)")
    parser.add_argument("--event-type", type=str, default=None,
                        choices=[
                            "buyback", "forecast_revision", "dividend_revision",
                            "earnings_material", "monthly_update", "management_strategy",
                            "capital_action",
                        ],
                        help="処理するイベント種別")
    parser.add_argument("--dry-run", action="store_true", help="DB保存・通知なし")
    parser.add_argument("--doc-id", type=str, default=None, help="特定文書IDを再処理")
    parser.add_argument("--list", action="store_true", help="検知済みイベント一覧")
    parser.add_argument("--since", type=str, default=None, help="一覧表示の開始日時")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # list モード
    if args.list:
        _print_list(event_type=args.event_type, since=args.since)
        sys.exit(0)

    # 文書取得
    if args.doc_id:
        docs = _fetch_single_doc(args.doc_id)
        print(f"[EVENT] Re-processing doc_id={args.doc_id}")
    else:
        target_date = args.date
        print(f"[EVENT] Fetching documents for date={target_date or 'today'}")
        docs = _fetch_docs_for_date(target_date)
        print(f"[EVENT] {len(docs)} documents fetched")

    if not docs:
        print("[EVENT] No documents to process")
        sys.exit(0)

    event_types = {args.event_type} if args.event_type else None

    result = process_documents(
        docs=docs,
        db_path=_get_db_path(),
        dry_run=args.dry_run,
        event_types=event_types,
        webhook_url="" if args.dry_run else _get_webhook_url(),
    )

    print()
    print("=" * 55)
    print("  EVENT PIPELINE RESULT")
    print("=" * 55)
    print(f"  processed     : {result.processed}")
    print(f"  detected      : {result.detected}")
    print(f"  saved         : {result.saved}")
    print(f"  filtered      : {result.filtered}")
    print(f"  notified      : {result.notified}")
    print(f"  errors        : {result.errors}")
    print(f"  skipped       : {result.skipped}")
    if args.dry_run:
        print("  [DRY-RUN MODE]")

    # 内訳
    type_counts: dict[str, dict[str, int]] = {}
    for d in result.details:
        etype = d.get("event_type", "unknown")
        action = d.get("action", "unknown")
        if etype not in type_counts:
            type_counts[etype] = {}
        type_counts[etype][action] = type_counts[etype].get(action, 0) + 1

    if type_counts:
        print()
        print("  --- breakdown ---")
        for etype, actions in sorted(type_counts.items()):
            parts = ", ".join(f"{a}={c}" for a, c in sorted(actions.items()))
            print(f"  {etype:25s}: {parts}")

    print("=" * 55)
    print()

    # 詳細
    for d in result.details:
        action = d.get("action", "?")
        etype = d.get("event_type", "?")
        sub = d.get("subtype", "")
        eid = d.get("event_id", "")[:12]
        ticker = d.get("ticker", "")
        summary = d.get("summary", "")
        print(f"  [{action:10s}] {etype:20s} {sub:15s} {eid} {ticker} {summary}")


if __name__ == "__main__":
    main()
