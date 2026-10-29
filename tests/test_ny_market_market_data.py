from __future__ import annotations

import hashlib
import json
import io
from pathlib import Path
from datetime import date, datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from lib.ny_market_market_data import (
    CapturedEndOfDayQuoteProvider,
    DailyBar,
    DailySeries,
    LiveDiscrepancyArbitrator,
    MarketDataError,
    NasdaqOfficialCloseProvider,
    StockAnalysisGainersProvider,
    StockAnalysisHistoricalCloseProvider,
    YahooChartProvider,
    build_canonical_market_data_packet,
    build_index_sector_snapshot,
    default_transport,
    eligible_screener_row,
    fetch_all_or_fallback,
    parse_market_numeric_token,
    provider_family,
    rank_top20,
    reject_stale_undated_screener,
    resolve_discrepancy,
    resolve_latest_completed_sessions,
    screener_candidate_symbols,
    verify_corporate_action_evidence,
)


STAMP_AUG_28 = 1787923800
STAMP_AUG_31 = 1788183000
STAMP_SEP_1 = 1788269400
NOW = datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).parents[1]


def test_undated_nasdaq_screener_rejects_prior_session_bulk_prices():
    previous, target = date(2026, 10, 22), date(2026, 10, 23)
    symbols = [f"SYM{i:02d}" for i in range(25)]
    rows = [{"symbol": symbol, "lastsale": "$10.00"} for symbol in symbols]
    series = {
        symbol: DailySeries(symbol, "yahoo", "fixture", "2026-10-24T00:00:00Z", "0" * 64,
                            (DailyBar(previous, 10.0), DailyBar(target, 9.0)))
        for symbol in symbols
    }
    with pytest.raises(MarketDataError, match="Nasdaq screener is stale"):
        reject_stale_undated_screener(
            {"provider": "nasdaq_stock_screener", "rows": rows}, series, target, symbols,
        )
    rows[0]["lastsale"] = "$9.00"
    for row in rows[1:]:
        row["lastsale"] = "$9.00"
    reject_stale_undated_screener(
        {"provider": "nasdaq_stock_screener", "rows": rows}, series, target, symbols,
    )


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


def test_yahoo_preserves_zero_volume_for_no_trade_detection():
    payload = json.loads(yahoo_bytes())
    payload["chart"]["result"][0]["indicators"]["quote"][0]["volume"] = [123, 0]
    provider = YahooChartProvider(
        transport=lambda *_: json.dumps(payload).encode(), now=lambda: NOW,
    )
    result = provider.fetch("^SOX", date(2026, 8, 31), date(2026, 9, 1))
    assert [bar.volume for bar in result.bars] == [123.0, 0.0]


def test_captured_class_share_alias_requires_matching_issuer_identity():
    raw = (ROOT / "tests/fixtures/ny_market_gtn_a_yahoo_20261029.json").read_bytes()
    requests = []

    def transport(url, _headers):
        requests.append(url)
        if "/GTN.A?" in url:
            raise HTTPError(url, 404, "Not Found", None, None)
        return raw

    provider = YahooChartProvider(transport=transport, expected_names={"GTN.A": "Gray Media, Inc."})
    result = provider.fetch("GTN.A", date(2026, 10, 19), date(2026, 10, 29))
    assert [bar.session_date for bar in result.bars][-1] == date(2026, 10, 29)
    assert ["/GTN.A?" in url for url in requests] == [True, False]
    assert "/GTN-A?" in result.source_identifier
    assert result.symbol == "GTN.A"
    assert result.symbol_mapping_evidence == {
        "canonical_symbol": "GTN.A", "provider_symbol": "GTN-A",
        "screener_company_name": "Gray Media, Inc.",
        "provider_company_name": "Gray Media, Inc.",
        "provider_instrument_type": "EQUITY", "provider_currency": "USD",
        "source_identifier": result.source_identifier,
        "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
        "rule": "single_class_suffix_dot_to_hyphen_after_404",
    }


@pytest.mark.parametrize("expected_name", ["Different Company, Inc.", ""])
def test_class_share_alias_rejects_unverified_issuer_identity(expected_name):
    raw = (ROOT / "tests/fixtures/ny_market_gtn_a_yahoo_20261029.json").read_bytes()

    def transport(url, _headers):
        if "/GTN.A?" in url:
            raise HTTPError(url, 404, "Not Found", None, None)
        return raw

    provider = YahooChartProvider(transport=transport, expected_names={"GTN.A": expected_name})
    with pytest.raises((MarketDataError, HTTPError)):
        provider.fetch("GTN.A", date(2026, 10, 19), date(2026, 10, 29))


def captured_refr_state(pct: str) -> bytes:
    return f"""0 AXWebArea REFR Stock Price | Research Frontiers Inc. Stock Quote (U.S.: Nasdaq) | MarketWatch
1 text AFTER HOURS Last Updated: Sep 18, 2026 at 6:05 p.m. EDT
2 table
  3 row
    4 cell
      5 text CLOSE
    6 cell
      7 text CHG
    8 cell
      9 text CHG %
  10 row
    11 cell
      12 text $0.6513
    13 cell
      14 text 0.2648
    15 cell
      16 text {pct}%
17 text Historical and current end-of-day data provided by
18 link Description: FACTSET
""".encode()


def test_captured_eod_quote_accepts_display_rounding_interval(monkeypatch):
    monkeypatch.setattr(Path, "read_bytes", lambda _path: captured_refr_state("68.49"))
    provider = CapturedEndOfDayQuoteProvider({
        "REFR": {"path": "marketwatch_refr.ax.txt", "source_url": "https://www.marketwatch.com/investing/stock/refr"},
    })
    result = provider.fetch("REFR", date(2026, 9, 18))
    assert result["target_close"] == pytest.approx(0.6513)
    assert result["previous_close"] == pytest.approx(0.3865)


def test_captured_historical_quote_uses_dated_close_and_rejects_wrong_session(monkeypatch):
    raw = b'''<html><head><title>SOAR Historical Prices | ChartExchange</title>
<link rel="canonical" href="https://chartexchange.com/symbol/nyseamerican-soar/historical/"/></head>
<body><table><tr><td>Date<br>( EDT )</td><td>Open</td><td>High</td><td>Low</td>
<td>Close</td><td>Change</td></tr>
<tr><td>2026-09-29</td><td>0.31</td><td>0.43</td><td>0.29</td><td>0.390000</td><td>+22.334%</td></tr>
<tr><td>2026-09-28</td><td>0.37</td><td>0.46</td><td>0.31</td><td>0.318800</td><td>+77.604%</td></tr>
<tr><td>2026-09-25</td><td>0.18</td><td>0.18</td><td>0.17</td><td>0.179500</td><td>-2.233%</td></tr>
</table></body></html>'''
    monkeypatch.setattr(Path, "read_bytes", lambda _path: raw)
    provider = CapturedEndOfDayQuoteProvider({
        "SOAR": {"path": "captured.html", "source_url": "https://chartexchange.com/symbol/nyseamerican-soar/historical/"},
    })
    result = provider.fetch("SOAR", date(2026, 9, 28))
    assert result["target_close"] == pytest.approx(0.3188)
    assert result["previous_close"] == pytest.approx(0.1795)
    assert result["primary_exchange"] == "NYSEAMERICAN"
    with pytest.raises(MarketDataError, match="target/previous session missing"):
        provider.fetch("SOAR", date(2026, 9, 25))


