# ============================================================
# test_fetcher.py — フィルタリング・分類ロジックのテスト
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.fetcher import normalize_title, classify_disclosure, _matches_filter, is_instrument_excluded
from src.models import DisclosureType


class TestNormalizeTitle:
    """normalize_title のテスト"""

    def test_fullwidth_to_halfwidth(self):
        result = normalize_title("Ａ Ｂ Ｃ")
        assert result == "abc"

    def test_newline_removed(self):
        result = normalize_title("業績予想の\n修正に関する\nお知らせ")
        assert "\n" not in result

    def test_spaces_removed(self):
        result = normalize_title("業績 予想 の 修正")
        assert " " not in result

    def test_lowercase(self):
        result = normalize_title("Financial Results")
        assert result == "financialresults"


class TestClassifyDisclosure:
    """classify_disclosure のテスト"""

    def test_financial_statement_kessan_tanshin(self):
        title = "2026年3月期 第2四半期決算短信〔IFRS〕（連結）"
        assert classify_disclosure(title) == DisclosureType.FINANCIAL_STATEMENT

    def test_forecast_revision_tsuki(self):
        title = "通期連結業績予想の修正に関するお知らせ"
        assert classify_disclosure(title) == DisclosureType.FORECAST_REVISION

    def test_forecast_revision_2q(self):
        title = "2026年3月期第2四半期業績予想の修正に関するお知らせ"
        assert classify_disclosure(title) == DisclosureType.FORECAST_REVISION

    def test_forecast_revision_with_dividend(self):
        """表記ゆれ: 業績予想及び配当予想の修正"""
        title = "業績予想及び配当予想の修正に関するお知らせ"
        assert classify_disclosure(title) == DisclosureType.FORECAST_REVISION

    def test_forecast_revision_newline(self):
        """改行入りタイトル"""
        title = "業績予想の\n修正に関するお知らせ"
        assert classify_disclosure(title) == DisclosureType.FORECAST_REVISION

    def test_forecast_revision_sai(self):
        """差異のみ"""
        title = "業績予想と実績値との差異に関するお知らせ"
        assert classify_disclosure(title) == DisclosureType.FORECAST_REVISION

    def test_forecast_revision_henko(self):
        """変更表現"""
        title = "業績予想の変更に関するお知らせ"
        assert classify_disclosure(title) == DisclosureType.FORECAST_REVISION

    def test_forecast_revision_uwashu(self):
        """上方修正"""
        title = "通期業績予想の上方修正に関するお知らせ"
        assert classify_disclosure(title) == DisclosureType.FORECAST_REVISION

    def test_forecast_revision_kashu(self):
        """下方修正"""
        title = "通期業績予想の下方修正に関するお知らせ"
        assert classify_disclosure(title) == DisclosureType.FORECAST_REVISION

    def test_dividend_only_classified(self):
        """配当のみは DIVIDEND_REVISION として分類"""
        title = "配当予想の修正に関するお知らせ"
        assert classify_disclosure(title) == DisclosureType.DIVIDEND_REVISION

    def test_unrelated_excluded(self):
        """無関係な開示"""
        title = "自己株式取得に関するお知らせ"
        assert classify_disclosure(title) is None

    def test_ir_material(self):
        """IR説明資料（対象外）"""
        title = "IR説明資料のご案内"
        assert classify_disclosure(title) is None

    def test_teisei_kessan(self):
        """訂正決算短信"""
        title = "2026年3月期 訂正決算短信"
        assert classify_disclosure(title) == DisclosureType.FINANCIAL_STATEMENT

    def test_tsuki_kessan(self):
        """通期決算"""
        title = "2026年3月期 通期決算"
        assert classify_disclosure(title) == DisclosureType.FINANCIAL_STATEMENT

    def test_quarterly_kessan(self):
        """四半期決算"""
        title = "2026年3月期 第1四半期決算"
        assert classify_disclosure(title) == DisclosureType.FINANCIAL_STATEMENT

    def test_fullwidth_numbers(self):
        """全角数字"""
        title = "２０２６年３月期通期連結業績予想の修正に関するお知らせ"
        assert classify_disclosure(title) == DisclosureType.FORECAST_REVISION


class TestMatchesFilter:
    """_matches_filter のテスト"""

    def test_forecast_passes(self):
        assert _matches_filter("業績予想の修正に関するお知らせ") is True

    def test_financial_passes(self):
        assert _matches_filter("2026年3月期 第2四半期決算短信") is True

    def test_unrelated_blocked(self):
        assert _matches_filter("自己株式取得") is False

    def test_exclude_keyword_blocks(self):
        """除外キーワードが含まれていれば除外"""
        assert _matches_filter("業績予想の修正及び訴訟に関するお知らせ") is False


class TestInstrumentExcluded:
    """is_instrument_excluded のテスト"""

    def test_etf_in_title(self):
        assert is_instrument_excluded("1596", "iシェアーズ コア 日経225 ETF 決算短信", "ブラックロック") is True

    def test_etf_fullwidth(self):
        """全角ＥＴＦ → 正規化でetfになる"""
        assert is_instrument_excluded("1596", "ＥＴＦ決算短信", "ブラックロック") is True

    def test_investment_trust(self):
        assert is_instrument_excluded("9999", "追加型投資信託に関するお知らせ", "テスト") is True

    def test_fund_in_name(self):
        assert is_instrument_excluded("9999", "決算短信", "○○インデックスファンド") is True

    def test_normal_company_not_excluded(self):
        """通常の上場企業は除外されない"""
        assert is_instrument_excluded("7203", "2026年3月期 第3四半期決算短信", "トヨタ自動車") is False

    def test_ishares_in_name(self):
        assert is_instrument_excluded("1329", "決算短信", "iシェアーズ日経225") is True

    def test_etf_code_range_with_hint(self):
        """ETFコード帯 + 指数ヒント"""
        assert is_instrument_excluded("1570", "決算短信", "日経レバレッジ") is True

    def test_etf_code_range_without_hint(self):
        """ETFコード帯でもヒントなしなら除外しない"""
        assert is_instrument_excluded("1500", "決算短信", "普通の会社") is False

    def test_alpha_ticker_not_excluded(self):
        """英字付きコード（418A）は除外されない"""
        assert is_instrument_excluded("418A", "2025年3月期 第3四半期決算短信", "ウリドキ") is False

    def test_alpha_ticker_not_excluded_2(self):
        """英字付きコード（429A）は除外されない"""
        assert is_instrument_excluded("429A", "2025年3月期 第2四半期決算短信", "テクセンドフォトマスク") is False

