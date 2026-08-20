import sqlite3

from src.fetcher import classify_disclosure
from src.jquants.classifier import classify_disclosure_jquants
from src.models import DisclosureType
from src.review_completion import (
    PROCEDURAL_REVIEW_COMPLETION,
    classify_procedural_review_completion,
    should_suppress_after_financial_comparison,
    should_suppress_earnings_notification,
)
from src.events.earnings_production_pipeline import _is_tanshin_title
from src.events.earnings_production_pipeline import _should_suppress_review_completion_with_history


def test_review_completion_title_variants_are_procedural():
    variants = [
        "2027年3月期 第1四半期決算短信〔日本基準〕（連結）\n（公認会計士等による期中レビューの完了）",
        "（公認会計士等による期中レビューの完了）のお知らせ",
        "( 公認会計士等による期中レビューの完了 )",
        "期中レビュー完了",
        "期中レビューの完了に関するお知らせ",
        "2027年3月期 第1四半期決算短信（監査法人による期中レビューの完了）",
    ]
    for title in variants:
        assert classify_procedural_review_completion(title) == PROCEDURAL_REVIEW_COMPLETION
        assert should_suppress_earnings_notification(title)


def test_tdnet_and_jquants_do_not_classify_procedural_revision_as_earnings():
    title = "2027年3月期 第1四半期決算短信〔日本基準〕（連結）（公認会計士等による期中レビューの完了）"
    assert classify_disclosure(title) == DisclosureType.REVIEW_COMPLETION
    assert classify_disclosure_jquants(["11304"], title) == DisclosureType.REVIEW_COMPLETION
    assert not _is_tanshin_title(title)


def test_plain_review_completion_notice_generates_no_earnings_candidate():
    title = "（公認会計士等による期中レビューの完了）のお知らせ"
    assert classify_disclosure(title) == DisclosureType.REVIEW_COMPLETION
    assert not _is_tanshin_title(title)


def test_normal_earnings_still_notifies():
    title = "2027年3月期 第1四半期決算短信〔日本基準〕（連結）"
    assert classify_disclosure(title) == DisclosureType.FINANCIAL_STATEMENT
    assert classify_disclosure_jquants(["11304"], title) == DisclosureType.FINANCIAL_STATEMENT
    assert _is_tanshin_title(title)


def test_review_completion_with_actual_correction_is_not_suppressed():
    title = "2027年3月期 第1四半期決算短信（訂正）（公認会計士等による期中レビューの完了）"
    assert classify_procedural_review_completion(title) is None
    assert classify_disclosure(title) == DisclosureType.FINANCIAL_STATEMENT
    assert _is_tanshin_title(title)


def test_review_completion_with_disclosure_change_is_conservatively_retained():
    title = "2026年12月期第1四半期決算短信（期中レビューの完了及び開示事項の変更）"
    assert classify_procedural_review_completion(title) is None
    assert _is_tanshin_title(title)


def test_ambiguous_disclosure_change_is_suppressed_only_when_values_match():
    title = "2026年12月期第1四半期決算短信（期中レビューの完了及び開示事項の変更）"
    previous = {"sales_value": 203, "op_value": -74, "guidance_eps": -60.45}
    same = {"sales_value": 203, "op_value": -74, "guidance_eps": -60.45}
    changed = {"sales_value": 204, "op_value": -74, "guidance_eps": -60.45}
    assert should_suppress_after_financial_comparison(title, previous, same)
    assert not should_suppress_after_financial_comparison(title, previous, changed)
    assert not should_suppress_after_financial_comparison(title, None, same)


def test_same_company_period_history_suppresses_ambiguous_notification():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE earnings_summaries (
        id INTEGER PRIMARY KEY, ticker TEXT, fiscal_year TEXT, quarter TEXT,
        disclosure_date TEXT, sales_value REAL, op_value REAL,
        guidance_sales REAL, guidance_op REAL, guidance_eps REAL)"""
    )
    conn.execute(
        "INSERT INTO earnings_summaries VALUES (1,'281A','2026','1Q','2026-05-14',203,-74,845,-395,-60.45)"
    )
    title = "2026年12月期第1四半期決算短信（期中レビュー完了及び開示事項の変更）"
    same = {
        "ticker": "281A", "fiscal_year": "2026", "quarter": "1Q",
        "sales_value": 203, "op_value": -74,
        "guidance_sales": 845, "guidance_op": -395, "guidance_eps": -60.45,
    }
    changed = {**same, "guidance_eps": -55.0}
    assert _should_suppress_review_completion_with_history(conn, title, same)
    assert not _should_suppress_review_completion_with_history(conn, title, changed)


def test_forecast_and_dividend_revisions_keep_their_routes_even_with_review_marker():
    forecast = "業績予想の修正及び公認会計士等による期中レビューの完了に関するお知らせ"
    dividend = "配当予想の修正及び期中レビュー完了に関するお知らせ"
    assert classify_disclosure(forecast) == DisclosureType.FORECAST_REVISION
    assert classify_disclosure(dividend) == DisclosureType.DIVIDEND_REVISION
    assert classify_disclosure_jquants(["11304", "11350"], forecast) == DisclosureType.FORECAST_REVISION
    assert classify_disclosure_jquants(["11304", "11360"], dividend) == DisclosureType.DIVIDEND_REVISION


def test_ordinary_corrected_statement_keeps_existing_earnings_behavior():
    title = "2027年3月期 第1四半期決算短信〔日本基準〕（連結）（訂正）"
    assert classify_disclosure(title) == DisclosureType.FINANCIAL_STATEMENT
    assert _is_tanshin_title(title)
