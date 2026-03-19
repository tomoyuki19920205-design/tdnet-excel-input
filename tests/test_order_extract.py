#!/usr/bin/env python3
"""受注メトリクス抽出テスト"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.extractor import (
    extract_order_metrics,
    _extract_total_from_table,
    _extract_numbers_from_line,
    ORDERS_KEYWORDS,
    BACKLOG_KEYWORDS,
    TOTAL_ROW_KEYWORDS,
)
from src.models import OrderMetric, ExtractedOrderMetrics
from src.migration.migration_db import MigrationDB


# ============================================================
# _extract_total_from_table テスト
# ============================================================

class TestExtractTotalFromTable:
    def test_total_row_found(self):
        """合計行があるケース → confidence=high"""
        lines = [
            "受注工事高の状況",
            "建築  100,000",
            "土木   50,000",
            "合計  150,000",
        ]
        val, conf, raw = _extract_total_from_table(lines, 0, "受注")
        assert val == 150_000
        assert conf == "high"

    def test_no_total_row(self):
        """合計行がないケース → None, low"""
        lines = [
            "受注工事高の状況",
            "建築  100,000",
            "土木   50,000",
        ]
        val, conf, raw = _extract_total_from_table(lines, 0, "受注")
        assert val is None
        assert conf == "low"

    def test_total_row_with_keyword(self):
        """「受注高合計」のような結合キーワード"""
        lines = [
            "セグメント別受注",
            "建築  100,000",
            "受注高合計  200,000",
        ]
        val, conf, raw = _extract_total_from_table(lines, 0, "受注高")
        assert val == 200_000
        assert conf == "high"


# ============================================================
# DB upsert テスト
# ============================================================

class TestOrderMetricsDB:
    def setup_method(self):
        from src.persist_policy import init_persist_policy, reset_persist_policy
        reset_persist_policy()
        init_persist_policy(cli_flag=True)

    def teardown_method(self):
        from src.persist_policy import reset_persist_policy
        reset_persist_policy()
    def test_upsert_insert(self, tmp_path):
        db = MigrationDB(str(tmp_path / "test.db"))
        result = db.upsert_order_metric(
            "1801", "2025-03-31", "3Q", "orders_total",
            value=15000, raw_value=15000, unit="百万円",
            confidence="high", raw_text="合計 15,000",
        )
        assert result == "inserted"
        rows = db.get_order_metrics("1801", "2025-03-31", "3Q")
        assert len(rows) == 1
        assert rows[0]["metric_name"] == "orders_total"
        assert rows[0]["value"] == 15000
        db.close()

    def test_upsert_no_change(self, tmp_path):
        db = MigrationDB(str(tmp_path / "test.db"))
        db.upsert_order_metric(
            "1801", "2025-03-31", "3Q", "orders_total",
            value=15000, raw_value=15000, unit="百万円",
        )
        result = db.upsert_order_metric(
            "1801", "2025-03-31", "3Q", "orders_total",
            value=15000, raw_value=15000, unit="百万円",
        )
        assert result == "no_change"
        db.close()

    def test_upsert_update(self, tmp_path):
        db = MigrationDB(str(tmp_path / "test.db"))
        db.upsert_order_metric(
            "1801", "2025-03-31", "3Q", "orders_total",
            value=15000, raw_value=15000,
        )
        result = db.upsert_order_metric(
            "1801", "2025-03-31", "3Q", "orders_total",
            value=16000, raw_value=16000,
        )
        assert result == "updated"
        rows = db.get_order_metrics("1801", "2025-03-31", "3Q")
        assert rows[0]["value"] == 16000
        db.close()

    def test_quarantine_record(self, tmp_path):
        db = MigrationDB(str(tmp_path / "test.db"))
        db.quarantine_record(
            "1801", "no_total_row",
            fiscal_year_end="2025-03-31", quarter="3Q",
            metric_type="orders_total",
            detail="受注高キーワードあるが合計行なし",
        )
        db.commit()
        # quarantine テーブルに記録されたか確認
        cur = db._conn.execute("SELECT * FROM quarantine")
        rows = cur.fetchall()
        assert len(rows) == 1
        db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
