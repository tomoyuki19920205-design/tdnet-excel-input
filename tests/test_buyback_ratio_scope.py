"""The card numerator must describe the same purchase as the ratio."""
from src.events.buyback_classifier import classify_buyback
from src.events.buyback_extractor import extract_buyback_event


def test_disclosed_program_ratio_wins_over_calculation():
    text = (
        "自己株式取得に係る事項の決定\n"
        "取得する株式の総数 4,000,000株（上限）\n"
        "（発行済株式総数（自己株式を除く）に対する割合 8.5％）\n"
        "発行済株式総数（自己株式を除く） 47,105,105株\n"
    )
    event = extract_buyback_event(text, "buyback_decision", ticker="4220", disclosure_date="2026-07-31")
    assert event.shares_limit == 4_000_000
    assert event.ratio_to_outstanding == 8.5
    assert event.ratio_numerator_shares == 4_000_000
    assert event.ratio_scope == "program_limit"
    assert event.ratio_source == "disclosed"


def test_current_limit_ignores_previous_program_ratio_and_cumulative_count():
    title = "自己株式立会外買付取引（ＴｏＳＴＮｅＴ－３）による自己株式の買付けに関するお知らせ"
    text = (
        "自己株式の買付けについて\n"
        "取得する株式の総数 560,000株（上限）\n"
        "取得結果の公表は明日行います。\n"
        "（ご参考）\n"
        "取得する株式の総数 4,000,000株（8.5％）\n"
        "取得した株式の総数 2,110,400株\n"
    )
    kind = classify_buyback(title, text).event_type_candidate
    assert kind == "buyback_decision"
    event = extract_buyback_event(
        text, kind, ticker="4220", disclosure_date="2026-09-28", title=title,
        source_url="https://www.release.tdnet.info/inbs/140120260925540559.pdf",
    )
    assert event.shares_limit == 560_000
    assert event.shares_acquired is None
    assert event.shares_acquired_cumulative == 2_110_400
    assert event.ratio_numerator_shares == 560_000
    assert event.ratio_scope == "transaction_limit"
    assert event.ratio_source == "calculated"
    assert event.ratio_denominator_shares == 44_994_705
    assert event.ratio_denominator_base_as_of == "2026-06-30"
    assert event.ratio_denominator_as_of == "2026-09-28"
    assert round(event.ratio_to_outstanding, 2) == 1.24


def test_unknown_denominator_keeps_count_and_leaves_ratio_empty():
    event = extract_buyback_event(
        "取得する株式の総数 560,000株（上限）\n（ご参考）\n取得した株式の総数 2,110,400株",
        "buyback_decision", ticker="9999", disclosure_date="2026-09-28",
    )
    assert event.shares_limit == 560_000
    assert event.ratio_to_outstanding is None
    assert event.ratio_denominator_shares is None


def test_market_cap_amount_ratio_is_not_share_ratio():
    event = extract_buyback_event(
        "取得する株式の総数 100,000株（上限）\n取得金額の時価総額比 1.5％",
        "buyback_decision", ticker="9999", disclosure_date="2026-09-28",
    )
    assert event.shares_limit == 100_000
    assert event.ratio_to_outstanding is None


def test_status_uses_disclosed_period_ratio_not_reference_program_ratio():
    event = extract_buyback_event(
        "取得した株式の数 120,000株\n発行済株式総数（自己株式を除く）に対する割合 0.25％\n"
        "（ご参考）\n取得しうる株式の総数 4,000,000株（8.5％）",
        "buyback_status", ticker="4220", disclosure_date="2026-09-28",
    )
    assert event.shares_acquired == 120_000
    assert event.ratio_numerator_shares == 120_000
    assert event.ratio_scope == "period_acquired"
    assert event.ratio_to_outstanding == 0.25
    assert event.ratio_source == "disclosed"


def test_result_uses_this_acquisition_not_cumulative():
    title = "自己株式立会外買付取引（ＴｏＳＴＮｅＴ－３）による自己株式の取得結果に関するお知らせ"
    text = (
        "取得した株式の総数 403,400株\n"
        "（ご参考）\n取得しうる株式の総数 4,000,000株（8.5％）\n"
        "取得した株式の総数 2,110,400株"
    )
    event = extract_buyback_event(
        text, "buyback_result", ticker="4220", disclosure_date="2026-09-18",
        title=title, source_url="https://www.release.tdnet.info/inbs/140120260917538071.pdf",
    )
    assert event.shares_acquired == event.ratio_numerator_shares == 403_400
    assert event.shares_acquired_cumulative == 2_110_400
    assert event.ratio_scope == "transaction_acquired"
    assert event.ratio_denominator_shares == 45_398_105
    assert event.ratio_denominator_as_of == "2026-09-18"
    assert event.ratio_denominator_timing == "before_acquisition"
    assert event.ratio_denominator_adjustment_shares == 1_707_000
    assert event.ratio_denominator_adjustment_as_of == "2026-09-17"
    assert round(event.ratio_to_outstanding, 2) == 0.89


def test_first_result_uses_shares_before_that_acquisition():
    text = (
        "取得した株式の総数 1,301,900株\n"
        "（ご参考）\n取得しうる株式の総数 4,000,000株（8.5％）\n"
        "取得した株式の総数 1,707,000株"
    )
    event = extract_buyback_event(
        text, "buyback_result", ticker="4220", disclosure_date="2026-09-11",
        title="自己株式立会外買付取引（ＴｏＳＴＮｅＴ－３）による自己株式の取得結果に関するお知らせ",
        source_url="https://www.release.tdnet.info/inbs/140120260910534375.pdf",
    )
    assert event.ratio_numerator_shares == 1_301_900
    assert event.ratio_denominator_shares == 46_700_005
    assert event.ratio_denominator_adjustment_shares == 405_100
    assert event.ratio_denominator_adjustment_as_of == "2026-09-10"
    assert event.ratio_denominator_timing == "before_acquisition"
    assert round(event.ratio_to_outstanding, 2) == 2.79


def test_result_with_ambiguous_same_day_count_keeps_ratio_empty():
    event = extract_buyback_event(
        "取得した株式の総数 100,000株\n発行済株式総数（自己株式を除く） 9,000,000株",
        "buyback_result", ticker="9999", disclosure_date="2026-09-18",
    )
    assert event.shares_acquired == 100_000
    assert event.ratio_to_outstanding is None
