"""tests/test_tdnet_title_classification.py — TDnet タイトル分類テスト"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.backfill.listing_sources.tdnet_html import (
    _classify_disclosure,
    _normalize_title_simple,
    _matches_filter,
)


class TestClassifyDisclosureFinancialStatement:
    """決算短信 → financial_statement に分類されるケース。"""

    def test_q3_jpgaap_consolidated(self):
        title = "2026年3月期 第3四半期決算短信〔日本基準〕（連結）"
        assert _classify_disclosure(title) == "financial_statement"

    def test_fy_ifrs_consolidated(self):
        title = "2026年3月期 決算短信〔IFRS〕（連結）"
        assert _classify_disclosure(title) == "financial_statement"

    def test_q1(self):
        title = "2026年3月期 第1四半期決算短信〔日本基準〕（連結）"
        assert _classify_disclosure(title) == "financial_statement"

    def test_q2(self):
        title = "2025年12月期 第2四半期決算短信〔日本基準〕（連結）"
        assert _classify_disclosure(title) == "financial_statement"

    def test_bare_kessan_tanshin(self):
        title = "決算短信"
        assert _classify_disclosure(title) == "financial_statement"

    def test_tsuuki_kessan(self):
        title = "2025年3月期 通期決算〔日本基準〕（連結）"
        assert _classify_disclosure(title) == "financial_statement"

    def test_correction(self):
        title = "（訂正）「2025年3月期 決算短信〔日本基準〕（連結）」の訂正について"
        assert _classify_disclosure(title) == "financial_statement"

    def test_teisei_kessan_tanshin(self):
        title = "訂正決算短信の公表について"
        assert _classify_disclosure(title) == "financial_statement"

    def test_usgaap(self):
        title = "2026年3月期 第3四半期決算短信〔米国基準〕（連結）"
        assert _classify_disclosure(title) == "financial_statement"

    def test_standalone(self):
        title = "2025年3月期 決算短信〔日本基準〕（非連結）"
        assert _classify_disclosure(title) == "financial_statement"


class TestClassifyDisclosureForecastRevision:
    """業績予想修正 → forecast_revision に分類されるケース。"""

    def test_gyoseki_yoso_shusei(self):
        title = "業績予想の修正に関するお知らせ"
        assert _classify_disclosure(title) == "forecast_revision"

    def test_gyoseki_yoso_henkou(self):
        title = "通期連結業績予想の変更に関するお知らせ"
        assert _classify_disclosure(title) == "forecast_revision"

    def test_gyoseki_sai(self):
        title = "業績予想との差異に関するお知らせ"
        assert _classify_disclosure(title) == "forecast_revision"


class TestClassifyDisclosureNone:
    """決算短信でも業績修正でもない → None。"""

    def test_share_buyback(self):
        title = "自己株式取得に係る事項の決定に関するお知らせ"
        assert _classify_disclosure(title) is None

    def test_personnel(self):
        title = "代表取締役の異動に関するお知らせ"
        assert _classify_disclosure(title) is None

    def test_stock_split(self):
        title = "株式分割及び定款の一部変更に関するお知らせ"
        assert _classify_disclosure(title) is None

    def test_dividend_only(self):
        title = "配当予想の修正に関するお知らせ"
        # 配当のみ（業績を含まない）→ None
        assert _classify_disclosure(title) is None

    def test_subsidiary(self):
        title = "連結子会社の設立に関するお知らせ"
        assert _classify_disclosure(title) is None


class TestNormalizeTitleSimple:
    """タイトル正規化の動作確認。"""

    def test_whitespace_removal(self):
        assert "決算短信" in _normalize_title_simple("  決算 短信  ")

    def test_fullwidth_brackets(self):
        n = _normalize_title_simple("〔日本基準〕（連結）")
        assert "連結" in n

    def test_nfkc_normalization(self):
        # 全角数字 → 半角数字
        n = _normalize_title_simple("２０２６年")
        assert "2026" in n


class TestMatchesFilter:
    """_matches_filter の動作確認 (classify + exclude)。"""

    def test_financial_statement_passes(self):
        assert _matches_filter("2026年3月期 決算短信〔日本基準〕（連結）") is True

    def test_other_excluded(self):
        assert _matches_filter("自己株式取得に係る事項の決定に関するお知らせ") is False

    def test_exclude_keyword_blocks(self):
        # 決算短信を含むが除外キーワードも含む → False
        assert _matches_filter("決算短信に関する自己株式の取得について") is False
