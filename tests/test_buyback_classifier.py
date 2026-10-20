#!/usr/bin/env python3
"""test_buyback_classifier.py — 文書分類器のテスト"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from src.events.buyback_classifier import classify_buyback, classify_buyback_subtype
from src.events.buyback_models import (
    BUYBACK_DECISION, BUYBACK_STATUS, BUYBACK_RESULT, TREASURY_CANCEL,
)


# ============================================================
# decision 判定
# ============================================================
class TestBuybackDecision:
    def test_decision_title_standard(self):
        r = classify_buyback("自己株式取得に係る事項の決定に関するお知らせ")
        assert r.is_buyback_related is True
        assert r.event_type_candidate == BUYBACK_DECISION
        assert r.confidence >= 0.4

    def test_decision_title_variant(self):
        r = classify_buyback("自己株式の取得に関するお知らせ")
        assert r.is_buyback_related is True
        assert r.event_type_candidate == BUYBACK_DECISION

    def test_decision_title_frame(self):
        r = classify_buyback("自己株式取得枠の設定に関するお知らせ")
        assert r.is_buyback_related is True
        assert r.event_type_candidate == BUYBACK_DECISION


# ============================================================
# status 判定
# ============================================================
class TestBuybackStatus:
    def test_status_title(self):
        r = classify_buyback("自己株式の取得状況に関するお知らせ")
        assert r.is_buyback_related is True
        assert r.event_type_candidate == BUYBACK_STATUS

    def test_status_title_monthly(self):
        r = classify_buyback("自己株式取得状況に関するお知らせ（2025年4月度）")
        assert r.is_buyback_related is True
        assert r.event_type_candidate == BUYBACK_STATUS


# ============================================================
# result 判定
# ============================================================
class TestBuybackResult:
    def test_result_title(self):
        r = classify_buyback("自己株式の取得結果及び取得終了に関するお知らせ")
        assert r.is_buyback_related is True
        assert r.event_type_candidate == BUYBACK_RESULT

    def test_result_title_end(self):
        r = classify_buyback("自己株式の取得終了に関するお知らせ")
        assert r.is_buyback_related is True
        assert r.event_type_candidate == BUYBACK_RESULT


@pytest.mark.parametrize("ticker,title", [
    ("2735", "自己株式立会外買付取引（ToSTNeT-3）による自己株式の取得結果に関するお知らせ"),
    ("6136", "自己株式立会外買付取引（ToSTNeT－3）による自己株式の取得結果に関するお知らせ"),
])
def test_october_13_result_disclosures_are_not_new_decisions(ticker, title):
    # Production notification IDs: 2735=7ced180f-0ace-4018-a972-458c51c00dc9,
    # 6136=17bcf838-7a87-4200-9557-81b35f258937.
    body = "（ご参考）2026年10月9日開催の取締役会における自己株式の取得に関する決議内容"
    assert classify_buyback_subtype(title) == "ignore", ticker
    assert classify_buyback(title, body).event_type_candidate == BUYBACK_RESULT


@pytest.mark.parametrize("title", [
    "自己株式の取得状況に関するお知らせ（9月度）",
    "自己株式の取得に関する月次進捗のお知らせ",
    "自己株式の取得結果及び取得終了に関するお知らせ",
    "自己株式の取得完了に関するお知らせ",
    "自己株式立会外買付取引（ToSTNeT-3）による買付結果について",
])
def test_progress_reports_are_ignored_even_with_tostnet(title):
    assert classify_buyback_subtype(title) == "ignore"


@pytest.mark.parametrize("title,expected_subtype", [
    ("自己株式の取得に係る事項の決定に関するお知らせ", "new_program"),
    ("自己株式取得枠の拡大に関するお知らせ", "new_program"),
    ("自己株式立会外買付取引（ToSTNeT-3）による自己株式の取得に関するお知らせ", "tostnet"),
    ("自己株式の取得及び取得結果の公表に関するお知らせ", "new_program"),
    ("自己株式の取得に係る事項の決定及び取得結果に関するお知らせ", "new_program"),
])
def test_fresh_decisions_remain_eligible(title, expected_subtype):
    assert classify_buyback_subtype(title) == expected_subtype
    assert classify_buyback(title).event_type_candidate == BUYBACK_DECISION


@pytest.mark.parametrize("title", [
    "（開示事項の経過）自己株式取得に係る事項の決定に関するお知らせ",
    "自己株式取得に係る事項の決定について(取得期間の延長)",
    "コミットメント型自己株式取得（FCSR）における事後調整のお知らせ",
    "ファシリティ型自己株式取得における調整取引完了のお知らせ",
    "ユーロ円CB発行及び自己株式取得に関する補足説明資料",
    "自己株式立会外買付取引（ToSTNeT-3）への売付け及び投資有価証券売却益のお知らせ",
])
def test_related_non_decisions_do_not_create_new_buyback_alerts(title):
    assert classify_buyback_subtype(title) == "ignore"


def test_planned_self_tender_is_not_a_confirmed_new_program():
    title = "自己株式の取得及び自己株式の公開買付けの予定に関するお知らせ"
    assert classify_buyback_subtype(title) == "conditional_self_tender_plan"


def test_new_frame_in_mixed_result_disclosure_still_creates_decision_alert():
    title = "自己株式の取得状況及び取得終了並びに自己株式の取得枠設定に関するお知らせ"
    assert classify_buyback_subtype(title) == "new_program"


# ============================================================
# cancel 判定
# ============================================================
class TestTreasuryCancel:
    def test_cancel_title(self):
        r = classify_buyback("自己株式の消却に関するお知らせ")
        assert r.is_buyback_related is True
        assert r.event_type_candidate == TREASURY_CANCEL

    def test_cancel_title_with_stock(self):
        r = classify_buyback("自己株式消却に関するお知らせ")
        assert r.is_buyback_related is True
        assert r.event_type_candidate == TREASURY_CANCEL


# ============================================================
# 除外判定
# ============================================================
class TestExclusions:
    def test_exclude_stock_option(self):
        r = classify_buyback("ストックオプション（新株予約権）の付与に関するお知らせ")
        assert r.is_buyback_related is False

    def test_exclude_disposal(self):
        r = classify_buyback("自己株式処分に関するお知らせ")
        assert r.is_buyback_related is False

    def test_exclude_shinkabu(self):
        r = classify_buyback("新株予約権の発行に関するお知らせ")
        assert r.is_buyback_related is False

    def test_exclude_restricted(self):
        r = classify_buyback("譲渡制限付株式の付与に関するお知らせ")
        assert r.is_buyback_related is False

    def test_exclude_third_party(self):
        r = classify_buyback("第三者割当による新株式発行に関するお知らせ")
        assert r.is_buyback_related is False

    def test_exclude_mochikabkai(self):
        r = classify_buyback("持株会向け自己株式処分に関するお知らせ")
        assert r.is_buyback_related is False

    def test_disposal_with_acquisition_not_excluded(self):
        """処分と取得の両方がある場合は除外しない"""
        r = classify_buyback("自己株式処分及び自己株式の取得に関するお知らせ")
        assert r.is_buyback_related is True


# ============================================================
# body_head による補強
# ============================================================
class TestBodyHead:
    def test_body_reinforces_weak_title(self):
        r = classify_buyback(
            "お知らせ",
            body_head="当社は本日の取締役会において自己株式取得に係る事項を決議いたしました。取得株式数は300万株を上限とします。",
        )
        assert r.is_buyback_related is True
        assert r.event_type_candidate == BUYBACK_DECISION

    def test_empty_title_and_body(self):
        r = classify_buyback("", "")
        assert r.is_buyback_related is False


# ============================================================
# confidence
# ============================================================
class TestConfidence:
    def test_high_confidence_for_clear_title(self):
        r = classify_buyback(
            "自己株式取得に係る事項の決定に関するお知らせ",
            "取得株式数 取得価額の総額 取得期間 取得方法",
        )
        assert r.confidence >= 0.6

    def test_low_confidence_for_vague(self):
        r = classify_buyback("本日の決議に関するお知らせ")
        assert r.confidence < 0.3


# ============================================================
# decision 本文構造推定
# ============================================================
class TestDecisionStructure:
    def test_decision_from_structure(self):
        """株数 + 金額 + 期間が本文にある場合は decision を推定"""
        r = classify_buyback(
            "決算短信",
            body_head="取得する株式の総数 3,000,000株 取得価額の総額 50億円 取得期間 2025年4月1日から2025年9月30日まで",
        )
        assert r.is_buyback_related is True
        assert r.event_type_candidate == BUYBACK_DECISION

    def test_decision_shares_and_amount(self):
        """株数 + 金額だけでも decision を推定"""
        r = classify_buyback(
            "お知らせ",
            body_head="自己株式の取得 取得し得る株式の総数 1,000,000株 取得価額の総額 30億円",
        )
        assert r.is_buyback_related is True
        assert r.event_type_candidate == BUYBACK_DECISION


# ============================================================
# treasury_cancel 社債系抑制
# ============================================================
class TestCancelSuppression:
    def test_suppress_shasai(self):
        """社債の消却は treasury_cancel 判定しない"""
        r = classify_buyback(
            "特別損失の計上に関するお知らせ",
            body_head="新株予約権付社債の帳簿価額を消却する。自己株式は消却しない。",
        )
        # 社債が近傍にあるので treasury_cancel にならない
        assert r.event_type_candidate != TREASURY_CANCEL

    def test_suppress_tenkan(self):
        """転換社債の買入消却は treasury_cancel 誤検出しない"""
        r = classify_buyback(
            "転換社債型新株予約権付社債の買入消却に関するお知らせ",
        )
        assert r.is_buyback_related is False

    def test_real_cancel_not_suppressed(self):
        """本物の自己株式消却は抑制されない"""
        r = classify_buyback("自己株式の消却に関するお知らせ")
        assert r.is_buyback_related is True
        assert r.event_type_candidate == TREASURY_CANCEL

