from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from lib.screener_snapshot import build_snapshot


DB = Path(__file__).resolve().parents[1] / "data" / "jquants.db"


@pytest.mark.skipif(not DB.exists(), reason="canonical J-Quants DB is not available")
def test_current_universe_coverage_gates_against_real_data() -> None:
    result = build_snapshot(DB)
    assert len(result.rows) == 3889
    assert result.coverage["forward_per_per_forecast_sales_growth"]["coverage_pct"] >= 70
    assert result.coverage["forward_peg"]["coverage_pct"] >= 47
    assert result.coverage["return_5d_pct"]["coverage_pct"] >= 94
    assert result.coverage["op_upward_revision_count_3y"]["coverage_pct"] >= 90
    # Sector columns are either not refreshed yet (legacy snapshot) or complete;
    # partial master ingestion must never be published.
    for metric in ("sector17_code", "sector33_code"):
        assert result.coverage[metric]["coverage_pct"] in (0.0, 100.0)


@pytest.mark.skipif(not DB.exists(), reason="canonical J-Quants DB is not available")
def test_price_statuses_and_accounting_standards_are_represented() -> None:
    result = build_snapshot(DB)
    statuses = {row["price_status"] for row in result.rows}
    standards = {row["accounting_standard"] for row in result.rows}
    assert {"current", "no_trade", "source_ineligible"} <= statuses
    assert {"JP_GAAP", "IFRS", "US_GAAP"} <= standards
    assert sum(row["price_status"] in {"no_trade", "stale_unknown"} for row in result.rows) == 33
    stale_sessions = {
        row["price_stale_sessions"] for row in result.rows
        if row["price_status"] in {"no_trade", "stale_unknown"}
    }
    assert {1, 2, 3, 4} <= stale_sessions


@pytest.mark.skipif(not DB.exists(), reason="canonical J-Quants DB is not available")
def test_real_split_revision_fiscal_and_profit_state_cases() -> None:
    result = build_snapshot(DB)
    with sqlite3.connect(DB) as connection:
        split_tickers = connection.execute(
            "SELECT COUNT(DISTINCT ticker) FROM market_data WHERE adj_factor NOT IN (1, 1.0)"
        ).fetchone()[0]
    assert split_tickers >= 10
    assert sum((row["any_earnings_upward_revision_event_count_3y"] or 0) >= 2 for row in result.rows) >= 10
    assert sum(row["fiscal_period_changed"] for row in result.rows) > 0
    assert sum(row["turnaround"] for row in result.rows) > 0
    assert sum(row["loss_expansion"] for row in result.rows) > 0
    assert sum(row["profit_to_loss"] for row in result.rows) > 0
    assert sum(row["insufficient_price_history"] for row in result.rows) > 0


@pytest.mark.skipif(not DB.exists(), reason="canonical J-Quants DB is not available")
def test_inverse_sales_valuation_matches_manual_calculation_for_ten_real_stocks() -> None:
    result = build_snapshot(DB)
    nonpositive = [
        row for row in result.rows
        if row["forecast_sales_growth_yoy_pct"] is not None
        and row["forecast_sales_growth_yoy_pct"] <= 0
    ]
    assert nonpositive
    assert all(row["forward_per_per_forecast_sales_growth"] is None for row in nonpositive)
    candidates = [
        row for row in result.rows
        if row["forward_per_per_forecast_sales_growth"] is not None
    ][:10]
    assert len(candidates) == 10
    for row in candidates:
        expected = row["forward_per"] / row["forecast_sales_growth_yoy_pct"]
        assert row["forecast_sales_growth_yoy_pct"] > 0
        assert row["forward_per_per_forecast_sales_growth"] == pytest.approx(expected)
