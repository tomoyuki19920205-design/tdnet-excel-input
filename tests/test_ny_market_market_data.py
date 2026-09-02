from __future__ import annotations

import json
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from lib.ny_market_market_data import (
    DailyBar,
    DailySeries,
    MarketDataError,
    YahooChartProvider,
    build_index_sector_snapshot,
    eligible_screener_row,
    fetch_all_or_fallback,
    parse_market_numeric_token,
    provider_family,
    rank_top20,
    resolve_discrepancy,
    resolve_latest_completed_sessions,
)


STAMP_AUG_28 = 1787923800
STAMP_AUG_31 = 1788183000
STAMP_SEP_1 = 1788269400
NOW = datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc)


def yahoo_bytes(symbol="^SOX", timestamps=None, closes=None, adjusted=None):
    timestamps = timestamps or [STAMP_AUG_31, STAMP_SEP_1]
    closes = closes or [100.0, 110.0]
    indicators = {"quote": [{"close": closes}]}
    if adjusted is not None:
        indicators["adjclose"] = [{"adjclose": adjusted}]
    return json.dumps({
        "chart": {"result": [{
            "meta": {"symbol": symbol, "dataGranularity": "1d", "exchangeTimezoneName": "America/New_York"},
            "timestamp": timestamps, "indicators": indicators,
        }], "error": None}
    }).encode()


def series(symbol, closes=(100.0, 110.0), days=(date(2026, 8, 31), date(2026, 9, 1)), provider="fixture"):
    return DailySeries(
        symbol=symbol, provider=provider, source_identifier=f"https://example.test/{symbol}",
        retrieved_at="2026-09-02T00:00:00+00:00", raw_response_sha256="a" * 64,
        bars=tuple(DailyBar(day, close) for day, close in zip(days, closes)),
    )


def screener(rows):
    return {
        "provider": "nasdaq_stock_screener", "source_identifier": "https://example.test/screener",
        "retrieved_at": "2026-09-02T00:00:00+00:00", "raw_response_sha256": "b" * 64,
        "rows": rows,
    }


def row(symbol, pct, close=11.0, name=None, cap=100_000_000, volume=100_000):
    previous = close / (1 + pct / 100)
    return {
        "symbol": symbol, "name": name or f"{symbol} Common Stock", "pctchange": str(pct),
        "lastsale": f"${close}", "netchange": str(close - previous), "volume": str(volume),
        "marketCap": str(cap),
    }


def test_yahoo_parses_unadjusted_regular_close_and_provenance():
    provider = YahooChartProvider(
        transport=lambda *_: yahoo_bytes(adjusted=[1.0, 2.0]), now=lambda: NOW,
    )
    result = provider.fetch("^SOX", date(2026, 8, 31), date(2026, 9, 1))
    assert [bar.session_date for bar in result.bars] == [date(2026, 8, 31), date(2026, 9, 1)]
    assert [bar.regular_close for bar in result.bars] == [100.0, 110.0]
    assert result.provider == "yahoo_chart_query1"
    assert len(result.raw_response_sha256) == 64
    parsed = urlparse(result.source_identifier)
    assert unquote(parsed.path).endswith("/^SOX")
    assert parse_qs(parsed.query)["interval"] == ["1d"]
    assert parse_qs(parsed.query)["includeAdjustedClose"] == ["false"]


@pytest.mark.parametrize(("raw", "expected"), [
    ("$15.10.", 15.10),
    ("$15.10", 15.10),
    ("$1,234.56.", 1234.56),
    ("  $15.10  ", 15.10),
    ("15.10", 15.10),
    ("15.10;", 15.10),
])
def test_generic_market_numeric_parser_accepts_one_unambiguous_token(raw, expected):
    assert parse_market_numeric_token(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [
    "15.10 - 15.20", "$15.10 / $16.00", "N/A", "", "15..10", "$15.10.20",
])
def test_generic_market_numeric_parser_rejects_ambiguous_or_malformed_values(raw):
    with pytest.raises(MarketDataError, match="single market numeric token"):
        parse_market_numeric_token(raw)


def test_latest_sessions_follow_daily_bar_existence_across_weekend_and_holiday():
    holiday = series(
        "^SOX", closes=(90, 95, 100),
        days=(date(2026, 7, 2), date(2026, 7, 6), date(2026, 7, 7)),
    )
    assert resolve_latest_completed_sessions(holiday, date(2026, 7, 7)) == (date(2026, 7, 2), date(2026, 7, 6))
    weekend = series("^SOX", days=(date(2026, 8, 27), date(2026, 8, 28)))
    assert resolve_latest_completed_sessions(weekend, date(2026, 8, 31)) == (date(2026, 8, 27), date(2026, 8, 28))


def test_missing_target_session_fails_closed():
    def transport(url, _headers):
        symbol = unquote(urlparse(url).path.rsplit("/", 1)[-1])
        return yahoo_bytes(symbol=symbol, timestamps=[STAMP_AUG_31], closes=[100.0])

    provider = YahooChartProvider(transport=transport)
    with pytest.raises(MarketDataError, match="target session"):
        build_index_sector_snapshot([provider], date(2026, 9, 1))


