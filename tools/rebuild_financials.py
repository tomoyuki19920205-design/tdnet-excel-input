#!/usr/bin/env python3
# ============================================================
# rebuild_financials.py — Supabase financials 完全再構築
# ============================================================
#
# 使い方:
#   cd C:\Users\takuy\OneDrive\tdnet-excel-input
#   .\.venv\Scripts\python.exe tools/rebuild_financials.py --dry-run
#   .\.venv\Scripts\python.exe tools/rebuild_financials.py
#
# 前提:
#   Supabase financials は事前に TRUNCATE 済みであること。
#   SQL Editor で TRUNCATE TABLE financials; を実行してから使用。
#
# ============================================================
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

import requests

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

# Windows cp932 対策
if sys.stdout and hasattr(sys.stdout, "encoding"):
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace",
        )
if sys.stderr and hasattr(sys.stderr, "encoding"):
    if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace",
        )

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("rebuild_financials")

from tools.sqlite_to_supabase import (
    _build_financials_rows_from_tdnet,
    _load_dotenv,
)

_DEFAULT_BATCH_SIZE = 500


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _get_supabase_config():
    _load_dotenv()
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_ANON_KEY", "")
    if not url or not key:
        raise ValueError("SUPABASE_URL / SUPABASE_ANON_KEY が .env に未設定です")
    return url, key


def _supabase_count(url: str, key: str) -> int:
    """financials テーブルの行数を取得"""
    r = requests.get(
        f"{url.rstrip('/')}/rest/v1/financials",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Prefer": "count=exact",
            "Range": "0-0",
        },
        params={"select": "ticker"},
        timeout=30,
    )
    r.raise_for_status()
    cr = r.headers.get("Content-Range", "")
    return int(cr.split("/")[1]) if "/" in cr else 0


def _dedup_payload(rows: list[dict]) -> list[dict]:
    """payload から重複キーを除去（最後の出現を採用）"""
    seen: dict[tuple, dict] = {}
    dups = 0
    for r in rows:
        k = (r["ticker"], r["period"], r["quarter"])
        if k in seen:
            dups += 1
        seen[k] = r
    if dups > 0:
        logger.warning(f"  ⚠ 重複キー除去: {dups} 件")
    return list(seen.values())


def _upsert_with_retry(
    url: str, key: str, batch: list[dict],
    *, fallback_sizes: tuple[int, ...] = (100, 10, 1),
) -> tuple[int, int, list[dict]]:
    """batch upsert with retry on failure.

    Returns (sent, errors, failed_rows).
    5xx → fallback to smaller batch sizes.
    """
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=headers-only,resolution=merge-duplicates",
    }
    params = {"on_conflict": "ticker,period,quarter"}

    try:
        resp = requests.post(
            f"{url.rstrip('/')}/rest/v1/financials",
            headers=headers, params=params, json=batch,
            timeout=60,
        )
        if resp.status_code in (200, 201):
            return len(batch), 0, []
    except requests.RequestException as e:
        logger.warning(f"    request error: {e}")

    # Batch failed — try smaller sizes
    sent = 0
    errors = 0
    failed = []

    for fb_size in fallback_sizes:
        remaining = [r for r in batch if r not in failed
                     and (r["ticker"], r["period"], r["quarter"])
                     not in {(f["ticker"], f["period"], f["quarter"]) for f in failed}]
        # Only retry rows not yet sent
        unsent = remaining[sent:]
        if not unsent:
            break

        for sub_chunk in _chunks(unsent, fb_size):
            try:
                resp = requests.post(
                    f"{url.rstrip('/')}/rest/v1/financials",
                    headers=headers, params=params, json=sub_chunk,
                    timeout=60,
                )
                if resp.status_code in (200, 201):
                    sent += len(sub_chunk)
                else:
                    if fb_size == 1:
                        errors += len(sub_chunk)
                        failed.extend(sub_chunk)
                        for r in sub_chunk:
                            logger.error(
                                f"    FAIL: {r['ticker']} {r['period']} "
                                f"{r['quarter']}: {resp.status_code}"
                            )
                    # else: will be retried at smaller size
            except requests.RequestException:
                if fb_size == 1:
                    errors += len(sub_chunk)
                    failed.extend(sub_chunk)

    return sent, errors, failed


