"""tests/test_partial_rescue.py -- partial rescue (sales only) テスト"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from src.analysis.segment_detection_v2 import SegmentRecordV2


class TestParseQualityField:
    """parse_quality フィールドの存在チェック。"""

    def test_default_is_full(self):
        rec = SegmentRecordV2()
        assert rec.parse_quality == "full"

    def test_partial_sales_only(self):
        rec = SegmentRecordV2(parse_quality="partial_sales_only")
        assert rec.parse_quality == "partial_sales_only"

    def test_sales_only_record(self):
        """sales のみ、profit=None のレコードが作れる。"""
        rec = SegmentRecordV2(
            segment_name="テスト事業",
            segment_sales=1000.0,
            segment_profit=None,
            parse_quality="partial_sales_only",
        )
        assert rec.segment_sales == 1000.0
        assert rec.segment_profit is None
        assert rec.parse_quality == "partial_sales_only"

    def test_full_record(self):
        """sales + profit 両方ある場合は full。"""
        rec = SegmentRecordV2(
            segment_name="テスト事業",
            segment_sales=1000.0,
            segment_profit=100.0,
            parse_quality="full",
        )
        assert rec.segment_sales == 1000.0
        assert rec.segment_profit == 100.0
        assert rec.parse_quality == "full"
