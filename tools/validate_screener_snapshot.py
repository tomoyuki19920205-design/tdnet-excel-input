#!/usr/bin/env python3
"""Produce the reproducible real-data validation report for the screener."""
from __future__ import annotations

from collections import Counter
import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lib.screener_snapshot import build_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "jquants.db"))
    parser.add_argument("--output")
    args = parser.parse_args()
    build = build_snapshot(args.db)
    with sqlite3.connect(args.db) as connection:
        split_tickers = [row[0] for row in connection.execute(
            "SELECT DISTINCT ticker FROM market_data WHERE adj_factor NOT IN (1,1.0) ORDER BY ticker"
        )]
        fiscal_months: Counter[str] = Counter()
        for (raw_text,) in connection.execute(
            "SELECT raw_json FROM jquants_financials_normalized WHERE raw_json IS NOT NULL"
        ):
            try:
                raw = json.loads(raw_text)
            except (TypeError, json.JSONDecodeError):
                continue
            period = str(raw.get("CurFYEn") or "")
            if len(period) >= 7:
                fiscal_months[period[5:7]] += 1
    report = {
        "batch_id": build.batch_id,
        "universe_date": build.universe_date,
        "row_count": len(build.rows),
        "coverage": build.coverage,
        "null_reasons": build.null_reasons,
        "price_status": dict(Counter(row["price_status"] for row in build.rows)),
        "price_stale_sessions": dict(Counter(str(row["price_stale_sessions"]) for row in build.rows if row["price_status"] in {"no_trade", "stale_unknown"})),
        "accounting_standard": dict(Counter(str(row["accounting_standard"]) for row in build.rows)),
        "fiscal_year_end_months": dict(sorted(fiscal_months.items())),
        "split_ticker_count": len(split_tickers),
        "split_validation_sample": split_tickers[:10],
        "multi_revision_tickers": sum((row["any_earnings_upward_revision_event_count_3y"] or 0) >= 2 for row in build.rows),
        "flags": {
            key: sum(bool(row[key]) for row in build.rows)
            for key in ("fiscal_period_changed", "turnaround", "loss_expansion", "profit_to_loss", "insufficient_price_history", "peg_denominator_small")
        },
        "revision_event_count": len(build.revision_events),
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
