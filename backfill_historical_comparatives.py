#!/usr/bin/env python3
# ============================================================
# backfill_historical_comparatives.py
# ============================================================
"""
過去開示の比較列/比較行から historical records を抽出・投入するバッチ。

Usage:
    # Phase 4A: dry-run (集計のみ, DB書き込みなし)
    python backfill_historical_comparatives.py --dry-run

    # Phase 4B: 小規模本投入 (先頭50件)
    python backfill_historical_comparatives.py --limit 50

    # Phase 4C: 全体本投入
    python backfill_historical_comparatives.py

    # オプション
    --db <path>     DB ファイルパス (default: data/decision_db.db)
    --cache <path>  tdnet_cache ディレクトリ (default: data/tdnet_cache)
    --limit <N>     処理する filing 数上限
    --dry-run       DB書き込みを行わない
    --audit <path>  監査ログ CSV 出力先 (default: data/backfill_audit.csv)
    --segment-only  セグメントのみ処理
    --order-only    受注のみ処理
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from pathlib import Path

# プロジェクトルート
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(str(PROJECT_ROOT))

import re as _re

from src.migration.migration_db import MigrationDB
from src.historical.schemas import HistoricalRecord
from src.historical.existing_check import filter_skip_existing


logger = logging.getLogger("backfill_historical")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# ============================================================
# FYE/Quarter パーサー (PDF タイトルから)
# ============================================================
_FYE_PATTERN = _re.compile(
    r"(\d{4})年(\d{1,2})月期"
)
_Q_PATTERN = _re.compile(
    r"第([１２３４1234])四半期"
)
_MONTH_TO_DAY = {
    "1": "01-31", "2": "02-28", "3": "03-31", "4": "04-30",
    "5": "05-31", "6": "06-30", "7": "07-31", "8": "08-31",
    "9": "09-30", "10": "10-31", "11": "11-30", "12": "12-31",
}
_ZENKAKU_Q = {"１": "1", "２": "2", "３": "3", "４": "4"}


def _parse_fye_quarter_from_title(title: str) -> tuple[str, str]:
    """タイトルから FYE と quarter を抽出する。

    Examples:
        "2026年3月期 第３四半期決算短信" → ("2026-03-31", "3Q")
        "2025年12月期 決算短信" → ("2025-12-31", "4Q")
    """
    fye = ""
    quarter = ""

    m = _FYE_PATTERN.search(title)
    if m:
        year, month = m.group(1), m.group(2)
        day = _MONTH_TO_DAY.get(month, "03-31")
        fye = f"{year}-{day}"

    m = _Q_PATTERN.search(title)
    if m:
        q = m.group(1)
        q = _ZENKAKU_Q.get(q, q)
        quarter = f"{q}Q"
    elif "決算短信" in title and "四半期" not in title:
        quarter = "4Q"  # 通期決算

    return fye, quarter


def _extract_title_from_pdf(pdf_path: str) -> str:
    """PDFの1ページ目からタイトル行を取得する。"""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            if pdf.pages:
                text = pdf.pages[0].extract_text() or ""
                lines = text.split("\n")
                # 最初の3行を結合してタイトルとする
                return " ".join(lines[:3])
    except Exception:
        pass
    return ""



# ============================================================
# 集計カウンター
# ============================================================
class Stats:
    def __init__(self):
        self.filings_processed = 0
        self.candidates_total = 0
        self.inserted_total = 0
        self.skipped_existing = 0
        self.skipped_unknown_basis = 0
        self.skipped_ratio_only = 0
        self.skipped_same_value_guard = 0
        self.historical_only_count = 0   # current=0 & hist>0
        self.current_and_historical_count = 0  # current>0 & hist>0
        self.order_records_inserted = 0
        self.segment_records_inserted = 0
        self.paired_table_records = 0
        self.errors = 0

    def report(self) -> str:
        lines = [
            "=" * 60,
            "=== BACKFILL STATS ===",
            f"filings_processed:            {self.filings_processed}",
            f"candidates_total:             {self.candidates_total}",
            f"inserted_total:               {self.inserted_total}",
            f"skipped_existing:             {self.skipped_existing}",
            f"skipped_unknown_basis:        {self.skipped_unknown_basis}",
            f"skipped_ratio_only:           {self.skipped_ratio_only}",
            f"skipped_same_value_guard:     {self.skipped_same_value_guard}",
            f"historical_only_count:        {self.historical_only_count}",
            f"current_and_historical_count: {self.current_and_historical_count}",
            f"order_records_inserted:       {self.order_records_inserted}",
            f"segment_records_inserted:     {self.segment_records_inserted}",
            f"paired_table_records:         {self.paired_table_records}",
            f"errors:                       {self.errors}",
            "=" * 60,
        ]
        return "\n".join(lines)


# ============================================================
# 監査ログ
# ============================================================
class AuditLog:
    FIELDS = [
        "ticker_code", "filing_id", "source_type", "extraction_path",
        "metric_name", "segment_name",
        "target_fiscal_year_end", "target_quarter", "target_period_type",
        "value", "source_basis",
        "action", "skipped_reason",
    ]

    def __init__(self, path: str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "w", newline="", encoding="utf-8-sig")
        self._writer = csv.DictWriter(self._file, fieldnames=self.FIELDS)
        self._writer.writeheader()

    def write(self, **kwargs):
        row = {f: kwargs.get(f, "") for f in self.FIELDS}
        self._writer.writerow(row)

    def close(self):
        self._file.close()


# ============================================================
# extraction_path 推定
# ============================================================
def _infer_extraction_path(rec: HistoricalRecord) -> str:
    """raw_text から extraction path を推定する。"""
    raw = getattr(rec, "raw_text", "") if hasattr(rec, "raw_text") else ""
    # ComparisonColumn.raw_text が HistoricalRecord に引き継がれていないため
    # source_basis + segment_name で推定
    if rec.segment_name:
        return "D"  # paired-table (most common for segments now)
    return "A"  # horizontal for orders


# ============================================================
# 1 filing の処理
# ============================================================
def process_filing(
    pdf_path: str,
    meta: dict,
    dir_name: str,
    db: MigrationDB | None,
    stats: Stats,
    audit: AuditLog,
    *,
    dry_run: bool = True,
    do_segment: bool = True,
    do_order: bool = True,
) -> None:
    """1 filing を処理する。"""
    ticker = meta.get("company_code", "")
    title = meta.get("title", "")
    fye = meta.get("fiscal_year_end", "")
    quarter = meta.get("quarter", "")

    # meta.json にない場合は PDF から取得
    if not title or not fye or not quarter:
        pdf_title = _extract_title_from_pdf(pdf_path)
        if pdf_title:
            if not title:
                title = pdf_title
            if not fye or not quarter:
                parsed_fye, parsed_q = _parse_fye_quarter_from_title(pdf_title)
                if not fye:
                    fye = parsed_fye
                if not quarter:
                    quarter = parsed_q

    # ticker を PDF タイトルから取得 (例: ㈱サンユウ（5697）)
    if not ticker:
        m = _re.search(r"[（(](\d{4})[）)]", title)
        if m:
            ticker = m.group(1)
        else:
            ticker = dir_name[:4]

    if not fye or not quarter:
        return

    stats.filings_processed += 1
    all_historical: list[HistoricalRecord] = []
    n_current = 0

    # ---- セグメント ----
    if do_segment:
        try:
            from src.historical.segment_backfill import extract_segment_with_historical
            seg_result = extract_segment_with_historical(
                pdf_path, title,
                company_code=ticker,
                fiscal_year_end=fye,
                quarter=quarter,
                period_type="cumulative",
                source_doc_id=dir_name,
                ticker=ticker,
            )
            n_current += len(seg_result.current_records)
            all_historical.extend(seg_result.historical_records)
            stats.skipped_unknown_basis += seg_result.stats.get("skipped_unknown_basis", 0)
            stats.skipped_ratio_only += seg_result.stats.get("skipped_ratio_only", 0)
        except Exception as e:
            logger.warning("segment error [%s]: %s", ticker, e)
            stats.errors += 1

    # ---- 受注系 ----
    if do_order:
        try:
            from src.historical.order_backfill import extract_order_metrics_with_historical
            ord_result = extract_order_metrics_with_historical(
                pdf_path, title,
                company_code=ticker,
                fiscal_year_end=fye,
                quarter=quarter,
                period_type="cumulative",
                source_doc_id=dir_name,
            )
            n_current += len(ord_result.current_records)
            all_historical.extend(ord_result.historical_records)
            stats.skipped_unknown_basis += ord_result.stats.get("skipped_unknown_basis", 0)
            stats.skipped_ratio_only += ord_result.stats.get("skipped_ratio_only", 0)
            stats.skipped_same_value_guard += ord_result.stats.get("skipped_same_value_guard", 0)
        except Exception as e:
            logger.warning("order error [%s]: %s", ticker, e)
            stats.errors += 1

    if not all_historical:
        return

    stats.candidates_total += len(all_historical)

    # ---- current/historical 分類 ----
    if n_current == 0 and len(all_historical) > 0:
        stats.historical_only_count += 1
    elif n_current > 0 and len(all_historical) > 0:
        stats.current_and_historical_count += 1

    # ---- filter_skip_existing ----
    if db is not None:
        writable, skipped = filter_skip_existing(all_historical, db)
        stats.skipped_existing += skipped
    else:
        writable = all_historical
        skipped = 0

    # ---- DB 書き込み or dry-run ----
    for rec in writable:
        is_segment = rec.segment_name is not None
        is_order = not is_segment
        extraction_path = _infer_extraction_path(rec)

        if not dry_run and db is not None:
            try:
                if is_segment:
                    # segment → upsert_segment
                    seg_sales = rec.value if rec.metric_name == "segment_sales" else None
                    seg_profit = rec.value if rec.metric_name == "segment_profit" else None
                    result = db.upsert_segment(
                        company_code=rec.company_code,
                        fiscal_year_end=rec.target_fiscal_year_end,
                        quarter=rec.target_quarter,
                        segment_name=rec.segment_name,
                        segment_order=99,  # backfill — order unknown
                        segment_sales=seg_sales,
                        segment_profit=seg_profit,
                        data_source="historical_backfill",
                        actor="backfill_batch",
                        source="historical_backfill",
                    )
                    if result == "inserted":
                        stats.inserted_total += 1
                        stats.segment_records_inserted += 1
                        if extraction_path == "D":
                            stats.paired_table_records += 1
                    elif result == "no_change":
                        stats.skipped_existing += 1
                else:
                    # order → upsert_order_metric
                    result = db.upsert_order_metric(
                        company_code=rec.company_code,
                        fiscal_year_end=rec.target_fiscal_year_end,
                        quarter=rec.target_quarter,
                        metric_name=rec.metric_name,
                        value=rec.value,
                        raw_value=rec.value,
                        unit=rec.unit,
                        confidence=rec.confidence,
                        source_doc_id=rec.source_doc_id,
                    )
                    if result == "inserted":
                        stats.inserted_total += 1
                        stats.order_records_inserted += 1
                    elif result == "no_change":
                        stats.skipped_existing += 1

                action = result
            except Exception as e:
                logger.warning("DB write error [%s]: %s", ticker, e)
                stats.errors += 1
                action = "error"
        else:
            # dry-run
            action = "dry_run"
            stats.inserted_total += 1
            if is_segment:
                stats.segment_records_inserted += 1
                if extraction_path == "D":
                    stats.paired_table_records += 1
            else:
                stats.order_records_inserted += 1

        # 監査ログ
        audit.write(
            ticker_code=rec.company_code,
            filing_id=rec.source_doc_id,
            source_type="segment" if is_segment else "order",
            extraction_path=extraction_path,
            metric_name=rec.metric_name,
            segment_name=rec.segment_name or "",
            target_fiscal_year_end=rec.target_fiscal_year_end,
            target_quarter=rec.target_quarter,
            target_period_type=rec.target_period_type,
            value=rec.value,
            source_basis=rec.source_basis,
            action=action,
            skipped_reason="",
        )

    # skipped のログも記録
    if db is not None:
        for rec in all_historical:
            if rec not in writable:
                audit.write(
                    ticker_code=rec.company_code,
                    filing_id=rec.source_doc_id,
                    source_type="segment" if rec.segment_name else "order",
                    extraction_path=_infer_extraction_path(rec),
                    metric_name=rec.metric_name,
                    segment_name=rec.segment_name or "",
                    target_fiscal_year_end=rec.target_fiscal_year_end,
                    target_quarter=rec.target_quarter,
                    target_period_type=rec.target_period_type,
                    value=rec.value,
                    source_basis=rec.source_basis,
                    action="skipped",
                    skipped_reason="existing_value",
                )


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Historical comparative backfill")
    parser.add_argument("--db", default="data/decision_db.db", help="DB path")
    parser.add_argument("--cache", default="data/tdnet_cache", help="Cache dir")
    parser.add_argument("--limit", type=int, default=0, help="Max filings (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="No DB writes")
    parser.add_argument("--audit", default="data/backfill_audit.csv", help="Audit CSV")
    parser.add_argument("--segment-only", action="store_true")
    parser.add_argument("--order-only", action="store_true")
    args = parser.parse_args()

    cache_dir = Path(args.cache)
    if not cache_dir.exists():
        logger.error("Cache dir not found: %s", cache_dir)
        sys.exit(1)

    # DB
    db = None if args.dry_run else MigrationDB(args.db)

    # 監査ログ
    audit = AuditLog(args.audit)

    do_segment = not args.order_only
    do_order = not args.segment_only

    # filing 列挙
    filings = sorted([
        d for d in cache_dir.iterdir()
        if d.is_dir() and (d / "source.pdf").exists()
    ])
    total = len(filings)
    limit = args.limit if args.limit > 0 else total

    mode_str = "DRY-RUN" if args.dry_run else "LIVE"
    logger.info("=== Backfill %s | %d filings (limit=%d) | seg=%s ord=%s ===",
                mode_str, total, limit, do_segment, do_order)

    stats = Stats()
    t0 = time.time()

    for i, d in enumerate(filings[:limit]):
        pdf_path = str(d / "source.pdf")
        meta = {}
        mf = d / "meta.json"
        if mf.exists():
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
            except Exception:
                pass

        process_filing(
            pdf_path, meta, d.name, db, stats, audit,
            dry_run=args.dry_run,
            do_segment=do_segment,
            do_order=do_order,
        )

        if (i + 1) % 50 == 0:
            elapsed = time.time() - t0
            logger.info(
                "  [%d/%d] %.1fs | ins=%d skip=%d err=%d",
                i + 1, limit, elapsed,
                stats.inserted_total, stats.skipped_existing, stats.errors,
            )

    elapsed = time.time() - t0

    # commit
    if db is not None and not args.dry_run:
        db._conn.commit()
        logger.info("DB committed")

    audit.close()

    # レポート
    print(f"\nCompleted in {elapsed:.1f}s")
    print(stats.report())
    print(f"\nAudit log: {args.audit}")


if __name__ == "__main__":
    main()
