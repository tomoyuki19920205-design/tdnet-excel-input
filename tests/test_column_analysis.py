#!/usr/bin/env python3
"""Phase D: 列ロール分類のテスト"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.column_analysis import classify_columns, ColumnRole


class TestClassifyColumns:
    def test_sales_and_profit(self):
        """売上列と利益列を正しく分類"""
        headers = ["売上高", "営業利益"]
        data = [
            ["50,000", "3,000"],
            ["30,000", "2,000"],
        ]
        result = classify_columns(data, headers)
        assert result.has_sales
        assert result.has_profit
        assert result.best_sales_col == 0
        assert result.best_profit_col == 1

    def test_label_plus_sales_profit(self):
        """ラベル列 + 売上列 + 利益列"""
        headers = ["セグメント", "売上高", "セグメント利益"]
        data = [
            ["建設事業", "50,000", "3,000"],
            ["開発事業", "30,000", "2,000"],
        ]
        result = classify_columns(data, headers)
        assert result.has_sales
        assert result.has_profit
        assert 0 in result.label_col_candidates
        assert result.best_sales_col == 1
        assert result.best_profit_col == 2

    def test_ratio_column_not_profit(self):
        """利益率列を利益列と誤判定しない"""
        headers = ["売上高", "営業利益率"]
        data = [
            ["50,000", "6.0%"],
            ["30,000", "6.7%"],
        ]
        result = classify_columns(data, headers)
        assert result.has_sales
        # 利益率は ratio であって profit ではない
        role = result.column_roles[1]
        assert role == ColumnRole.RATIO or not result.has_profit

    def test_yoy_column(self):
        """前年比列の認識"""
        headers = ["売上高", "前年比"]
        data = [
            ["50,000", "101.5%"],
            ["30,000", "98.2%"],
        ]
        result = classify_columns(data, headers)
        assert result.has_sales

    def test_empty_data(self):
        """空データ"""
        result = classify_columns([], [])
        assert not result.has_sales
        assert not result.has_profit

    def test_score_breakdown_exists(self):
        """role_score_breakdown が存在"""
        headers = ["売上高", "営業利益"]
        data = [["50,000", "3,000"]]
        result = classify_columns(data, headers)
        assert len(result.role_score_breakdown) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
