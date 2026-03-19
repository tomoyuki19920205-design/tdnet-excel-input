"""test_order_same_value.py — order backfill の同一値問題テスト

テストケース:
1. orders/backlog 別行別値 → 両方採用
2. orders/backlog 同一ヘッダー行 → 列位置で正しく分離
3. backlog/carryover 同値（建設業） → 許可
4. prev_period_end stock系のみ → 許可
5. yoy orders/backlog 正しく別値 → 許可
6. same-value guard: orders==backlog → skip
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.historical.order_backfill import (
    _detect_shared_header,
    _apply_same_value_guard,
    _extract_numbers,
)
from src.historical.schemas import HistoricalRecord


class TestDetectSharedHeader:
    """共有ヘッダー検出テスト"""

    def test_separate_lines(self):
        """別セクションにある場合 → 空dict"""
        metric_lines = {
            "orders_total": (100, "受注高"),
            "backlog_total": (120, "受注残高"),
        }
        result = _detect_shared_header(metric_lines)
        assert result == {}

    def test_same_line(self):
        """同一行にある場合 → 検出"""
        metric_lines = {
            "orders_total": (163, "受注高"),
            "backlog_total": (163, "受注残高"),
        }
        result = _detect_shared_header(metric_lines)
        assert "orders_total" in result
        assert "backlog_total" in result

    def test_only_orders(self):
        """片方しかない場合 → 空dict"""
        metric_lines = {
            "orders_total": (100, "受注高"),
        }
        result = _detect_shared_header(metric_lines)
        assert result == {}


class TestSameValueGuard:
    """Same-value guard テスト"""

    def _make_record(self, metric, value, company="TEST", fye="2025-03-31",
                     quarter="3Q", period_type="cumulative"):
        return HistoricalRecord(
            company_code=company,
            target_fiscal_year_end=fye,
            target_quarter=quarter,
            target_period_type=period_type,
            metric_name=metric,
            value=value,
            unit="百万円",
            source_basis="yoy",
            source_doc_id="test",
            source_expression_type="absolute",
            confidence="medium",
        )

    def test_orders_backlog_different_values(self):
        """orders/backlog 別値 → 両方採用"""
        records = [
            self._make_record("orders_total", 13860.0),
            self._make_record("backlog_total", 5228.0),
        ]
        filtered, skipped = _apply_same_value_guard(records)
        assert len(filtered) == 2
        assert skipped == 0

    def test_orders_backlog_same_value_skip(self):
        """orders/backlog 同値 → 両方skip (period_type が異なっても)"""
        records = [
            self._make_record("orders_total", 14153.372, period_type="cumulative"),
            self._make_record("backlog_total", 14153.372, period_type="point_in_time"),
        ]
        filtered, skipped = _apply_same_value_guard(records)
        assert len(filtered) == 0
        assert skipped == 2

    def test_backlog_carryover_same_value_allowed(self):
        """backlog/carryover 同値（建設業）→ 許可"""
        records = [
            self._make_record("backlog_total", 7062.0, period_type="point_in_time"),
            self._make_record("carryover_construction_total", 7062.0, period_type="point_in_time"),
        ]
        filtered, skipped = _apply_same_value_guard(records)
        assert len(filtered) == 2
        assert skipped == 0

    def test_orders_backlog_same_carryover_different(self):
        """orders==backlog → skip, carryover は別値で残る"""
        records = [
            self._make_record("orders_total", 50000.0),
            self._make_record("backlog_total", 50000.0),
            self._make_record("carryover_construction_total", 30000.0, period_type="point_in_time"),
        ]
        filtered, skipped = _apply_same_value_guard(records)
        assert len(filtered) == 1
        assert filtered[0].metric_name == "carryover_construction_total"
        assert skipped == 2

    def test_prev_period_end_stock_only(self):
        """prev_period_end で stock 系のみ → 許可 (orders がないので guard 発動しない)"""
        records = [
            self._make_record("backlog_total", 9114.0, period_type="point_in_time"),
        ]
        filtered, skipped = _apply_same_value_guard(records)
        assert len(filtered) == 1
        assert skipped == 0

    def test_yoy_orders_backlog_correct(self):
        """yoy の orders/backlog 正しく別値 → 許可 (0432相当)"""
        records = [
            self._make_record("orders_total", 13859.907),
            self._make_record("backlog_total", 5227.805, period_type="point_in_time"),
        ]
        filtered, skipped = _apply_same_value_guard(records)
        assert len(filtered) == 2
        assert skipped == 0


class TestExtractNumbers:
    """数値抽出テスト"""

    def test_total_row_with_numbers(self):
        """合計行の数値抽出"""
        text = "合計 15,529,548 5,460,502 15,863,514 5,051,671 333,966 △408,831"
        nums = _extract_numbers(text)
        assert len(nums) == 6
        assert nums[0] == 15529548
        assert nums[1] == 5460502
        assert nums[2] == 15863514
        assert nums[5] == -408831


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
