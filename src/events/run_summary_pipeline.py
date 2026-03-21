#!/usr/bin/env python3
"""run_summary_pipeline.py — AI要約パイプライン CLI

Usage:
    python -m src.events.run_summary_pipeline
    python -m src.events.run_summary_pipeline --dry-run
    python -m src.events.run_summary_pipeline --date 2026-03-20
    python -m src.events.run_summary_pipeline --include-low
    python -m src.events.run_summary_pipeline --cost-report
    python -m src.events.run_summary_pipeline --cost-report --date 2026-03-20

dry-run モード:
    - OpenAI API 呼び出し: なし (課金なし)
    - DB 書き込み: なし (メモリDB使用)
    - Discord 送信: なし (ログ出力のみ)

必須環境変数:
    OPENAI_API_KEY     : OpenAI APIキー (本番実行時必須)
    DISCORD_WEBHOOK_URL: Discord Webhook URL (通知時必須)
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

from src.events.env_loader import load_project_env, require_env, get_project_root
from src.events.common_models import DocumentMeta
from src.events.summary_pipeline import run_summary_pipeline
from src.events.summary_storage import ensure_summary_tables
from src.events.summary_cost_tracker import print_cost_report

logger = logging.getLogger("summary_pipeline")


def _get_db_path() -> str:
    return str(get_project_root() / "decision_db.db")


def _get_webhook_url() -> str:
    return os.environ.get("DISCORD_WEBHOOK_URL", "")


def _fetch_docs_for_date(target_date: str | None = None) -> list[DocumentMeta]:
    """指定日のTDNET開示を DocumentMeta リストとして取得"""
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
        ))
    return docs


def _run_cost_report(target_date: str | None = None):
    """コストレポートを表示"""
    db_path = _get_db_path()
    if not os.path.exists(db_path):
        print("[INFO] DB not found")
        return

    conn = sqlite3.connect(db_path)
    try:
        ensure_summary_tables(conn)
        print_cost_report(conn, target_date)
    finally:
        conn.close()


def main():
    if sys.stdout and hasattr(sys.stdout, "encoding"):
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace",
            )

    load_project_env()

    parser = argparse.ArgumentParser(
        description="TDNET AI要約パイプライン CLI",
    )
    parser.add_argument("--date", type=str, default=None, help="対象日付 (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true",
                        help="API呼び出しなし/DB書き込みなし/Discord送信なし (ログ出力のみ)")
    parser.add_argument("--include-low", action="store_true", help="low優先度も処理する")
    parser.add_argument("--cost-report", action="store_true", help="コストレポート表示")
    parser.add_argument("--model", type=str, default="", help="使用モデル (デフォルト: gpt-5.4-mini)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # コストレポートモード
    if args.cost_report:
        _run_cost_report(args.date)
        sys.exit(0)

    # OPENAI_API_KEY チェック (dry-run 以外は必須)
    if not args.dry_run:
        require_env("OPENAI_API_KEY", purpose="OpenAI Responses API による AI要約生成")

    # 文書取得
    target_date = args.date
    print(f"[SUMMARY] Fetching documents for date={target_date or 'today'}")
    docs = _fetch_docs_for_date(target_date)
    print(f"[SUMMARY] {len(docs)} documents fetched")

    if not docs:
        print("[SUMMARY] No documents to process")
        sys.exit(0)

    # AI要約パイプライン実行
    result = run_summary_pipeline(
        docs=docs,
        db_path=_get_db_path(),
        dry_run=args.dry_run,
        target_date=target_date,
        skip_low=not args.include_low,
        webhook_url="" if args.dry_run else _get_webhook_url(),
        model=args.model,
    )

    # 結果表示
    print()
    print("=" * 55)
    print("  AI SUMMARY PIPELINE RESULT")
    print("=" * 55)
    # V2: 決算短信要約
    print(f"  [V2 決算短信]")
    print(f"    earnings_generated  : {result.earnings_generated}")
    print(f"    earnings_saved      : {result.earnings_saved}")
    print(f"    earnings_notified   : {result.earnings_notified}")
    print(f"    earnings_filtered   : {result.earnings_filtered}")
    print(f"    earnings_no_yoy     : {result.earnings_no_yoy}")
    print(f"    earnings_duplicate  : {result.earnings_already_exists}")
    # V1: AI要約
    print(f"  [V1 AI要約]")
    print(f"    jobs_created        : {result.jobs_created}")
    print(f"    jobs_skipped        : {result.jobs_skipped}")
    print(f"    jobs_processed      : {result.jobs_processed}")
    print(f"    jobs_succeeded      : {result.jobs_succeeded}")
    print(f"    jobs_failed         : {result.jobs_failed}")
    print(f"    notifications       : {result.notifications_sent}")
    if args.dry_run:
        print("  [DRY-RUN MODE]")
    print("=" * 55)

    if result.errors:
        print("\n  [ERRORS]")
        for err in result.errors[:10]:
            print(f"    - {err}")

    print()


if __name__ == "__main__":
    main()
