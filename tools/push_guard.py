#!/usr/bin/env python3
# ============================================================
# push_guard.py — SQLite → Supabase push 前の安全検証
# ============================================================
#
# 使い方:
#   cd C:\Users\takuy\OneDrive\tdnet-excel-input
#   .\.venv\Scripts\python.exe tools/push_guard.py --dry-run
#   .\.venv\Scripts\python.exe tools/push_guard.py
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
logger = logging.getLogger("push_guard")

from tools.sqlite_to_supabase import (
    _build_financials_rows_from_tdnet,
    _load_dotenv,
)


# ============================================================
# 定数
# ============================================================
_MAX_SALES_MILLIONS = 100_000_000    # 100兆円 (百万円)
_DIGIT_DIFF_THRESHOLD = 3           # 同一銘柄内で3桁差 → 混在疑い

# 代表銘柄の期待値 (百万円)
_REPRESENTATIVE = {
    ("2301", "2026-10-31", "1Q"): {"sales": 1368, "operating_profit": -692},
    ("1736", "2026-03-31", "1Q"): {"sales": 6485, "operating_profit": 522},
    ("5805", "2026-03-31", "1Q"): {"sales": 8147, "operating_profit": 773},
}


# ============================================================
# ガード関数
# ============================================================
class GuardResult:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str):
        self.errors.append(msg)
        logger.error(f"  ❌ {msg}")

    def warn(self, msg: str):
        self.warnings.append(msg)
        logger.warning(f"  ⚠ {msg}")

    def ok(self, msg: str):
        logger.info(f"  ✅ {msg}")

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0


def guard_unit_anomaly(rows: list[dict], result: GuardResult):
    """ガード1: 単位異常検出 — sales > 100兆百万円"""
    bad = []
    for r in rows:
        s = r.get("sales")
        if s is not None and abs(s) > _MAX_SALES_MILLIONS:
            bad.append(
                f"{r['ticker']} {r['period']} {r['quarter']} "
                f"sales={s}"
            )
    if bad:
        result.error(f"単位異常 ({len(bad)}件): sales > {_MAX_SALES_MILLIONS}")
        for b in bad[:5]:
            logger.error(f"    {b}")
    else:
        result.ok("単位異常なし")


def guard_null_to_zero(
    sqlite_rows: list, payload: list[dict], result: GuardResult,
):
    """ガード2: NULL→0 変換検出"""
    # SQLite の None が payload で 0 になっていないかチェック
    # rows は同じ順序であること前提はないので、キーで照合
    payload_map = {}
    for r in payload:
        k = (r["ticker"], r["period"], r["quarter"])
        payload_map[k] = r

    from tools.sqlite_to_supabase import normalize_ticker, _normalize_financials_quarter

    violations = 0
    cols = ("sales", "gross_profit", "operating_profit")

    for sr in sqlite_rows:
        ticker = normalize_ticker(sr["company_code"])
        quarter = _normalize_financials_quarter(sr["quarter"])
        if not ticker or quarter is None:
            continue
        k = (ticker, sr["fiscal_year_end"], quarter)
        pr = payload_map.get(k)
        if pr is None:
            continue

        for col in cols:
            sqlite_val = sr[col]
            payload_val = pr.get(col)
            if sqlite_val is None and payload_val == 0:
                violations += 1
                if violations <= 3:
                    result.error(
                        f"NULL→0 変換: {ticker} {sr['fiscal_year_end']} {quarter} "
                        f"{col}: SQLite=None → payload=0"
                    )

    if violations == 0:
        result.ok("NULL→0 変換なし")
    elif violations > 3:
        result.error(f"  ...他 {violations - 3} 件")


def guard_count(
    sqlite_count: int, payload_count: int, result: GuardResult,
):
    """ガード3: 件数チェック"""
    if payload_count == 0:
        result.error("生成件数が 0")
        return

    ratio = payload_count / sqlite_count if sqlite_count > 0 else 0
    if ratio < 0.5:
        result.error(
            f"生成件数が少なすぎる: {payload_count}/{sqlite_count} "
            f"({ratio:.1%})"
        )
    else:
        result.ok(
            f"件数正常: {payload_count}/{sqlite_count} ({ratio:.1%})"
        )


def guard_representative(
    payload: list[dict], result: GuardResult,
):
    """ガード4: 代表銘柄サンプル照合"""
    for (ticker, period, quarter), expected in _REPRESENTATIVE.items():
        matches = [
            r for r in payload
            if r["ticker"] == ticker and r["period"] == period
            and r["quarter"] == quarter
        ]
        if not matches:
            result.warn(f"{ticker} {period} {quarter}: ペイロードに不在")
            continue

        row = matches[0]
        ok = True
        for col, exp_val in expected.items():
            actual = row.get(col)
            if actual != exp_val:
                result.error(
                    f"{ticker} {period} {quarter}: "
                    f"{col}={actual} (expected {exp_val})"
                )
                ok = False
        if ok:
            result.ok(
                f"{ticker} {period} {quarter}: "
                f"sales={row.get('sales')} op={row.get('operating_profit')}"
            )