class FakeProvider:
    def __init__(self, name, fail=()):
        self.name, self.fail, self.calls = name, set(fail), []

    def fetch(self, symbol, start_date, end_date):
        self.calls.append(symbol)
        if symbol in self.fail:
            raise MarketDataError("fixture failure")
        return series(symbol, provider=self.name)


def test_batch_fallback_restarts_complete_group_and_never_mixes():
    primary = FakeProvider("primary", fail={"B"})
    fallback = FakeProvider("fallback")
    result = fetch_all_or_fallback([primary, fallback], ["A", "B", "C"], date(2026, 8, 31), date(2026, 9, 1))
    assert result.provider == "fallback"
    assert set(primary.calls) == {"A", "B", "C"}
    assert fallback.calls == ["A", "B", "C"]
    assert {item.provider for item in result.series.values()} == {"fallback"}
    assert [attempt.status for attempt in result.attempts] == ["failed", "success"]


def test_sector_group_is_all_or_nothing():
    primary = FakeProvider("primary", fail={"XLY"})
    fallback = FakeProvider("fallback", fail={"XLK"})
    with pytest.raises(MarketDataError, match="canonical index/sector group failed"):
        build_index_sector_snapshot([primary, fallback], date(2026, 9, 1))


@pytest.mark.parametrize("name", [
    "Example ETF", "Example Warrants", "Example Rights", "Example Units",
    "Example Preferred Stock", "Example Fund",
])
def test_instrument_filter_excludes_non_common_equity(name):
    assert not eligible_screener_row(row("BAD", 10, name=name))


@pytest.mark.parametrize("name", [
    "Example Common Stock", "Example Ordinary Shares", "Example American Depositary Shares", "Example ADR",
])
def test_instrument_filter_allows_common_ordinary_and_ads(name):
    assert eligible_screener_row(row("OK", 10, name=name))


def test_reverse_split_artifact_is_removed_and_next_candidate_fills_top20():
    rows = [row("SPLT", 100, close=10)] + [row(f"T{i:02}", 50 - i, close=11) for i in range(21)]
    history = {"SPLT": series("SPLT", (10, 10))}
    history.update({f"T{i:02}": series(f"T{i:02}", (11 / (1 + (50 - i) / 100), 11)) for i in range(21)})
    ranked = rank_top20(screener(rows), target_session_date=date(2026, 9, 1), historical_series=history)
    assert "SPLT" not in [item["ticker"] for item in ranked]
    assert len(ranked) == 20


def test_stale_or_non_regular_screener_close_fails_closed():
    rows = [row(f"T{i:02}", 30 - i, close=11) for i in range(20)]
    history = {
        f"T{i:02}": series(
            f"T{i:02}",
            ((12 if i == 0 else 11) / (1 + (30 - i) / 100), 12 if i == 0 else 11),
        )
        for i in range(20)
    }
    with pytest.raises(MarketDataError, match="stale or non-regular"):
        rank_top20(screener(rows), target_session_date=date(2026, 9, 1), historical_series=history)


def test_dual_class_issuer_total_market_cap_covers_rdib_fixture():
    rows = [row("RDIB", 17.219, close=13)] + [row(f"T{i:02}", 16 - i / 10, close=10) for i in range(19)]
    components = {
        "RDIB": [
            {"class": "Class A", "price": 2.0, "shares_outstanding": 30_000_000},
            {"class": "Class B", "price": 13.0, "shares_outstanding": 1_000_000},
        ]
    }
    ranked = rank_top20(screener(rows), target_session_date=date(2026, 9, 1), issuer_components=components)
    rdib = ranked[0]
    assert rdib["market_cap"] == pytest.approx(73_000_000)
    assert rdib["market_cap_method"] == "issuer_total_dual_class"
    assert len(rdib["share_class_components"]) == 2


def test_screener_history_mismatch_fails_closed_when_not_split_pattern():
    rows = [row(f"T{i:02}", 20 - i / 10, close=11) for i in range(20)]
    history = {f"T{i:02}": series(f"T{i:02}", (10, 11)) for i in range(20)}
    with pytest.raises(MarketDataError, match="screener/history mismatch"):
        rank_top20(screener(rows), target_session_date=date(2026, 9, 1), historical_series=history)


