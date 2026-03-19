"""tests/test_segment_header_mapping.py -- ヘッダー synonym マッチング統合テスト"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from src.analysis.header_analysis import normalize_header, score_header_role


class TestNormalizeHeader:
    """normalize_header の正規化テスト。"""

    def test_fullwidth_to_halfwidth(self):
        assert normalize_header("売　上　高") == "売上高"

    def test_newline_removal(self):
        assert normalize_header("売上高\n（百万円）") == "売上高(百万円)"

    def test_spaces_removed(self):
        assert normalize_header("Operating   profit") == "Operatingprofit"

    def test_segment_profit_spaces(self):
        assert normalize_header("セグメント 利益") == "セグメント利益"


class TestSalesKeywords:
    """売上系 keyword マッチング。"""

    def test_uriage(self):
        s = score_header_role("売上高")
        assert s["sales"] >= 0.9

    def test_uriage_shueki(self):
        s = score_header_role("売上収益")
        assert s["sales"] >= 0.9

    def test_jun_uriage(self):
        """純売上高 (新規追加)"""
        s = score_header_role("純売上高")
        assert s["sales"] >= 0.9

    def test_uriage_goukei(self):
        """売上合計 (新規追加)"""
        s = score_header_role("売上合計")
        assert s["sales"] >= 0.7

    def test_net_sales(self):
        s = score_header_role("Net sales")
        assert s["sales"] >= 0.8

    def test_revenue(self):
        s = score_header_role("Revenue")
        assert s["sales"] >= 0.7

    def test_eigyo_shueki(self):
        s = score_header_role("営業収益")
        assert s["sales"] >= 0.7


class TestProfitKeywords:
    """利益系 keyword マッチング。"""

    def test_eigyo_rieki(self):
        s = score_header_role("営業利益")
        assert s["operating_profit"] >= 0.9

    def test_core_eigyo_rieki(self):
        """コア営業利益 (新規追加)"""
        s = score_header_role("コア営業利益")
        assert s["operating_profit"] >= 0.8

    def test_segment_rieki(self):
        s = score_header_role("セグメント利益")
        assert s["segment_profit"] >= 0.9

    def test_rieki_standalone(self):
        """利益単独 (新規追加、低スコア)"""
        s = score_header_role("利益")
        assert s["segment_profit"] >= 0.3
        assert s["segment_profit"] <= 0.6  # 高すぎない

    def test_rieki_mataha_sonshitsu(self):
        s = score_header_role("利益又は損失")
        assert s["segment_profit"] >= 0.8

    def test_profit(self):
        s = score_header_role("Profit")
        assert s["segment_profit"] >= 0.4


class TestRatioConflict:
    """利益率は ratio であって profit ではない。"""

    def test_rieki_ritsu(self):
        s = score_header_role("利益率")
        assert s["ratio"] > s["segment_profit"]

    def test_eigyo_rieki_ritsu(self):
        s = score_header_role("営業利益率")
        assert s["ratio"] > s["operating_profit"]
