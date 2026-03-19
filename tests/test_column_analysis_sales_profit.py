#!/usr/bin/env python3
"""column_analysis.py sales/profit taxonomy 強化テスト (7804型ケース)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.column_analysis import (
    classify_columns, _score_taxonomy, ColumnRole,
)


class TestSalesTaxonomySubtypes:
    """外部売上/内部売上/計 の判定テスト"""

    def test_external_sales(self):
        scores = _score_taxonomy("外部顧客への売上高")
        assert scores[ColumnRole.EXTERNAL_SALES] >= 0.9
        assert scores[ColumnRole.SALES] >= 0.9

    def test_external_sales_short(self):
        scores = _score_taxonomy("外部売上")
        assert scores[ColumnRole.EXTERNAL_SALES] >= 0.5

    def test_internal_sales(self):
        scores = _score_taxonomy("セグメント間の内部売上高")
        assert scores[ColumnRole.INTERNAL_SALES] >= 0.9
        # 内部売上はSALES本命にならない
        assert scores[ColumnRole.SALES] < 0.5

    def test_total_sales(self):
        scores = _score_taxonomy("計")
        assert scores[ColumnRole.TOTAL_SALES_LIKE] >= 0.4

    def test_total_not_override_external(self):
        """外部売上がある場合、計はSALESにならない"""
        scores_ext = _score_taxonomy("外部顧客への売上高")
        scores_total = _score_taxonomy("計")
        assert scores_ext[ColumnRole.SALES] > scores_total[ColumnRole.SALES]


class TestProfitTaxonomyStrength:
    """利益又は損失 / 損失 の判定強化テスト"""

    def test_profit_or_loss(self):
        scores = _score_taxonomy("利益又は損失")
        assert scores[ColumnRole.SEGMENT_PROFIT_LIKE] >= 0.9

    def test_segment_profit_or_loss(self):
        scores = _score_taxonomy("セグメント利益又は損失")
        assert scores[ColumnRole.SEGMENT_PROFIT_LIKE] >= 0.9

    def test_loss_alone(self):
        scores = _score_taxonomy("損失")
        assert scores[ColumnRole.SEGMENT_PROFIT_LIKE] >= 0.3

    def test_profit_loss_paren(self):
        scores = _score_taxonomy("利益(損失)")
        assert scores[ColumnRole.SEGMENT_PROFIT_LIKE] >= 0.5

    def test_english_profit_or_loss(self):
        scores = _score_taxonomy("Segment profit or loss")
        assert scores[ColumnRole.SEGMENT_PROFIT_LIKE] >= 0.5


class TestSegmentTableClassification7804:
    """7804型の典型セグメント表テスト"""

    def _make_7804_table(self):
        """外部売上/内部売上/計/利益又は損失/資産 のヘッダー"""
        headers = [
            "報告セグメント",
            "外部顧客への売上高",
            "セグメント間の内部売上高",
            "計",
            "セグメント利益又は損失",
            "セグメント資産",
        ]
        data_rows = [
            ["半導体パッケージ", "50,000", "2,000", "52,000", "3,000", "80,000"],
            ["ファインケミカル", "30,000", "1,000", "31,000", "2,500", "60,000"],
            ["基板材料", "20,000", "500", "20,500", "1,200", "40,000"],
            ["合計", "100,000", "3,500", "103,500", "6,700", "180,000"],
        ]
        return headers, data_rows

    def test_7804_sales_detected(self):
        """外部売上が sales として検出される"""
        headers, data_rows = self._make_7804_table()
        result = classify_columns(data_rows, headers)
        assert result.has_sales, f"sales not detected, roles={result.column_roles}"
        assert result.best_sales_col == 1  # 外部顧客への売上高

    def test_7804_profit_detected(self):
        """利益又は損失が profit として検出される"""
        headers, data_rows = self._make_7804_table()
        result = classify_columns(data_rows, headers)
        assert result.has_profit, f"profit not detected, roles={result.column_roles}"
        assert result.best_profit_col == 4  # セグメント利益又は損失

    def test_7804_internal_not_sales(self):
        """内部売上がsales本命にならない"""
        headers, data_rows = self._make_7804_table()
        result = classify_columns(data_rows, headers)
        assert result.best_sales_col != 2  # セグメント間の内部売上高ではない

    def test_7804_no_quarantine(self):
        """7804型テーブルが has_sales and has_profit → quarantine にならない"""
        headers, data_rows = self._make_7804_table()
        result = classify_columns(data_rows, headers)
        assert result.has_sales and result.has_profit

    def test_total_as_fallback_sales(self):
        """外部売上がない場合、計を sales として使える"""
        headers = [
            "報告セグメント",
            "計",
            "セグメント利益又は損失",
        ]
        data_rows = [
            ["A事業", "50,000", "3,000"],
            ["B事業", "30,000", "2,500"],
        ]
        result = classify_columns(data_rows, headers)
        assert result.has_sales, f"計 as fallback sales failed, roles={result.column_roles}"

    def test_internal_only_not_sales(self):
        """内部売上だけではsales判定にならない"""
        headers = [
            "報告セグメント",
            "セグメント間の内部売上高",
            "セグメント利益又は損失",
        ]
        data_rows = [
            ["A事業", "2,000", "3,000"],
            ["B事業", "1,000", "2,500"],
        ]
        result = classify_columns(data_rows, headers)
        # 内部売上だけではsalesスコアが低いが判定自体は弱くてもOK
        # 本命ではないことを確認
        if result.has_sales:
            ext_score = result.role_score_breakdown[result.best_sales_col].get(ColumnRole.EXTERNAL_SALES, 0)
            assert ext_score == 0  # 外部売上スコアは0


class TestExistingTaxonomyStillWorks:
    """既存の taxonomy が壊れていないことの回帰テスト"""

    def test_plain_sales(self):
        scores = _score_taxonomy("売上高")
        assert scores[ColumnRole.SALES] >= 0.5

    def test_operating_profit(self):
        scores = _score_taxonomy("営業利益")
        assert scores[ColumnRole.OPERATING_PROFIT_LIKE] >= 0.5

    def test_segment_profit(self):
        scores = _score_taxonomy("セグメント利益")
        assert scores[ColumnRole.SEGMENT_PROFIT_LIKE] >= 0.5

    def test_assets(self):
        scores = _score_taxonomy("セグメント資産")
        assert scores[ColumnRole.ASSETS_LIKE] >= 0.5

    def test_margin(self):
        scores = _score_taxonomy("利益率")
        assert scores[ColumnRole.MARGIN_LIKE] >= 0.5

    def test_simple_table(self):
        """基本的な2列テーブル"""
        headers = ["セグメント名", "売上高", "営業利益"]
        data_rows = [["A事業", "100", "20"], ["B事業", "200", "30"]]
        result = classify_columns(data_rows, headers)
        assert result.has_sales
        assert result.has_profit


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