def guard_scale_consistency(
    payload: list[dict], result: GuardResult,
):
    """ガード5: 同一銘柄内の桁差で円/百万円混在を検出"""
    from collections import defaultdict
    import math

    ticker_sales: dict[str, list[float]] = defaultdict(list)
    for r in payload:
        s = r.get("sales")
        if s is not None and s != 0:
            ticker_sales[r["ticker"]].append(abs(s))

    suspicious = []
    for ticker, values in ticker_sales.items():
        if len(values) < 2:
            continue
        max_v = max(values)
        min_v = min(values)
        if min_v > 0:
            digit_diff = math.log10(max_v) - math.log10(min_v)
            if digit_diff > _DIGIT_DIFF_THRESHOLD:
                suspicious.append(
                    f"{ticker}: min={min_v:.0f} max={max_v:.0f} "
                    f"(桁差={digit_diff:.1f})"
                )

    if suspicious:
        result.warn(
            f"桁差が大きい銘柄 ({len(suspicious)}件) — 混在の可能性:"
        )
        for s in suspicious[:10]:
            logger.warning(f"    {s}")
    else:
        result.ok("桁一貫性チェック OK")


def guard_payload_unique(
    payload: list[dict], result: GuardResult,
):
    """ガード6: payload key 重複検知

    _build_financials_rows_from_tdnet のマージ後は重複ゼロが期待される。
    万一重複が残っていれば batch upsert 500 error の原因になるため error。
    """
    from collections import Counter

    key_counts = Counter(
        (r["ticker"], r["period"], r["quarter"]) for r in payload
    )
    duplicates = {k: v for k, v in key_counts.items() if v > 1}

    if duplicates:
        result.error(
            f"payload key 重複: {len(duplicates)} 件"
        )
        for (ticker, period, quarter), count in sorted(duplicates.items()):
            logger.error(
                f"    {ticker} {period} {quarter}: {count}回"
            )
    else:
        result.ok(f"payload key 一意: {len(payload)} 行 全ユニーク")


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="SQLite → Supabase push 前の安全検証",
    )
    parser.add_argument("--db", default="decision_db.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="検証のみ実行、push はしない")
    args = parser.parse_args()

    db_path = args.db
    if not os.path.isabs(db_path):
        db_path = os.path.join(_PROJECT_ROOT, db_path)

    print()
    print("=" * 55)
    print("  push_guard: SQLite → Supabase 安全検証")
    print("=" * 55)
    print()

    # Step 1: SQLite からペイロード生成
    logger.info("[Step 1/3] SQLite からペイロード生成...")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sqlite_rows = conn.execute(
        "SELECT * FROM quarterly_results ORDER BY id"
    ).fetchall()
    conn.close()
    logger.info(f"  SQLite 行数: {len(sqlite_rows):,}")

    payload = _build_financials_rows_from_tdnet(sqlite_rows)
    logger.info(f"  生成行数: {len(payload):,}")

    # サンプル表示
    logger.info("[サンプル]")
    for r in payload[:3]:
        logger.info(
            f"  {r['ticker']} {r['period']} {r['quarter']} "
            f"sales={r['sales']} gp={r['gross_profit']} op={r['operating_profit']}"
        )

    # Step 2: ガード実行
    logger.info("\n[Step 2/3] ガード検証...")
    guard = GuardResult()

    logger.info("[Guard 1] 単位異常検出...")
    guard_unit_anomaly(payload, guard)

    logger.info("[Guard 2] NULL→0 変換検出...")
    guard_null_to_zero(sqlite_rows, payload, guard)

    logger.info("[Guard 3] 件数チェック...")
    guard_count(len(sqlite_rows), len(payload), guard)

    logger.info("[Guard 4] 代表銘柄照合...")
    guard_representative(payload, guard)

    logger.info("[Guard 5] 桁一貫性チェック...")
    guard_scale_consistency(payload, guard)

    logger.info("[Guard 6] payload key 重複検知...")
    guard_payload_unique(payload, guard)

    # Step 3: 結果
    print()
    print("=" * 55)
    if guard.passed:
        print(f"  ✅ 全ガード通過 (warnings={len(guard.warnings)})")
        if guard.warnings:
            print(f"  ⚠ {len(guard.warnings)} 件の警告あり（上のログ参照）")
    else:
        print(f"  ❌ ガード失敗: {len(guard.errors)} 件のエラー")
        for e in guard.errors:
            print(f"    ❌ {e}")
        print()
        print("  push を中止します。エラーを修正してから再実行してください。")
    print("=" * 55)
    print()

    sys.exit(0 if guard.passed else 1)


if __name__ == "__main__":
    main()
