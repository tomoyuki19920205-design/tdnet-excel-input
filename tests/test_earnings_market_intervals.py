from src.analysis.earnings_market_intervals import (
    DisclosureEvent, _adjusted_per_share, analysis_tags, analyze_ticker, daily_valuations,
    formal_events, next_trading_day, select_interval_boundaries,
)


def test_next_trading_day_skips_weekend_by_observed_calendar():
    assert next_trading_day(["2026-08-07", "2026-08-10"], "2026-08-07") == "2026-08-10"


def test_boundaries_use_next_trading_day_and_pre_event_day():
    events = [DisclosureEvent("1000", "e2", "2026-01-10", None, "2026-03-31", "2Q", "2QFinancialStatements"), DisclosureEvent("1000", "e1", "2026-04-10", None, "2026-03-31", "FY", "FYFinancialStatements")]
    dates = ["2026-01-09", "2026-01-13", "2026-04-09", "2026-04-13", "2026-04-14"]
    result = select_interval_boundaries(events, dates, "2026-04-14")
    assert result["period_a"] == ("2026-01-13", "2026-04-09")
    assert result["period_b"] == ("2026-04-13", "2026-04-14")


def test_split_adjustment_keeps_raw_close_valuation_consistent():
    # A 2:1 split changes the adjustment factor .5 -> 1.0.  EPS 100 then
    # becomes 50 in the post-split raw share basis, so close 500 remains 10x.
    assert _adjusted_per_share(100, 0.5, 1.0) == 50


def test_negative_eps_is_not_a_negative_per():
    rows = [{"ticker": "1000", "date": "2026-01-05", "close": 100, "adj_close": 100, "volume": 1, "adj_volume": 1, "turnover": 100, "adj_factor": 1}]
    from src.analysis.earnings_market_intervals import FundamentalInterval
    obs = [FundamentalInterval("1000", "forecast_eps", -10, "negative_eps", "d", "2026-01-02", "2026-01-05", 1, "2026-03-31", "FY", "jquants")]
    assert daily_valuations(rows, obs)[0]["forward_per"] is None


def test_asof_does_not_apply_future_forecast():
    market = [{"ticker": "1000", "date": d, "close": 100, "adj_close": 100 + i, "volume": 10, "adj_volume": 10, "turnover": 1000, "adj_factor": 1} for i, d in enumerate(["2026-01-02", "2026-01-05", "2026-02-02", "2026-02-03", "2026-02-04"])]
    financial = [
        {"ticker": "1000", "disclosed_date": "2026-01-01", "fiscal_year_end": "2026-03-31", "quarter": "2Q", "type_of_document": "2QFinancialStatements", "raw": {"DiscNo": "e2"}},
        {"ticker": "1000", "disclosed_date": "2026-02-01", "fiscal_year_end": "2026-03-31", "quarter": "FY", "type_of_document": "FYFinancialStatements", "raw": {"DiscNo": "e1"}},
    ]
    per_share = [{"ticker": "1000", "disclosed_date": "2026-01-01", "period": "2026-03-31", "quarter": "2Q", "forecast_eps": 10, "forecast_dividend_annual": 1, "bps": 50, "source": "jquants"}, {"ticker": "1000", "disclosed_date": "2026-02-10", "period": "2026-03-31", "quarter": "FY", "forecast_eps": 20, "forecast_dividend_annual": 2, "bps": 60, "source": "jquants"}]
    result = analyze_ticker("1000", market, financial, per_share, as_of_date="2026-02-04", as_of_timestamp="2026-02-04T23:59:59+09:00")
    assert all(x["forecast_eps"] == 10 for x in result["daily_valuations"] if x["forecast_eps"] is not None)


def test_formal_event_excludes_revision_and_correction_as_new_boundary():
    rows = [
        {"ticker": "1000", "disclosed_date": "2026-01-10", "fiscal_year_end": "2026-03-31", "quarter": "3Q", "type_of_document": "3QFinancialStatements", "raw": {"DiscNo": "formal"}},
        {"ticker": "1000", "disclosed_date": "2026-01-12", "fiscal_year_end": "2026-03-31", "quarter": "3Q", "type_of_document": "3QFinancialStatementsCorrection", "raw": {"DiscNo": "corrected"}},
        {"ticker": "1000", "disclosed_date": "2026-01-13", "fiscal_year_end": "2026-03-31", "quarter": "3Q", "type_of_document": "EarnForecastRevision", "raw": {"DiscNo": "revision"}},
    ]
    events = formal_events(rows, as_of_date="2026-01-31")
    assert [x.disclosure_id for x in events] == ["formal"]


def test_formal_event_prefers_consolidated_document_for_same_period():
    rows = [
        {"ticker": "1000", "disclosed_date": "2026-01-10", "fiscal_year_end": "2026-03-31", "quarter": "3Q", "type_of_document": "3QFinancialStatements_NonConsolidated", "raw": {"DiscNo": "noncon"}},
        {"ticker": "1000", "disclosed_date": "2026-01-10", "fiscal_year_end": "2026-03-31", "quarter": "3Q", "type_of_document": "3QFinancialStatements_Consolidated", "raw": {"DiscNo": "con"}},
    ]
    assert formal_events(rows, as_of_date="2026-01-31")[0].disclosure_id == "con"


def test_constant_price_has_explicit_unknown_price_position():
    from src.analysis.earnings_market_intervals import summarize_period
    rows = [{"date": "2026-01-05", "adj_close": 100, "volume": 1, "adj_volume": 1, "turnover": 100}, {"date": "2026-01-06", "adj_close": 100, "volume": 1, "adj_volume": 1, "turnover": 100}]
    summary = summarize_period(rows, label="A", start="2026-01-05", end="2026-01-06")
    assert summary["price_position"] is None
    assert summary["forward_per_coverage_ratio"] == 0


def test_tags_are_explanatory_and_partial_data_is_not_scored():
    assert analysis_tags({"calculation_status": "partial"}, {"calculation_status": "available"}, {}) == ["insufficient_data"]
    a = {"calculation_status": "available", "forward_dividend_yield_median": 0.02}
    b = {"calculation_status": "available", "forward_dividend_yield_median": 0.02}
    comparison = {"price_change_factor": 1.12, "eps_change_factor": 1.10, "per_change_factor": 1.01, "average_turnover_ratio": 1.0}
    assert "earnings_driven_rise" in analysis_tags(a, b, comparison)
