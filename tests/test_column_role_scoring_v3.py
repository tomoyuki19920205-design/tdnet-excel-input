#!/usr/bin/env python3
"""test_column_role_scoring_v3.py — Phase 5: 列role推定テスト"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.column_analysis import classify_columns, ColumnRole
from src.analysis.header_analysis import score_header_role, normalize_header


class TestNewSynonyms:
    """Phase 5 synonym 追加のテスト"""

    def test_core_operating_income(self):
        scores = score_header_role("Core Operating Income")
        assert scores.get("segment_profit", 0) >= 0.8 or scores.get("operating_profit", 0) >= 0.8

    def test_adjusted_operating_income(self):
        scores = score_header_role("Adjusted Operating Income")
        assert scores.get("segment_profit", 0) >= 0.8 or scores.get("operating_profit", 0) >= 0.8

    def test_adjusted_profit(self):
        scores = score_header_role("Adjusted profit")
        assert scores.get("segment_profit", 0) >= 0.6

    def test_amount_low_score(self):
        """Amount は低スコア (単独で sales 採用しない)"""
        scores = score_header_role("Amount")
        assert scores.get("sales", 0) <= 0.3

    def test_kingaku_low_score(self):
        """金額 は低スコア"""
        scores = score_header_role("金額")
        assert scores.get("sales", 0) <= 0.3

    def test_gross_premiums_medium(self):
        """Grosspremiums は中スコア"""
        scores = score_header_role("Gross premiums")
        assert scores.get("sales", 0) >= 0.3
        assert scores.get("sales", 0) <= 0.7

    def test_sou_uriage(self):
        """総売上高 は高スコア"""
        scores = score_header_role("総売上高")
        assert scores.get("sales", 0) >= 0.8

    def test_jun_shuueki(self):
        """純収益 は中〜高スコア"""
        scores = score_header_role("純収益")
        assert scores.get("sales", 0) >= 0.5


class TestColumnPairEstimation:
    """sales/profit ペア推定テスト"""

    def test_sales_profit_basic(self):
        """基本: 売上高 + 営業利益"""
        headers = ["セグメント", "売上高", "営業利益"]
        data = [
            ["建設事業", "50,000", "3,000"],
            ["開発事業", "30,000", "2,000"],
            ["環境事業", "20,000", "1,500"],
        ]
        result = classify_columns(data, headers)
        assert result.has_sales
        assert result.has_profit

    def test_rieki_alone_with_sales(self):
        """「利益」単独 + sales が同表にある場合は昇格"""
        headers = ["区分", "売上高", "利益"]
        data = [
            ["事業A", "100,000", "5,000"],
            ["事業B", "80,000", "3,000"],
            ["事業C", "60,000", "2,000"],
        ]
        result = classify_columns(data, headers)
        assert result.has_sales
        # 「利益」は sales 存在 + segment行 >= 2 の条件で昇格
        assert result.has_profit

    def test_yoy_not_profit(self):
        """前年比列は profit に昇格しない"""
        headers = ["セグメント", "売上高", "前年比"]
        data = [
            ["事業A", "100,000", "5.2"],
            ["事業B", "80,000", "3.1"],
        ]
        result = classify_columns(data, headers)
        assert result.has_sales
        # yoy 列が profit に誤認されていない
        if result.has_profit:
            assert result.best_profit_col != 2

    def test_margin_not_profit(self):
        """利益率列は profit に昇格しない"""
        headers = ["セグメント", "売上高", "利益率"]
        data = [
            ["事業A", "100,000", "5.2%"],
            ["事業B", "80,000", "3.1%"],
        ]
        result = classify_columns(data, headers)
        assert result.has_sales
        # margin が profit に誤認されていない
        if result.has_profit:
            assert result.best_profit_col != 2


class TestFourColumnTable:
    """4列前後の表への対応テスト"""

    def test_four_column_layout(self):
        """[segment, sales, profit, yoy] パターン"""
        headers = ["セグメント", "売上高", "営業利益", "前年比"]
        data = [
            ["事業A", "100,000", "5,000", "3.2"],
            ["事業B", "80,000", "3,000", "2.1"],
        ]
        result = classify_columns(data, headers)
        assert result.has_sales
        assert result.has_profit
        assert result.best_sales_col == 1
        assert result.best_profit_col == 2

    def test_four_column_with_assets(self):
        """[segment, sales, assets, profit]"""
        headers = ["セグメント", "売上高", "資産", "セグメント利益"]
        data = [
            ["事業A", "100,000", "200,000", "5,000"],
            ["事業B", "80,000", "150,000", "3,000"],
        ]
        result = classify_columns(data, headers)
        assert result.has_sales
        assert result.has_profit


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
