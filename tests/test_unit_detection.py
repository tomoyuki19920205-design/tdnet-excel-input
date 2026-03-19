#!/usr/bin/env python3
"""unit_detection.py のテスト"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.unit_detection import (
    detect_unit_from_text, detect_unit_for_table, merge_unit_candidates,
    UnitDetectionResult,
)


class TestDetectUnitFromText:
    def test_hyakuman_en(self):
        r = detect_unit_from_text("（単位：百万円）")
        assert r.unit_multiplier == 1_000_000
        assert r.currency == "JPY"

    def test_sen_en(self):
        r = detect_unit_from_text("単位：千円")
        assert r.unit_multiplier == 1_000
        assert r.currency == "JPY"

    def test_oku_en(self):
        r = detect_unit_from_text("（億円）")
        assert r.unit_multiplier == 100_000_000
        assert r.currency == "JPY"

    def test_millions_of_yen(self):
        r = detect_unit_from_text("Unit: millions of yen")
        assert r.unit_multiplier == 1_000_000
        assert r.currency == "JPY"

    def test_thousands_of_yen(self):
        r = detect_unit_from_text("Unit: thousands of yen")
        assert r.unit_multiplier == 1_000
        assert r.currency == "JPY"

    def test_sen_bei_doru(self):
        r = detect_unit_from_text("単位：千米ドル")
        assert r.unit_multiplier == 1_000
        assert r.currency == "USD"

    def test_hyakuman_bei_doru(self):
        r = detect_unit_from_text("百万米ドル")
        assert r.unit_multiplier == 1_000_000
        assert r.currency == "USD"

    def test_empty(self):
        r = detect_unit_from_text("")
        assert r.unit_multiplier is None

    def test_no_unit(self):
        r = detect_unit_from_text("セグメント情報")
        assert r.unit_multiplier is None


class TestDetectUnitForTable:
    def test_header_priority(self):
        """ヘッダーがページ上部より優先"""
        r = detect_unit_for_table(
            page_text="（単位：千円）\n売上高  利益",
            table_headers=["売上高（百万円）", "営業利益"],
        )
        assert r.unit_multiplier == 1_000_000  # header優先
        assert r.unit_source == "header"

    def test_page_fallback(self):
        """ヘッダーになければページから"""
        r = detect_unit_for_table(
            page_text="（単位：百万円）\n売上高  利益\n事業A 50,000",
            table_headers=["売上高", "営業利益"],
        )
        assert r.unit_multiplier == 1_000_000

    def test_unknown(self):
        r = detect_unit_for_table("", [], None)
        assert r.unit_multiplier is None


class TestMergeCandidates:
    def test_best_wins(self):
        candidates = [
            UnitDetectionResult(unit_raw="千円", unit_multiplier=1_000, currency="JPY", confidence=0.5),
            UnitDetectionResult(unit_raw="百万円", unit_multiplier=1_000_000, currency="JPY", confidence=0.9),
        ]
        r = merge_unit_candidates(candidates)
        assert r.unit_multiplier == 1_000_000

    def test_empty(self):
        r = merge_unit_candidates([])
        assert r.unit_multiplier is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