def test_non_nasdaq_primary_exchange_eod_resolves_nasdaq_last_sale_difference():
    resolved = resolve_discrepancy(
        candidate={
            "symbol": "GENR", "_close": 0.511, "_change_pct": 50.029,
            "netchange": "0.1704", "volume": "193313853",
        },
        historical_provider="yahoo_chart_query1",
        historical_previous_close=0.3406,
        historical_target_close=0.511,
        historical_change_pct=50.029,
        official={
            "provider": "nasdaq_official_historical_nls", "provider_family": "nasdaq",
            "target_close_verified": True, "target_timestamp": "Closed at Sep 23, 2026 4:00 PM ET",
            "target_close_source": "nasdaq_info", "target_session_date": "2026-09-23",
            "previous_close": 0.3406, "target_close": 0.4943,
            "source_identifiers": ["https://nasdaq.test/history"], "raw_response_sha256": ["a" * 64],
        },
        corporate_action={
            "provider": "yahoo_corporate_actions", "provider_family": "yahoo", "status": "checked_none",
            "source_identifier": "https://yahoo.test/actions", "raw_response_sha256": "b" * 64,
        },
        minute_close={
            "provider": "yahoo_minute_close", "provider_family": "yahoo", "price_field": "boundary_open",
            "previous_close": 0.3406, "target_close": 0.511,
            "source_identifier": "https://yahoo.test/minute", "raw_response_sha256": "c" * 64,
        },
        independent_sources=[{
            "provider": "captured_end_of_day_quote", "provider_family": "factset",
            "session": "regular_close", "target_session_date": "2026-09-23",
            "previous_close": 0.34, "target_close": 0.51,
            "primary_exchange": "NYSE American", "raw_value": "0.51",
            "source_identifier": "https://www.marketwatch.com/investing/stock/genr",
            "raw_response_sha256": "d" * 64,
        }],
        tolerance_pct=0.2,
        resolved_at="2026-09-23T22:00:00+00:00",
    )
    assert resolved["discrepancy_reason"] == "non_nasdaq_primary_exchange_eod"
    assert resolved["basis_evidence"]["primary_exchange"] == "NYSE American"
    assert resolved["basis_evidence"]["nasdaq_info_close"] == pytest.approx(0.4943)


def test_captured_eod_quote_rejects_change_outside_display_rounding_interval(monkeypatch):
    monkeypatch.setattr(Path, "read_bytes", lambda _path: captured_refr_state("68.60"))
    provider = CapturedEndOfDayQuoteProvider({
        "REFR": {"path": "marketwatch_refr.ax.txt", "source_url": "https://www.marketwatch.com/investing/stock/refr"},
    })
    with pytest.raises(MarketDataError, match="EOD quote arithmetic mismatch"):
        provider.fetch("REFR", date(2026, 9, 18))


def test_captured_eod_quote_accepts_negative_change(monkeypatch):
    state = """0 AXWebArea BENF Stock Price | Beneficient Stock Quote (U.S.: Nasdaq) | MarketWatch
1 text AFTER HOURS Last Updated: Sep 24, 2026
2 table
  3 row
    4 cell
      5 text CLOSE
    6 cell
      7 text CHG
    8 cell
      9 text CHG %
  10 row
    11 cell
      12 text $1.4500
    13 cell
      14 text -1.4500
    15 cell
      16 text -50.00%
17 text Historical and current end-of-day data provided by
18 link Description: FACTSET
""".encode()
    monkeypatch.setattr(Path, "read_bytes", lambda _path: state)
    provider = CapturedEndOfDayQuoteProvider({
        "BENF": {"path": "marketwatch_benf.ax.txt", "source_url": "https://www.marketwatch.com/investing/stock/benf"},
    })
    result = provider.fetch("BENF", date(2026, 9, 24))
    assert result["target_close"] == pytest.approx(1.45)
    assert result["previous_close"] == pytest.approx(2.90)


def test_captured_eod_display_rounding_supports_official_close_arbitration():
    resolved = resolve_discrepancy(
        candidate={
            "symbol": "REFR", "_close": 0.6513, "_change_pct": 68.469,
            "netchange": "0.2647", "volume": "91327476",
        },
        historical_provider="yahoo_chart_query1",
        historical_previous_close=0.38999998569488525,
        historical_target_close=0.6513000130653381,
        historical_change_pct=67.00000947561054,
        official={
            "provider": "nasdaq_official_historical_nls", "provider_family": "nasdaq",
            "target_close_verified": True, "target_timestamp": "Closed at Sep 18, 2026 4:00 PM ET",
            "target_close_source": "nasdaq_info", "target_session_date": "2026-09-18",
            "previous_close": 0.3866, "target_close": 0.6513,
            "source_identifiers": ["https://nasdaq.test/history"], "raw_response_sha256": ["a" * 64],
        },
        corporate_action={
            "provider": "yahoo_corporate_actions", "provider_family": "yahoo", "status": "checked_none",
            "source_identifier": "https://yahoo.test/actions", "raw_response_sha256": "b" * 64,
        },
        minute_close={
            "provider": "yahoo_minute_close", "provider_family": "yahoo", "price_field": "boundary_open",
            "previous_close": 0.3802, "target_close": 0.6501,
            "source_identifier": "https://yahoo.test/minute", "raw_response_sha256": "c" * 64,
        },
        independent_sources=[{
            "provider": "captured_end_of_day_quote", "provider_family": "factset",
            "session": "regular_close", "target_session_date": "2026-09-18",
            "previous_close": 0.3865, "target_close": 0.6513,
            "raw_value": "0.6513", "reported_change_raw": "0.2648",
            "display_decimal_places": 4, "source_identifier": "https://www.marketwatch.com/investing/stock/refr",
            "raw_response_sha256": "d" * 64,
        }],
        tolerance_pct=0.2,
        resolved_at="2026-09-18T22:00:00+00:00",
    )
    assert resolved["discrepancy_reason"] == "official_closed_eod_with_secondary_boundary_difference"
    assert resolved["official_target_close"] == pytest.approx(0.6513)


def test_captured_eod_display_rounding_accepts_dated_nasdaq_historical_close():
    resolved = resolve_discrepancy(
        candidate={
            "symbol": "REFR", "_close": 0.6513, "_change_pct": 68.469,
            "netchange": "0.2647", "volume": "91327476",
        },
        historical_provider="yahoo_chart_query1",
        historical_previous_close=0.38999998569488525,
        historical_target_close=0.6499999761581421,
        historical_change_pct=66.66666666666667,
        official={
            "provider": "nasdaq_official_historical_nls", "provider_family": "nasdaq",
            "target_close_verified": True,
            "target_timestamp": "Nasdaq historical close 09/18/2026",
            "target_close_source": "nasdaq_historical", "target_session_date": "2026-09-18",
            "previous_close": 0.3866, "target_close": 0.6513,
            "source_identifiers": ["https://nasdaq.test/history"], "raw_response_sha256": ["a" * 64],
        },
        corporate_action={
            "provider": "yahoo_corporate_actions", "provider_family": "yahoo", "status": "checked_none",
            "source_identifier": "https://yahoo.test/actions", "raw_response_sha256": "b" * 64,
        },
        minute_close={
            "provider": "yahoo_minute_close", "provider_family": "yahoo", "price_field": "boundary_open",
            "previous_close": 0.3802, "target_close": 0.6501,
            "source_identifier": "https://yahoo.test/minute", "raw_response_sha256": "c" * 64,
        },
        independent_sources=[{
            "provider": "captured_end_of_day_quote", "provider_family": "factset",
            "session": "regular_close", "target_session_date": "2026-09-18",
            "previous_close": 0.3865, "target_close": 0.6513,
            "raw_value": "0.6513", "reported_change_raw": "0.2648",
            "display_decimal_places": 4, "source_identifier": "https://www.marketwatch.com/investing/stock/refr",
            "raw_response_sha256": "d" * 64,
        }],
        tolerance_pct=0.2,
        resolved_at="2026-09-19T22:00:00+00:00",
    )
    assert resolved["discrepancy_reason"] == "official_closed_eod_with_secondary_boundary_difference"
    assert resolved["official_target_close"] == pytest.approx(0.6513)


