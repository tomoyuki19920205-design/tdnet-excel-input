from tools.extract_per_share_from_raw import (
    _actual_component_adjustments,
    _extract_next_year_forecast,
    _merge_next_year_record,
    _merge_primary_record,
    _next_fiscal_year_end,
    extract_per_share,
)


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


def test_actual_interim_plus_forecast_year_end_without_split():
    row = extract_per_share(_raw(Div2Q="30", FDivFY="40"))

    assert row is not None
    assert row["forecast_dividend_annual"] == 70


def test_pre_split_actual_component_is_normalized_but_post_split_forecast_is_not():
    prior = _raw(DiscDate="2026-05-11", FDiv2Q="63", FDivFY="21")
    current = _raw(DiscDate="2026-08-06", Div2Q="63", FDivFY="24")
    adjustments = _actual_component_adjustments(
        current,
        [prior, current],
        [("2026-06-29", 1 / 3)],
    )
    row = extract_per_share(
        current,
        actual_dividend_adjustments=adjustments,
    )

    assert row is not None
    assert row["dividend_q2"] == 21
    assert row["forecast_dividend_annual"] == 45


def test_both_components_pre_split_remain_on_disclosure_basis_for_viewer_action():
    prior = _raw(DiscDate="2026-04-01", FDiv2Q="30", FDivFY="40")
    current = _raw(DiscDate="2026-06-01", Div2Q="30", FDivFY="40")
    adjustments = _actual_component_adjustments(
        current,
        [prior, current],
        [("2026-07-01", 0.5)],
    )
    row = extract_per_share(current, actual_dividend_adjustments=adjustments)

    assert row is not None
    assert adjustments == {}
    assert row["forecast_dividend_annual"] == 70


def test_both_components_already_post_split_are_not_adjusted_twice():
    prior = _raw(DiscDate="2026-07-02", FDiv2Q="15", FDivFY="20")
    current = _raw(DiscDate="2026-08-01", Div2Q="15", FDivFY="20")
    adjustments = _actual_component_adjustments(
        current,
        [prior, current],
        [("2026-07-01", 0.5)],
    )
    row = extract_per_share(current, actual_dividend_adjustments=adjustments)

    assert row is not None
    assert adjustments == {}
    assert row["forecast_dividend_annual"] == 35


def test_forward_split_not_yet_in_market_data_uses_forecast_share_basis():
    row = extract_per_share(
        _raw(
            Div2Q="86.5",
            FDivFY="97.5",
            FNP="8601000000",
            FEPS="119.3",
            AvgSh="17992751",
            ShOutFY="18700000",
            TrShFY="675484",
        )
    )

    assert row is not None
    assert row["forecast_eps"] == 477.2
    assert row["forecast_dividend_annual"] == 476.5


def test_annual_forecast_only_remains_authoritative():
    row = extract_per_share(_raw(FDivAnn="90", FDiv2Q="40", FDivFY="50"))

    assert row is not None
    assert row["forecast_dividend_annual"] == 90


def test_full_year_actual_remains_authoritative():
    row = extract_per_share(
        _raw(CurPerType="FY", DivAnn="95", Div2Q="40", DivFY="50")
    )

    assert row is not None
    assert row["dividend_annual"] == 95


def test_forward_reverse_split_direction_is_not_inverted():
    row = extract_per_share(
        _raw(
            Div2Q="10",
            FDivFY="30",
            FNP="2000",
            FEPS="20",
            AvgSh="200",
            ShOutFY="220",
            TrShFY="20",
        )
    )

    assert row is not None
    assert row["forecast_eps"] == 10
    assert row["forecast_dividend_annual"] == 25


def test_matching_post_split_forecast_share_basis_is_not_adjusted_again():
    row = extract_per_share(
        _raw(
            Div2Q="15",
            FDivFY="20",
            FNP="4000",
            FEPS="20",
            AvgSh="200",
            ShOutFY="220",
            TrShFY="20",
        )
    )

    assert row is not None
    assert row["forecast_eps"] == 20
    assert row["forecast_dividend_annual"] == 35


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