class GenericFixtureArbitrator:
    def __init__(self, *, independent=True, action_status="checked_none", official_previous=15.10):
        self.independent = independent
        self.action_status = action_status
        self.official_previous = official_previous

    def resolve(self, *, candidate, historical_series, previous, target, tolerance_pct, **_kwargs):
        independent = [{
            "provider": "independent_fixture", "provider_family": "independent_fixture",
            "previous_close": self.official_previous, "target_close": float(candidate["_close"]),
            "raw_value": "$15.10.", "parsed_value": 15.10,
            "source_identifier": "https://independent.test/history", "raw_response_sha256": "d" * 64,
        }] if self.independent else []
        return resolve_discrepancy(
            candidate=candidate, historical_provider=historical_series.provider,
            historical_previous_close=previous.regular_close, historical_target_close=target.regular_close,
            historical_change_pct=(target.regular_close / previous.regular_close - 1) * 100,
            official={
                "provider": "nasdaq_official_fixture", "provider_family": "nasdaq",
                "previous_close": self.official_previous, "target_close": float(candidate["_close"]),
                "source_identifiers": ["https://nasdaq.test/history"],
                "raw_response_sha256": ["e" * 64],
            },
            corporate_action={
                "provider": "action_fixture", "provider_family": "yahoo", "status": self.action_status,
                "source_identifier": "https://yahoo.test/events", "raw_response_sha256": "f" * 64,
            },
            minute_close={
                "provider": "minute_fixture", "provider_family": "yahoo",
                "previous_close": self.official_previous, "target_close": float(candidate["_close"]),
                "source_identifier": "https://yahoo.test/minute", "raw_response_sha256": "1" * 64,
            },
            independent_sources=independent, tolerance_pct=tolerance_pct,
            resolved_at="2026-09-02T00:00:00+00:00",
        )


def test_generic_stale_daily_bar_discrepancy_is_resolved_without_ticker_special_case():
    rows = [row("GENR", 17.219, close=17.70, volume=123_972)]
    rows += [row(f"T{i:02}", 16 - i / 10, close=10) for i in range(19)]
    history = {"GENR": series("GENR", (15.305, 17.70), provider="yahoo_chart_query1")}
    history.update({
        f"T{i:02}": series(f"T{i:02}", (10 / (1 + (16 - i / 10) / 100), 10), provider="yahoo_chart_query1")
        for i in range(19)
    })
    ranked = rank_top20(
        screener(rows), target_session_date=date(2026, 9, 1), historical_series=history,
        discrepancy_arbitrator=GenericFixtureArbitrator(),
    )
    item = ranked[0]
    assert item["ticker"] == "GENR"
    assert item["change_pct"] == pytest.approx(17.219)
    assert item["discrepancy_status"] == "resolved"
    assert item["discrepancy_reason"] == "stale_daily_bar"
    assert item["corporate_action_status"] == "checked_none"
    assert item["liquidity_flag"] == "low_liquidity"
    assert item["official_previous_close"] == pytest.approx(15.10)
    assert set(item["compared_providers"]) == {"nasdaq", "yahoo", "independent_fixture"}
    independent = next(source for source in item["supporting_sources"] if source["role"] == "independent_support")
    assert independent["raw_value"] == "$15.10."
    assert independent["parsed_value"] == pytest.approx(15.10)
    assert "discrepancy_resolved" in item["review_flags"]


def test_unresolved_discrepancy_still_fails_closed():
    rows = [row("GENR", 17.219, close=17.70)] + [row(f"T{i:02}", 16 - i / 10) for i in range(19)]
    history = {"GENR": series("GENR", (15.305, 17.70), provider="yahoo_chart_query1")}
    history.update({f"T{i:02}": series(f"T{i:02}", (11 / (1 + (16 - i / 10) / 100), 11)) for i in range(19)})
    with pytest.raises(MarketDataError, match="no independent supporting source"):
        rank_top20(
            screener(rows), target_session_date=date(2026, 9, 1), historical_series=history,
            discrepancy_arbitrator=GenericFixtureArbitrator(independent=False),
        )


def test_corporate_action_must_be_resolved_before_arbitration_passes():
    candidate = {**row("GENR", 17.219, close=17.70), "_change_pct": 17.219, "_close": 17.70}
    with pytest.raises(MarketDataError, match="corporate action is not resolved"):
        GenericFixtureArbitrator(action_status="corporate_action_found").resolve(
            ticker="GENR", candidate=candidate, historical_series=series("GENR", (15.305, 17.70)),
            previous=DailyBar(date(2026, 8, 31), 15.305), target=DailyBar(date(2026, 9, 1), 17.70),
            target_session_date=date(2026, 9, 1), tolerance_pct=0.20,
        )


def test_query1_and_query2_are_one_yahoo_family_not_independent_evidence():
    assert provider_family("yahoo_chart_query1") == provider_family("yahoo_chart_query2") == "yahoo"
    candidate = {**row("GENR", 17.219, close=17.70), "_change_pct": 17.219, "_close": 17.70}
    with pytest.raises(MarketDataError, match="no independent supporting source"):
        resolve_discrepancy(
            candidate=candidate, historical_provider="yahoo_chart_query1",
            historical_previous_close=15.305, historical_target_close=17.70,
            historical_change_pct=15.648,
            official={"provider": "nasdaq_official", "provider_family": "nasdaq", "previous_close": 15.10, "target_close": 17.70},
            corporate_action={"provider": "yahoo_events", "provider_family": "yahoo", "status": "checked_none"},
            minute_close={"provider": "yahoo_minute", "provider_family": "yahoo", "previous_close": 15.10, "target_close": 17.70},
            independent_sources=[
                {"provider": "yahoo_chart_query1", "previous_close": 15.10},
                {"provider": "yahoo_chart_query2", "previous_close": 15.10},
            ], tolerance_pct=0.20, resolved_at="2026-09-02T00:00:00+00:00",
        )
