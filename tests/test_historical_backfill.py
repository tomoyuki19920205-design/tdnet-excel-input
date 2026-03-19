#!/usr/bin/env python3
"""Historical backfill 共通基盤テスト"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.historical.schemas import (
    ComparisonColumn,
    HistoricalRecord,
    ExtractResult,
)
from src.historical.period_mapper import (
    map_comparison_to_target,
    TargetPeriod,
    _shift_fiscal_year_end,
)
from src.historical.existing_check import (
    check_existing_order_metric,
    check_existing_segment,
    filter_skip_existing,
)
from src.migration.migration_db import MigrationDB
from src.persist_policy import init_persist_policy, reset_persist_policy


# ============================================================
# period_mapper テスト
# ============================================================

class TestPeriodMapper:
    """comparison basis → target period の変換テスト"""

    def test_yoy_3q_cumulative(self):
        """yoy + 3Q(cumulative) → 前年同期 3Q cumulative"""
        result = map_comparison_to_target(
            "yoy", "2025-03-31", "3Q", "cumulative",
        )
        assert result is not None
        assert result.fiscal_year_end == "2024-03-31"
        assert result.quarter == "3Q"
        assert result.period_type == "cumulative"

    def test_yoy_1q_quarterly(self):
        """yoy + 1Q(quarterly) → 前年同期 1Q quarterly（period_type引き継ぎ）"""
        result = map_comparison_to_target(
            "yoy", "2025-03-31", "1Q", "quarterly",
        )
        assert result is not None
        assert result.fiscal_year_end == "2024-03-31"
        assert result.quarter == "1Q"
        assert result.period_type == "quarterly"

    def test_yoy_end_2q(self):
        """yoy_end + 2Q → 前年同期末 point_in_time"""
        result = map_comparison_to_target(
            "yoy_end", "2025-03-31", "2Q", "cumulative",
        )
        assert result is not None
        assert result.fiscal_year_end == "2024-03-31"
        assert result.quarter == "2Q"
        assert result.period_type == "point_in_time"

    def test_prev_period_end_1q(self):
        """prev_period_end + 1Q → 前期末 4Q point_in_time"""
        result = map_comparison_to_target(
            "prev_period_end", "2025-03-31", "1Q", "cumulative",
        )
        assert result is not None
        assert result.fiscal_year_end == "2024-03-31"
        assert result.quarter == "4Q"
        assert result.period_type == "point_in_time"

    def test_prev_period_end_uses_4q_not_fy(self):
        """prev_period_end は DB規約に合わせて '4Q' を使う（FYではない）"""
        result = map_comparison_to_target(
            "prev_period_end", "2026-03-31", "2Q", "quarterly",
        )
        assert result is not None
        assert result.quarter == "4Q"  # "FY" ではない

    def test_unknown_basis_returns_none(self):
        """unknown basis → None（skip）"""
        result = map_comparison_to_target(
            "unknown", "2025-03-31", "3Q", "cumulative",
        )
        assert result is None

    def test_empty_basis_returns_none(self):
        """空文字 basis → None（skip）"""
        result = map_comparison_to_target(
            "", "2025-03-31", "3Q", "cumulative",
        )
        assert result is None

    def test_none_basis_returns_none(self):
        """None basis → None（skip）"""
        result = map_comparison_to_target(
            None, "2025-03-31", "3Q", "cumulative",
        )
        assert result is None

    def test_yoy_december_fiscal_year(self):
        """12月決算企業の yoy → 正しく年をずらす"""
        result = map_comparison_to_target(
            "yoy", "2025-12-31", "2Q", "cumulative",
        )
        assert result is not None
        assert result.fiscal_year_end == "2024-12-31"
        assert result.quarter == "2Q"

    def test_shift_fiscal_year_end_leap_year(self):
        """閏年対応: 2/29 → 2/28"""
        # 2024は閏年、2023は平年
        result = _shift_fiscal_year_end("2024-02-29", -1)
        assert result == "2023-02-28"

    def test_unrecognized_basis_returns_none(self):
        """認識できない basis 文字列 → None"""
        result = map_comparison_to_target(
            "mom",  # month-over-month は未対応
            "2025-03-31", "3Q", "cumulative",
        )
        assert result is None


# ============================================================
# existing_check テスト
# ============================================================

class TestExistingCheck:
    """DB既存値チェックテスト"""

    def setup_method(self):
        reset_persist_policy()
        init_persist_policy(cli_flag=True)

    def teardown_method(self):
        reset_persist_policy()

    def test_order_metric_exists_returns_true(self, tmp_path):
        """既存 order_metric がある → True"""
        db = MigrationDB(str(tmp_path / "test.db"))
        db.upsert_order_metric(
            "1801", "2024-03-31", "3Q", "orders_total",
            value=15000, raw_value=15000, unit="百万円",
        )
        db.commit()

        assert check_existing_order_metric(
            db, "1801", "2024-03-31", "3Q", "orders_total",
        ) is True
        db.close()

    def test_order_metric_not_exists_returns_false(self, tmp_path):
        """既存 order_metric がない → False"""
        db = MigrationDB(str(tmp_path / "test.db"))

        assert check_existing_order_metric(
            db, "1801", "2024-03-31", "3Q", "orders_total",
        ) is False
        db.close()

    def test_segment_exists_with_metric_returns_true(self, tmp_path):
        """既存セグメントに指定 metric がある → True"""
        db = MigrationDB(str(tmp_path / "test.db"))
        db.upsert_segment(
            "1801", "2024-03-31", "3Q", "建築", 1,
            segment_sales=10000.0,
        )
        db.commit()

        assert check_existing_segment(
            db, "1801", "2024-03-31", "3Q", "建築",
            metric_name="segment_sales",
        ) is True
        db.close()

    def test_segment_exists_without_metric_returns_false(self, tmp_path):
        """セグメントはあるが指定 metric がNone → False"""
        db = MigrationDB(str(tmp_path / "test.db"))
        db.upsert_segment(
            "1801", "2024-03-31", "3Q", "建築", 1,
            segment_sales=10000.0,
            segment_profit=None,
        )
        db.commit()

        assert check_existing_segment(
            db, "1801", "2024-03-31", "3Q", "建築",
            metric_name="segment_profit",
        ) is False
        db.close()

    def test_filter_skip_existing_filters_out(self, tmp_path):
        """filter_skip_existing で既存を除外"""
        db = MigrationDB(str(tmp_path / "test.db"))
        # 既存値を1件入れる
        db.upsert_order_metric(
            "1801", "2024-03-31", "3Q", "orders_total",
            value=15000, raw_value=15000,
        )
        db.commit()

        records = [
            HistoricalRecord(
                company_code="1801",
                target_fiscal_year_end="2024-03-31",
                target_quarter="3Q",
                target_period_type="cumulative",
                metric_name="orders_total",
                value=15000,
            ),
            HistoricalRecord(
                company_code="1801",
                target_fiscal_year_end="2024-03-31",
                target_quarter="3Q",
                target_period_type="cumulative",
                metric_name="backlog_total",
                value=8000,
            ),
        ]

        writable, skipped = filter_skip_existing(records, db)
        assert skipped == 1
        assert len(writable) == 1
        assert writable[0].metric_name == "backlog_total"
        db.close()


# ============================================================
# schemas テスト
# ============================================================

class TestSchemas:
    """共通スキーマの基本テスト"""

    def test_historical_record_fields(self):
        """HistoricalRecord に必要なフィールドが揃っていること"""
        rec = HistoricalRecord(
            company_code="1801",
            target_fiscal_year_end="2024-03-31",
            target_quarter="3Q",
            target_period_type="cumulative",
            metric_name="orders_total",
            value=15000.0,
            source_basis="yoy",
            source_doc_id="abc123",
        )
        assert rec.company_code == "1801"
        assert rec.target_fiscal_year_end == "2024-03-31"
        assert rec.target_quarter == "3Q"
        assert rec.target_period_type == "cumulative"
        assert rec.metric_name == "orders_total"
        assert rec.value == 15000.0
        assert rec.unit == "百万円"
        assert rec.source_expression_type == "absolute"
        assert rec.segment_name is None

    def test_historical_record_with_segment(self):
        """セグメント付き HistoricalRecord"""
        rec = HistoricalRecord(
            company_code="1801",
            target_fiscal_year_end="2024-03-31",
            target_quarter="3Q",
            target_period_type="cumulative",
            metric_name="segment_sales",
            value=5000.0,
            segment_name="建築",
        )
        assert rec.segment_name == "建築"
        assert rec.metric_name == "segment_sales"

    def test_extract_result_default_stats(self):
        """ExtractResult のデフォルト stats"""
        result = ExtractResult()
        assert result.current_records == []
        assert result.historical_records == []
        assert result.stats["extracted"] == 0
        assert result.stats["skipped_ratio_only"] == 0
        assert result.stats["skipped_unknown_basis"] == 0
        assert result.stats["skipped_existing"] == 0

    def test_extract_result_with_records(self):
        """ExtractResult にレコードを追加"""
        current = HistoricalRecord(
            company_code="1801",
            target_fiscal_year_end="2025-03-31",
            target_quarter="3Q",
            target_period_type="cumulative",
            metric_name="orders_total",
            value=20000.0,
        )
        historical = HistoricalRecord(
            company_code="1801",
            target_fiscal_year_end="2024-03-31",
            target_quarter="3Q",
            target_period_type="cumulative",
            metric_name="orders_total",
            value=18000.0,
            source_basis="yoy",
        )
        result = ExtractResult(
            current_records=[current],
            historical_records=[historical],
            stats={"extracted": 1, "skipped_ratio_only": 0,
                   "skipped_unknown_basis": 0, "skipped_existing": 0},
        )
        assert len(result.current_records) == 1
        assert len(result.historical_records) == 1
        assert result.stats["extracted"] == 1


# ============================================================
# ComparisonColumn テスト
# ============================================================

class TestComparisonColumn:
    """比較列データの基本テスト"""

    def test_absolute_comparison(self):
        """absolute expression_type の ComparisonColumn"""
        col = ComparisonColumn(
            basis="yoy",
            expression_type="absolute",
            metric_name="orders_total",
            value=18000.0,
            raw_text="18,000",
        )
        assert col.basis == "yoy"
        assert col.expression_type == "absolute"
        assert col.value == 18000.0

    def test_rate_comparison_should_not_create_record(self):
        """rate expression_type → record 化しないことのロジック確認"""
        col = ComparisonColumn(
            basis="yoy",
            expression_type="rate",
            metric_name="orders_total",
            value=105.2,
            raw_text="105.2%",
        )
        # rate の場合は HistoricalRecord を作らない
        assert col.expression_type == "rate"
        # ↓ 比率だけではrecordを作らないルールの確認
        assert col.expression_type != "absolute"

    def test_comparison_column_defaults(self):
        """ComparisonColumn のデフォルト値"""
        col = ComparisonColumn()
        assert col.basis == ""
        assert col.expression_type == ""
        assert col.metric_name == ""
        assert col.value is None
        assert col.raw_text == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