def test_dated_eod_resolves_previous_boundary_open_mismatch_without_using_minute_as_close():
    kwargs = dict(
        candidate={
            "symbol": "SHFS", "_close": 0.3429, "_change_pct": 122.37,
            "netchange": "0.1887", "volume": "956552100",
        },
        historical_provider="yahoo_chart_query1",
        historical_previous_close=0.15399999916553497,
        historical_target_close=0.34299999475479126,
        historical_change_pct=122.72727272727273,
        official={
            "provider": "nasdaq_official_historical_nls", "provider_family": "nasdaq",
            "target_close_verified": True,
            "target_timestamp": "Nasdaq historical close 10/22/2026",
            "target_close_source": "nasdaq_historical", "target_session_date": "2026-10-22",
            "previous_session_date": "2026-10-21",
            "previous_close": 0.1542, "target_close": 0.3429,
            "source_identifiers": ["https://nasdaq.test/history"],
            "raw_response_sha256": ["a" * 64],
        },
        corporate_action={
            "provider": "yahoo_corporate_actions", "provider_family": "yahoo", "status": "checked_none",
            "source_identifier": "https://yahoo.test/actions", "raw_response_sha256": "b" * 64,
        },
        minute_close={
            "provider": "yahoo_minute_close", "provider_family": "yahoo", "price_field": "boundary_open",
            "previous_close": 0.1516, "target_close": 0.3429,
            "source_identifier": "https://yahoo.test/minute", "raw_response_sha256": "c" * 64,
        },
        tolerance_pct=0.2,
        resolved_at="2026-10-24T11:17:40+00:00",
    )
    independent = {
        "provider": "captured_end_of_day_quote", "provider_family": "chartexchange",
        "session": "regular_close", "target_session_date": "2026-10-22",
        "previous_close": 0.1542, "target_close": 0.3429,
        "raw_value": "0.3429", "reported_change_raw": "122.37%",
        "display_decimal_places": 4,
        "source_identifier": "https://chartexchange.test/shfs/historical",
        "raw_response_sha256": "d" * 64,
    }
    resolved = resolve_discrepancy(**kwargs, independent_sources=[independent])
    assert resolved["discrepancy_reason"] == "official_closed_eod_with_secondary_boundary_difference"
    assert resolved["official_previous_close"] == pytest.approx(0.1542)
    assert resolved["basis_evidence"]["secondary_boundary_mismatch_sessions"] == ["previous"]
    assert resolved["basis_evidence"]["comparison_basis"] == "dated_regular_close_not_secondary_boundary_open"
    with pytest.raises(MarketDataError, match="no independent supporting source"):
        resolve_discrepancy(**kwargs, independent_sources=[])


@pytest.mark.parametrize("independent_target,passes", [(0.93, True), (0.91, False)])
def test_dated_independent_eod_resolves_boundary_difference_and_audits_outlier(independent_target, passes):
    prior, target = date(2026, 10, 2), date(2026, 10, 5)

    class Official:
        def fetch(self, symbol, day, _last):
            assert (symbol, day) == ("SCKT", target)
            return {
                "provider": "nasdaq_official_historical_nls", "provider_family": "nasdaq",
                "target_close_verified": True, "target_timestamp": "Nasdaq historical close 10/05/2026",
                "target_close_source": "nasdaq_historical", "target_session_date": target.isoformat(),
                "previous_session_date": prior.isoformat(), "previous_close": 0.7073, "target_close": 0.9309,
                "notifications": [], "source_identifiers": ["https://nasdaq.test/history"],
                "raw_response_sha256": ["a" * 64],
            }

    class Actions:
        def fetch(self, *_):
            return {"provider": "yahoo_corporate_actions", "provider_family": "yahoo", "status": "checked_none",
                    "source_identifier": "https://yahoo.test/actions", "raw_response_sha256": "b" * 64}

    class Minute:
        def fetch(self, *_):
            return {"provider": "yahoo_minute_close", "provider_family": "yahoo", "price_field": "boundary_open",
                    "previous_close": 0.7073, "target_close": 0.921,
                    "source_identifier": "https://yahoo.test/minute", "raw_response_sha256": "c" * 64}

    class ConflictingQuote:
        name = "captured_end_of_day_quote"
        def fetch(self, *_):
            return {"provider": self.name, "provider_family": "chartexchange", "session": "regular_close",
                    "target_session_date": target.isoformat(), "previous_close": 0.7073, "target_close": 0.91,
                    "raw_value": "0.91", "reported_change_raw": "28.66%", "display_decimal_places": 2,
                    "source_identifier": "https://chartexchange.test/sckt", "raw_response_sha256": "d" * 64}

    class DatedHistory:
        name = "stockanalysis_historical_s_p_global"
        def fetch_pair(self, symbol, previous, day):
            assert (symbol, previous, day) == ("SCKT", prior, target)
            return {"provider": self.name, "provider_family": "s_p_global",
                    "previous_session_date": prior.isoformat(), "target_session_date": target.isoformat(),
                    "previous_split_normalized_close": 0.71, "target_regular_close": independent_target,
                    "previous_raw_close": "0.71", "target_raw_close": str(independent_target),
                    "previous_display_decimal_places": 2, "target_display_decimal_places": 2,
                    "reported_change_raw": "31.61%", "price_basis": "regular_close_normalized_for_split",
                    "source_identifier": "https://stockanalysis.test/sckt/history", "raw_response_sha256": "e" * 64}

    arbitrator = LiveDiscrepancyArbitrator(
        official_provider=Official(), action_provider=Actions(), minute_provider=Minute(),
        independent_providers=[ConflictingQuote()], split_history_provider=DatedHistory(), now=lambda: NOW,
    )
    kwargs = dict(
        ticker="SCKT", candidate={"_close": 0.9309, "_change_pct": 31.61},
        historical_series=series("SCKT", (0.71, 0.93), (prior, target), "yahoo_chart_query1"),
        previous=DailyBar(prior, 0.71), target=DailyBar(target, 0.93),
        target_session_date=target, tolerance_pct=0.2,
        expected_previous_session_date=prior, source_top20_floor_pct=25.81,
    )
    if not passes:
        with pytest.raises(MarketDataError, match="Top20 SCKT arbitration failed"):
            arbitrator.resolve(**kwargs)
        return
    resolved = arbitrator.resolve(**kwargs)
    assert resolved["discrepancy_reason"] == "official_closed_eod_with_secondary_boundary_difference"
    assert resolved["official_target_close"] == 0.9309
    assert resolved["basis_evidence"]["conflicting_independent_eod"][0]["target_close"] == 0.91
    assert any(source["provider_family"] == "s_p_global" for source in resolved["supporting_sources"])


