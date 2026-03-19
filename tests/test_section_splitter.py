#!/usr/bin/env python3
"""tests/test_section_splitter.py — 見出し分割テスト"""
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.filing_diff.text_extractor import (
    normalize_section_name,
    split_into_sections,
    clean_text,
)


class TestNormalizeSectionName:
    """見出し正規化"""

    def test_operating_results_basic(self):
        assert normalize_section_name("経営成績に関する説明") == "operating_results"

    def test_operating_results_with_number(self):
        assert normalize_section_name("1. 経営成績に関する説明") == "operating_results"

    def test_operating_results_quarterly(self):
        assert normalize_section_name("当四半期の経営成績の概況") == "operating_results"

    def test_operating_results_qualitative(self):
        assert normalize_section_name("当四半期決算に関する定性的情報") == "operating_results"

    def test_financial_position(self):
        assert normalize_section_name("財政状態に関する説明") == "financial_position"

    def test_cash_flow(self):
        assert normalize_section_name("キャッシュ・フローに関する説明") == "cash_flow"

    def test_cash_flow_variant(self):
        assert normalize_section_name("キャッシュ・フローの状況") == "cash_flow"

    def test_guidance_basic(self):
        assert normalize_section_name("業績予想に関する説明") == "guidance"

    def test_guidance_outlook(self):
        assert normalize_section_name("今後の見通し") == "guidance"

    def test_guidance_full_year(self):
        assert normalize_section_name("通期の見通し") == "guidance"

    def test_going_concern(self):
        assert normalize_section_name("継続企業の前提") == "going_concern"

    def test_segment(self):
        assert normalize_section_name("セグメントの概況") == "segment"

    def test_significant_events(self):
        assert normalize_section_name("重要事象等") == "significant_events"

    def test_unknown(self):
        assert normalize_section_name("あいうえお") == "other"

    def test_numbered_prefix_removed(self):
        """番号プレフィックスが除去されて正規化される"""
        assert normalize_section_name("(1) 経営成績に関する説明") == "operating_results"

    def test_circled_number_prefix(self):
        assert normalize_section_name("① 経営成績に関する説明") == "operating_results"


class TestSplitIntoSections:
    """見出し分割"""

    def test_basic_split(self):
        text = (
            "経営成績に関する説明\n"
            "当期は堅調に推移しました。売上高は増加しました。\n"
            "\n"
            "財政状態に関する説明\n"
            "総資産は前年度末に比べ増加しました。\n"
            "\n"
            "業績予想に関する説明\n"
            "通期の見通しは据え置きます。\n"
        )
        sections = split_into_sections(text)
        names = [s.section_name_normalized for s in sections]
        assert "operating_results" in names
        assert "financial_position" in names
        assert "guidance" in names

    def test_no_sections(self):
        sections = split_into_sections("普通のテキストです。特に見出しはありません。")
        assert len(sections) == 0

    def test_section_text_content(self):
        text = """経営成績に関する説明
当期は堅調に推移しました。
"""
        sections = split_into_sections(text)
        assert len(sections) == 1
        assert "堅調に推移" in sections[0].section_text

    def test_section_order(self):
        text = """
経営成績に関する説明
テスト1

財政状態に関する説明
テスト2

キャッシュ・フローに関する説明
テスト3
"""
        sections = split_into_sections(text)
        assert sections[0].section_order == 0
        assert sections[1].section_order == 1
        assert sections[2].section_order == 2


class TestCleanText:
    """テキストクリーニング"""

    def test_multiple_newlines(self):
        result = clean_text("a\n\n\n\n\nb")
        assert "\n\n\n" not in result

    def test_page_numbers(self):
        result = clean_text("テスト\n- 3 -\nテスト")
        assert "- 3 -" not in result

    def test_multiple_spaces(self):
        result = clean_text("a    b")
        assert "a b" in result
