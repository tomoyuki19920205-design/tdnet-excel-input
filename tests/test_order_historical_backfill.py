#!/usr/bin/env python3
"""Phase 2: 受注系 historical backfill テスト"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.historical.schemas import ComparisonColumn, HistoricalRecord, ExtractResult, is_balance_metric, resolve_period_type
from src.historical.comparison_classifier import (
    detect_basis_from_label,
    detect_basis_from_header,
    detect_expression_type,
    is_comparison_column_header,
    is_current_column_header,
    is_change_column_header,
)
from src.historical.order_backfill import (
    extract_vertical_comparisons,
    extract_horizontal_comparisons,
    convert_comparisons_to_historical,
)
from src.historical.existing_check import filter_skip_existing
from src.migration.migration_db import MigrationDB
from src.persist_policy import init_persist_policy, reset_persist_policy


# ============================================================
# comparison_classifier テスト
# ============================================================

class TestBasisDetection:
    """basis 判定テスト"""

    def test_yoy_label(self):
        assert detect_basis_from_label("前年同期受注高") == "yoy"

    def test_yoy_end_label(self):
        assert detect_basis_from_label("前年同期末受注残高") == "yoy_end"

    def test_prev_period_end_label(self):
        assert detect_basis_from_label("前期末受注残高") == "prev_period_end"

    def test_current_label_returns_none(self):
        assert detect_basis_from_label("受注高") is None

    def test_yoy_header(self):
        assert detect_basis_from_header("前年同四半期累計期間") == "yoy"

    def test_current_header_returns_none(self):
        assert detect_basis_from_header("当第3四半期累計期間") is None


class TestExpressionTypeDetection:
    """expression_type 判定テスト"""

    def test_absolute_value(self):
        assert detect_expression_type("受注高", "18,000") == "absolute"

    def test_rate_with_percent(self):
        assert detect_expression_type("増減率", "105.2%") == "rate"

    def test_change_value(self):
        assert detect_expression_type("増減", "2,000") == "change_value"


# ============================================================
# 横持ち比較列テスト
# ============================================================

class TestHorizontalComparisons:
    """A. 横持ち比較列テスト"""

    def test_standard_horizontal_table(self):
        """当期 + 前年同期 + 増減率 → current ignored, historical 1件"""
        lines = [
            "受注高及び繰越高の状況",
            "   当第3四半期累計期間    前年同四半期累計期間    増減率",
            "   (百万円)              (百万円)              (%)",
            "受注高",
            "  建築   100,000   95,000   5.3",
            "  土木    50,000   48,000   4.2",
            "  合計   150,000  143,000   4.9",
        ]
        result = extract_horizontal_comparisons(
            lines,
            keyword_line_idx=3,  # "受注高" 行
            keyword="受注高",
            metric_name="orders_total",
            scale_str="百万円",
        )
        # 比較列から ComparisonColumn が生成される
        assert len(result) >= 1
        comp = result[0]
        assert comp.basis == "yoy"
        assert comp.expression_type == "absolute"
        assert comp.value == 143000

    def test_horizontal_rate_only(self):
        """前年列が％のみ → historical 0件"""
        lines = [
            "受注高の推移",
            "   当期    前年同期比",
            "受注高",
            "  合計  150,000  105.2%",
        ]
        result = extract_horizontal_comparisons(
            lines,
            keyword_line_idx=2,
            keyword="受注高",
            metric_name="orders_total",
            scale_str="百万円",
        )
        # 前年同期比 → basis=yoy だが expression_type=rate
        rate_comps = [c for c in result if c.expression_type == "rate"]
        abs_comps = [c for c in result if c.expression_type == "absolute"]
        # rate は historical に変換されない
        assert len(abs_comps) == 0

    def test_1928_style_single_space_header(self):
        """前期実績 当期実績 header (single-space separated) → historical"""
        lines = [
            "積水ハウス決算概要",
            "(単位：百万円)",
            "＜連結＞ 前期実績 当期実績 前期比(%) 次期予想 当期比(%)",
            "売上高 4,058,583 4,197,922 3.4 4,353,000 3.7",
            "営業利益 331,366 341,402 3.0 350,000 2.5",
            "受注高 4,052,604 4,247,762 4.8 4,493,037 5.8",
            "受注残高 1,754,577 1,804,417 2.8 1,944,454 7.8",
        ]
        # orders_total
        result_orders = extract_horizontal_comparisons(
            lines, keyword_line_idx=5, keyword="受注高",
            metric_name="orders_total", scale_str="百万円",
        )
        assert len(result_orders) >= 1
        assert result_orders[0].basis == "yoy"
        assert result_orders[0].expression_type == "absolute"
        assert result_orders[0].value == 4052604
        # backlog_total
        result_backlog = extract_horizontal_comparisons(
            lines, keyword_line_idx=6, keyword="受注残高",
            metric_name="backlog_total", scale_str="百万円",
        )
        assert len(result_backlog) >= 1
        assert result_backlog[0].value == 1754577


# ============================================================
# 縦持ち比較行テスト
# ============================================================

class TestVerticalComparisons:
    """B. 縦持ち比較行テスト"""

    def test_yoy_order(self):
        """前年同期受注高 → historical 生成"""
        lines = [
            "受注高    150,000",
            "前年同期受注高    143,000",
            "受注残高   200,000",
        ]
        result = extract_vertical_comparisons(lines, "百万円")
        yoy_comps = [c for c in result if c.basis == "yoy"]
        assert len(yoy_comps) == 1
        assert yoy_comps[0].metric_name == "orders_total"
        assert yoy_comps[0].value == 143000
        assert yoy_comps[0].expression_type == "absolute"

    def test_yoy_end_backlog(self):
        """前年同期末受注残高 → historical + point_in_time"""
        lines = [
            "受注残高    200,000",
            "前年同期末受注残高    185,000",
        ]
        result = extract_vertical_comparisons(lines, "百万円")
        assert len(result) == 1
        assert result[0].basis == "yoy_end"
        assert result[0].metric_name == "backlog_total"
        assert result[0].value == 185000

    def test_prev_period_end_backlog(self):
        """前期末受注残高 → historical + 4Q + point_in_time"""
        lines = [
            "受注残高    200,000",
            "前期末受注残高    190,000",
        ]
        result = extract_vertical_comparisons(lines, "百万円")
        assert len(result) == 1
        assert result[0].basis == "prev_period_end"
        assert result[0].metric_name == "backlog_total"
        assert result[0].value == 190000


# ============================================================
# ComparisonColumn → HistoricalRecord 変換テスト
# ============================================================

class TestConvertComparisons:
    """ComparisonColumn → HistoricalRecord 変換テスト"""

    def test_absolute_generates_record(self):
        """absolute → HistoricalRecord 生成"""
        comps = [
            ComparisonColumn(
                basis="yoy",
                expression_type="absolute",
                metric_name="orders_total",
                value=143000,
                raw_text="合計 143,000",
            ),
        ]
        records, stats = convert_comparisons_to_historical(
            comps,
            company_code="1801",
            current_fiscal_year_end="2025-03-31",
            current_quarter="3Q",
            current_period_type="cumulative",
        )
        assert len(records) == 1
        assert records[0].target_fiscal_year_end == "2024-03-31"
        assert records[0].target_quarter == "3Q"
        assert records[0].target_period_type == "cumulative"
        assert records[0].metric_name == "orders_total"
        assert records[0].value == 143000
        assert stats["extracted"] == 1

    def test_rate_skipped(self):
        """rate → historical 0件, skipped_ratio_only += 1"""
        comps = [
            ComparisonColumn(
                basis="yoy",
                expression_type="rate",
                metric_name="orders_total",
                value=105.2,
                raw_text="105.2%",
            ),
        ]
        records, stats = convert_comparisons_to_historical(
            comps,
            company_code="1801",
            current_fiscal_year_end="2025-03-31",
            current_quarter="3Q",
            current_period_type="cumulative",
        )
        assert len(records) == 0
        assert stats["skipped_ratio_only"] == 1

    def test_unknown_basis_skipped(self):
        """basis不明 → historical 0件"""
        comps = [
            ComparisonColumn(
                basis="",
                expression_type="absolute",
                metric_name="orders_total",
                value=143000,
            ),
        ]
        records, stats = convert_comparisons_to_historical(
            comps,
            company_code="1801",
            current_fiscal_year_end="2025-03-31",
            current_quarter="3Q",
            current_period_type="cumulative",
        )
        assert len(records) == 0
        assert stats["skipped_unknown_basis"] == 1

    def test_change_value_skipped(self):
        """増減額のみ → historical 0件"""
        comps = [
            ComparisonColumn(
                basis="yoy",
                expression_type="change_value",
                metric_name="orders_total",
                value=7000,
                raw_text="増減 7,000",
            ),
        ]
        records, stats = convert_comparisons_to_historical(
            comps,
            company_code="1801",
            current_fiscal_year_end="2025-03-31",
            current_quarter="3Q",
            current_period_type="cumulative",
        )
        assert len(records) == 0
        assert stats["skipped_ratio_only"] == 1

    def test_prev_period_end_maps_to_4q(self):
        """prev_period_end → 4Q + point_in_time"""
        comps = [
            ComparisonColumn(
                basis="prev_period_end",
                expression_type="absolute",
                metric_name="backlog_total",
                value=190000,
            ),
        ]
        records, stats = convert_comparisons_to_historical(
            comps,
            company_code="1801",
            current_fiscal_year_end="2025-03-31",
            current_quarter="1Q",
            current_period_type="cumulative",
        )
        assert len(records) == 1
        assert records[0].target_quarter == "4Q"
        assert records[0].target_period_type == "point_in_time"
        assert records[0].target_fiscal_year_end == "2024-03-31"

    def test_yoy_end_maps_to_point_in_time(self):
        """yoy_end → point_in_time"""
        comps = [
            ComparisonColumn(
                basis="yoy_end",
                expression_type="absolute",
                metric_name="backlog_total",
                value=185000,
            ),
        ]
        records, stats = convert_comparisons_to_historical(
            comps,
            company_code="1801",
            current_fiscal_year_end="2025-03-31",
            current_quarter="2Q",
            current_period_type="cumulative",
        )
        assert len(records) == 1
        assert records[0].target_quarter == "2Q"
        assert records[0].target_period_type == "point_in_time"

    def test_orders_stays_cumulative(self):
        """orders_total (フロー系) → cumulative を引き継ぐ"""
        comps = [
            ComparisonColumn(
                basis="yoy",
                expression_type="absolute",
                metric_name="orders_total",
                value=143000,
            ),
        ]
        records, _ = convert_comparisons_to_historical(
            comps,
            company_code="1801",
            current_fiscal_year_end="2025-03-31",
            current_quarter="3Q",
            current_period_type="cumulative",
        )
        assert records[0].target_period_type == "cumulative"

    def test_backlog_forced_point_in_time(self):
        """backlog_total (残高系) → point_in_time 固定"""
        comps = [
            ComparisonColumn(
                basis="yoy",
                expression_type="absolute",
                metric_name="backlog_total",
                value=185000,
            ),
        ]
        records, _ = convert_comparisons_to_historical(
            comps,
            company_code="1801",
            current_fiscal_year_end="2025-03-31",
            current_quarter="3Q",
            current_period_type="cumulative",
        )
        assert records[0].target_period_type == "point_in_time"

    def test_carryover_forced_point_in_time(self):
        """carryover_construction_total (残高系) → point_in_time 固定"""
        comps = [
            ComparisonColumn(
                basis="yoy",
                expression_type="absolute",
                metric_name="carryover_construction_total",
                value=50000,
            ),
        ]
        records, _ = convert_comparisons_to_historical(
            comps,
            company_code="1801",
            current_fiscal_year_end="2025-03-31",
            current_quarter="3Q",
            current_period_type="cumulative",
        )
        assert records[0].target_period_type == "point_in_time"


# ============================================================
# filter_skip_existing テスト
# ============================================================

class TestFilterSkipExisting:
    """DB既存値チェック統合テスト"""

    def setup_method(self):
        reset_persist_policy()
        init_persist_policy(cli_flag=True)

    def teardown_method(self):
        reset_persist_policy()

    def test_existing_skipped(self, tmp_path):
        """既存値あり → filter_skip_existing で除外"""
        db = MigrationDB(str(tmp_path / "test.db"))
        db.upsert_order_metric(
            "1801", "2024-03-31", "3Q", "orders_total",
            value=143000, raw_value=143000,
        )
        db.commit()

        records = [
            HistoricalRecord(
                company_code="1801",
                target_fiscal_year_end="2024-03-31",
                target_quarter="3Q",
                target_period_type="cumulative",
                metric_name="orders_total",
                value=143000,
                source_basis="yoy",
            ),
        ]
        writable, skipped = filter_skip_existing(records, db)
        assert skipped == 1
        assert len(writable) == 0
        db.close()

# ============================================================
# is_balance_metric / resolve_period_type テスト
# ============================================================

class TestBalanceMetric:
    """残高系メトリクス判定テスト"""

    def test_orders_is_not_balance(self):
        assert is_balance_metric("orders_total") is False

    def test_backlog_is_balance(self):
        assert is_balance_metric("backlog_total") is True

    def test_carryover_is_balance(self):
        assert is_balance_metric("carryover_construction_total") is True

    def test_resolve_orders_cumulative(self):
        assert resolve_period_type("orders_total", "cumulative") == "cumulative"

    def test_resolve_backlog_forces_point_in_time(self):
        assert resolve_period_type("backlog_total", "cumulative") == "point_in_time"

    def test_resolve_carryover_forces_point_in_time(self):
        assert resolve_period_type("carryover_construction_total", "quarterly") == "point_in_time"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