def test_stockanalysis_dated_history_preserves_close_display_precision():
    html = b"""<html><body>Past prices are adjusted for stock splits.
    <table><tr><th>Date</th><th>Close</th><th>Change</th></tr>
    <tr><td>Oct 5, 2026</td><td>$0.93</td><td>31.61%</td></tr>
    <tr><td>Oct 2, 2026</td><td>$0.71</td><td>2.00%</td></tr></table></body></html>"""
    pair = StockAnalysisHistoricalCloseProvider(transport=lambda *_: html).fetch_pair(
        "SCKT", date(2026, 10, 2), date(2026, 10, 5),
    )
    assert pair["previous_split_normalized_close"] == 0.71
    assert pair["target_regular_close"] == 0.93
    assert pair["target_raw_close"] == "$0.93"
    assert pair["previous_display_decimal_places"] == pair["target_display_decimal_places"] == 2
    assert pair["reported_change_raw"] == "31.61%"


def test_nasdaq_official_close_uses_dated_historical_row_when_info_date_is_stale():
    def transport(url, _headers):
        if "/historical?" in url:
            return json.dumps({
                "status": {"rCode": 200},
                "data": {"tradesTable": {"rows": [
                    {"date": "09/04/2026", "close": "$1.04", "volume": "747,778"},
                    {"date": "09/03/2026", "close": "$0.8866", "volume": "404,574"},
                ]}},
            }).encode()
        if "/realtime-trades?" in url:
            return json.dumps({"data": {"topTable": {"rows": [
                {"previousClose": "$0.8866"},
            ]}}}).encode()
        return json.dumps({"data": {
            "symbol": "PAAI", "assetClass": "STOCKS", "notifications": [],
            "primaryData": {"lastSalePrice": "$1.02", "lastTradeTimestamp": "Sep 3, 2026"},
            "secondaryData": None,
        }}).encode()

    result = NasdaqOfficialCloseProvider(transport=transport, now=lambda: NOW).fetch(
        "PAAI", date(2026, 9, 4), 1.04,
    )
    assert result["previous_session_date"] == "2026-09-03"
    assert result["previous_close"] == pytest.approx(0.8866)
    assert result["target_close"] == pytest.approx(1.04)
    assert result["target_close_source"] == "nasdaq_historical"
    assert result["target_timestamp"] == "Nasdaq historical close 09/04/2026"


def test_nasdaq_official_close_does_not_compare_live_nls_with_backfill_session():
    def transport(url, _headers):
        if "/historical?" in url:
            return json.dumps({
                "status": {"rCode": 200},
                "data": {"tradesTable": {"rows": [
                    {"date": "09/29/2026", "close": "$0.40", "volume": "1000"},
                    {"date": "09/28/2026", "close": "$0.32", "volume": "1000"},
                    {"date": "09/25/2026", "close": "$0.18", "volume": "1000"},
                ]}},
            }).encode()
        if "/realtime-trades?" in url:
            return json.dumps({"data": {"topTable": {"rows": [
                {"previousClose": "$0.32"},
            ]}}}).encode()
        return json.dumps({"data": {
            "symbol": "SOAR", "assetClass": "STOCKS", "notifications": [],
            "primaryData": None,
            "secondaryData": {"lastSalePrice": "$0.40", "lastTradeTimestamp": "Closed at Sep 29, 2026 4:00 PM ET"},
        }}).encode()

    provider = NasdaqOfficialCloseProvider(
        transport=transport, now=lambda: datetime(2026, 9, 29, 22, tzinfo=timezone.utc),
    )
    backfill = provider.fetch("SOAR", date(2026, 9, 28), 0.32)
    assert backfill["previous_close"] == pytest.approx(0.18)
    assert backfill["target_close"] == pytest.approx(0.32)

    def conflicting_transport(url, headers):
        if "/realtime-trades?" in url:
            return json.dumps({"data": {"topTable": {"rows": [
                {"previousClose": "$0.18"},
            ]}}}).encode()
        return transport(url, headers)

    current_provider = NasdaqOfficialCloseProvider(
        transport=conflicting_transport,
        now=lambda: datetime(2026, 9, 29, 22, tzinfo=timezone.utc),
    )
    with pytest.raises(MarketDataError, match="official historical/NLS previous close mismatch"):
        current_provider.fetch("SOAR", date(2026, 9, 29), 0.40)


def test_nasdaq_official_close_rejects_info_historical_target_mismatch():
    def transport(url, _headers):
        if "/historical?" in url:
            return json.dumps({
                "status": {"rCode": 200},
                "data": {"tradesTable": {"rows": [
                    {"date": "09/04/2026", "close": "$1.04", "volume": "747,778"},
                    {"date": "09/03/2026", "close": "$0.8866", "volume": "404,574"},
                ]}},
            }).encode()
        if "/realtime-trades?" in url:
            return json.dumps({"data": {"topTable": {"rows": [
                {"previousClose": "$0.8866"},
            ]}}}).encode()
        return json.dumps({"data": {
            "symbol": "PAAI", "assetClass": "STOCKS", "notifications": [],
            "primaryData": None,
            "secondaryData": {
                "lastSalePrice": "$1.02",
                "lastTradeTimestamp": "Closed at Sep 4, 2026 4:00 PM ET",
            },
        }}).encode()

    with pytest.raises(MarketDataError, match="info/historical target close mismatch"):
        NasdaqOfficialCloseProvider(transport=transport).fetch("PAAI", date(2026, 9, 4), 1.04)


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


def test_instrument_filter_does_not_exclude_community_name():
    assert eligible_screener_row(row(
        "CMCT", 25.0, name="Creative Media & Community Trust Corporation Common Shares",
    ))


def test_stockanalysis_scans_beyond_first_twenty_before_filtering():
    entries = []
    for index in range(21):
        symbol = "UNT" if index == 0 else "CMCT" if index == 1 else f"T{index:02}"
        name = (
            "Acme Acquisition Corp Units" if index == 0 else
            "Creative Media & Community Trust Corporation Common Shares" if index == 1 else
            f"Example {index} Common Stock"
        )
        entries.append(
            f'{{no:{index + 1},s:"{symbol}",n:"{name}",change:{100 - index},'
            'priceDate:"2026-09-30",price:1.0,volume:100,marketCap:1000}'
        )
    provider = StockAnalysisGainersProvider(
        transport=lambda *_: "".join(entries).encode(), now=lambda: NOW,
    )
    snapshot = provider.fetch(date(2026, 9, 30))
    assert len(snapshot["rows"]) == 21
    assert screener_candidate_symbols(snapshot) == ["CMCT", *[f"T{index:02}" for index in range(2, 21)]]


def test_stockanalysis_uses_fifty_ranked_candidates_for_replacement_audit():
    api_rows = [
        {"no": index, "s": f"T{index:02}", "n": f"Company {index}",
         "change": 101 - index, "price": 10.0, "volume": 1000, "marketCap": 1000000,
         "priceDate": "2026-10-05"}
        for index in range(1, 51)
    ]
    raw = json.dumps({"status": 200, "data": {"data": api_rows, "resultsCount": 200}}).encode()
    provider = StockAnalysisGainersProvider(transport=lambda *_: raw, now=lambda: NOW)
    snapshot = provider.fetch(date(2026, 10, 5))
    assert len(snapshot["rows"]) == 50
    assert snapshot["rows"][20]["symbol"] == "T21"
    assert snapshot["source_identifier"].startswith(StockAnalysisGainersProvider.API_URL)


