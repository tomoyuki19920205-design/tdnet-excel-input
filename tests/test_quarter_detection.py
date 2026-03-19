#!/usr/bin/env python3
"""quarter 判定修正のテスト"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.extractor import _detect_quarter_from_context


class TestDetectQuarterFromContext:
    """_detect_quarter_from_context の修正テスト"""

    def test_first_quarter_member(self):
        ctx = "CurrentYearDuration_FirstQuarterMember_ConsolidatedMember"
        assert _detect_quarter_from_context(ctx) == "1Q"

    def test_second_quarter_member(self):
        ctx = "CurrentYearDuration_SecondQuarterMember_ConsolidatedMember"
        assert _detect_quarter_from_context(ctx) == "2Q"

    def test_third_quarter_member(self):
        ctx = "CurrentYearDuration_ThirdQuarterMember_ConsolidatedMember"
        assert _detect_quarter_from_context(ctx) == "3Q"

    def test_year_end_member(self):
        ctx = "CurrentYearDuration_YearEndMember_ConsolidatedMember"
        assert _detect_quarter_from_context(ctx) == "4Q"

    def test_annual_member(self):
        ctx = "CurrentYearDuration_AnnualMember_ConsolidatedMember"
        assert _detect_quarter_from_context(ctx) == "4Q"

    def test_current_year_duration_only_returns_empty(self):
        """CurrentYearDuration 単独は確定不可 → 空文字"""
        ctx = "CurrentYearDuration_ConsolidatedMember_ResultMember"
        assert _detect_quarter_from_context(ctx) == ""

    def test_empty_string(self):
        assert _detect_quarter_from_context("") == ""


class TestQuarterTitlePriority:
    """タイトル由来 quarter が iXBRL より優先されるテスト"""

    def test_title_1q_vs_xbrl_empty(self):
        """2026年10月期 第1四半期 + CurrentYearDuration → 1Q"""
        from src.year_parser import detect_quarter
        q = detect_quarter("2026年10月期 第1四半期決算短信")
        assert q == "1Q"

    def test_title_2q(self):
        from src.year_parser import detect_quarter
        q = detect_quarter("2025年3月期 第2四半期決算短信")
        assert q == "2Q"

    def test_title_3q(self):
        from src.year_parser import detect_quarter
        q = detect_quarter("2025年3月期 第3四半期決算短信")
        assert q == "3Q"

    def test_title_annual(self):
        from src.year_parser import detect_quarter
        q = detect_quarter("2025年3月期 通期決算短信")
        assert q == "4Q"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
