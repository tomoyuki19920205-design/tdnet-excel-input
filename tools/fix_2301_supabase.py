#!/usr/bin/env python3
# ============================================================
# fix_2301_supabase.py — 2301 Supabase 修復スクリプト
# ============================================================
#
# 目的:
#   SQLite で修正済みの 2301 データを Supabase に反映する。
#   1. 誤った FY 行 (2301, 2026-10-31, FY) を削除
#   2. 全体再 push (upsert) で正しいデータを反映
#   3. 修復後の検証
#
# 使い方:
#   cd C:\Users\takuy\OneDrive\tdnet-excel-input
#   .\.venv\Scripts\python.exe tools/fix_2301_supabase.py
#   .\.venv\Scripts\python.exe tools/fix_2301_supabase.py --dry-run
#
# ============================================================
from __future__ import annotations

import io
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
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
if sys.stderr and hasattr(sys.stderr, "encoding"):
    if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace"
        )

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fix_2301")


# ============================================================
# .env 読み込み
# ============================================================
def _load_dotenv():
    env_path = Path(_PROJECT_ROOT) / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


def _get_supabase_config():
    _load_dotenv()
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_ANON_KEY", "")
    if not url or not key:
        raise ValueError(
            "SUPABASE_URL / SUPABASE_ANON_KEY が .env に未設定です"
        )
    return url, key


