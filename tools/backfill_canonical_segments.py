#!/usr/bin/env python3
r"""
backfill_canonical_segments.py — 既存セグメントデータ → canonical_segments (long) 一括投入

SQLite segment_financials (excel_legacy) を読み取り、canonical_segments テーブルに
long format で upsert する。write_segments_canonical() を再利用。

Usage:
  .\.venv\Scripts\python.exe tools\backfill_canonical_segments.py --dry-run
  .\.venv\Scripts\python.exe tools\backfill_canonical_segments.py --apply
  .\.venv\Scripts\python.exe tools\backfill_canonical_segments.py --apply --batch-size 500
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger("backfill_canonical_seg")
JST = timezone(timedelta(hours=9))

# ============================================================
# segment_financials から有効行を取得
# ============================================================

_SKIP_SEGMENT_NAMES = {
    "売上", "利益", "月次売上", "累計", "0", "#VALUE!", "",
}
_QUARTER_MAP = {"4Q": "FY"}
_VALID_QUARTERS = {"1Q", "2Q", "3Q", "FY"}


def _classify_skip_reason(row: dict) -> str:
    """スキップ理由。valid なら空文字列。"""
    name = (row.get("segment_name") or "").strip()
    if not name:
        return "empty_name"
    if name in _SKIP_SEGMENT_NAMES:
        return "header"
    if name.startswith("UNKNOWN_"):
        return "unknown"
    sales = row.get("segment_sales") or 0
    profit = row.get("segment_profit") or 0
    if sales == 0 and profit == 0:
        return "zero_value"
    quarter = row.get("quarter") or ""
    if quarter == "?Q":
        return "invalid_quarter"
    if sales is not None and abs(sales) > 0 and abs(sales) < 1:
        return "ratio"
    if profit is not None and abs(profit) > 0 and abs(profit) < 1:
        return "ratio"
    return ""


def load_sqlite_segments(db_path: str) -> list[dict]:
    """SQLite segment_financials から有効行を全件読み込む。"""
    if not os.path.isfile(db_path):
        logger.error(f"DB not found: {db_path}")
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT company_code, fiscal_year_end, quarter, segment_name, "
        "segment_sales, segment_profit, data_source "
        "FROM segment_financials"
    ).fetchall()
    conn.close()

    valid = []
    skip_counts: dict[str, int] = {}
    for row in rows:
        rdict = dict(row)
        reason = _classify_skip_reason(rdict)
        if reason:
            skip_counts[reason] = skip_counts.get(reason, 0) + 1
            continue
        quarter = _QUARTER_MAP.get(rdict["quarter"], rdict["quarter"])
        if quarter not in _VALID_QUARTERS:
            skip_counts["invalid_quarter"] = skip_counts.get("invalid_quarter", 0) + 1
            continue
        raw_sales = rdict["segment_sales"]
        raw_profit = rdict["segment_profit"]
        valid.append({
            "ticker": rdict["company_code"],
            "period": rdict["fiscal_year_end"],
            "quarter": quarter,
            "segment_name": rdict["segment_name"].strip(),
            "sales": int(raw_sales) if raw_sales is not None else None,
            "profit": int(raw_profit) if raw_profit is not None else None,
            "source": rdict.get("data_source") or "excel_legacy",
        })

    logger.info(f"[load] total={len(rows):,} valid={len(valid):,} skipped={dict(skip_counts)}")
    return valid


# ============================================================
# canonical_segments に upsert (batch)
# ============================================================

def push_to_canonical(
    valid_rows: list[dict],
    *,
    dry_run: bool = True,
    batch_size: int = 200,
) -> dict:
    """有効行を canonical_segments (long format) に upsert する。"""
    from lib.pipeline.canonical_writer import write_segments_canonical
    from lib.pipeline.db import load_env, get_supabase_write_config

    load_env()
    config = get_supabase_write_config()
    if not config:
        logger.error("Supabase write config not available")
        return {"written": 0, "skipped": 0, "errors": 0, "batches": 0}

    # per-ticker-period-quarter にグループ化
    groups: dict[tuple, list[dict]] = {}
    for row in valid_rows:
        key = (row["ticker"], row["period"], row["quarter"], row["source"])
        if key not in groups:
            groups[key] = []
        groups[key].append({
            "segment_name": row["segment_name"],
            "sales": row["sales"],
            "profit": row["profit"],
        })

    total_written = 0
    total_skipped = 0
    total_errors = 0
    batch_count = 0

    # dry-run: 件数だけカウント
    if dry_run:
        for (ticker, period, quarter, source), segs in groups.items():
            # long format: 各 seg × (sales, profit) → 最大 2 行/seg
            for seg in segs:
                for metric in ("sales", "profit"):
                    if seg.get(metric) is not None:
                        total_written += 1
                    else:
                        total_skipped += 1
        return {
            "written": total_written,
            "skipped": total_skipped,
            "errors": 0,
            "batches": 0,
            "groups": len(groups),
        }

    # apply: バッチで write
    group_list = list(groups.items())
    for i in range(0, len(group_list), batch_size):
        batch = group_list[i:i + batch_size]
        batch_count += 1
        for (ticker, period, quarter, source), segs in batch:
            result = write_segments_canonical(
                ticker=ticker,
                period=period,
                quarter=quarter,
                segments=segs,
                source=source,
                config=config,
            )
            total_written += result["written"]
            total_skipped += result["skipped"]
            total_errors += result["errors"]

        logger.info(
            f"[batch {batch_count}] groups={len(batch)} "
            f"written_so_far={total_written} errors={total_errors}"
        )

    return {
        "written": total_written,
        "skipped": total_skipped,
        "errors": total_errors,
        "batches": batch_count,
        "groups": len(groups),
    }


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="既存セグメントデータ → canonical_segments (long format) 一括投入",
    )
    parser.add_argument("--apply", action="store_true", help="Supabase に書き込み")
    parser.add_argument("--dry-run", action="store_true", help="件数のみ (default)")
    parser.add_argument("--db", default="decision_db.db", help="SQLite DB パス")
    parser.add_argument("--batch-size", type=int, default=200, help="バッチサイズ")
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
    valid_rows = load_sqlite_segments(db_path)
    if not valid_rows:
        logger.warning("[end] no valid segment rows found")
        return 0

    # 2. unique ticker/period/quarter stats
    unique_tickers = set(r["ticker"] for r in valid_rows)
    unique_periods = set((r["ticker"], r["period"], r["quarter"]) for r in valid_rows)
    logger.info(
        f"[stats] valid_rows={len(valid_rows):,} "
        f"unique_tickers={len(unique_tickers)} "
        f"unique_period_combos={len(unique_periods)}"
    )

    # 2b. before/after normalization samples
    from lib.pipeline.canonical_writer import normalize_segment_name, normalize_segment_key
    seen_names: set[str] = set()
    samples: list[tuple[str, str, str]] = []
    for r in valid_rows:
        raw = r["segment_name"]
        if raw in seen_names:
            continue
        seen_names.add(raw)
        norm_name = normalize_segment_name(raw)
        norm_key = normalize_segment_key(raw)
        if raw != norm_name:  # only show changed ones
            samples.append((raw, norm_name, norm_key))
    if samples:
        print()
        print("  [Normalization Samples] before -> after (segment_key)")
        print("  " + "-" * 56)
        for raw, norm, key in samples[:20]:
            print(f"    {raw:30s} -> {norm:20s} (key={key})")
        if len(samples) > 20:
            print(f"    ... and {len(samples) - 20} more")
        print()

    # 3. Push
    result = push_to_canonical(
        valid_rows,
        dry_run=dry_run,
        batch_size=opts.batch_size,
    )

    # 4. Summary
    print()
    print("=" * 60)
    print(f"  canonical_segments Backfill - {mode}")
    print("=" * 60)
    print(f"  input_source            : SQLite segment_financials (excel_legacy)")
    print(f"  input_valid_rows        : {len(valid_rows):,}")
    print(f"  unique_tickers          : {len(unique_tickers)}")
    print(f"  unique_period_combos    : {len(unique_periods)}")
    print(f"  groups                  : {result.get('groups', 0)}")
    print(f"  long_rows_written       : {result['written']:,}")
    print(f"  long_rows_skipped       : {result['skipped']:,}")
    print(f"  errors                  : {result['errors']}")
    print(f"  batches                 : {result.get('batches', 0)}")
    print()
    print("  source_row_key format   : cs|{ticker}|{period}|{quarter}|{segment_key}|{metric}|{source}|")
    print("  metrics expanded        : sales, profit")
    print("  upsert conflict key     : source_row_key")
    print("=" * 60)

    if not dry_run and result["errors"] == 0:
        logger.info("[end] backfill completed successfully")
    elif not dry_run:
        logger.warning(f"[end] backfill completed with {result['errors']} errors")
    else:
        logger.info("[end] dry-run completed - use --apply to write")

    return 0 if result["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
