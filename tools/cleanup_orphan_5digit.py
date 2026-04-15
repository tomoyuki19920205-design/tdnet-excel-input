#!/usr/bin/env python3
r"""
cleanup_orphan_5digit.py -- 正規化済み4桁行が存在する旧5桁行の削除ツール

目的:
  canonical_financials に 5桁ticker行(旧) と 正規化済み4桁行(新) が
  共存している場合、旧5桁行を安全に削除する。

前提:
  fix_ticker_normalization.py --dry-run の結果で
  identical / unit_mismatch として検出された行が対象。

方式:
  - fix_ticker_normalization の scan_candidates + detect_collisions を再利用
  - identical / unit_mismatch の collision を delete-only 対象とする
  - true_conflict は絶対に触らない
  - insert は行わない (既に4桁行が存在するため)

Usage:
  cd C:\Users\takuy\OneDrive\tdnet-excel-input
  .\.venv\Scripts\python.exe tools/cleanup_orphan_5digit.py --dry-run
  .\.venv\Scripts\python.exe tools/cleanup_orphan_5digit.py --delete --limit 1000
  .\.venv\Scripts\python.exe tools/cleanup_orphan_5digit.py --delete --source jquants
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta

_ROOT = r"C:\Users\takuy\OneDrive\tdnet-excel-input"
sys.path.insert(0, _ROOT)
from lib.pipeline.db import load_env, get_supabase_config, get_supabase_write_config
from tools.fix_ticker_normalization import (
    scan_candidates,
    detect_collisions,
    _safe_delete,
    BATCH_SIZE,
)

load_env()
import requests

logger = logging.getLogger("cleanup_orphan")
JST = timezone(timedelta(hours=9))


def main():
    parser = argparse.ArgumentParser(
        description="Delete orphan 5-digit ticker rows (where 4-digit normalized row already exists)"
    )
    parser.add_argument("--delete", action="store_true",
                        help="Execute deletion")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan and report only (default)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Delete only first N rows (0=unlimited)")
    parser.add_argument("--source", type=str, default="",
                        help="Delete only rows matching this source")
    parser.add_argument("--enable-alpha-map", action="store_true",
                        help="Include ALPHA_MAP conversions")
    parser.add_argument("--collision-types", type=str, default="identical,unit_mismatch",
                        help="Collision types to delete (comma-separated, default: identical,unit_mismatch)")
    args = parser.parse_args()
    is_delete = args.delete and not args.dry_run

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    mode = "DELETE" if is_delete else "DRY-RUN"
    logger.info(f"=== cleanup_orphan_5digit START ({mode}) ===")

    allowed_types = set(args.collision_types.split(","))
    logger.info(f"[CONFIG] collision_types={allowed_types}")

    config = get_supabase_config()
    session = requests.Session()
    try:
        # Phase 1: 候補抽出 (fix_ticker_normalization と共通)
        logger.info("[SCAN] canonical_financials ...")
        candidates, skipped_invalid = scan_candidates(
            session, config, enable_alpha_map=args.enable_alpha_map,
        )

        if not candidates:
            print("\n  No 5-digit ticker candidates found.\n")
            return 0

        # Phase 2: 衝突判定
        logger.info("[COLLISION] Checking existing rows ...")
        updatable, collisions = detect_collisions(session, config, candidates)

        # Phase 3: delete-only 対象を抽出
        delete_targets = [
            c for c in collisions
            if c.get("collision_type") in allowed_types
        ]

        # source フィルタ
        if args.source:
            before = len(delete_targets)
            delete_targets = [c for c in delete_targets if c.get("source") == args.source]
            logger.info(f"[FILTER] --source={args.source}: {before} -> {len(delete_targets)}")

        # limit フィルタ
        if args.limit > 0 and len(delete_targets) > args.limit:
            logger.info(f"[FILTER] --limit={args.limit}: {len(delete_targets)} -> {args.limit}")
            delete_targets = delete_targets[:args.limit]

        # 集計
        type_counts = {}
        for c in delete_targets:
            ct = c.get("collision_type", "unknown")
            type_counts[ct] = type_counts.get(ct, 0) + 1

        # レポート
        print()
        print("=" * 60)
        print(f"  Orphan 5-digit Cleanup - {mode}")
        print("=" * 60)
        print(f"  candidates (5-char)     : {len(candidates):>10,}")
        print(f"  collisions              : {len(collisions):>10,}")
        print(f"  updatable (no collision): {len(updatable):>10,}")
        print(f"  delete targets          : {len(delete_targets):>10,}")
        for ct, cnt in sorted(type_counts.items()):
            print(f"    {ct:<25}: {cnt:>10,}")
        print()

        # サンプル
        n_show = min(20, len(delete_targets))
        if delete_targets:
            print(f"  Sample ({n_show} / {len(delete_targets):,}):")
            print(f"    {'type':<15} {'raw':>7} {'norm':>5} {'period':<12} {'metric':<20} {'src':<10}")
            print(f"    {'-'*15} {'-'*7} {'-'*5} {'-'*12} {'-'*20} {'-'*10}")
            for c in delete_targets[:n_show]:
                print(f"    {c.get('collision_type',''):<15} "
                      f"{c['raw_ticker']:>7} {c['norm_ticker']:>5} "
                      f"{c.get('period',''):>12} "
                      f"{c.get('metric',''):<20} {c.get('source',''):<10}")
            print()

        if not is_delete:
            print("  DRY-RUN complete. Use --delete to execute.\n")
            return 0

        if not delete_targets:
            print("  No rows to delete.\n")
            return 0

        # 確認プロンプト
        print(f"  About to DELETE {len(delete_targets):,} orphan 5-digit rows.")
        print("  WARNING: This only deletes old 5-digit rows. No insert is performed.")
        answer = input("  Proceed? (yes/no): ").strip().lower()
        if answer != "yes":
            print("  Aborted by user.")
            return 0

        # Phase 4: delete 実行
        write_config = get_supabase_write_config()
        if not write_config:
            logger.error("[DELETE] No write config (service role key missing)")
            return 1

        deleted = 0
        failed = 0
        t0 = time.monotonic()

        for i, c in enumerate(delete_targets, 1):
            del_result = _safe_delete(session, write_config, "canonical_financials", {
                "source_row_key": f"eq.{c['old_key']}",
            })
            if del_result.get("ok"):
                deleted += 1
            else:
                failed += 1
                logger.warning(f"[DELETE] FAILED: old_key={c['old_key'][:80]}")

            if i % 100 == 0:
                logger.info(f"[DELETE] progress: {i}/{len(delete_targets)} deleted={deleted} failed={failed}")

        elapsed = time.monotonic() - t0

        print()
        print("=" * 60)
        print("  DELETE Results")
        print("=" * 60)
        print(f"    targets    : {len(delete_targets):>10,}")
        print(f"    deleted    : {deleted:>10,}")
        print(f"    failed     : {failed:>10,}")
        print(f"    elapsed    : {elapsed:>9.1f}s")
        print()

        logger.info(f"[DONE] deleted={deleted} failed={failed} elapsed={elapsed:.1f}s")
        return 1 if failed > 0 else 0

    finally:
        session.close()


if __name__ == "__main__":
    import io
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'buffer'):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    sys.exit(main())