def test_stockanalysis_excludes_individual_stale_rows_without_discarding_current_session():
    api_rows = [
        {"no": index, "s": f"T{index:02}", "n": f"Company {index}",
         "change": 101 - index, "price": 10.0, "volume": 1000, "marketCap": 1000000,
         "priceDate": "2026-10-22" if index == 2 else "2026-10-23"}
        for index in range(1, 51)
    ]
    raw = json.dumps({"status": 200, "data": {"data": api_rows, "resultsCount": 200}}).encode()
    provider = StockAnalysisGainersProvider(transport=lambda *_: raw, now=lambda: NOW)
    snapshot = provider.fetch(date(2026, 10, 23))
    assert len(snapshot["rows"]) == 49
    assert snapshot["excluded_stale_rows"] == [{"symbol": "T02", "price_date": "2026-10-22"}]
    assert "T02" not in screener_candidate_symbols(snapshot)


def test_stockanalysis_rejects_table_without_twenty_current_session_rows():
    api_rows = [
        {"no": index, "s": f"T{index:02}", "n": f"Company {index}",
         "change": 101 - index, "price": 10.0, "volume": 1000, "marketCap": 1000000,
         "priceDate": "2026-10-22"}
        for index in range(1, 51)
    ]
    raw = json.dumps({"status": 200, "data": {"data": api_rows, "resultsCount": 200}}).encode()
    provider = StockAnalysisGainersProvider(transport=lambda *_: raw, now=lambda: NOW)
    with pytest.raises(MarketDataError, match="only 0 rows"):
        provider.fetch(date(2026, 10, 23))


def test_unverified_reverse_split_artifact_stops_ranking():
    rows = [row("SPLT", 100, close=10)] + [row(f"T{i:02}", 50 - i, close=11) for i in range(21)]
    history = {"SPLT": series("SPLT", (10, 10))}
    history.update({f"T{i:02}": series(f"T{i:02}", (11 / (1 + (50 - i) / 100), 11)) for i in range(21)})
    with pytest.raises(MarketDataError, match="Top20 SPLT screener/history mismatch"):
        rank_top20(screener(rows), target_session_date=date(2026, 9, 1), historical_series=history)


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


def test_ads_issuer_total_market_cap_uses_underlying_share_price():
    rows = [row("GENR", 17.219, close=9)] + [row(f"T{i:02}", 16 - i / 10, close=10) for i in range(19)]
    rows[0]["name"] = "Generic Holdings American Depositary Shares"
    components = {"GENR": [{"class": "Ordinary shares represented by ADS",
                             "price": 9 / 3, "shares_outstanding": 6_000_000,
                             "quoted_security_ratio": 3}]}
    ranked = rank_top20(screener(rows), target_session_date=date(2026, 9, 1), issuer_components=components)
    assert ranked[0]["market_cap"] == pytest.approx(18_000_000)
    assert ranked[0]["market_cap_method"] == "issuer_total_ads"


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


class MissingHistoryFixtureArbitrator(GenericFixtureArbitrator):
    def resolve_missing_previous(self, *, candidate, historical_series, target, **_kwargs):
        official_previous = float(candidate["_close"]) / (1.0 + float(candidate["_change_pct"]) / 100.0)
        return {
            "discrepancy_status": "resolved",
            "discrepancy_reason": "future_corporate_action_vendor_history_omission",
            "compared_providers": ["nasdaq", "yahoo", "independent_fixture"],
            "official_previous_close": official_previous,
            "official_target_close": float(candidate["_close"]),
            "supporting_sources": [{
                "provider": "fixture", "role": "official_market_source",
                "raw_response_sha256": "a" * 64,
            }],
            "resolved_at": "2026-09-11T00:00:00+00:00",
            "corporate_action_status": "official_future_action_verified",
            "liquidity_flag": "normal_liquidity",
            "screener_change_pct": float(candidate["_change_pct"]),
            "screener_last_sale": float(candidate["_close"]),
            "screener_net_change": None,
            "screener_implied_previous_close": official_previous,
            "historical_previous_close": None,
            "historical_target_close": target.regular_close,
            "historical_change_pct": float(candidate["_change_pct"]),
        }

def test_missing_vendor_previous_bar_requires_and_uses_verified_fallback():
    rows = [row("MISS", 20.0, close=12.0)] + [row(f"T{i:02}", 19 - i / 10, close=10) for i in range(19)]
    history = {
        "MISS": DailySeries(
            symbol="MISS", provider="yahoo_chart_query1",
            source_identifier="https://query1.finance.yahoo.com/chart/MISS",
            retrieved_at="2026-09-11T00:00:00+00:00", raw_response_sha256="c" * 64,
            bars=(DailyBar(date(2026, 9, 1), 12.0),),
        ),
    }
    history.update({
        f"T{i:02}": series(f"T{i:02}", (10 / (1 + (19 - i / 10) / 100), 10))
        for i in range(19)
    })
    ranked = rank_top20(
        screener(rows), target_session_date=date(2026, 9, 1), historical_series=history,
        discrepancy_arbitrator=MissingHistoryFixtureArbitrator(),
    )
    assert ranked[0]["ticker"] == "MISS"
    assert ranked[0]["discrepancy_reason"] == "future_corporate_action_vendor_history_omission"
    assert ranked[0]["historical_previous_close"] is None
    assert ranked[0]["historical_change_pct"] == pytest.approx(20.0)


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


def test_vendor_omitted_immediate_previous_session_uses_dated_official_pair():
    class Provider:
        def __init__(self, value):
            self.value = value

        def fetch(self, *_args):
            return self.value

    candidate = {
        **row("GAP", 83.838, close=3.64, volume=15_619_424),
        "_change_pct": 83.838,
        "_close": 3.64,
    }
    arbitrator = LiveDiscrepancyArbitrator(
        official_provider=Provider({
            "provider": "nasdaq_official_fixture", "provider_family": "nasdaq",
            "target_close_verified": True,
            "target_timestamp": "Closed at Sep 23, 2026 4:00 PM ET",
            "target_close_source": "nasdaq_info", "target_session_date": "2026-09-23",
            "previous_session_date": "2026-09-22", "previous_close": 1.98,
            "target_close": 3.64, "notifications": [],
            "source_identifiers": ["https://nasdaq.test/history"],
            "raw_response_sha256": ["a" * 64],
        }),
        action_provider=Provider({
            "provider": "action_fixture", "provider_family": "yahoo",
            "status": "checked_none", "events": {},
            "source_identifier": "https://yahoo.test/events", "raw_response_sha256": "b" * 64,
        }),
        minute_provider=Provider({
            "provider": "minute_fixture", "provider_family": "yahoo",
            "price_field": "boundary_open", "previous_close": 1.98,
            "target_close": 3.64, "previous_last_regular_bar_close": None,
            "target_last_regular_bar_close": 3.66,
            "source_identifier": "https://yahoo.test/minute", "raw_response_sha256": "c" * 64,
        }),
        independent_providers=(Provider({
            "provider": "independent_fixture", "provider_family": "independent_fixture",
            "previous_close": 1.98, "target_close": 3.64,
            "source_identifier": "https://independent.test/quote", "raw_response_sha256": "d" * 64,
        }),),
        now=lambda: NOW,
    )
    resolved = arbitrator.resolve(
        ticker="GAP", candidate=candidate,
        historical_series=series(
            "GAP", closes=(2.07, 3.64),
            days=(date(2026, 9, 21), date(2026, 9, 23)),
            provider="yahoo_chart_query1",
        ),
        previous=DailyBar(date(2026, 9, 21), 2.07),
        target=DailyBar(date(2026, 9, 23), 3.64),
        target_session_date=date(2026, 9, 23), tolerance_pct=0.20,
    )
    assert resolved["discrepancy_reason"] == "vendor_omitted_previous_session"
    assert resolved["basis_evidence"]["official_previous_session_date"] == "2026-09-22"
    assert resolved["official_previous_close"] == pytest.approx(1.98)

    # A 16:00 boundary quote may disagree with the final close. The dated
    # independent EOD pair still establishes the missing vendor session.
    arbitrator.minute_provider.value["target_close"] = 3.60
    arbitrator.independent_providers[0].value.update({
        "session": "regular_close", "target_session_date": "2026-09-23",
    })
    resolved = arbitrator.resolve(
        ticker="GAP", candidate=candidate,
        historical_series=series(
            "GAP", closes=(2.07, 3.64),
            days=(date(2026, 9, 21), date(2026, 9, 23)),
            provider="yahoo_chart_query1",
        ),
        previous=DailyBar(date(2026, 9, 21), 2.07),
        target=DailyBar(date(2026, 9, 23), 3.64),
        target_session_date=date(2026, 9, 23), tolerance_pct=0.20,
    )
    assert resolved["discrepancy_reason"] == "vendor_omitted_previous_session"
    assert resolved["basis_evidence"]["comparison_basis"] == "dated_regular_close"
    assert resolved["basis_evidence"]["independent_eod"]


