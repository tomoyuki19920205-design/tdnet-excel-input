#!/usr/bin/env python3
"""Read-only live probe for the 2026-09-01 canonical market snapshot."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.ny_market_market_data import (
    LiveDiscrepancyArbitrator,
    MarketDataError,
    NasdaqScreenerProvider,
    YahooChartProvider,
    build_index_sector_snapshot,
    fetch_all_or_fallback,
    rank_top20,
    screener_candidate_symbols,
)


TARGET = date(2026, 9, 1)
EXPECTED_INDEXES = {
    "SOX": (11288.6123, -2.136),
    "S&P500": (7631.4702, -0.711),
    "Dow": (52766.8789, -0.788),
    "Nasdaq": (26099.7734, -1.028),
    "Russell 2000": (2920.1323, -1.229),
}
EXPECTED_SECTORS = {
    "XLE": 1.266, "XLU": 0.781, "XLV": 0.663, "XLP": 0.318,
    "XLRE": -0.159, "XLC": -0.520, "XLF": -0.884, "XLB": -1.177,
    "XLI": -1.370, "XLK": -1.534, "XLY": -1.715,
}
EXPECTED_TOP20 = [
    ("SSM", 77.425), ("FLYE", 61.765), ("BIAF", 44.518), ("RDAC", 41.447),
    ("GPRO", 40.379), ("NWGL", 38.807), ("SWVL", 31.615), ("PETZ", 28.696),
    ("FRVO", 28.414), ("LIDR", 27.826), ("SST", 26.786), ("FBLG", 22.674),
    ("GWAV", 19.388), ("RDIB", 17.219), ("MOVE", 16.986), ("PXS", 16.231),
    ("NVNI", 16.214), ("GYGY", 15.966), ("PTN", 15.516), ("PSIG", 15.337),
]


def close_enough(actual: float, expected: float, tolerance: float) -> bool:
    return abs(actual - expected) <= tolerance


def run() -> dict:
    providers = [
        YahooChartProvider(host="query1.finance.yahoo.com"),
        YahooChartProvider(host="query2.finance.yahoo.com"),
    ]
    snapshot = build_index_sector_snapshot(providers, TARGET)
    index_checks = []
    for item in snapshot["indexes"]:
        expected_close, expected_pct = EXPECTED_INDEXES[item["symbol"]]
        passed = close_enough(item["regular_close"], expected_close, 0.05) and close_enough(
            item["change_pct"], expected_pct, 0.003
        )
        index_checks.append({
            "symbol": item["symbol"], "regular_close": item["regular_close"],
            "change_pct": item["change_pct"], "provider": item["provider"], "pass": passed,
        })
    sector_checks = []
    sectors_by_symbol = {item["symbol"]: item for item in snapshot["sectors"]}
    for symbol, expected_pct in EXPECTED_SECTORS.items():
        item = sectors_by_symbol[symbol]
        sector_checks.append({
            "symbol": symbol, "regular_close": item["regular_close"], "change_pct": item["change_pct"],
            "provider": item["provider"], "pass": close_enough(item["change_pct"], expected_pct, 0.003),
        })

    screener = NasdaqScreenerProvider().fetch()
    candidate_symbols = screener_candidate_symbols(screener, limit=60)
    historical = fetch_all_or_fallback(
        providers, candidate_symbols, date(2026, 8, 31), TARGET,
    )
    top20 = rank_top20(
        screener, target_session_date=TARGET, historical_series=historical.series,
        discrepancy_arbitrator=LiveDiscrepancyArbitrator(),
    )
    top_checks = []
    for index, (expected_symbol, expected_change) in enumerate(EXPECTED_TOP20):
        item = top20[index]
        top_checks.append({
            "rank": index + 1, "ticker": item["ticker"], "change_pct": item["change_pct"],
            "expected_ticker": expected_symbol, "expected_change_pct": expected_change,
            "pass": item["ticker"] == expected_symbol and close_enough(item["change_pct"], expected_change, 0.001),
            "market_cap": item["market_cap"], "market_cap_method": item["market_cap_method"],
            "review_flags": item["review_flags"],
            "discrepancy_status": item["discrepancy_status"],
            "discrepancy_reason": item["discrepancy_reason"],
            "official_previous_close": item["official_previous_close"],
            "official_target_close": item["official_target_close"],
            "compared_providers": item["compared_providers"],
            "supporting_sources": item["supporting_sources"],
        })
    checks = {
        "indices": f"{sum(item['pass'] for item in index_checks)}/5",
        "sectors": f"{sum(item['pass'] for item in sector_checks)}/11",
        "top20_composition": f"{sum(item['ticker'] == EXPECTED_TOP20[i][0] for i, item in enumerate(top20))}/20",
        "top20_rank_change": f"{sum(item['pass'] for item in top_checks)}/20",
    }
    overall = "PASS" if checks == {
        "indices": "5/5", "sectors": "11/11", "top20_composition": "20/20", "top20_rank_change": "20/20",
    } else "FAIL"
    return {
        "probe": "live_read_only_2026-09-01", "target_session_date": TARGET.isoformat(),
        "overall": overall, "checks": checks,
        "historical_primary": "yahoo_chart_query1", "historical_fallback": "yahoo_chart_query2",
        "fallback_scope": "complete_group_only", "price_basis": "regular_close", "adjusted": False,
        "index_sector_provider_attempts": snapshot["provider_attempts"],
        "top20_history_provider_attempts": [attempt.__dict__ for attempt in historical.attempts],
        "screener": {key: screener[key] for key in ("provider", "source_identifier", "retrieved_at", "raw_response_sha256")},
        "indices": index_checks, "sectors": sector_checks, "top20": top_checks,
        "discrepancies": [item for item in top20 if item["discrepancy_status"] != "not_applicable"],
        "production_writes": {"inbox": 0, "sqlite": 0, "supabase": 0, "frontend": 0},
    }


if __name__ == "__main__":
    try:
        print(json.dumps(run(), ensure_ascii=False, indent=2, sort_keys=True))
    except MarketDataError as exc:
        print(json.dumps({
            "probe": "live_read_only_2026-09-01", "overall": "FAIL",
            "error": str(exc), "production_writes": {"inbox": 0, "sqlite": 0, "supabase": 0, "frontend": 0},
        }, ensure_ascii=False, indent=2, sort_keys=True))
        raise SystemExit(1)
