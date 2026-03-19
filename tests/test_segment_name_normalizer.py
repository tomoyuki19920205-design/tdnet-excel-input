#!/usr/bin/env python3
"""segment_name_normalizer.py のテスト"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.segment_name_normalizer import (
    normalize_segment_name, normalize_segment_names,
    SegmentNameNormalizationResult,
)


class TestNormalizeSegmentName:
    def test_jidosha_jigyo(self):
        r = normalize_segment_name("自動車事業")
        assert r.normalized_name == "自動車"
        assert "suffix_remove" in (r.normalize_rule or "")

    def test_automotive_en(self):
        r = normalize_segment_name(" Automotive ")
        assert r.normalized_name == "自動車"

    def test_denshi_kanren(self):
        r = normalize_segment_name("電子関連")
        assert r.normalized_name == "電子"

    def test_jutaku_segment(self):
        r = normalize_segment_name("住宅セグメント")
        assert r.normalized_name == "住宅"

    def test_shakai_infra(self):
        r = normalize_segment_name("社会インフラ")
        assert r.normalized_name == "社会インフラ"

    def test_strip_note_marker(self):
        r = normalize_segment_name("自動車事業※1")
        assert r.normalized_name == "自動車"
        assert "note_remove" in (r.normalize_rule or "")

    def test_whitespace_newline(self):
        r = normalize_segment_name("建設\n事業")
        assert r.normalized_name == "建設"

    def test_empty_protection(self):
        """空にならない保証"""
        r = normalize_segment_name("事業")
        assert r.normalized_name == "事業"  # 接尾辞除去すると空になるので保持

    def test_unknown_stays(self):
        r = normalize_segment_name("特殊化学品")
        assert r.normalized_name == "特殊化学品"

    def test_raw_preserved(self):
        r = normalize_segment_name("自動車事業")
        assert r.raw_name == "自動車事業"


class TestNormalizeSegmentNames:
    def test_batch(self):
        results = normalize_segment_names(["自動車事業", "電子関連", "住宅"])
        assert len(results) == 3
        assert results[0].normalized_name == "自動車"
        assert results[1].normalized_name == "電子"
        assert results[2].normalized_name == "住宅"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
