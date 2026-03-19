#!/usr/bin/env python3
"""ヘッダー解析モジュールのテスト"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.header_analysis import (
    normalize_header,
    merge_multiline_headers,
    score_header_role,
    infer_header_roles,
    detect_numeric_columns,
    detect_unit_annotations,
    HeaderRole,
    HeaderAnalysis,
)


# ============================================================
# normalize_header テスト
# ============================================================

class TestNormalizeHeader:
    def test_space_in_japanese(self):
        """'売 上 高' → '売上高'"""
        assert normalize_header("売 上 高") == "売上高"

    def test_with_unit_annotation(self):
        """'売上高(百万円)' → '売上高(百万円)'"""
        assert normalize_header("売上高(百万円)") == "売上高(百万円)"

    def test_newline_with_unit(self):
        r"""'売上高\n（百万円）' → '売上高(百万円)'"""
        assert normalize_header("売上高\n（百万円）") == "売上高(百万円)"

    def test_english_spaces(self):
        """'Operating   profit' → 'Operatingprofit'"""
        assert normalize_header("Operating   profit") == "Operatingprofit"

    def test_fullwidth(self):
        """全角英数 → 半角"""
        assert normalize_header("Ｒｅｖｅｎｕｅ") == "Revenue"

    def test_segment_profit_spaces(self):
        """'セグメント 利益' → 'セグメント利益'"""
        assert normalize_header("セグメント 利益") == "セグメント利益"


# ============================================================
# score_header_role テスト
# ============================================================

class TestScoreHeaderRole:
    def test_sales_header(self):
        """'売上高' → sales high score"""
        scores = score_header_role("売上高")
        assert scores["sales"] >= 0.9
        assert scores["operating_profit"] < 0.3

    def test_sales_with_unit(self):
        """'売上高(百万円)' → sales"""
        scores = score_header_role("売上高(百万円)")
        assert scores["sales"] >= 0.9

    def test_operating_profit(self):
        """'営業利益' → operating_profit"""
        scores = score_header_role("営業利益")
        assert scores["operating_profit"] >= 0.9

    def test_segment_profit(self):
        """'セグメント利益' → segment_profit"""
        scores = score_header_role("セグメント利益")
        assert scores["segment_profit"] >= 0.9

    def test_revenue_english(self):
        """'Revenue' → sales"""
        scores = score_header_role("Revenue")
        assert scores["sales"] >= 0.8

    def test_operating_profit_english(self):
        """'Operating profit' → operating_profit"""
        scores = score_header_role("Operating profit")
        assert scores["operating_profit"] >= 0.9

    def test_ratio(self):
        """'前年比' → ratio"""
        scores = score_header_role("前年比")
        assert scores["ratio"] >= 0.8
        # 利益率ではないので profit は低い
        assert scores["operating_profit"] <= 0.3

    def test_profit_ratio(self):
        """'利益率' → ratio (not profit)"""
        scores = score_header_role("利益率")
        assert scores["ratio"] >= 0.8
        assert scores["operating_profit"] <= 0.3
        assert scores["segment_profit"] <= 0.3

    def test_space_in_header(self):
        """'売 上 高' → sales (正規化後マッチ)"""
        scores = score_header_role("売 上 高")
        assert scores["sales"] >= 0.9

    def test_unknown(self):
        """不明なヘッダー → unknown"""
        scores = score_header_role("備考")
        assert scores["unknown"] >= 0.9

    def test_ordinary_profit(self):
        """'経常利益' → ordinary_profit"""
        scores = score_header_role("経常利益")
        assert scores["ordinary_profit"] >= 0.9

    def test_newline_header(self):
        r"""'売上高\n（百万円）' → sales"""
        scores = score_header_role("売上高\n（百万円）")
        assert scores["sales"] >= 0.9


# ============================================================
# merge_multiline_headers テスト
# ============================================================

class TestMergeMultilineHeaders:
    def test_single_line(self):
        result = merge_multiline_headers(["売上高  セグメント利益"])
        assert len(result) == 1
        assert "売上高" in result[0]

    def test_two_lines(self):
        result = merge_multiline_headers([
            "売上高    セグメント",
            "（百万円）  利益",
        ])
        assert len(result) == 1
        assert "売上高" in result[0]

    def test_empty(self):
        result = merge_multiline_headers([])
        assert result == []


# ============================================================
# infer_header_roles テスト
# ============================================================

class TestInferHeaderRoles:
    def test_basic_roles(self):
        analysis = infer_header_roles(["売上高", "営業利益"])
        assert analysis.has_sales is True
        assert analysis.has_profit is True
        assert analysis.roles[0] == HeaderRole.SALES
        assert analysis.roles[1] == HeaderRole.OPERATING_PROFIT

    def test_profit_label(self):
        analysis = infer_header_roles(["売上高", "セグメント利益"])
        assert analysis.profit_label == "セグメント利益"

    def test_unknown_cols(self):
        analysis = infer_header_roles(["備考", "その他"])
        assert analysis.has_sales is False
        assert analysis.has_profit is False


# ============================================================
# detect_numeric_columns テスト
# ============================================================

class TestDetectNumericColumns:
    def test_basic(self):
        rows = [
            ["建設", "50,000", "3,000"],
            ["開発", "30,000", "2,000"],
        ]
        result = detect_numeric_columns(rows)
        assert result == [False, True, True]

    def test_empty(self):
        assert detect_numeric_columns([]) == []


# ============================================================
# detect_unit_annotations テスト
# ============================================================

class TestDetectUnitAnnotations:
    def test_million_yen(self):
        assert detect_unit_annotations("（単位：百万円）") == "百万円"

    def test_billion_yen(self):
        assert detect_unit_annotations("(億円)") == "億円"

    def test_no_unit(self):
        assert detect_unit_annotations("売上高") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
