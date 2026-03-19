#!/usr/bin/env python3
"""column_analysis taxonomy テスト"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.column_analysis import classify_columns, ColumnRole, _score_taxonomy


class TestScoreTaxonomy:
    def test_eigyo_rieki(self):
        scores = _score_taxonomy("営業利益")
        assert scores[ColumnRole.OPERATING_PROFIT_LIKE] > 0

    def test_segment_rieki(self):
        scores = _score_taxonomy("セグメント利益")
        assert scores[ColumnRole.SEGMENT_PROFIT_LIKE] > 0

    def test_keijo_rieki(self):
        scores = _score_taxonomy("経常利益")
        assert scores[ColumnRole.ORDINARY_PROFIT_LIKE] > 0

    def test_rieki_ritsu(self):
        """利益率は margin_like であって profit ではない"""
        scores = _score_taxonomy("営業利益率")
        assert scores[ColumnRole.MARGIN_LIKE] > 0
        assert scores[ColumnRole.OPERATING_PROFIT_LIKE] <= 0.1

    def test_segment_shisan(self):
        scores = _score_taxonomy("セグメント資産")
        assert scores[ColumnRole.ASSETS_LIKE] > 0

    def test_depreciation(self):
        scores = _score_taxonomy("減価償却費")
        assert scores[ColumnRole.DEPRECIATION_LIKE] > 0

    def test_capex(self):
        scores = _score_taxonomy("設備投資額")
        assert scores[ColumnRole.CAPEX_LIKE] > 0

    def test_revenue(self):
        scores = _score_taxonomy("Revenue")
        assert scores[ColumnRole.SALES] > 0

    def test_core_operating_profit(self):
        scores = _score_taxonomy("Core operating profit")
        assert scores[ColumnRole.OPERATING_PROFIT_LIKE] > 0

    def test_jigyo_rieki(self):
        """事業利益は operating_profit_like"""
        scores = _score_taxonomy("事業利益")
        assert scores[ColumnRole.OPERATING_PROFIT_LIKE] > 0


class TestClassifyColumnsTaxonomy:
    def test_margin_not_profit(self):
        """利益率列を利益列と誤判定しない"""
        headers = ["売上高", "営業利益率"]
        data = [["50,000", "6.0%"], ["30,000", "6.7%"]]
        result = classify_columns(data, headers)
        assert result.has_sales
        role = result.column_roles[1]
        assert role in (ColumnRole.MARGIN_LIKE, ColumnRole.RATIO)

    def test_adoptable_profit(self):
        """採用可能 profit が has_profit になる"""
        headers = ["売上高", "セグメント利益"]
        data = [["50,000", "3,000"]]
        result = classify_columns(data, headers)
        assert result.has_profit
        assert result.profit_role in ColumnRole.ADOPTABLE_PROFIT_ROLES


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
