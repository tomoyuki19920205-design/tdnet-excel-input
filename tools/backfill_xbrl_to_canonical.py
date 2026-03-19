#!/usr/bin/env python3
r"""
backfill_xbrl_to_canonical.py -- segment_canonical (wide) → canonical_segments (EAV) backfill

segment_canonical テーブルの source='xbrl' 行を読み取り、
write_segments_canonical() を使って canonical_segments (EAV) に投入する。

特徴:
- idempotent: source_row_key ベースの upsert なので何度実行しても安全
- write_segments_canonical() を使うため、segment_key / source_row_key / recency_key が
  日次 dual-write と完全に一致する
- dry-run で対象 row 数 / ticker 数 / batch 数 / upsert 想定件数を表示

Usage:
  .\.venv\Scripts\python.exe tools\backfill_xbrl_to_canonical.py --dry-run
  .\.venv\Scripts\python.exe tools\backfill_xbrl_to_canonical.py --apply
  .\.venv\Scripts\python.exe tools\backfill_xbrl_to_canonical.py --apply --source xbrl
  .\.venv\Scripts\python.exe tools\backfill_xbrl_to_canonical.py --apply --source tdnet
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.pipeline.db import load_env, get_supabase_config, get_supabase_write_config, supabase_select
from lib.pipeline.canonical_writer import write_segments_canonical

logger = logging.getLogger("backfill_xbrl")


def fetch_segment_canonical_rows(
    config: dict,
    source: str = "xbrl",
) -> list[dict]:
    """segment_canonical テーブルから指定 source の行を全取得。"""
    rows = supabase_select(
        "segment_canonical",
        params={
            "source": f"eq.{source}",
            "select": "ticker,period,quarter,segment_name,sales,profit,source,updated_at",
            "order": "ticker,period,quarter,segment_name",
        },
        config=config,
    )
    return rows


def build_batches(rows: list[dict]) -> dict[tuple, list[dict]]:
    """ticker/period/quarter でバッチ集約。"""
    batches: dict[tuple, list[dict]] = {}
    for row in rows:
        ticker = row.get("ticker", "")
        period = row.get("period", "")
        quarter = row.get("quarter", "")
        if not ticker or not period:
            continue
        key = (ticker, period, quarter)
        if key not in batches:
            batches[key] = []
        batches[key].append({
            "segment_name": row["segment_name"],
            "sales": row.get("sales"),
            "profit": row.get("profit"),
        })
    return batches


def run(*, dry_run: bool = False, source: str = "xbrl") -> int:
    load_env(_PROJECT_ROOT)
    config = get_supabase_config()
    write_config = get_supabase_write_config() if not dry_run else None

    # 1. segment_canonical から source 行を取得
    logger.info(f"[BACKFILL] Fetching segment_canonical rows (source={source})...")
    rows = fetch_segment_canonical_rows(config, source=source)
    logger.info(f"[BACKFILL] Fetched {len(rows)} rows from segment_canonical")

    if not rows:
        print(f"\n  No rows found for source='{source}' in segment_canonical")
        return 0

    # 2. バッチ集約
    batches = build_batches(rows)
    tickers = set(k[0] for k in batches)

    # metric 展開後の想定 EAV 行数 (各セグメントの sales + profit)
    estimated_eav_rows = sum(
        sum(1 for seg in segs for m in ("sales", "profit") if seg.get(m) is not None)
        for segs in batches.values()
    )

    # 3. dry-run レポート
    print()
    print("=" * 60)
    print(f"  Backfill segment_canonical → canonical_segments")
    print(f"  Source: {source}")
    print(f"  Mode: {'DRY-RUN' if dry_run else 'APPLY'}")
    print("=" * 60)
    print(f"    source rows (wide)       : {len(rows)}")
    print(f"    tickers                  : {len(tickers)}")
    print(f"    batches                  : {len(batches)}")
    print(f"    estimated EAV rows       : {estimated_eav_rows}")
    if len(tickers) <= 20:
        print(f"    ticker list              : {sorted(tickers)}")
    else:
        print(f"    ticker sample            : {sorted(tickers)[:10]}...")
    print()

    if dry_run:
        print("  ** DRY-RUN: no writes performed **")
        print()
        return 0

    if not write_config:
        logger.error("[BACKFILL] No write config available")
        return 1

    # 4. apply: write_segments_canonical() でバッチ upsert
    total_written = 0
    total_errors = 0
    total_skipped = 0

    for i, ((ticker, period, quarter), segs) in enumerate(batches.items()):
        result = write_segments_canonical(
            ticker=ticker,
            period=period,
            quarter=quarter,
            segments=segs,
            source=source,
            config=write_config,
        )
        total_written += result["written"]
        total_errors += result["errors"]
        total_skipped += result["skipped"]

        if (i + 1) % 50 == 0:
            logger.info(
                f"[BACKFILL] progress: {i + 1}/{len(batches)} batches, "
                f"written={total_written}"
            )

    print(f"    written                  : {total_written}")
    print(f"    skipped                  : {total_skipped}")
    print(f"    errors                   : {total_errors}")
    print()

    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(
        description="Backfill segment_canonical (wide) → canonical_segments (EAV)",
    )
    parser.add_argument("--apply", action="store_true", help="書き込み実行")
    parser.add_argument("--dry-run", action="store_true", help="書き込みなし (デフォルト)")
    parser.add_argument("--source", default="xbrl",
                        help="対象 source (default: xbrl)")
    args = parser.parse_args()
    dry_run = not args.apply or args.dry_run
    sys.exit(run(dry_run=dry_run, source=args.source))
