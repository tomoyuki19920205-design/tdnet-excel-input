#!/usr/bin/env python3
"""Phase F-G: v2 統合テスト"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.segment_detection_v2 import (
    run_segment_detection_v2,
    V2DetectionResult,
    _extract_numbers_from_line,
    _normalize_unit,
    _compute_confidence,
)


class TestExtractNumbers:
    def test_basic(self):
        nums = _extract_numbers_from_line("建設事業  50,000  3,000")
        assert nums == [50000.0, 3000.0]

    def test_negative(self):
        nums = _extract_numbers_from_line("調整額  △500")
        assert nums == [-500.0]

    def test_no_numbers(self):
        nums = _extract_numbers_from_line("売上高  セグメント利益")
        assert nums == []


class TestNormalizeUnit:
    def test_oku(self):
        assert _normalize_unit(10.0, "億円") == 1000.0

    def test_sen(self):
        assert _normalize_unit(5000.0, "千円") == 5.0

    def test_million(self):
        assert _normalize_unit(50000.0, "百万円") == 50000.0

    def test_no_unit(self):
        assert _normalize_unit(100.0, "") == 100.0


class TestComputeConfidence:
    def test_high_confidence(self):
        conf = _compute_confidence(0.8, 0.7, 0.8, 0.6, 0.7)
        assert conf >= 0.5

    def test_low_confidence(self):
        conf = _compute_confidence(0.1, 0.1, 0.3, 0.1, 0.1)
        assert conf < 0.3


class TestV2DetectionResult:
    def test_empty_result(self):
        r = V2DetectionResult()
        assert not r.success
        assert r.segments == []

    def test_with_quarantine(self):
        r = V2DetectionResult(
            quarantine_reason="no_segment_page_candidate",
            failed_stage="page_scoring",
        )
        assert not r.success


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
