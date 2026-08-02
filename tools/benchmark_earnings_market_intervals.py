#!/usr/bin/env python3
"""Measure batched versus per-ticker source reads for interval analysis."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.analyze_earnings_market_intervals import read_sources
from src.analysis.earnings_market_intervals import analyze_ticker


def tickers(db: Path, count: int) -> list[str]:
    conn = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True)
    rows = conn.execute("SELECT ticker FROM market_data WHERE source='jquants' GROUP BY ticker HAVING COUNT(*) >= 120 ORDER BY ticker LIMIT ?", (count,)).fetchall()
    conn.close()
    return [str(row[0]) for row in rows]


def one_run(db: Path, size: int, as_of: str) -> dict:
    selected = tickers(db, size)
    tracemalloc.start(); started = time.perf_counter()
    market, financial, per_share = read_sources(db, selected, as_of)
    read_seconds = time.perf_counter() - started
    compute_started = time.perf_counter()
    results = [analyze_ticker(t, [r for r in market if r["ticker"] == t], financial.get(t, []), [r for r in per_share if r["ticker"] == t], as_of_date=as_of, as_of_timestamp=f"{as_of}T23:59:59+09:00") for t in selected]
    compute_seconds = time.perf_counter() - compute_started
    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
    serial_started = time.perf_counter()
    serial_rows = 0
    for ticker in selected:
        m, f, p = read_sources(db, [ticker], as_of); serial_rows += len(m) + sum(len(v) for v in f.values()) + len(p)
    serial_seconds = time.perf_counter() - serial_started
    return {
        "ticker_count": len(selected), "batched_query_count": 3, "serial_query_count": 3 * len(selected),
        "market_rows": len(market), "financial_rows": sum(len(v) for v in financial.values()), "per_share_rows": len(per_share),
        "batched_read_seconds": read_seconds, "calculation_seconds": compute_seconds, "total_seconds": read_seconds + compute_seconds,
        "serial_read_seconds": serial_seconds, "serial_rows": serial_rows, "peak_memory_bytes": peak,
        "completed_analyses": sum("periods" in r for r in results),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "jquants.db"))
    parser.add_argument("--as-of", default="2026-07-14")
    parser.add_argument("--sizes", default="15,50,100")
    parser.add_argument("--output-dir", default=str(ROOT / "output" / "earnings_market_interval_evidence"))
    args = parser.parse_args()
    result = {"as_of_date": args.as_of, "runs": [one_run(Path(args.db), int(size), args.as_of) for size in args.sizes.split(",") if size.strip()]}
    out = Path(args.output_dir); out.mkdir(parents=True, exist_ok=True)
    (out / f"benchmark_{args.as_of}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
