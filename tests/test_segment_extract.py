#!/usr/bin/env python3
"""セグメント抽出テスト"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.extractor import (
    extract_segment_financials,
    _find_segment_table_region,
    _is_segment_skip_label,
    _extract_numbers_from_line,
    SegmentExtracted,
)


class TestSegmentExtraction:
    def test_find_segment_table(self):
        lines = [
            "連結経営成績",
            "売上高 100,000",
            "",
            "報告セグメントの概要",
            "売上高  セグメント利益",
            "建設事業  50,000  3,000",
            "開発事業  30,000  2,000",
            "",
            "",
            "次のセクション",
        ]
        result = _find_segment_table_region(lines)
        assert result is not None
        start, end = result
        assert start == 3  # "報告セグメントの概要"

    def test_skip_labels(self):
        assert _is_segment_skip_label("合計") == True
        assert _is_segment_skip_label("調整額") == True
        assert _is_segment_skip_label("消去又は全社") == True
        assert _is_segment_skip_label("建設事業") == False
        assert _is_segment_skip_label("不動産") == False

    def test_no_segment_table(self):
        lines = ["売上高 100,000", "営業利益 10,000"]
        result = _find_segment_table_region(lines)
        assert result is None


class TestSegmentDB:
    def test_upsert_segment(self, tmp_path):
        from src.migration.migration_db import MigrationDB
        db = MigrationDB(str(tmp_path / "test.db"))
        result = db.upsert_segment(
            "1801", "2025-03-31", "3Q",
            segment_name="建設事業",
            segment_order=1,
            segment_sales=50000,
            segment_profit=3000,
        )
        assert result == "inserted"
        segs = db.get_segments("1801", "2025-03-31", "3Q")
        assert len(segs) == 1
        assert segs[0]["segment_name"] == "建設事業"
        assert segs[0]["segment_sales"] == 50000
        db.close()

    def test_upsert_no_change(self, tmp_path):
        from src.migration.migration_db import MigrationDB
        db = MigrationDB(str(tmp_path / "test.db"))
        db.upsert_segment("1801", "2025-03-31", "3Q", "建設", 1, 50000, 3000)
        result = db.upsert_segment("1801", "2025-03-31", "3Q", "建設", 1, 50000, 3000)
        assert result == "no_change"
        db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