# ============================================================
# Supabase REST helpers
# ============================================================
class _API:
    def __init__(self, url: str, key: str):
        self.rest = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def select(self, table: str, params: dict) -> list[dict]:
        r = requests.get(
            f"{self.rest}/{table}",
            headers={**self.headers, "Prefer": ""},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def delete(self, table: str, params: dict) -> int:
        r = requests.delete(
            f"{self.rest}/{table}",
            headers={**self.headers, "Prefer": "return=representation"},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        return len(r.json())

    def count(self, table: str, params: dict | None = None) -> int:
        r = requests.get(
            f"{self.rest}/{table}",
            headers={
                **self.headers,
                "Prefer": "count=exact",
                "Range": "0-0",
            },
            params={**(params or {}), "select": "ticker"},
            timeout=30,
        )
        r.raise_for_status()
        cr = r.headers.get("Content-Range", "")
        # "0-0/12345" → 12345
        if "/" in cr:
            return int(cr.split("/")[1])
        return 0


# ============================================================
# メイン
# ============================================================
def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="2301 Supabase 修復"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Supabase への変更を実際には行わない",
    )
    parser.add_argument(
        "--db", default="decision_db.db",
        help="SQLite DB パス (default: decision_db.db)",
    )
    args = parser.parse_args()

    url, key = _get_supabase_config()
    api = _API(url, key)

    db_path = args.db
    if not os.path.isabs(db_path):
        db_path = os.path.join(_PROJECT_ROOT, db_path)

    print()
    print("=" * 55)
    print("  2301 Supabase 修復")
    print("=" * 55)
    if args.dry_run:
        print("  Mode: dry-run")
    print()

    # =========================================================
    # Step 1: 修復前の状態表示
    # =========================================================
    logger.info("[Step 1/4] 修復前の状態確認...")

    # SQLite
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sqlite_rows = conn.execute(
        "SELECT company_code, fiscal_year_end, quarter, sales, "
        "operating_profit, unit FROM quarterly_results "
        "WHERE company_code IN ('2301', '23010') "
        "ORDER BY fiscal_year_end DESC, quarter"
    ).fetchall()
    conn.close()
    logger.info(f"  SQLite 2301/23010: {len(sqlite_rows)} 行")

    # 修正済み 1Q 行を確認
    fixed_row = None
    for r in sqlite_rows:
        if (r["company_code"] == "2301"
                and r["fiscal_year_end"] == "2026-10-31"
                and r["quarter"] == "1Q"):
            fixed_row = dict(r)
            break
    if fixed_row:
        logger.info(
            f"  修正済み行: ticker=2301 period=2026-10-31 Q=1Q "
            f"sales={fixed_row['sales']} op={fixed_row['operating_profit']}"
        )
    else:
        logger.warning("  ⚠ 修正済み 1Q 行が SQLite に見つかりません!")

    # Supabase 修復前
    supa_2301 = api.select("financials", {
        "ticker": "eq.2301",
        "select": "ticker,period,quarter,sales,operating_profit",
        "order": "period.desc,quarter",
    })
    logger.info(f"  Supabase 2301: {len(supa_2301)} 行")

    # 誤った FY 行
    bad_fy = api.select("financials", {
        "ticker": "eq.2301",
        "period": "eq.2026-10-31",
        "quarter": "eq.FY",
        "select": "ticker,period,quarter,sales,operating_profit",
    })
    if bad_fy:
        logger.info(f"  ❌ 誤った FY 行: {bad_fy[0]}")
    else:
        logger.info("  ✅ 誤った FY 行は既に存在しない")

    # 正しい 1Q 行
    good_1q = api.select("financials", {
        "ticker": "eq.2301",
        "period": "eq.2026-10-31",
        "quarter": "eq.1Q",
        "select": "ticker,period,quarter,sales,operating_profit",
    })
    if good_1q:
        logger.info(f"  ✅ 正しい 1Q 行: {good_1q[0]}")
    else:
        logger.info("  ❌ 正しい 1Q 行がまだ存在しない")

    # =========================================================
    # Step 2: 誤った FY 行の削除
    # =========================================================
    logger.info("[Step 2/4] 誤った FY 行の削除...")
    if bad_fy:
        if args.dry_run:
            logger.info("  [dry-run] DELETE はスキップ")
        else:
            deleted = api.delete("financials", {
                "ticker": "eq.2301",
                "period": "eq.2026-10-31",
                "quarter": "eq.FY",
            })
            logger.info(f"  ✅ {deleted} 行削除完了")
    else:
        logger.info("  スキップ（対象行なし）")

    # =========================================================
    # Step 3: 全体再 push (既存スクリプト利用)
    # =========================================================
    logger.info("[Step 3/4] 全体再 push (upsert)...")

    if args.dry_run:
        logger.info("  [dry-run] push はスキップ")
    else:
        # tools/sqlite_to_supabase.py の push_sqlite_to_supabase を呼ぶ
        try:
            from tools.sqlite_to_supabase import push_sqlite_to_supabase

            stats = push_sqlite_to_supabase(
                db_path=db_path,
                supabase_url=url,
                supabase_key=key,
                dry_run=False,
            )
            logger.info(
                f"  push 完了: "
                f"sqlite_rows={stats.get('sqlite_rows', '?')} "
                f"financials_inserted={stats.get('financials_inserted', '?')} "
                f"errors={stats.get('errors', '?')}"
            )
            if stats.get("errors", 0) > 0:
                logger.warning(
                    f"  ⚠ {stats['errors']} 件のエラーが発生"
                )
        except Exception as e:
            logger.error(f"  push 失敗: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

    # =========================================================
    # Step 4: 修復後の検証
    # =========================================================
    logger.info("[Step 4/4] 修復後の検証...")

    # 4a. 誤った FY 行が消えたか
    bad_fy_after = api.select("financials", {
        "ticker": "eq.2301",
        "period": "eq.2026-10-31",
        "quarter": "eq.FY",
        "select": "ticker,period,quarter,sales,operating_profit",
    })
    if bad_fy_after:
        logger.error(f"  ❌ FY 行がまだ存在: {bad_fy_after[0]}")
    else:
        logger.info("  ✅ FY 行: 削除済み")

    # 4b. 正しい 1Q 行があるか
    good_1q_after = api.select("financials", {
        "ticker": "eq.2301",
        "period": "eq.2026-10-31",
        "quarter": "eq.1Q",
        "select": "ticker,period,quarter,sales,operating_profit",
    })
    if good_1q_after:
        row = good_1q_after[0]
        expected_sales = 1368000000
        expected_op = -692000000
        sales_ok = row.get("sales") == expected_sales
        op_ok = row.get("operating_profit") == expected_op
        if sales_ok and op_ok:
            logger.info(
                f"  ✅ 1Q 行: sales={row['sales']} op={row['operating_profit']} (正常)"
            )
        else:
            logger.error(
                f"  ❌ 1Q 行の値が不一致: "
                f"sales={row['sales']}(expected {expected_sales}) "
                f"op={row['operating_profit']}(expected {expected_op})"
            )
    else:
        logger.error("  ❌ 1Q 行がまだ存在しない")

    # 4c. 2301 の全行数
    supa_2301_after = api.select("financials", {
        "ticker": "eq.2301",
        "select": "ticker,period,quarter",
    })
    logger.info(f"  Supabase 2301 全行数: {len(supa_2301_after)}")

    # 4d. 他銘柄の影響チェック (サンプルで 1736 確認)
    supa_1736 = api.select("financials", {
        "ticker": "eq.1736",
        "select": "ticker,period,quarter",
    })
    total = api.count("financials")
    logger.info(f"  Supabase 1736: {len(supa_1736)} 行 (副作用なし確認用)")
    logger.info(f"  Supabase financials 全体: {total} 行")

    # =========================================================
    # 最終レポート
    # =========================================================
    print()
    print("=" * 55)
    all_ok = (not bad_fy_after) and bool(good_1q_after)
    if args.dry_run:
        print("  🔍 dry-run 完了 (変更なし)")
    elif all_ok:
        print("  ✅ 2301 Supabase 修復完了")
    else:
        print("  ⚠ 一部検証に失敗 — 上のログを確認")
    print("=" * 55)
    print()

    sys.exit(0 if (all_ok or args.dry_run) else 1)


if __name__ == "__main__":
    main()
