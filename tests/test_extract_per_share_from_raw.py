from tools.extract_per_share_from_raw import extract_per_share


def _raw(**overrides):
    raw = {
        "Code": "74800",
        "CurFYEn": "2027-03-31",
        "CurPerType": "1Q",
        "DiscDate": "2026-08-03",
    }
    raw.update(overrides)
    return raw


def test_derives_bps_and_balance_sheet_aliases_from_jquants_short_keys():
    row = extract_per_share(
        _raw(Eq="16557000000", TA="29046000000", EqAR="0.57", ShOutFY="13282000", TrShFY="601635")
    )

    assert row is not None
    assert row["bps"] == 1305.72
    assert row["equity"] == 16_557_000_000
    assert row["total_assets"] == 29_046_000_000
    assert row["equity_ratio"] == 0.57


def test_explicit_bps_remains_authoritative():
    row = extract_per_share(
        _raw(BPS="123.45", Eq="999999", ShOutFY="100", TrShFY="0")
    )

    assert row is not None
    assert row["bps"] == 123.45


def test_forecast_dividend_aggregates_interim_and_year_end_components():
    row = extract_per_share(_raw(FDiv2Q="86", FDivFY="90"))

    assert row is not None
    assert row["forecast_dividend_annual"] == 176


def test_partial_interim_dividend_does_not_masquerade_as_annual():
    row = extract_per_share(_raw(FDiv2Q="86", FDivFY=""))

    assert row is not None
    assert row["forecast_dividend_annual"] is None


def test_authoritative_annual_dividend_preserves_special_dividend_total():
    row = extract_per_share(
        _raw(FDivAnn="125", FDiv2Q="40", FDivFY="60")
    )

    assert row is not None
    assert row["forecast_dividend_annual"] == 125


def test_actual_dividend_components_only_aggregate_with_fy_end_value():
    complete = extract_per_share(_raw(Div2Q="36", DivFY="75.5"))
    partial = extract_per_share(_raw(Div2Q="36", DivFY=""))

    assert complete is not None and complete["dividend_annual"] == 111.5
    assert partial is not None and partial["dividend_annual"] is None


def test_null_and_invalid_share_inputs_fail_closed():
    row = extract_per_share(
        _raw(Eq="16557000000", ShOutFY="601635", TrShFY="601635")
    )

    assert row is not None
    assert row["bps"] is None
