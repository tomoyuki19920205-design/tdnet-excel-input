#!/usr/bin/env python3
"""debug_2301_gross_profit_gap.py — 2301 gross_profit 欠損調査ツール

2301 の gross_profit 欠損（2024-10-31 2Q / 2022-10-31 FY）の
原因を可視化する調査用スクリプト。

Usage:
  cd "C:\\Users\\takuy\\OneDrive\\tdnet-excel-input"
  .\\.venv\\Scripts\\python.exe tools/debug_2301_gross_profit_gap.py
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows cp932 対策
if sys.stdout and hasattr(sys.stdout, "encoding"):
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )


def _safe(val):
    """表示用の安全な文字列変換"""
    if val is None:
        return "NULL"
    return str(val)


def main():
    os.chdir(_PROJECT_ROOT)

    print("=" * 70)
    print("  2301 gross_profit 欠損 調査レポート")
    print("=" * 70)

    # =========================================================
    # 1. J-Quants DB: 同一期間の複数行を表示
    # =========================================================
    print("\n--- 1. J-Quants DB: 2301 全行 ---")
    jq_db = "data/jquants.db"
    if not os.path.isfile(jq_db):
        print(f"  {jq_db} が見つかりません")
    else:
        conn = sqlite3.connect(jq_db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT disclosed_date, current_fiscal_year_end_date AS fye,
                   type_of_current_period AS quarter,
                   net_sales, gross_profit, operating_profit
            FROM jquants_financials_normalized
            WHERE local_code LIKE '%2301%'
            ORDER BY current_fiscal_year_end_date, type_of_current_period, disclosed_date
        """)
        rows = cur.fetchall()
        print(f"  行数: {len(rows)}")
        print(f"  {'disclosed':>12s}  {'fye':>12s}  {'q':>3s}  {'net_sales':>15s}  {'gross_profit':>15s}  {'op':>15s}")
        print(f"  {'-'*12}  {'-'*12}  {'-'*3}  {'-'*15}  {'-'*15}  {'-'*15}")
        for r in rows:
            gp = r["gross_profit"]
            flag = " *** NULL ***" if gp is None else ""
            print(f"  {r['disclosed_date']:>12s}  {r['fye']:>12s}  {_safe(r['quarter']):>3s}"
                  f"  {_safe(r['net_sales']):>15s}  {_safe(gp):>15s}  {_safe(r['operating_profit']):>15s}{flag}")
        conn.close()

    # =========================================================
    # 2. 対象2件の詳細
    # =========================================================
    TARGET_CASES = [
        ("2024-10-31", "2Q"),
        ("2022-10-31", "FY"),
    ]

    if os.path.isfile(jq_db):
        conn = sqlite3.connect(jq_db)
        conn.row_factory = sqlite3.Row
        for fye, q in TARGET_CASES:
            print(f"\n--- 2. 詳細: {fye} {q} ---")
            cur = conn.cursor()
            cur.execute("""
                SELECT disclosed_date, net_sales, gross_profit, operating_profit
                FROM jquants_financials_normalized
                WHERE local_code LIKE '%2301%'
                  AND current_fiscal_year_end_date = ?
                  AND type_of_current_period = ?
                ORDER BY disclosed_date
            """, (fye, q))
            rows = cur.fetchall()
            print(f"  行数: {len(rows)}")
            for r in rows:
                gp = r["gross_profit"]
                flag = " << NULL: これが原因" if gp is None else " (有値)"
                print(f"  disclosed={r['disclosed_date']} sales={_safe(r['net_sales'])} "
                      f"gp={_safe(gp)} op={_safe(r['operating_profit'])}{flag}")

            if len(rows) >= 2:
                first = rows[0]
                last = rows[-1]
                if first["gross_profit"] is not None and last["gross_profit"] is None:
                    print(f"  >> 診断: 先行開示({first['disclosed_date']})にgross_profit有, "
                          f"訂正開示({last['disclosed_date']})にgross_profit=NULL")
                    print(f"  >> 影響: ROW_NUMBER DESC で訂正開示が採用され、gross_profit が消失")
                    print(f"  >> 修正: field-level COALESCE merge で先行開示の値を保持")
        conn.close()

    # =========================================================
    # 3. 修正後のSQL結果確認
    # =========================================================
    print("\n--- 3. 修正後SQL (field-level COALESCE) の結果 ---")
    if os.path.isfile(jq_db):
        conn = sqlite3.connect(jq_db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # 修正後クエリを直接実行
        cur.execute("""
            WITH latest AS (
              SELECT local_code, current_fiscal_year_end_date, type_of_current_period,
                     ROW_NUMBER() OVER (
                       PARTITION BY local_code, current_fiscal_year_end_date, type_of_current_period
                       ORDER BY disclosed_date DESC
                     ) AS rn,
                     net_sales, gross_profit, operating_profit
              FROM jquants_financials_normalized
              WHERE local_code LIKE '%2301%'
            ),
            field_best AS (
              SELECT local_code, current_fiscal_year_end_date, type_of_current_period,
                (SELECT s.net_sales FROM latest s WHERE s.local_code=latest.local_code
                 AND s.current_fiscal_year_end_date=latest.current_fiscal_year_end_date
                 AND s.type_of_current_period=latest.type_of_current_period
                 AND s.net_sales IS NOT NULL ORDER BY s.rn LIMIT 1) AS net_sales,
                (SELECT s.gross_profit FROM latest s WHERE s.local_code=latest.local_code
                 AND s.current_fiscal_year_end_date=latest.current_fiscal_year_end_date
                 AND s.type_of_current_period=latest.type_of_current_period
                 AND s.gross_profit IS NOT NULL ORDER BY s.rn LIMIT 1) AS gross_profit,
                (SELECT s.operating_profit FROM latest s WHERE s.local_code=latest.local_code
                 AND s.current_fiscal_year_end_date=latest.current_fiscal_year_end_date
                 AND s.type_of_current_period=latest.type_of_current_period
                 AND s.operating_profit IS NOT NULL ORDER BY s.rn LIMIT 1) AS operating_profit
              FROM latest WHERE rn = 1
            )
            SELECT * FROM field_best
            WHERE current_fiscal_year_end_date IN ('2024-10-31', '2022-10-31')
              AND type_of_current_period IN ('2Q', 'FY')
            ORDER BY current_fiscal_year_end_date, type_of_current_period
        """)
        for r in cur.fetchall():
            gp = r["gross_profit"]
            status = "FIXED" if gp is not None else "STILL NULL"
            print(f"  {r['current_fiscal_year_end_date']} {r['type_of_current_period']}: "
                  f"sales={_safe(r['net_sales'])} gp={_safe(gp)} op={_safe(r['operating_profit'])} [{status}]")
        conn.close()

    # =========================================================
    # 4. decision_db 状態
    # =========================================================
    print("\n--- 4. decision_db.db: 2301 ---")
    dec_db = "decision_db.db"
    if os.path.isfile(dec_db):
        conn = sqlite3.connect(dec_db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT fiscal_year_end, quarter, sales, gross_profit, operating_profit
            FROM quarterly_results
            WHERE company_code = '2301'
            ORDER BY fiscal_year_end, quarter
        """)
        for r in cur.fetchall():
            print(f"  {r['fiscal_year_end']} {r['quarter']}: "
                  f"sales={_safe(r['sales'])} gp={_safe(r['gross_profit'])} op={_safe(r['operating_profit'])}")
        conn.close()
    else:
        print(f"  {dec_db} が見つかりません")

    print("\n" + "=" * 70)
    print("  調査完了")
    print("=" * 70)


if __name__ == "__main__":
    main()
