"""tests/test_filing_selector.py — filing_selector.py のユニットテスト"""
import pytest

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.backfill.filing_selector import (
    normalize_title,
    is_correction_title,
    is_excluded_material_title,
    is_earnings_summary_title,
    should_process_for_segment_backfill,
)


# ============================================================
# normalize_title
# ============================================================

class TestNormalizeTitle:
    def test_strip_whitespace(self):
        assert normalize_title("  決算短信  ") == "決算短信"

    def test_fullwidth_space(self):
        assert normalize_title("決算　短信") == "決算 短信"

    def test_newline_to_space(self):
        assert normalize_title("決算\n短信") == "決算 短信"

    def test_consecutive_spaces(self):
        assert normalize_title("決算   短信") == "決算 短信"

    def test_lowercase(self):
        assert normalize_title("Financial Results") == "financial results"

    def test_nfkc(self):
        # ① → 1, etc.
        result = normalize_title("第１四半期")
        assert "1" in result


# ============================================================
# is_correction_title
# ============================================================

class TestIsCorrectionTitle:
    def test_teisei(self):
        assert is_correction_title("2026年3月期 決算短信の一部訂正") is True

    def test_suuchi_teisei(self):
        assert is_correction_title("数値データ訂正に関するお知らせ") is True

    def test_ichibu_teisei(self):
        assert is_correction_title("一部訂正のお知らせ") is True

    def test_correction_en(self):
        assert is_correction_title("Correction of Financial Results") is True

    def test_amendment_en(self):
        assert is_correction_title("Amendment to Q3 Results") is True

    def test_normal_earnings(self):
        assert is_correction_title("2026年3月期 第3四半期決算短信") is False


# ============================================================
# is_excluded_material_title
# ============================================================

class TestIsExcludedMaterialTitle:
    def test_setsumei_shiryou(self):
        assert is_excluded_material_title("2026年3月期 決算説明資料") is True

    def test_hosoku_setsumei(self):
        assert is_excluded_material_title("2026年3月期 補足説明資料") is True

    def test_hosoku_shiryou(self):
        assert is_excluded_material_title("第3四半期 補足資料") is True

    def test_presentation(self):
        assert is_excluded_material_title("Q3 FY2026 Financial Results Presentation") is True

    def test_briefing(self):
        assert is_excluded_material_title("2026年3月期 第3四半期 Financial Results Briefing Materials") is True

    def test_factbook(self):
        assert is_excluded_material_title("FactBook 2026") is True

    def test_getsuji(self):
        assert is_excluded_material_title("月次売上推移") is True

    def test_sankou_shiryou(self):
        assert is_excluded_material_title("決算参考資料") is True

    def test_data_shu(self):
        assert is_excluded_material_title("データ集 2026") is True

    def test_gyouseki_yosou(self):
        assert is_excluded_material_title("業績予想修正に関するお知らせ") is True

    def test_supplementary(self):
        assert is_excluded_material_title("Supplementary Information") is True

    def test_normal_earnings(self):
        assert is_excluded_material_title("2026年3月期 第3四半期決算短信〔日本基準〕（連結）") is False


# ============================================================
# is_earnings_summary_title
# ============================================================

class TestIsEarningsSummaryTitle:
    def test_fy_nippon(self):
        assert is_earnings_summary_title("2026年3月期 決算短信〔日本基準〕（連結）") is True

    def test_fy_ifrs(self):
        assert is_earnings_summary_title("2026年3月期 決算短信〔IFRS〕（連結）") is True

    def test_q3(self):
        assert is_earnings_summary_title("2026年3月期 第3四半期決算短信〔日本基準〕（連結）") is True

    def test_q1(self):
        assert is_earnings_summary_title("2026年3月期 第1四半期決算短信") is True

    def test_q2(self):
        assert is_earnings_summary_title("2026年3月期 第2四半期決算短信〔日本基準〕") is True

    def test_honesty_non_tanshin(self):
        assert is_earnings_summary_title("2026年3月期 決算説明資料") is False

    def test_monthly(self):
        assert is_earnings_summary_title("月次売上推移") is False


# ============================================================
# should_process_for_segment_backfill
# ============================================================

class TestShouldProcess:
    def test_standard_earnings_accepted(self):
        ok, reason = should_process_for_segment_backfill(
            "2026年3月期 第3四半期決算短信〔日本基準〕（連結）"
        )
        assert ok is True
        assert reason == "included_earnings_summary"

    def test_fy_earnings_accepted(self):
        ok, reason = should_process_for_segment_backfill(
            "2026年3月期 決算短信〔IFRS〕（連結）"
        )
        assert ok is True
        assert reason == "included_earnings_summary"

    def test_correction_excluded(self):
        ok, reason = should_process_for_segment_backfill(
            "2026年3月期 決算短信の一部訂正"
        )
        assert ok is False
        assert reason == "excluded_correction"

    def test_correction_with_flag_off(self):
        """exclude_corrections=False にすれば訂正も通る."""
        ok, reason = should_process_for_segment_backfill(
            "2026年3月期 決算短信の一部訂正",
            exclude_corrections=False,
        )
        # 「決算短信」キーワードを含むので通る
        assert ok is True
        assert reason == "included_earnings_summary"

    def test_presentation_excluded(self):
        ok, reason = should_process_for_segment_backfill(
            "2026年3月期 決算説明資料"
        )
        assert ok is False
        assert reason == "excluded_presentation"

    def test_supplementary_excluded(self):
        ok, reason = should_process_for_segment_backfill(
            "2026年3月期 補足資料"
        )
        assert ok is False
        assert reason == "excluded_presentation"

    def test_non_financial_excluded(self):
        ok, reason = should_process_for_segment_backfill(
            "代表取締役の異動に関するお知らせ"
        )
        assert ok is False
        assert reason == "excluded_non_earnings_summary"

    def test_ambiguous_excluded(self):
        """不明なものは除外 (迷ったら除外)."""
        ok, reason = should_process_for_segment_backfill(
            "通気取決算概況"
        )
        assert ok is False
        assert reason == "excluded_non_earnings_summary"

    def test_only_earnings_summary_off(self):
        """only_earnings_summary=False → 短信以外も通る."""
        ok, reason = should_process_for_segment_backfill(
            "配当予想の修正のお知らせ",
            only_earnings_summary=False,
        )
        assert ok is True
        assert reason == "included_financial_statement"

    # --- 判定順の確認 ---

    def test_correction_before_presentation(self):
        """訂正 + 説明資料 → 訂正除外が優先."""
        ok, reason = should_process_for_segment_backfill(
            "決算説明資料の訂正"
        )
        assert ok is False
        assert reason == "excluded_correction"

    def test_presentation_before_earnings(self):
        """説明資料に決算短信を含む場合 → 説明資料除外が優先."""
        ok, reason = should_process_for_segment_backfill(
            "決算短信に関する補足資料"
        )
        assert ok is False
        assert reason == "excluded_presentation"
