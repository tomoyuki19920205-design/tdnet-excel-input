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
