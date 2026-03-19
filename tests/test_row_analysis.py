#!/usr/bin/env python3
"""Phase E: 行ロール分類のテスト"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.row_analysis import classify_rows, RowRole


class TestClassifyRows:
    def test_segment_rows(self):
        """セグメント名行が正しく検出される"""
        lines = [
            "              売上高    セグメント利益",  # header
            "建設事業     50,000       3,000",
            "開発事業     30,000       2,000",
            "環境事業     10,000       1,000",
        ]
        result = classify_rows(lines, header_band_height=1)
        segments = result.segment_rows
        assert len(segments) == 3
        assert segments[0].label == "建設事業"
        assert segments[1].label == "開発事業"
        assert segments[2].label == "環境事業"

    def test_total_row(self):
        """合計行が検出される"""
        lines = [
            "ヘッダー",
            "事業A    50,000    3,000",
            "合計     80,000    5,000",
        ]
        result = classify_rows(lines, header_band_height=1)
        assert len(result.total_rows) == 1
        assert result.total_rows[0].label == "合計"

    def test_adjustment_row(self):
        """調整額行が検出される"""
        lines = [
            "ヘッダー",
            "事業A    50,000    3,000",
            "調整額              △500",
        ]
        result = classify_rows(lines, header_band_height=1)
        skip = result.skip_rows
        adjustment = [r for r in skip if r.role == RowRole.ADJUSTMENT]
        assert len(adjustment) == 1

    def test_corporate_row(self):
        """全社行が検出される"""
        lines = [
            "ヘッダー",
            "事業A    50,000",
            "全社共通            △200",
        ]
        result = classify_rows(lines, header_band_height=1)
        skip = result.skip_rows
        corporate = [r for r in skip if r.role == RowRole.CORPORATE]
        assert len(corporate) == 1

    def test_note_row(self):
        """注記行が検出される"""
        lines = [
            "ヘッダー",
            "事業A    50,000",
            "（注）セグメント利益は、営業利益ベースの数値であります。",
        ]
        result = classify_rows(lines, header_band_height=1)
        note = [r for r in result.rows if r.role == RowRole.NOTE]
        assert len(note) == 1

    def test_blank_row(self):
        """空行"""
        lines = [
            "ヘッダー",
            "",
            "事業A    50,000",
        ]
        result = classify_rows(lines, header_band_height=1)
        blank = [r for r in result.rows if r.role == RowRole.BLANK]
        assert len(blank) == 1

    def test_header_band(self):
        """ヘッダーバンド内の行はHEADER"""
        lines = [
            "              売上高    セグメント利益",
            "              （百万円）",
            "建設事業     50,000       3,000",
        ]
        result = classify_rows(lines, header_band_height=2)
        headers = [r for r in result.rows if r.role == RowRole.HEADER]
        assert len(headers) == 2
        assert result.extractable_count == 1

    def test_消去又は全社(self):
        """消去又は全社のスキップ"""
        lines = [
            "ヘッダー",
            "事業A    50,000    3,000",
            "消去又は全社        △1,000",
        ]
        result = classify_rows(lines, header_band_height=1)
        skip = [r for r in result.skip_rows if r.role in (RowRole.CORPORATE, RowRole.ADJUSTMENT, RowRole.ELIMINATION)]
        assert len(skip) >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
