#!/usr/bin/env python3
"""Build independent, leakage-safe between-earnings market analysis outputs."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.analysis.earnings_market_intervals import CALCULATION_VERSION, analyze_ticker


def read_sources(db_path: Path, tickers: list[str], as_of: str):
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in tickers)
    market = [dict(r) for r in conn.execute(f"SELECT ticker,date,close,adj_close,volume,adj_volume,turnover,adj_factor FROM market_data WHERE source='jquants' AND ticker IN ({placeholders}) AND date<=? ORDER BY ticker,date", [*tickers, as_of])]
    # normalized codes are five-character J-Quants codes while market tickers are normalized.
    codes = [f"{ticker}0" if len(ticker) == 4 else ticker for ticker in tickers]
    financial = [dict(r) for r in conn.execute(f"SELECT local_code,disclosed_date,current_fiscal_year_end_date,type_of_current_period,type_of_document,raw_json FROM jquants_financials_normalized WHERE local_code IN ({','.join('?' for _ in codes)}) AND disclosed_date<=? ORDER BY local_code,disclosed_date", [*codes, as_of])]
    per_share = [dict(r) for r in conn.execute(f"SELECT * FROM per_share_data WHERE ticker IN ({placeholders}) AND disclosed_date<=? ORDER BY ticker,disclosed_date,period,quarter", [*tickers, as_of])]
    conn.close()
    by_financial = {ticker: [] for ticker in tickers}
    for row in financial:
        ticker = str(row["local_code"])[:-1] if str(row["local_code"]).endswith("0") else str(row["local_code"])
        by_financial.setdefault(ticker, []).append({"ticker": ticker, "disclosed_date": row["disclosed_date"], "fiscal_year_end": row["current_fiscal_year_end_date"], "quarter": row["type_of_current_period"], "type_of_document": row["type_of_document"], "raw": row["raw_json"]})
    return market, by_financial, per_share


def markdown(results: list[dict]) -> str:
    lines = ["# 決算間市場・評価倍率分析", "", f"- calculation_version: `{CALCULATION_VERSION}`", "- 株価推移は adj_close、評価倍率は close と同時点の分割調整済み一株指標を使用。", "- 開示値の適用開始日は常に翌営業日。", "", "| ticker | A return | B return | A PER median | B PER median | A PBR median | B PBR median | status |", "|---|---:|---:|---:|---:|---:|---:|---|"]
    for r in results:
        periods = r.get("periods", [{}, {}]); a, b = periods[0], periods[1]
        def f(v): return "" if v is None else f"{v:.4f}"
        lines.append(f"| {r['ticker']} | {f(a.get('total_return'))} | {f(b.get('total_return'))} | {f(a.get('forward_per_median'))} | {f(b.get('forward_per_median'))} | {f(a.get('pbr_median'))} | {f(b.get('pbr_median'))} | {a.get('calculation_status', r.get('calculation_status',''))}/{b.get('calculation_status','')} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", required=True, help="comma-separated normalized tickers")
    parser.add_argument("--as-of", required=True, help="inclusive last usable trade date, YYYY-MM-DD")
    parser.add_argument("--db", default=str(ROOT / "data" / "jquants.db"))
    parser.add_argument("--output-dir", default=str(ROOT / "output" / "earnings_market_interval_evidence"))
    parser.add_argument("--dry-run", action="store_true", help="read and calculate, do not write outputs")
    args = parser.parse_args()
    tickers = [x.strip().upper() for x in args.tickers.split(",") if x.strip()]
    market, financial, per_share = read_sources(Path(args.db), tickers, args.as_of)
    results = []
    for ticker in tickers:
        results.append(analyze_ticker(ticker, [r for r in market if r["ticker"] == ticker], financial.get(ticker, []), [r for r in per_share if r["ticker"] == ticker], as_of_date=args.as_of, as_of_timestamp=f"{args.as_of}T23:59:59+09:00"))
    summary = {"calculation_version": CALCULATION_VERSION, "as_of_date": args.as_of, "ticker_count": len(tickers), "source_rows": {"market_data": len(market), "financials": sum(len(v) for v in financial.values()), "per_share_data": len(per_share)}, "results": results}
    if args.dry_run:
        print(json.dumps({k: v for k, v in summary.items() if k != "results"}, ensure_ascii=False, indent=2)); return 0
    output = Path(args.output_dir); output.mkdir(parents=True, exist_ok=True)
    (output / f"analysis_{args.as_of}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / f"analysis_{args.as_of}.md").write_text(markdown(results), encoding="utf-8")
    with (output / f"analysis_{args.as_of}.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = ["ticker", "period_label", "period_start_date", "period_end_date", "trading_days", "total_return", "annualized_volatility", "max_drawdown", "average_volume", "median_volume", "average_turnover", "median_turnover", "forward_per_median", "pbr_median", "forward_dividend_yield_median", "calculation_status", "insufficient_reasons"]
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
        for result in results:
            for period in result.get("periods", []): writer.writerow({key: result["ticker"] if key == "ticker" else period.get(key) for key in fields})
    print(json.dumps({"output_dir": str(output), "ticker_count": len(tickers), "source_rows": summary["source_rows"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