def test_historical_rdib_stale_daily_incident_remains_a_permanent_regression_fixture():
    incident = json.loads((ROOT / "tests" / "fixtures" / "ny_market_rdib_stale_daily_incident.json").read_text(encoding="utf-8"))
    candidate = {
        **incident["screener"], "symbol": incident["ticker"],
        "_change_pct": float(incident["screener"]["pctchange"]),
        "_close": 17.70,
    }
    resolved = resolve_discrepancy(
        candidate=candidate,
        historical_provider=incident["historical"]["provider"],
        historical_previous_close=incident["historical"]["previous_close"],
        historical_target_close=incident["historical"]["target_close"],
        historical_change_pct=incident["historical"]["change_pct"],
        official=incident["official"], corporate_action=incident["corporate_action"],
        minute_close=incident["minute_close"], independent_sources=incident["independent"],
        tolerance_pct=0.20, resolved_at="2026-09-02T00:00:00+00:00",
    )
    assert resolved["discrepancy_status"] == "resolved"
    assert resolved["discrepancy_reason"] == "stale_daily_bar"
    assert resolved["liquidity_flag"] == "low_liquidity"
    assert resolved["official_previous_close"] == pytest.approx(15.10)
    assert candidate["_change_pct"] == pytest.approx(17.219)


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


def test_canonical_packet_contains_full_provider_and_raw_hash_provenance():
    class PacketProvider:
        name = "packet_fixture"

        def fetch(self, symbol, _start_date, _end_date):
            closes = (100.0, 110.0) if symbol.startswith("^") or symbol.startswith("X") else (10.0, 11.0)
            return series(symbol, closes, provider=self.name)

    class PacketScreener:
        def fetch(self):
            return screener([row(f"T{i:02}", 10.0, close=11.0) for i in range(20)])

    packet = build_canonical_market_data_packet(
        date(2026, 9, 1), historical_providers=[PacketProvider()],
        screener_provider=PacketScreener(), discrepancy_arbitrator=GenericFixtureArbitrator(),
    )
    assert packet["market_data_contract_version"] == "ny_market_data_v1"
    assert len(packet["indexes"]) == 5
    assert len(packet["sectors"]) == 11
    assert len(packet["top_gainers_20"]) == 20
    assert packet["discrepancy_count"] == 0
    assert packet["providers"] == ["nasdaq_stock_screener", "packet_fixture"]
    assert packet["screener"]["raw_response_sha256"] == "b" * 64
    assert set(packet["raw_response_hashes"]) == {"a" * 64, "b" * 64}


def test_canonical_packet_stops_for_stale_candidate_in_acquisition_window():
    class PacketProvider:
        name = "packet_fixture"

        def fetch(self, symbol, _start_date, _end_date):
            if symbol == "STALE":
                original = series(symbol, (9.0, 10.0), provider=self.name)
                return DailySeries(
                    symbol=original.symbol,
                    provider=original.provider,
                    source_identifier=original.source_identifier,
                    retrieved_at=original.retrieved_at,
                    raw_response_sha256=original.raw_response_sha256,
                    bars=(original.bars[0],),
                )
            closes = (100.0, 110.0) if symbol.startswith("^") or symbol.startswith("X") else (10.0, 11.0)
            return series(symbol, closes, provider=self.name)

    class PacketScreener:
        def fetch(self):
            rows = [row(f"T{i:02}", 10.0, close=11.0) for i in range(20)]
            rows.append(row("STALE", 1.0, close=10.0))
            return screener(rows)

    with pytest.raises(MarketDataError, match="complete-group acquisition for target session"):
        build_canonical_market_data_packet(
            date(2026, 9, 1), historical_providers=[PacketProvider()],
            screener_provider=PacketScreener(), discrepancy_arbitrator=GenericFixtureArbitrator(),
        )


def test_top20_excludes_zero_volume_target_bar_and_records_provenance():
    rows = [row("HALT", 25.0, close=10.0)]
    rows.extend(row(f"T{i:02}", 24.0 - i / 10, close=10.0) for i in range(20))
    history = {
        item["symbol"]: series(
            item["symbol"],
            (10.0 / (1 + float(item["pctchange"]) / 100.0), 10.0),
        )
        for item in rows
    }
    halted = history["HALT"]
    history["HALT"] = DailySeries(
        symbol=halted.symbol,
        provider=halted.provider,
        source_identifier=halted.source_identifier,
        retrieved_at=halted.retrieved_at,
        raw_response_sha256=halted.raw_response_sha256,
        bars=(halted.bars[0], DailyBar(date(2026, 9, 1), 10.0, 0.0)),
    )
    excluded: list[dict] = []
    ranked = rank_top20(
        screener(rows),
        target_session_date=date(2026, 9, 1),
        historical_series=history,
        excluded_candidates=excluded,
    )
    assert "HALT" not in [item["ticker"] for item in ranked]
    assert len(ranked) == 20
    assert excluded == [{
        "ticker": "HALT",
        "reason": "zero_volume_target_bar",
        "session_date": "2026-09-01",
        "provider": "fixture",
        "source_identifier": "https://example.test/HALT",
        "raw_response_sha256": "a" * 64,
    }]


ACTION_CLOSES = json.loads((ROOT / "tests/fixtures/ny_market_202610_action_closes.json").read_text(encoding="utf-8"))


def _action_screener_and_history(symbol, action_change, target_close, previous_close, previous_day, target_day):
    rows = [row(symbol, action_change, close=target_close)]
    rows.extend(row(f"T{i:02}", 50 - i, close=11) for i in range(20))
    history = {
        item["symbol"]: series(
            item["symbol"], (11 / (1 + float(item["pctchange"]) / 100), 11),
            days=(previous_day, target_day),
        )
        for item in rows[1:]
    }
    history[symbol] = series(symbol, (previous_close, target_close), days=(previous_day, target_day))
    return screener(rows), history


