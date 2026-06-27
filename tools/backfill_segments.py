#!/usr/bin/env python3
# ============================================================
# backfill_segments.py — 過去の決算短信PDFからセグメントを再抽出
# ============================================================
#
# 使い方:
#   .\.venv\Scripts\python.exe tools\backfill_segments.py --max-items 10 --dry-run
#   .\.venv\Scripts\python.exe tools\backfill_segments.py --max-items 50
#   .\.venv\Scripts\python.exe tools\backfill_segments.py --tickers 1801,7203,9619
#
# ============================================================
from __future__ import annotations

import argparse
import calendar
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

import pdfplumber
from src.extractor import extract_segment_financials
from src.migration.migration_db import MigrationDB
from src.utils import setup_logger
from src.year_parser import parse_reiwa, extract_fiscal_info

logger = logging.getLogger("backfill_seg")
JST = timezone(timedelta(hours=9))


def _now_jst_iso() -> str:
    return datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


# 証券コード抽出パターン
_TICKER_RE = re.compile(
    r"(?:コード番号|証券コード|コード)\s*[:：]?\s*(\d{4})"
)


def _extract_meta_from_pdf(pdf_path: str) -> dict | None:
    """PDFの1ページ目からticker/fiscal情報を抽出"""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if not pdf.pages:
                return None
            text = pdf.pages[0].extract_text() or ""
    except Exception:
        return None

    if not text:
        return None

    # ticker (4桁)
    m = _TICKER_RE.search(text)
    if not m:
        return None
    ticker = m.group(1)

    # fiscal year / quarter
    fiscal_year, quarter = extract_fiscal_info("", text)

    return {
        "ticker": ticker,
        "fiscal_year": fiscal_year,
        "quarter": quarter,
        "first_page": text[:200],
    }



_PL_PERIODS_CACHE = {}

def get_true_fiscal_year_end(ticker: str, extracted_date: str) -> str | None:
    if not extracted_date or not ticker: return extracted_date
    if ticker not in _PL_PERIODS_CACHE:
        from lib.pipeline.db import get_supabase_read_config, supabase_select, load_env
        load_env()
        cr = get_supabase_read_config()
        res = supabase_select("canonical_financials", params={"ticker": f"eq.{ticker}", "select": "period"}, config=cr)
        _PL_PERIODS_CACHE[ticker] = sorted(list(set(r["period"] for r in (res or []))))
        
    periods = _PL_PERIODS_CACHE[ticker]
    valid = [p for p in periods if p >= extracted_date]
    if valid:
        return min(valid)
    return None

def _fiscal_year_end(r_str: str) -> str | None:
    """R表記 → fiscal_year_end (YYYY-MM-DD)"""
    parsed = parse_reiwa(r_str)
    if not parsed:
        return None
    ad_year, month = parsed
    last_day = calendar.monthrange(ad_year, month)[1]
    return f"{ad_year:04d}-{month:02d}-{last_day:02d}"


