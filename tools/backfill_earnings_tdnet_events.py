#!/usr/bin/env python3
"""backfill_earnings_tdnet_events.py — earnings_summaries → tdnet_events バックフィル

既存の earnings_summaries SQLite データを Supabase tdnet_events へ upsert する。
新規パイプライン追加前に蓄積されたデータ（5032 等）を Viewer に反映させるために使う。

Usage:
    python tools/backfill_earnings_tdnet_events.py --dry-run
    python tools/backfill_earnings_tdnet_events.py --ticker 5032
    python tools/backfill_earnings_tdnet_events.py --ticker 5032 --dry-run
    python tools/backfill_earnings_tdnet_events.py --limit 50
    python tools/backfill_earnings_tdnet_events.py  # 全件
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.runtime_paths import runtime_path
from lib.pipeline.db import load_env
from src.events.common_models import EventRecord
from src.events.tdnet_event_store import save_event_to_supabase
from src.events.earnings_production_pipeline import _compute_earnings_fingerprint

logger = logging.getLogger("backfill_earnings")
JST = timezone(timedelta(hours=9))


# ============================================================
# earnings_summaries 行 → EventRecord 変換
# ============================================================
def _row_to_event_record(row: dict) -> EventRecord:
    """earnings_summaries の1行を EventRecord に変換する。"""
    ticker = row.get("ticker", "")
    title = row.get("title", "")
    fiscal_year = row.get("fiscal_year", "")
    quarter = row.get("quarter", "")
    company_name = row.get("company_name", "")
    disclosure_date = row.get("disclosure_date", "")  # "YYYY-MM-DD"
    source_url = row.get("source_url", "")
    summary_full = row.get("summary_full", "")  # format_earnings_message() の出力
    summary_short = row.get("summary_short", "")
    fingerprint = row.get("fingerprint", "")

    # extracted payload 構築
    extracted: dict = {
        "ticker": ticker,
        "fiscal_year": fiscal_year,
        "quarter": quarter,
        "sales_current": row.get("sales_value"),
        "sales_yoy": row.get("sales_yoy"),
        "op_current": row.get("op_value"),
        "op_yoy": row.get("op_yoy"),
        "has_yoy": row.get("sales_yoy") is not None or row.get("op_yoy") is not None,
        "segments": [],
        "source_url": source_url,
        "xbrl_path": row.get("archive_path", ""),
    }

    # セグメント JSON 展開
    seg_json = row.get("segment_summary_json", "")
    if seg_json:
        try:
            extracted["segments"] = json.loads(seg_json)
        except (json.JSONDecodeError, TypeError):
            pass

    # ガイダンス
    g_sales = row.get("guidance_sales")
    g_op = row.get("guidance_op")
    if g_sales is not None or g_op is not None:
        extracted["guidance"] = {
            "sales_forecast": g_sales,
            "op_forecast": g_op,
            "eps_forecast": row.get("guidance_eps"),
            "sales_yoy": row.get("guidance_sales_yoy"),
            "op_yoy": row.get("guidance_op_yoy"),
            "eps_yoy": row.get("guidance_eps_yoy"),
        }

    raw_payload = {"title": title}

    # disclosure_datetime: disclosure_date → JST 09:00 補完
    # "YYYY-MM-DD" 形式のみ受け付ける（時刻文字列や空文字は除外）
    disclosure_dt = ""
    if disclosure_date and len(disclosure_date) >= 10 and disclosure_date[4:5] == "-" and disclosure_date[7:8] == "-":
        disclosure_dt = f"{disclosure_date[:10]}T09:00:00+09:00"

    # formatted_message: summary_full が最優先
    formatted_message = summary_full or summary_short or summary_short

    return EventRecord(
        source_doc_id=fingerprint,          # fingerprint を source_doc_id に代用
        ticker=ticker,
        company_name=company_name,
        disclosure_datetime=disclosure_dt,
        title=title,
        doc_url=source_url,
        event_type="earnings",
        subtype=quarter,                    # "FY" / "1Q" / "2Q" / "3Q"
        importance=60,
        summary_text=formatted_message,     # Viewer の formatted_message に使われる
        raw_payload_json=json.dumps(
            {"raw": raw_payload}, ensure_ascii=False
        ),
        extracted_payload_json=json.dumps(
            extracted, ensure_ascii=False, default=str
        ),
        fingerprint=fingerprint,
    )


# ============================================================
# メイン処理
# ============================================================
def run(
    *,
    ticker: str | None = None,
    limit: int = 0,
    dry_run: bool = False,
    db_path: str = "",
    since_days: int = 0,
) -> dict:
    """earnings_summaries → tdnet_events バックフィル実行。

    Returns:
        {"total": int, "inserted": int, "dedup_skipped": int, "errors": int}
    """
    if not db_path:
        db_path = str(runtime_path(Path(_PROJECT_ROOT) / "decision_db.db", code_root=Path(_PROJECT_ROOT)))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # クエリ構築
    where_parts = []
    params: list = []
    if ticker:
        where_parts.append("ticker = ?")
        params.append(ticker)
    if since_days > 0:
        cutoff = (datetime.now(JST) - timedelta(days=since_days)).isoformat()
        where_parts.append("created_at >= ?")
        params.append(cutoff)
        logger.info(f"[BACKFILL] since_days={since_days} cutoff(created_at)={cutoff}")

    where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    limit_clause = f"LIMIT {limit}" if limit > 0 else ""

    sql = f"""
        SELECT *
        FROM earnings_summaries
        {where_clause}
        ORDER BY disclosure_date DESC, created_at DESC
        {limit_clause}
    """

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    total = len(rows)
    counts = {"total": total, "inserted": 0, "dedup_skipped": 0, "errors": 0, "dry_run": 0}

    logger.info(
        f"[BACKFILL] start: total={total} ticker={ticker or 'all'} "
        f"limit={limit} dry_run={dry_run}"
    )

    if total == 0:
        logger.info("[BACKFILL] no records found.")
        return counts

    for row in rows:
        row_dict = dict(row)
        ticker_val = row_dict.get("ticker", "?")
        fp = row_dict.get("fingerprint", "?")[:12]

        try:
            record = _row_to_event_record(row_dict)
            result = save_event_to_supabase(record, dry_run=dry_run)
            action = result.get("action", "error")

            if action == "inserted":
                counts["inserted"] += 1
                logger.info(
                    f"[BACKFILL] INSERTED: ticker={ticker_val} "
                    f"quarter={row_dict.get('quarter')} fp={fp}... "
                    f"dedupe={result.get('dedupe_key', '')[:12]}..."
                )
            elif action == "dedup_skipped":
                counts["dedup_skipped"] += 1
                logger.debug(
                    f"[BACKFILL] SKIP(dedup): ticker={ticker_val} fp={fp}..."
                )
            elif action == "dry_run":
                counts["dry_run"] += 1
                logger.info(
                    f"[BACKFILL] DRY-RUN: ticker={ticker_val} "
                    f"quarter={row_dict.get('quarter')} "
                    f"title={row_dict.get('title', '')[:40]}"
                )
            else:
                counts["errors"] += 1
                logger.warning(
                    f"[BACKFILL] ERROR: ticker={ticker_val} fp={fp}... "
                    f"error={result.get('error', 'unknown')}"
                )

        except Exception as e:
            counts["errors"] += 1
            logger.error(f"[BACKFILL] EXCEPTION: ticker={ticker_val} fp={fp}...: {e}")

    logger.info(
        f"[BACKFILL] done: total={counts['total']} "
        f"inserted={counts['inserted']} dedup={counts['dedup_skipped']} "
        f"errors={counts['errors']}"
        + (f" dry_run={counts['dry_run']}" if dry_run else "")
    )
    return counts


# ============================================================
# CLI エントリポイント
# ============================================================
def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="earnings_summaries → tdnet_events バックフィル"
    )
    parser.add_argument("--ticker", type=str, default=None, help="対象 ticker（例: 5032）")
    parser.add_argument("--limit", type=int, default=0, help="最大件数（0=全件）")
    parser.add_argument("--since", type=int, default=0, metavar="DAYS",
                        help="直近N日以内の disclosure_date のみ対象（例: --since 30）")
    parser.add_argument("--dry-run", action="store_true", help="DB書き込みをスキップ")
    parser.add_argument("--db-path", type=str, default="", help="decision_db.db パス（省略時は自動）")
    args = parser.parse_args()

    load_env(_PROJECT_ROOT)

    result = run(
        ticker=args.ticker,
        limit=args.limit,
        dry_run=args.dry_run,
        db_path=args.db_path,
        since_days=args.since,
    )

    print()
    print("=" * 50)
    print("  BACKFILL SUMMARY")
    print("=" * 50)
    print(f"  total        : {result['total']}")
    print(f"  inserted     : {result['inserted']}")
    print(f"  dedup_skipped: {result['dedup_skipped']}")
    print(f"  errors       : {result['errors']}")
    if args.dry_run:
        print(f"  dry_run      : {result['dry_run']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