def test_nstr_post_combination_same_symbol_closes_are_compared_before_exclusion():
    captured = ACTION_CLOSES["NSTR"]
    prior_day = date.fromisoformat(captured["prior_session_date"])
    target_day = date.fromisoformat(captured["target_session_date"])
    source, history = _action_screener_and_history("NSTR", 100, 4.45, 7.72, prior_day, target_day)
    history["NSTR"] = DailySeries(
        symbol="NSTR", provider="fixture", source_identifier="https://example.test/NSTR",
        retrieved_at=NOW.isoformat(), raw_response_sha256="a" * 64,
        bars=(DailyBar(target_day, captured["target_regular_close"]),),
    )
    class Official:
        def fetch(self, symbol, day, _last):
            assert (symbol, day) == ("NSTR", target_day)
            return {
                "provider": "nasdaq_official_historical_nls", "provider_family": "nasdaq",
                "previous_session_date": prior_day.isoformat(), "target_session_date": target_day.isoformat(),
                "previous_close": 7.7199, "target_close": 4.45,
                "source_identifiers": ["https://api.nasdaq.com/api/quote/NSTR/historical"],
                "raw_response_sha256": ["d" * 64],
            }

    class IndependentHistory:
        def fetch_pair(self, symbol, previous, target):
            assert (symbol, previous, target) == ("NSTR", prior_day, target_day)
            return {
                "provider": "stockanalysis_historical_s_p_global", "provider_family": "s_p_global",
                "source_identifier": "https://stockanalysis.com/stocks/nstr/history/",
                "raw_response_sha256": "c" * 64,
                "previous_split_normalized_close": 7.72, "target_regular_close": 4.45,
            }

    arbitrator = LiveDiscrepancyArbitrator(
        corporate_action_evidence={"NSTR": {
            "event_type": "business_combination", "prior_ticker": captured["prior_symbol"],
            "effective_session_date": "2026-10-01", "exchange": "NYSE American",
            "source_url": captured["source_url"],
        }},
        notice_transport=lambda *_: b"VACI NSTR NYSE American October 1, 2026 Business Combination",
        official_provider=Official(), split_history_provider=IndependentHistory(),
    )
    excluded = []
    ranked = rank_top20(source, target_session_date=target_day, historical_series=history,
                          discrepancy_arbitrator=arbitrator, excluded_action_candidates=excluded,
                          expected_previous_session_date=prior_day)
    assert len(ranked) == 20 and "NSTR" not in [item["ticker"] for item in ranked]
    assert excluded[0]["reason"] == "post_combination_same_symbol_move_below_verified_top20_floor"
    assert excluded[0]["screener_rank"] == 1
    assert excluded[0]["verified_change_pct"] == pytest.approx((4.45 / 7.7199 - 1) * 100)


@pytest.mark.parametrize("predecessor_quote,expected_factor", [(0.15, 10), (1.51, 1), (1.50, 1), (1.49, None)])
def test_vjet_captured_reverse_split_is_normalized_once_and_removed_below_floor(predecessor_quote, expected_factor):
    captured = ACTION_CLOSES["VJET"]
    prior_day = date.fromisoformat(captured["prior_session_date"])
    target_day = date.fromisoformat(captured["target_session_date"])
    source, history = _action_screener_and_history(
        "VJET", captured["screener_change_pct"], captured["target_regular_close"],
        captured["prior_regular_close"], prior_day, target_day,
    )

    class PriorProvider:
        name = "yahoo_daily_close"
        def fetch(self, symbol, _start, _end):
            assert symbol == captured["prior_symbol"]
            return series(symbol, (predecessor_quote,), days=(prior_day,), provider=self.name)

    class SplitHistory:
        def fetch_pair(self, symbol, previous, target):
            assert (symbol, previous, target) == ("VJET", prior_day, target_day)
            return {
                "provider": "stockanalysis_historical_s_p_global",
                "provider_family": "s_p_global", "source_identifier": "https://stockanalysis.com/stocks/vjet/history/",
                "raw_response_sha256": "c" * 64,
                "previous_split_normalized_close": captured["independent_split_normalized_prior_close"],
                "target_regular_close": captured["target_regular_close"],
            }

    arbitrator = LiveDiscrepancyArbitrator(
        corporate_action_evidence={"VJET": {
            "event_type": "reverse_split", "prior_ticker": captured["prior_symbol"],
            "effective_session_date": "2026-10-05", "exchange": "NYSE American",
            "new_shares": 1, "old_shares": 10, "source_url": captured["source_url"],
        }},
        notice_transport=lambda *_: b"VTAK VJET NYSE American October 5, 2026 1-for-10 reverse stock split",
        predecessor_provider=PriorProvider(), split_history_provider=SplitHistory(),
    )
    excluded = []
    if expected_factor is None:
        with pytest.raises(MarketDataError, match="predecessor split basis is ambiguous or disagrees"):
            rank_top20(source, target_session_date=target_day, historical_series=history,
                       discrepancy_arbitrator=arbitrator, excluded_action_candidates=excluded,
                       expected_previous_session_date=prior_day)
        assert not excluded
        return
    ranked = rank_top20(source, target_session_date=target_day, historical_series=history,
                          discrepancy_arbitrator=arbitrator, excluded_action_candidates=excluded,
                          expected_previous_session_date=prior_day)
    assert len(ranked) == 20 and "VJET" not in [item["ticker"] for item in ranked]
    assert excluded[0]["reason"] == "screener_split_artifact_below_verified_top20_floor"
    assert excluded[0]["verified_change_pct"] == pytest.approx((1.62 / 1.51 - 1) * 100)
    assert excluded[0]["basis_evidence"]["share_basis_factor"] == 10
    assert excluded[0]["basis_evidence"]["applied_factor"] == expected_factor


def test_new_listing_requires_primary_document_and_missing_vendor_history_without_it_stops():
    day = date(2026, 10, 5)
    prior = date(2026, 10, 2)
    source, history = _action_screener_and_history("NEWA", 90, 10, 10, prior, day)
    history["NEWA"] = DailySeries("NEWA", "fixture", "https://example.test/NEWA", NOW.isoformat(), "a" * 64,
                                   (DailyBar(day, 10),))
    with pytest.raises(MarketDataError, match="verified corporate-action evidence"):
        rank_top20(source, target_session_date=day, historical_series=history,
                   discrepancy_arbitrator=LiveDiscrepancyArbitrator(), expected_previous_session_date=prior)
    excluded = []
    arbitrator = LiveDiscrepancyArbitrator(
        corporate_action_evidence={"NEWA": {
            "event_type": "new_listing", "effective_session_date": "2026-10-05",
            "exchange": "Nasdaq", "source_url": "https://www.sec.gov/Archives/edgar/data/123/456/offer.htm",
        }}, notice_transport=lambda *_: b"NEWA Nasdaq October 5, 2026 Initial public offering",
    )
    ranked = rank_top20(source, target_session_date=day, historical_series=history,
                        discrepancy_arbitrator=arbitrator, excluded_action_candidates=excluded,
                        expected_previous_session_date=prior)
    assert len(ranked) == 20 and excluded[0]["reason"] == "new_listing_no_comparable_previous_close"