def main():
    parser = argparse.ArgumentParser(description="過去PDFからセグメントを再抽出")
    parser.add_argument("--max-items", type=int, default=0, help="処理件数上限 (0=無制限)")
    parser.add_argument("--tickers", type=str, default="", help="対象tickerカンマ区切り")
    parser.add_argument("--dry-run", action="store_true", help="DB書き込みなし")
    args = parser.parse_args()

    log_path = os.path.join(_PROJECT_ROOT, "logs", "backfill_seg.log")
    setup_logger(log_path, name="backfill_seg")

    db_path = os.path.join(_PROJECT_ROOT, "decision_db.db")
    decision_db = MigrationDB(db_path)

    # 進捗（再開可能）
    progress_path = os.path.join(_PROJECT_ROOT, "data", "backfill_seg_progress.json")
    processed_ids: set[str] = set()
    if os.path.exists(progress_path):
        with open(progress_path, "r") as f:
            processed_ids = set(json.load(f).get("processed_ids", []))
        logger.info(f"[RESUME] 既に処理済み: {len(processed_ids)}件")

    docs_dir = os.path.join(_PROJECT_ROOT, "data", "docs")
    if not os.path.isdir(docs_dir):
        print(f"docs dir not found: {docs_dir}")
        return

    pdfs = sorted(Path(docs_dir).glob("*.pdf"))
    logger.info(f"[SCAN] PDF files found: {len(pdfs)}")

    ticker_filter = set(args.tickers.split(",")) if args.tickers else set()
    if ticker_filter:
        logger.info(f"[FILTER] tickers: {ticker_filter}")

    stats = {
        "total_pdfs": len(pdfs),
        "processed": 0,
        "segments_inserted": 0,
        "segments_updated": 0,
        "segments_no_change": 0,
        "no_segment_table": 0,
        "quarantined": 0,
        "skipped_already_done": 0,
        "skipped_no_meta": 0,
        "skipped_ticker_filter": 0,
        "errors": 0,
    }

    count = 0
    for pdf_path in pdfs:
        doc_id = pdf_path.stem

        if doc_id in processed_ids:
            stats["skipped_already_done"] += 1
            continue

        if args.max_items > 0 and count >= args.max_items:
            logger.info(f"[LIMIT] max-items={args.max_items} reached")
            break

        # PDFからメタ情報取得
        meta = _extract_meta_from_pdf(str(pdf_path))
        if not meta:
            stats["skipped_no_meta"] += 1
            processed_ids.add(doc_id)
            continue

        code = meta["ticker"]
        quarter = meta["quarter"] or "?Q"
        fye = _fiscal_year_end(meta["fiscal_year"]) if meta["fiscal_year"] else None
        raw_fye = fye or meta["fiscal_year"] or "unknown"
        fiscal_year_end = get_true_fiscal_year_end(code, raw_fye)
        if not fiscal_year_end:
            logger.warning(f"[NEEDS_REVIEW] Cannot map fiscal_year_end for {code} {quarter} {raw_fye}")
            stats["quarantined"] += 1
            processed_ids.add(doc_id)
            continue

        if ticker_filter and code not in ticker_filter:
            stats["skipped_ticker_filter"] += 1
            processed_ids.add(doc_id)
            continue

        count += 1

        # セグメント抽出
        try:
            seg_list, seg_reason = extract_segment_financials(
                pdf_path=str(pdf_path),
                title="",
            )

            if seg_list:
                for seg in seg_list:
                    if args.dry_run:
                        logger.info(
                            f"[DRY] {code} {fiscal_year_end} {quarter} "
                            f"{seg.segment_name}: S={seg.segment_sales} P={seg.segment_profit}"
                        )
                        stats["segments_inserted"] += 1
                    else:
                        result = decision_db.upsert_segment(
                            company_code=code,
                            fiscal_year_end=fiscal_year_end,
                            quarter=quarter,
                            segment_name=seg.segment_name,
                            segment_order=seg.segment_order,
                            segment_sales=seg.segment_sales,
                            segment_profit=seg.segment_profit,
                            unit_raw=getattr(seg, "unit_raw", None),
                            unit_multiplier=getattr(seg, "unit_multiplier", None),
                            raw_profit_label=seg.raw_profit_label,
                            data_source="tdnet",
                            actor="backfill_seg",
                            source="tdnet",
                        )
                        if result == "inserted":
                            stats["segments_inserted"] += 1
                        elif result == "updated":
                            stats["segments_updated"] += 1
                        else:
                            stats["segments_no_change"] += 1
                if not args.dry_run:
                    decision_db.commit()
                logger.info(
                    f"[OK] {code} {fiscal_year_end} {quarter}: {len(seg_list)} segs"
                )
            elif seg_reason == "no_segment_table":
                stats["no_segment_table"] += 1
            elif seg_reason:
                stats["quarantined"] += 1
                if not args.dry_run:
                    decision_db.quarantine_record(
                        company_code=code,
                        reason=seg_reason,
                        fiscal_year_end=fiscal_year_end,
                        quarter=quarter,
                        metric_type="segment_backfill",
                        source_doc_id=doc_id,
                    )
                    decision_db.commit()
                logger.info(f"[QUARANTINE] {code} {fiscal_year_end}: {seg_reason[:60]}")

            stats["processed"] += 1

        except Exception as e:
            stats["errors"] += 1
            logger.warning(f"[ERROR] {code} {doc_id}: {e}")

        processed_ids.add(doc_id)
        if count % 10 == 0:
            with open(progress_path, "w") as f:
                json.dump({"processed_ids": list(processed_ids), "updated_at": _now_jst_iso()}, f)
            logger.info(f"[PROGRESS] {count} processed")

    # 最終進捗保存
    with open(progress_path, "w") as f:
        json.dump({"processed_ids": list(processed_ids), "updated_at": _now_jst_iso()}, f)

    print()
    print("=" * 60)
    print("  Backfill Segments - Result Summary")
    print("=" * 60)
    for k, v in stats.items():
        print(f"  {k:<28}: {v}")

    decision_db.close()


if __name__ == "__main__":
    main()