def test_nxf_uses_a_distinct_next_fiscal_year_key():
    current = extract_per_share(
        _raw(CurFYEn="2027-03-31", CurPerType="FY", EPS="100", NxFEPS="120")
    )
    next_year = _extract_next_year_forecast(
        _raw(CurFYEn="2027-03-31", CurPerType="FY", EPS="100", NxFEPS="120"),
        current,
    )

    assert current is not None and next_year is not None
    assert (current["period"], next_year["period"]) == (
        "2027-03-31",
        "2028-03-31",
    )


def test_period_native_fy_result_promotes_over_old_nxf_placeholder():
    old_raw = _raw(
        CurFYEn="2027-03-31",
        CurPerType="FY",
        DiscDate="2028-06-30",
        EPS="90",
        NxFEPS="120",
        NxFDivAnn="24",
    )
    old_fy = extract_per_share(old_raw)
    old_nxf = _extract_next_year_forecast(old_raw, old_fy)
    current = extract_per_share(
        _raw(
            CurFYEn="2028-03-31",
            CurPerType="FY",
            DiscDate="2028-05-15",
            EPS="118",
            FEPS="",
            DivAnn="20",
            Div2Q="8",
            DivFY="12",
        )
    )
    best = {(old_nxf["ticker"], old_nxf["period"], "FY"): old_nxf}

    _merge_primary_record(best, current)
    promoted = best[("7480", "2028-03-31", "FY")]

    assert promoted["source"] == "jquants"
    assert promoted["disclosed_date"] == "2028-05-15"
    assert promoted["eps"] == 118
    assert promoted["forecast_eps"] is None
    assert promoted["forecast_dividend_annual"] == 20
    assert promoted["initial_forecast_eps"] == 120


def test_repeated_direct_disclosure_uses_newest_values():
    first = extract_per_share(
        _raw(CurPerType="FY", DiscDate="2027-05-10", EPS="100")
    )
    correction = extract_per_share(
        _raw(CurPerType="FY", DiscDate="2027-05-12", EPS="101")
    )
    best = {}

    _merge_primary_record(best, first)
    _merge_primary_record(best, correction)

    assert best[("7480", "2027-03-31", "FY")]["eps"] == 101
    assert best[("7480", "2027-03-31", "FY")]["disclosed_date"] == "2027-05-12"


def test_missing_fiscal_year_metadata_does_not_create_nxf():
    raw = _raw(CurFYEn="", CurPerType="FY", NxFEPS="120")

    assert extract_per_share(raw) is None
    assert _next_fiscal_year_end("") is None


def test_non_march_fiscal_year_end_is_advanced_without_month_shift():
    assert _next_fiscal_year_end("2027-12-31") == "2028-12-31"
    assert _next_fiscal_year_end("2024-02-29") == "2025-02-28"


def test_late_old_nxf_cannot_override_completed_fy():
    completed = extract_per_share(
        _raw(
            CurFYEn="2028-03-31",
            CurPerType="FY",
            DiscDate="2028-05-15",
            EPS="118",
        )
    )
    old_raw = _raw(
        CurFYEn="2027-03-31",
        CurPerType="FY",
        DiscDate="2028-06-30",
        EPS="90",
        NxFEPS="120",
    )
    old_nxf = _extract_next_year_forecast(old_raw, extract_per_share(old_raw))
    best = {("7480", "2028-03-31", "FY"): completed}

    _merge_next_year_record(best, old_nxf)
    result = best[("7480", "2028-03-31", "FY")]

    assert result["source"] == "jquants"
    assert result["disclosed_date"] == "2028-05-15"
    assert result["forecast_eps"] is None
    assert result["initial_forecast_eps"] == 120
