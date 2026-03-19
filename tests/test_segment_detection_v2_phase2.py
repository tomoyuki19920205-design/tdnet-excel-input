#!/usr/bin/env python3
"""v2 Phase 2 統合テスト"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.segment_detection_v2 import (
    SegmentRecordV2, V2DetectionResult,
    _extract_numbers_from_line, _normalize_unit_legacy,
    _apply_unit_multiplier, _compute_confidence,
)


class TestPhase2Fields:
    def test_segment_record_v2_has_phase2_fields(self):
        rec = SegmentRecordV2(
            segment_name="自動車",
            segment_name_raw="自動車事業",
            segment_name_normalized="自動車",
            unit_raw="百万円",
            unit_multiplier=1_000_000,
            currency="JPY",
            row_role="segment",
            is_reportable_segment=True,
            extraction_engine="v2",
            sales_col_role="sales",
            profit_col_role="operating_profit_like",
        )
        assert rec.segment_name_raw == "自動車事業"
        assert rec.segment_name_normalized == "自動車"
        assert rec.unit_multiplier == 1_000_000
        assert rec.is_reportable_segment
        assert rec.extraction_engine == "v2"

    def test_v2_result_has_unit_info(self):
        r = V2DetectionResult()
        assert r.unit_info is None
        assert r.candidate_tables_count == 0
        assert r.scored_pages_count == 0


class TestApplyUnitMultiplier:
    def test_hyakuman(self):
        assert _apply_unit_multiplier(50000, 1_000_000) == 50000

    def test_oku(self):
        result = _apply_unit_multiplier(10, 100_000_000)
        assert result == 1000.0  # 10 * 100

    def test_sen(self):
        result = _apply_unit_multiplier(50000, 1_000)
        assert result == pytest.approx(50.0)  # 50000千円 = 50百万円


class TestBackwardCompat:
    def test_legacy_normalize(self):
        assert _normalize_unit_legacy(10, "億円") == 1000
        assert _normalize_unit_legacy(50000, "百万円") == 50000


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
