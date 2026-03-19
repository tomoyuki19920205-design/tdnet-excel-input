#!/usr/bin/env python3
r"""
backfill_canonical_financials.py -- SQLite quarterly_results -> canonical_financials (long) backfill

Usage:
  .\.venv\Scripts\python.exe tools\backfill_canonical_financials.py --dry-run
  .\.venv\Scripts\python.exe tools\backfill_canonical_financials.py --apply
  .\.venv\Scripts\python.exe tools\backfill_canonical_financials.py --apply --batch-size 500
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger("backfill_canonical_fin")
JST = timezone(timedelta(hours=9))

# ============================================================
# metric mapping: quarterly_results column -> canonical metric
# ============================================================

# SQLite quarterly_results の wide カラム -> canonical_financials の metric 名
_METRIC_MAP = {
    "sales": "sales",
    "gross_profit": "gross_profit",
    "sga": "sga",
    "operating_profit": "operating_profit",
}

# gross_margin は比率のため除外 (value=金額のみ)
# unit: quarterly_results.unit が "JPY" or None -> "JPY"

_VALID_QUARTERS = {"1Q", "2Q", "3Q", "FY"}
_QUARTER_MAP = {"4Q": "FY"}


# ============================================================
# source 判定
# ============================================================

def _detect_source(row: dict) -> str:
    """field_sources / source_doc_id から source を判定する。"""
    fs = row.get("field_sources")
    if fs:
        # field_sources は JSON か、"summary_xbrl" / "attachment_xbrl" 等
        if isinstance(fs, str):
            try:
                fs_dict = json.loads(fs)
                # {"sales": "summary_xbrl", "gross_profit": "attachment_xbrl"}
                sources = set(fs_dict.values())
                if "summary_xbrl" in sources:
                    return "summary_xbrl"
                if "attachment_xbrl" in sources:
                    return "attachment_xbrl"
                if sources:
                    return list(sources)[0]
            except (json.JSONDecodeError, AttributeError):
                if "xbrl" in fs.lower():
                    return "summary_xbrl"
                if "pdf" in fs.lower():
                    return "pdf_table"
                if "html" in fs.lower():
                    return "html_table"

    doc_id = row.get("source_doc_id") or ""
    url = row.get("source_url") or ""
    if "xbrl" in doc_id.lower() or ".zip" in url.lower():
        return "summary_xbrl"
    if "pdf" in url.lower():
        return "pdf_table"

    return "legacy_excel"


# ============================================================
# Load from SQLite
# ============================================================

def load_sqlite_financials(db_path: str) -> list[dict]:
    """quarterly_results から有効行を読み込む。"""
    if not os.path.isfile(db_path):
        logger.error(f"DB not found: {db_path}")
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT company_code, fiscal_year_end, quarter, "
        "sales, gross_profit, sga, operating_profit, "
        "unit, source_doc_id, source_url, field_sources "
        "FROM quarterly_results"
    ).fetchall()
    conn.close()

    valid = []
    skip_counts: dict[str, int] = {}

    for row in rows:
        rdict = dict(row)
        quarter = _QUARTER_MAP.get(rdict.get("quarter", ""), rdict.get("quarter", ""))
        if quarter not in _VALID_QUARTERS:
            skip_counts["invalid_quarter"] = skip_counts.get("invalid_quarter", 0) + 1
            continue

        ticker = rdict.get("company_code", "")
        period = rdict.get("fiscal_year_end", "")
        if not ticker or not period:
            skip_counts["missing_key"] = skip_counts.get("missing_key", 0) + 1
            continue

        # metric dict を構築
        metrics_dict: dict[str, int | float | None] = {}
        has_any = False
        for src_col, metric_name in _METRIC_MAP.items():
            val = rdict.get(src_col)
            if val is not None:
                try:
                    metrics_dict[metric_name] = int(val)
                    has_any = True
                except (ValueError, TypeError):
                    metrics_dict[metric_name] = None
            else:
                metrics_dict[metric_name] = None

        if not has_any:
            skip_counts["all_null"] = skip_counts.get("all_null", 0) + 1
            continue

        source = _detect_source(rdict)

        valid.append({
            "ticker": ticker,
            "period": period,
            "quarter": quarter,
            "metrics": metrics_dict,
            "source": source,
            "unit": rdict.get("unit") or "JPY",
            "filing_id": rdict.get("source_doc_id"),
        })

    logger.info(f"[load] total={len(rows):,} valid={len(valid):,} skipped={dict(skip_counts)}")
    return valid


# ============================================================
# Push to canonical_financials
# ============================================================

def push_to_canonical(
    valid_rows: list[dict],
    *,
    dry_run: bool = True,
    batch_size: int = 200,
) -> dict:
    """wide rows -> canonical_financials (long) に upsert。"""
    from lib.pipeline.canonical_writer import write_financials_canonical
    from lib.pipeline.db import load_env, get_supabase_write_config

    load_env()
    config = get_supabase_write_config()
    if not config:
        logger.error("Supabase write config not available")
        return {"written": 0, "skipped": 0, "errors": 0, "batches": 0}

    total_written = 0
    total_skipped = 0
    total_errors = 0
    batch_count = 0

    # dry-run: 件数カウントのみ
    if dry_run:
        for row in valid_rows:
            for metric, val in row["metrics"].items():
                if val is not None:
                    total_written += 1
                else:
                    total_skipped += 1
        return {
            "written": total_written,
            "skipped": total_skipped,
            "errors": 0,
            "batches": 0,
        }

    # apply: バッチで write
    for i in range(0, len(valid_rows), batch_size):
        batch = valid_rows[i:i + batch_size]
        batch_count += 1
        for row in batch:
            result = write_financials_canonical(
                ticker=row["ticker"],
                period=row["period"],
                quarter=row["quarter"],
                metrics_dict=row["metrics"],
                source=row["source"],
                filing_id=row.get("filing_id"),
                unit=row.get("unit", "JPY"),
                config=config,
            )
            total_written += result["written"]
            total_skipped += result["skipped"]
            total_errors += result["errors"]

        logger.info(
            f"[batch {batch_count}] rows={len(batch)} "
            f"written_so_far={total_written} errors={total_errors}"
        )

    return {
        "written": total_written,
        "skipped": total_skipped,
        "errors": total_errors,
        "batches": batch_count,
    }


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SQLite quarterly_results -> canonical_financials (long) backfill",
    )
    parser.add_argument("--apply", action="store_true", help="Supabase write")
    parser.add_argument("--dry-run", action="store_true", help="Count only (default)")
    parser.add_argument("--db", default="decision_db.db", help="SQLite DB path")
    parser.add_argument("--batch-size", type=int, default=200, help="Batch size")
    return parser


def main(args: list[str] | None = None) -> int:
    parser = build_parser()
    opts = parser.parse_args(args)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    dry_run = not opts.apply or opts.dry_run
    mode = "DRY-RUN" if dry_run else "APPLY"
    db_path = os.path.join(_PROJECT_ROOT, opts.db)

    logger.info(f"[start] mode={mode} db={db_path}")

    # 1. Load
    valid_rows = load_sqlite_financials(db_path)
    if not valid_rows:
        logger.warning("[end] no valid financials rows found")
        return 0

    # 2. Stats
    unique_tickers = set(r["ticker"] for r in valid_rows)
    unique_combos = set((r["ticker"], r["period"], r["quarter"]) for r in valid_rows)
    source_counts: dict[str, int] = {}
    for r in valid_rows:
        src = r["source"]
        source_counts[src] = source_counts.get(src, 0) + 1

    logger.info(
        f"[stats] valid_rows={len(valid_rows):,} "
        f"unique_tickers={len(unique_tickers)} "
        f"unique_combos={len(unique_combos)}"
    )

    # 3. Push
    result = push_to_canonical(
        valid_rows,
        dry_run=dry_run,
        batch_size=opts.batch_size,
    )

    # 4. Summary
    print()
    print("=" * 60)
    print(f"  canonical_financials Backfill - {mode}")
    print("=" * 60)
    print(f"  input_source            : SQLite quarterly_results")
    print(f"  input_valid_rows        : {len(valid_rows):,}")
    print(f"  unique_tickers          : {len(unique_tickers)}")
    print(f"  unique_period_combos    : {len(unique_combos)}")
    print(f"  long_rows_written       : {result['written']:,}")
    print(f"  long_rows_skipped       : {result['skipped']:,}")
    print(f"  errors                  : {result['errors']}")
    print(f"  batches                 : {result.get('batches', 0)}")
    print()
    print(f"  source breakdown:")
    for src, cnt in sorted(source_counts.items()):
        print(f"    {src:25s}: {cnt:,}")
    print()
    print(f"  metrics                 : {', '.join(_METRIC_MAP.values())}")
    print(f"  source_row_key format   : cf|{{ticker}}|{{period}}|{{quarter}}|{{metric}}|{{source}}|{{filing_id}}")
    print(f"  upsert conflict key     : source_row_key")
    print("=" * 60)

    if not dry_run and result["errors"] == 0:
        logger.info("[end] backfill completed successfully")
    elif not dry_run:
        logger.warning(f"[end] backfill with {result['errors']} errors")
    else:
        logger.info("[end] dry-run completed - use --apply to write")

    # 5. Verification SQL
    print()
    print("  -- Verification SQL --")
    print("  SELECT count(*) FROM canonical_financials;")
    print("  SELECT ticker, period, quarter, metric, value, source FROM canonical_financials LIMIT 10;")
    print("  SELECT count(*) FROM api_latest_financials;")
    print()

    return 0 if result["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
