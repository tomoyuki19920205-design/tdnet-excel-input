#!/usr/bin/env python3
"""check_missing_gross_profit.py — gross_profit 欠損 / overwrite_risk 3分類検知ツール

3分類:
  A. raw_missing      — gross_profit IS NULL の単純一覧
  B. suspicious_missing — sales+op有 だが gross_profit=NULL (REIT/IFRS等 除外可)
  C. overwrite_risk   — 同一(ticker,period,quarter)に gp非NULL行とNULL行が共存

Usage:
  cd "C:\\Users\\takuy\\OneDrive\\tdnet-excel-input"
  .\\.venv\\Scripts\\python.exe tools/check_missing_gross_profit.py
  .\\.venv\\Scripts\\python.exe tools/check_missing_gross_profit.py --output-dir artifacts/gp_risk_report
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Windows cp932 対策
if sys.stdout and hasattr(sys.stdout, "encoding"):
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

JST = timezone(timedelta(hours=9))


# ============================================================
# テーブルごとの列名マッピング
# ============================================================
_TABLE_CONFIGS = {
    "jquants_financials_normalized": {
        "ticker": "local_code",
        "period": "current_fiscal_year_end_date",
        "quarter": "type_of_current_period",
        "sales": "net_sales",
        "gross_profit": "gross_profit",
        "operating_profit": "operating_profit",
        "disclosed_date": "disclosed_date",
    },
    "quarterly_results": {
        "ticker": "company_code",
        "period": "fiscal_year_end",
        "quarter": "quarter",
        "sales": "sales",
        "gross_profit": "gross_profit",
        "operating_profit": "operating_profit",
        "disclosed_date": None,  # not available
    },
}


# ============================================================
# A. raw_missing
# ============================================================
def check_raw_missing(db_path: str, table: str) -> list[dict]:
    """gross_profit IS NULL の全行。"""
    config = _TABLE_CONFIGS.get(table, _TABLE_CONFIGS["quarterly_results"])
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    query = f"""
        SELECT {config['ticker']} AS ticker,
               {config['period']} AS period,
               {config['quarter']} AS quarter,
               {config['sales']} AS sales,
               {config['gross_profit']} AS gross_profit,
               {config['operating_profit']} AS operating_profit
        FROM [{table}]
        WHERE {config['gross_profit']} IS NULL
        ORDER BY {config['ticker']}, {config['period']}, {config['quarter']}
    """
    rows = [dict(r) for r in conn.execute(query).fetchall()]
    conn.close()
    return rows


# ============================================================
# B. suspicious_missing
# ============================================================
def check_suspicious_missing(db_path: str, table: str) -> list[dict]:
    """sales+op有 かつ gross_profit=NULL の行。"""
    config = _TABLE_CONFIGS.get(table, _TABLE_CONFIGS["quarterly_results"])
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    query = f"""
        SELECT {config['ticker']} AS ticker,
               {config['period']} AS period,
               {config['quarter']} AS quarter,
               {config['sales']} AS sales,
               {config['gross_profit']} AS gross_profit,
               {config['operating_profit']} AS operating_profit
        FROM [{table}]
        WHERE {config['sales']} IS NOT NULL
          AND {config['operating_profit']} IS NOT NULL
          AND {config['gross_profit']} IS NULL
        ORDER BY {config['ticker']}, {config['period']}, {config['quarter']}
    """
    rows = [dict(r) for r in conn.execute(query).fetchall()]
    conn.close()
    return rows


# ============================================================
# C. overwrite_risk
# ============================================================
def check_overwrite_risk(db_path: str, table: str) -> list[dict]:
    """同一 (ticker, period, quarter) に gp 非NULL行と NULL行が共存。"""
    config = _TABLE_CONFIGS.get(table, _TABLE_CONFIGS["quarterly_results"])
    disclosed_col = config.get("disclosed_date")
    if not disclosed_col:
        return []  # disclosed_date がないテーブルでは検出不可

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 同一グループに gp_nonnull と gp_null が共存するケースを検出
    query = f"""
        WITH grouped AS (
          SELECT
            {config['ticker']} AS ticker,
            {config['period']} AS period,
            {config['quarter']} AS quarter,
            COUNT(*) AS total_rows,
            SUM(CASE WHEN {config['gross_profit']} IS NOT NULL THEN 1 ELSE 0 END) AS gp_nonnull_rows,
            SUM(CASE WHEN {config['gross_profit']} IS NULL THEN 1 ELSE 0 END) AS gp_null_rows,
            MAX({disclosed_col}) AS latest_disclosed_date
          FROM [{table}]
          WHERE {config['sales']} IS NOT NULL
            AND {config['operating_profit']} IS NOT NULL
          GROUP BY {config['ticker']}, {config['period']}, {config['quarter']}
          HAVING gp_nonnull_rows > 0 AND gp_null_rows > 0
        )
        SELECT g.*,
          -- latest row details
          (SELECT {config['gross_profit']} FROM [{table}]
           WHERE {config['ticker']} = g.ticker
             AND {config['period']} = g.period
             AND {config['quarter']} = g.quarter
           ORDER BY {disclosed_col} DESC LIMIT 1
          ) AS latest_gross_profit,
          -- previous non-null row details
          (SELECT {disclosed_col} FROM [{table}]
           WHERE {config['ticker']} = g.ticker
             AND {config['period']} = g.period
             AND {config['quarter']} = g.quarter
             AND {config['gross_profit']} IS NOT NULL
           ORDER BY {disclosed_col} DESC LIMIT 1
          ) AS nonnull_disclosed_date,
          (SELECT {config['gross_profit']} FROM [{table}]
           WHERE {config['ticker']} = g.ticker
             AND {config['period']} = g.period
             AND {config['quarter']} = g.quarter
             AND {config['gross_profit']} IS NOT NULL
           ORDER BY {disclosed_col} DESC LIMIT 1
          ) AS nonnull_gross_profit
        FROM grouped g
        ORDER BY g.ticker, g.period, g.quarter
    """
    rows = [dict(r) for r in conn.execute(query).fetchall()]
    conn.close()
    return rows


# ============================================================
# レポート生成
# ============================================================
def generate_report(
    *,
    db_path: str,
    table: str,
    raw: list[dict],
    suspicious: list[dict],
    overwrite: list[dict],
) -> str:
    """影響レポート Markdown を生成する。"""
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

    lines = [
        "# gross_profit 欠損 / overwrite_risk レポート",
        "",
        f"- **実行時刻**: {now}",
        f"- **DB**: `{db_path}`",
        f"- **Table**: `{table}`",
        "",
        "## サマリ",
        "",
        "| 分類 | 件数 | 説明 |",
        "|:---|---:|:---|",
        f"| A. raw_missing | {len(raw)} | gross_profit IS NULL の全行 |",
        f"| B. suspicious_missing | {len(suspicious)} | sales+op有 かつ gp=NULL |",
        f"| **C. overwrite_risk** | **{len(overwrite)}** | **同一グループに gp非NULL行とNULL行が共存** |",
        "",
    ]

    if overwrite:
        # overwrite_risk の上位 30 件を表示
        lines.append("## C. overwrite_risk 詳細 (上位30件)")
        lines.append("")
        lines.append(
            "| ticker | period | q | total | gp_nonnull | gp_null "
            "| latest_date | latest_gp | nonnull_date | nonnull_gp |"
        )
        lines.append(
            "|:---|:---|:---|---:|---:|---:|:---|:---|:---|:---|"
        )
        for r in overwrite[:30]:
            lg = r.get("latest_gross_profit")
            lg_str = "NULL" if lg is None else str(lg)
            ng = r.get("nonnull_gross_profit")
            ng_str = str(ng) if ng is not None else "N/A"
            lines.append(
                f"| {r['ticker']} | {r['period']} | {r['quarter']} "
                f"| {r['total_rows']} | {r['gp_nonnull_rows']} | {r['gp_null_rows']} "
                f"| {r['latest_disclosed_date']} | {lg_str} "
                f"| {r.get('nonnull_disclosed_date', 'N/A')} | {ng_str} |"
            )
        if len(overwrite) > 30:
            lines.append(f"\n*...他 {len(overwrite) - 30} 件省略*")
        lines.append("")

    # overwrite_risk が修正後 sync で回避されるか
    lines.append("## 対策状況")
    lines.append("")
    lines.append(
        "- `sync_financials.py` は field-level COALESCE merge 実装済み"
    )
    lines.append(
        "- 上記 overwrite_risk ケースは sync 実行時に最新非NULL値が採用される"
    )
    lines.append(
        "- Supabase への反映には `sync_financials.py` の再実行が必要"
    )
    lines.append("")

    return "\n".join(lines)


# ============================================================
# CSV 出力
# ============================================================
def _write_csv(rows: list[dict], path: str) -> None:
    if not rows:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("")
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


# ============================================================
# メイン
# ============================================================
def main(args=None):
    parser = argparse.ArgumentParser(
        description="gross_profit 欠損 / overwrite_risk 3分類検知ツール"
    )
    parser.add_argument("--db", default="data/jquants.db",
                        help="SQLite DB パス")
    parser.add_argument("--table", default="jquants_financials_normalized",
                        help="テーブル名")
    parser.add_argument("--output-dir", default=None,
                        help="CSV/レポート出力先 (指定時のみ出力)")
    opts = parser.parse_args(args)

    os.chdir(_PROJECT_ROOT)

    db_path = opts.db
    if not os.path.isfile(db_path):
        print(f"DB not found: {db_path}")
        return 1

    raw = check_raw_missing(db_path, opts.table)
    suspicious = check_suspicious_missing(db_path, opts.table)
    overwrite = check_overwrite_risk(db_path, opts.table)

    print(f"\n{'='*60}")
    print(f"  gross_profit 欠損 / overwrite_risk 3分類レポート")
    print(f"  DB: {db_path}")
    print(f"  Table: {opts.table}")
    print(f"{'='*60}")
    print(f"\n  A. raw_missing:         {len(raw):>5,} 行")
    print(f"  B. suspicious_missing:  {len(suspicious):>5,} 行")
    print(f"  C. overwrite_risk:      {len(overwrite):>5,} グループ  *** 最優先 ***")

    if overwrite:
        print(f"\n  overwrite_risk 上位10件:")
        print(f"  {'ticker':>8s}  {'period':>12s}  {'q':>3s}  {'total':>5s}  {'gp+':>4s}  {'gp-':>4s}  {'latest_gp':>15s}")
        for r in overwrite[:10]:
            lg = r.get("latest_gross_profit")
            lg_str = "NULL" if lg is None else str(lg)
            print(
                f"  {r['ticker']:>8s}  {r['period']:>12s}  {r['quarter']:>3s}"
                f"  {r['total_rows']:>5d}  {r['gp_nonnull_rows']:>4d}"
                f"  {r['gp_null_rows']:>4d}  {lg_str:>15s}"
            )
        if len(overwrite) > 10:
            print(f"  ...他 {len(overwrite) - 10} 件")

    if not overwrite and not suspicious:
        print("\n  ✅ 問題なし")

    # 出力
    if opts.output_dir:
        od = opts.output_dir
        Path(od).mkdir(parents=True, exist_ok=True)
        _write_csv(raw, os.path.join(od, "raw_missing.csv"))
        _write_csv(suspicious, os.path.join(od, "suspicious_missing.csv"))
        _write_csv(overwrite, os.path.join(od, "overwrite_risk.csv"))
        report = generate_report(
            db_path=db_path, table=opts.table,
            raw=raw, suspicious=suspicious, overwrite=overwrite,
        )
        rpt_path = os.path.join(od, "gp_risk_report.md")
        with open(rpt_path, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\n  出力先: {od}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