# 代表銘柄の期待値 (百万円)
_REPRESENTATIVE_TICKERS = {
    ("2301", "2026-10-31", "1Q"): {"sales": 1368, "operating_profit": -692},
    ("1736", "2026-03-31", "1Q"): {"sales": 6485, "operating_profit": 522},
    ("5805", "2026-03-31", "1Q"): {"sales": 8147, "operating_profit": 773},
}


def main():
    parser = argparse.ArgumentParser(
        description="Supabase financials 完全再構築",
    )
    parser.add_argument("--db", default="decision_db.db")
    parser.add_argument("--batch-size", type=int, default=_DEFAULT_BATCH_SIZE)
    parser.add_argument("--dry-run", action="store_true",
                        help="Supabase への書き込みを行わない")
    args = parser.parse_args()

    url, key = _get_supabase_config()

    db_path = args.db
    if not os.path.isabs(db_path):
        db_path = os.path.join(_PROJECT_ROOT, db_path)

    print()
    print("=" * 55)
    print("  Supabase financials 完全再構築")
    print("=" * 55)
    if args.dry_run:
        print("  Mode: dry-run")
    print()

    # =========================================================
    # Step 1: 現行件数を記録
    # =========================================================
    logger.info("[Step 1/6] 現行件数を記録...")
    before_count = _supabase_count(url, key)
    logger.info(f"  既存 financials: {before_count:,} 行")

    # =========================================================
    # Step 2: TRUNCATE 確認
    # =========================================================
    logger.info("[Step 2/6] TRUNCATE 確認...")
    if before_count > 0 and not args.dry_run:
        print()
        print("  ⚠ financials テーブルに既存データがあります。")
        print(f"  既存件数: {before_count:,} 行")
        print()
        print("  先に SQL Editor で以下を実行してください:")
        print("    TRUNCATE TABLE financials;")
        print()
        ans = input("  TRUNCATE 済みなら 'yes' を入力: ").strip().lower()
        if ans != "yes":
            print("  中止しました。")
            sys.exit(1)
        actual = _supabase_count(url, key)
        if actual > 0:
            logger.error(f"  ❌ TRUNCATE が未完了です (残り {actual:,} 行)")
            sys.exit(1)
        logger.info("  ✅ TRUNCATE 確認済み (0 行)")
    elif args.dry_run:
        logger.info("  [dry-run] TRUNCATE スキップ")

    # =========================================================
    # Step 3: SQLite からペイロード生成 + 重複除去
    # =========================================================
    logger.info("[Step 3/6] SQLite からペイロード生成...")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM quarterly_results ORDER BY id"
    ).fetchall()
    conn.close()
    logger.info(f"  SQLite 行数: {len(rows):,}")

    fin_rows_raw = _build_financials_rows_from_tdnet(rows)
    logger.info(f"  生成行数 (重複含む): {len(fin_rows_raw):,}")

    # 重複キー除去 — 同一 (ticker, period, quarter) の最後の出現を採用
    fin_rows = _dedup_payload(fin_rows_raw)
    logger.info(f"  重複除去後: {len(fin_rows):,}")

    if not fin_rows:
        logger.error("  ❌ 生成行数が 0 です。中止。")
        sys.exit(1)

    # =========================================================
    # Step 4: Supabase へ batch upsert (retry 付き)
    # =========================================================
    logger.info("[Step 4/6] Supabase へ batch upsert...")
    t0 = time.time()
    total_sent = 0
    total_errors = 0
    all_failed: list[dict] = []

    if args.dry_run:
        logger.info("  [dry-run] push スキップ")
        for r in fin_rows[:5]:
            logger.info(f"  sample: {r['ticker']} {r['period']} {r['quarter']} "
                        f"sales={r['sales']} op={r['operating_profit']}")
    else:
        batches = list(_chunks(fin_rows, args.batch_size))
        for i, chunk in enumerate(batches):
            sent, errs, failed = _upsert_with_retry(url, key, chunk)
            total_sent += sent
            total_errors += errs
            all_failed.extend(failed)

            if (i + 1) % 50 == 0 or i == 0 or errs > 0:
                pct = total_sent / len(fin_rows) * 100
                logger.info(
                    f"  batch {i+1}/{len(batches)}: "
                    f"{total_sent:,}/{len(fin_rows):,} ({pct:.1f}%) "
                    f"errors={total_errors}"
                )

    elapsed = time.time() - t0
    logger.info(f"  完了: sent={total_sent:,} errors={total_errors} ({elapsed:.1f}秒)")

    if all_failed:
        logger.error(f"  ❌ {len(all_failed)} 行が送信失敗:")
        for r in all_failed[:10]:
            logger.error(
                f"    {r['ticker']} {r['period']} {r['quarter']} "
                f"sales={r.get('sales')}"
            )

    # =========================================================
    # Step 5: 最終照合 (生成件数 == Supabase件数)
    # =========================================================
    logger.info("[Step 5/6] 最終照合...")
    all_ok = True

    if not args.dry_run:
        after_count = _supabase_count(url, key)
        expected_count = len(fin_rows)
        match = after_count == expected_count
        logger.info(f"  生成: {expected_count:,} → Supabase: {after_count:,}")
        logger.info(f"  件数一致: {match}")

        if not match:
            logger.error(
                f"  ❌ 件数不一致: 差分 = {expected_count - after_count} 行"
            )
            all_ok = False
    else:
        logger.info(f"  [dry-run] 生成件数: {len(fin_rows):,}")

    # =========================================================
    # Step 6: 代表銘柄検証
    # =========================================================
    logger.info("[Step 6/6] 代表銘柄検証...")

    if not args.dry_run:
        auth_headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
        }
        for (ticker, period, quarter), expected in _REPRESENTATIVE_TICKERS.items():
            r4 = requests.get(
                f"{url.rstrip('/')}/rest/v1/financials",
                headers=auth_headers,
                params={
                    "ticker": f"eq.{ticker}",
                    "period": f"eq.{period}",
                    "quarter": f"eq.{quarter}",
                    "select": "ticker,period,quarter,sales,operating_profit",
                },
                timeout=30,
            )
            if r4.status_code == 200 and r4.json():
                row = r4.json()[0]
                ok = True
                for col, exp_val in expected.items():
                    actual = row.get(col)
                    if actual != exp_val:
                        logger.error(
                            f"  ❌ {ticker} {period} {quarter}: "
                            f"{col}={actual} (expected {exp_val})"
                        )
                        ok = False
                        all_ok = False
                if ok:
                    logger.info(
                        f"  ✅ {ticker} {period} {quarter}: "
                        f"sales={row.get('sales')} op={row.get('operating_profit')}"
                    )
            else:
                logger.error(f"  ❌ {ticker} {period} {quarter}: 行なし")
                all_ok = False
    else:
        for (ticker, period, quarter), expected in _REPRESENTATIVE_TICKERS.items():
            matches = [
                r for r in fin_rows
                if r["ticker"] == ticker and r["period"] == period
                and r["quarter"] == quarter
            ]
            if matches:
                row = matches[0]
                ok = True
                for col, exp_val in expected.items():
                    if row.get(col) != exp_val:
                        logger.error(
                            f"  ❌ {ticker} {period} {quarter}: "
                            f"{col}={row.get(col)} (expected {exp_val})"
                        )
                        ok = False
                        all_ok = False
                if ok:
                    logger.info(
                        f"  ✅ {ticker} {period} {quarter}: "
                        f"sales={row.get('sales')} op={row.get('operating_profit')}"
                    )
            else:
                logger.warning(
                    f"  ⚠ {ticker} {period} {quarter}: ペイロードに不在"
                )

    # 最終レポート
    print()
    print("=" * 55)
    if args.dry_run:
        icon = "🔍"
        label = f"dry-run 完了 (生成: {len(fin_rows):,} 行)"
    elif all_ok and total_errors == 0:
        icon = "✅"
        label = f"再構築完了 ({total_sent:,} 行)"
    else:
        icon = "⚠"
        label = f"一部問題あり (sent={total_sent:,} errors={total_errors})"
    print(f"  {icon} {label}")
    print("=" * 55)
    print()

    sys.exit(0 if (all_ok or args.dry_run) else 1)


if __name__ == "__main__":
    main()
