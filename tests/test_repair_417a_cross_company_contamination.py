from tools.repair_417a_cross_company_contamination import PRESERVE_PERIODS, _period_key


def test_blue_zones_official_march_periods_are_preserved() -> None:
    for period, quarter in PRESERVE_PERIODS:
        assert _period_key({"period": period, "quarter": quarter}) in PRESERVE_PERIODS


def test_kaizen_december_periods_are_not_preserved_for_417a() -> None:
    assert _period_key({"period": "2025-12-31", "quarter": "FY"}) not in PRESERVE_PERIODS
    assert _period_key({"period": "2026-12-31", "quarter": "1Q"}) not in PRESERVE_PERIODS
