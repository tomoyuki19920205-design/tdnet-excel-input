from __future__ import annotations

import json
from pathlib import Path
from datetime import date, datetime, timezone
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
    YahooChartProvider,
    build_canonical_market_data_packet,
    build_index_sector_snapshot,
    eligible_screener_row,
    fetch_all_or_fallback,
    parse_market_numeric_token,
    provider_family,
    rank_top20,
    resolve_discrepancy,
    resolve_latest_completed_sessions,
    screener_candidate_symbols,
)


STAMP_AUG_28 = 1787923800
STAMP_AUG_31 = 1788183000
STAMP_SEP_1 = 1788269400
NOW = datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).parents[1]


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
<body><table><tr><td>Date<br>(EDT)</td><td>Open</td><td>High</td><td>Low</td>
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


def test_sub_dollar_daily_bar_precision_rounding_is_resolved_generically():
    candidate = {
        **row("PENNY", 26.453, close=0.1501, volume=863_667_458),
        "_change_pct": 26.453,
        "_close": 0.1501,
    }
    resolved = resolve_discrepancy(
        candidate=candidate,
        historical_provider="yahoo_chart_query1",
        historical_previous_close=0.12,
        historical_target_close=0.1501,
        historical_change_pct=25.083333,
        official={
            "provider": "nasdaq_official_fixture", "provider_family": "nasdaq",
            "previous_close": 0.1187, "target_close": 0.1501,
        },
        corporate_action={
            "provider": "action_fixture", "provider_family": "yahoo", "status": "checked_none",
        },
        minute_close={
            "provider": "minute_fixture", "provider_family": "yahoo",
            "previous_close": 0.1187, "target_close": 0.1688,
        },
        independent_sources=[{
            "provider": "independent_fixture", "provider_family": "independent_fixture",
            "previous_close": 0.12, "target_close": None,
        }],
        tolerance_pct=0.20,
        resolved_at="2026-09-03T00:00:00+00:00",
    )
    assert resolved["discrepancy_status"] == "resolved"
    assert resolved["discrepancy_reason"] == "daily_bar_precision_rounding"


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


def test_canonical_packet_allows_stale_low_rank_screener_row_without_target_bar():
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

    packet = build_canonical_market_data_packet(
        date(2026, 9, 1), historical_providers=[PacketProvider()],
        screener_provider=PacketScreener(), discrepancy_arbitrator=GenericFixtureArbitrator(),
    )
    assert [item["ticker"] for item in packet["top_gainers_20"]] == [f"T{i:02}" for i in range(20)]


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
