import json
from datetime import date, datetime, timezone

import pytest

from lib.ny_market_price_basis import (
    official_regular_close, parse_exchange_split_notice, previous_on_target_basis,
    classify_official_discrepancy,
    validate_vendor_actions,
)
from lib.ny_market_market_data import YahooMinuteCloseProvider


def info():
    return {"symbol": "EXAMPLE", "assetClass": "STOCKS",
            "primaryData": {"lastSalePrice": "$0.3954", "lastTradeTimestamp": "Sep 3, 2026 7:41 PM ET"},
            "secondaryData": {"lastSalePrice": "$0.3838", "lastTradeTimestamp": "Closed at Sep 3, 2026 4:00 PM ET"}}


def test_after_hours_primary_is_never_the_official_regular_close():
    assert official_regular_close(info(), "EXAMPLE", date(2026, 9, 3))[0] == .3838


@pytest.mark.parametrize("symbol,session", [("WRONG", date(2026, 9, 3)), ("EXAMPLE", date(2026, 9, 4))])
def test_wrong_symbol_or_us_session_is_rejected(symbol, session):
    with pytest.raises(ValueError):
        official_regular_close(info(), symbol, session)


def test_after_hours_without_dated_regular_close_fails():
    value = info()
    del value["secondaryData"]
    with pytest.raises(ValueError):
        official_regular_close(value, "EXAMPLE", date(2026, 9, 3))


def notice():
    return parse_exchange_split_notice(
        b"<p>Example Ltd. (EXAMPLE) will effect a one-for-fifty (1-50) reverse split of its Class A Common Shares. The reverse stock split will become effective on Friday, September 4, 2026.</p>",
        "https://www.nasdaqtrader.com/TraderNews.aspx?id=ECA-fixture", "EXAMPLE")


def test_split_basis_only_changes_when_effective_session_is_crossed():
    event = notice()
    assert previous_on_target_basis(.2299, date(2026, 9, 2), date(2026, 9, 3), event) == .2299
    assert previous_on_target_basis(.2299, date(2026, 9, 3), date(2026, 9, 4), event) == pytest.approx(11.495)
    assert (12 / previous_on_target_basis(.2299, date(2026, 9, 3), date(2026, 9, 4), event) - 1) * 100 == pytest.approx(4.393214441)


def arguments():
    return dict(official={"target_close_verified": True, "previous_close": .2299, "target_close": .3838,
                          "target_session_date": "2026-09-03"},
                history_previous=11.494999885559082, history_target=.3837999999523163,
                minute={"price_field": "boundary_open", "previous_close": .2299, "target_close": .3838},
                action={"status": "corporate_action_adjusted", "official_action": notice()})


def test_real_anonymous_incident_requires_exact_official_action_ratio():
    reason, evidence = classify_official_discrepancy(**arguments())
    assert reason == "corporate_action_timing_mismatch"
    assert evidence["normalization_divisor"] == 50
    assert evidence["history_previous_normalized"] == pytest.approx(.2299)
    assert (.3838 / .2299 - 1) * 100 == pytest.approx(66.9421487603)


@pytest.mark.parametrize("mutation", ["ratio", "date", "minute", "unverified"])
def test_no_corporate_action_arbitration_without_matching_evidence(mutation):
    args = arguments()
    if mutation == "ratio":
        args["action"]["official_action"]["old_shares"] = 40
    elif mutation == "date":
        args["action"]["official_action"]["effective_session_date"] = "2026-09-02"
    elif mutation == "minute":
        args["minute"]["previous_close"] = .237
    else:
        args["official"]["target_close_verified"] = False
    assert classify_official_discrepancy(**args)[0] is None


def test_16_hour_bar_close_is_after_hours_and_not_boundary_evidence():
    stamps = [int(datetime(2026, 9, day, 20, tzinfo=timezone.utc).timestamp()) for day in (2, 3)]
    raw = json.dumps({"chart": {"result": [{"timestamp": stamps, "indicators": {"quote": [
        {"open": [.2299, .3838], "close": [.237, .3666], "volume": [265, 106446]}
    ]}}]}}).encode()
    provider = YahooMinuteCloseProvider(transport=lambda *_: raw)
    value = provider.fetch("ANOTHER", date(2026, 9, 2), date(2026, 9, 3))
    assert value["previous_close"] == .2299
    assert value["target_close"] == .3838
    assert value["price_field"] == "boundary_open"


def test_non_official_notice_cannot_authorize_normalization():
    with pytest.raises(ValueError):
        parse_exchange_split_notice(b"", "https://example.org/TraderNews.aspx", "EXAMPLE")


def test_full_arbitrator_does_not_mislabel_split_timing_as_stale_daily():
    from lib.ny_market_market_data import resolve_discrepancy
    args = arguments()
    result = resolve_discrepancy(
        candidate={"_change_pct": 66.942, "_close": .3838, "netchange": .1539, "volume": 107789065},
        historical_provider="yahoo_chart_query1", historical_previous_close=args["history_previous"],
        historical_target_close=args["history_target"], historical_change_pct=-96.66,
        official={**args["official"], "provider": "nasdaq_official"},
        corporate_action={**args["action"], "provider": "yahoo_events", "provider_family": "yahoo"},
        minute_close={**args["minute"], "provider": "yahoo_minute", "provider_family": "yahoo"},
        independent_sources=[{"provider": "public_com", "previous_close": .23}],
        tolerance_pct=.2, resolved_at="2026-09-03T23:45:00Z")
    assert result["discrepancy_reason"] == "corporate_action_timing_mismatch"
    assert result["basis_evidence"]["normalization_divisor"] == 50


@pytest.mark.parametrize("events", [
    {"dividends": {"x": {"amount": .01}}},
    {"splits": {"x": {"numerator": 1, "denominator": 40, "date": 1788528600}}},
    {"splits": {"x": {"numerator": 1, "denominator": 50, "date": 1788442200}}},
])
def test_official_notice_does_not_override_conflicting_vendor_events(events):
    with pytest.raises(ValueError):
        validate_vendor_actions(events, notice())


def test_matching_vendor_event_is_accepted():
    stamp = int(datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc).timestamp())
    validate_vendor_actions({"splits": {"x": {"numerator": 1, "denominator": 50, "date": stamp}}}, notice())


def test_original_small_discrepancy_is_not_claimed_to_be_split_adjustment():
    args = arguments()
    args["history_previous"] = .22949999570846558
    args["minute"]["previous_last_regular_bar_close"] = .22949999570846558
    reason, evidence = classify_official_discrepancy(**args)
    assert reason == "provider_error"
    assert evidence["diagnosis"] == "vendor_daily_matches_last_minute_trade_not_exchange_close"
    del args["minute"]["previous_last_regular_bar_close"]
    assert classify_official_discrepancy(**args)[0] is None