def test_new_listing_date_can_be_corroborated_by_official_nasdaq_event():
    sec = b"LYCA Nasdaq Initial public offering"
    event = b"LYCA Nasdaq Opening Bell October 23, 2026"
    def transport(url, _headers):
        return event if "/events/" in url else sec
    evidence = {
        "event_type": "new_listing", "effective_session_date": "2026-10-23",
        "exchange": "Nasdaq",
        "source_url": "https://www.sec.gov/Archives/edgar/data/1/filing.htm",
        "date_confirmation_url": "https://www.nasdaq.com/events/lyca-opening-bell",
    }
    verified = verify_corporate_action_evidence("LYCA", evidence, transport=transport)
    assert verified["date_confirmation_sha256"] == hashlib.sha256(event).hexdigest()


def test_psky_captured_screener_artifact_is_excluded_only_with_dated_independent_closes():
    captured = ACTION_CLOSES["PSKY"]
    prior_day = date.fromisoformat(captured["prior_session_date"])
    target_day = date.fromisoformat(captured["target_session_date"])
    source, history = _action_screener_and_history(
        "PSKY", captured["screener_change_pct"], captured["target_regular_close"],
        captured["prior_regular_close"], prior_day, target_day,
    )

    class Official:
        def fetch(self, symbol, day, _last):
            assert (symbol, day) == ("PSKY", target_day)
            return {
                "provider": "nasdaq_official_historical_nls", "provider_family": "nasdaq",
                "previous_session_date": prior_day.isoformat(), "target_session_date": target_day.isoformat(),
                "previous_close": 9.5, "target_close": 9.775,
                "source_identifiers": ["https://api.nasdaq.com/api/quote/PSKY/historical"],
                "raw_response_sha256": ["d" * 64],
            }

    class Action:
        def fetch(self, symbol, day):
            return {"provider": "yahoo_corporate_actions", "provider_family": "yahoo",
                    "status": "checked_none", "source_identifier": "https://query1.finance.yahoo.com/psky",
                    "raw_response_sha256": "e" * 64}

    class Independent:
        name = "public_com_previous_close"
        def fetch(self, symbol, day):
            return {"provider": self.name, "provider_family": "public_com", "previous_close": 9.5,
                    "target_session_date": day.isoformat(), "source_identifier": "https://public.com/stocks/psky",
                    "raw_response_sha256": "f" * 64}

    excluded = []
    arbitrator = LiveDiscrepancyArbitrator(
        official_provider=Official(), action_provider=Action(), independent_providers=[Independent()],
    )
    ranked = rank_top20(source, target_session_date=target_day, historical_series=history,
                        discrepancy_arbitrator=arbitrator, excluded_action_candidates=excluded,
                        expected_previous_session_date=prior_day)
    assert len(ranked) == 20
    assert excluded[0]["reason"] == "screener_change_conflicts_with_verified_regular_closes"
    assert excluded[0]["verified_change_pct"] == pytest.approx((9.775 / 9.5 - 1) * 100)


def test_transient_http_retries_are_bounded(monkeypatch):
    from lib import ny_market_market_data as market_data

    calls = []
    monkeypatch.setattr(market_data.time_module, "sleep", lambda duration: None)
    def transient(_request, timeout):
        calls.append(timeout)
        if len(calls) < 3:
            raise HTTPError("https://example.test", 503, "busy", {}, None)
        return io.BytesIO(b"ok")
    monkeypatch.setattr(market_data, "urlopen", transient)
    assert default_transport("https://example.test", {}) == b"ok"
    assert len(calls) == 3
    calls.clear()
    def unavailable(_request, timeout):
        calls.append(timeout)
        raise URLError("offline")
    monkeypatch.setattr(market_data, "urlopen", unavailable)
    with pytest.raises(URLError):
        default_transport("https://example.test", {})
    assert len(calls) == 3


@pytest.mark.parametrize("historical_previous,basis,factor", [(0.3441,"unadjusted",9.0),(3.0969,"already_split_normalized",1.0)])
def test_effective_split_precedes_nls_comparison_without_double_application(historical_previous,basis,factor):
    notice_url="https://www.nasdaqtrader.com/TraderNews.aspx?id=fixture"
    notice=b"<p>Example (TEST) will effect a one-for-nine (1-9) reverse split. The reverse stock split will become effective on Tuesday, October 27, 2026.</p>"
    def transport(url,headers):
        if "/historical?" in url:
            previous_volume = "N/A" if basis == "already_split_normalized" else "100"
            return json.dumps({"status":{"rCode":200},"data":{"tradesTable":{"rows":[{"date":"10/26/2026","close":str(historical_previous),"volume":previous_volume},{"date":"10/27/2026","close":"5.41","volume":"100"}]}}}).encode()
        if "/realtime-trades?" in url:
            return json.dumps({"data":{"topTable":{"rows":[{"previousClose":"3.0969"}]}}}).encode()
        return json.dumps({"data":{"symbol":"TEST","assetClass":"STOCKS","secondaryData":{"lastSalePrice":"5.41","lastTradeTimestamp":"Closed at Oct 27, 2026 4:00 PM ET"}}}).encode()
    provider=NasdaqOfficialCloseProvider(transport=transport,now=lambda:datetime(2026,10,27,22,tzinfo=timezone.utc),corporate_action_notices={"TEST":notice_url},notice_transport=lambda *_:notice)
    result=provider.fetch("TEST",date(2026,10,27),5.41)
    assert result["previous_close"]==pytest.approx(3.0969)
    assert result["target_close"]==5.41
    assert result["previous_close_raw"]==historical_previous
    assert result["previous_price_basis"]==basis
    assert result["applied_split_factor"]==factor
    assert result["historical_volume_basis_marker"] == ("N/A" if factor == 1 else "100")
    assert (result["target_close"]/result["previous_close"]-1)*100==pytest.approx(74.6908224353)
    # Without exchange notice the original mismatch check must still reject.
    if factor==9:
        with pytest.raises(MarketDataError,match="official historical/NLS previous close mismatch"):
            NasdaqOfficialCloseProvider(transport=transport,now=lambda:datetime(2026,10,27,22,tzinfo=timezone.utc)).fetch("TEST",date(2026,10,27),5.41)


def test_effective_split_requires_dated_independent_pair_and_keeps_exchange_close():
    args=dict(candidate={"_close":5.41,"_change_pct":74.69,"volume":1000},historical_provider="yahoo_chart_query1",
        historical_previous_close=3.0654,historical_target_close=5.41,historical_change_pct=76.48,
        official={"provider":"nasdaq_official","previous_close":3.0969,"previous_close_raw":.3441,
        "target_close":5.41,"target_close_verified":True,"previous_close_on_target_basis":True,
        "previous_price_basis":"unadjusted","applied_split_factor":9,
        "previous_session_date":"2026-10-26","target_session_date":"2026-10-27"},
        corporate_action={"provider":"yahoo_actions","status":"corporate_action_adjusted","official_action":{
        "effective_session_date":"2026-10-27","old_shares":9,"new_shares":1}},
        minute_close={"provider":"yahoo_minute","price_field":"boundary_open","previous_close":3.0969,
        "target_close":5.37,"previous_last_regular_bar_close":3.0654},
        independent_sources=[{"provider":"stockanalysis","provider_family":"stockanalysis","session":"regular_close",
        "target_session_date":"2026-10-27","previous_close":3.10,"target_close":5.41,"display_decimal_places":2}],
        tolerance_pct=.2,resolved_at="2026-10-27T23:00:00Z")
    result=resolve_discrepancy(**args)
    assert result["discrepancy_reason"]=="official_split_normalized_eod"
    assert result["official_target_close"]==5.41
    args["independent_sources"][0]["target_close"]=5.37
    with pytest.raises(MarketDataError):resolve_discrepancy(**args)
