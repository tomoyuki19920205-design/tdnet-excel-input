#!/usr/bin/env python3
"""row_analysis role 詳細化テスト"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.row_analysis import classify_rows, RowRole


class TestRowRolesPhase2:
    def test_segment_reportable(self):
        lines = ["ヘッダー", "自動車    50,000    3,000"]
        result = classify_rows(lines, header_band_height=1)
        seg = result.segment_rows[0]
        assert seg.role == RowRole.SEGMENT
        assert seg.is_reportable_segment is True

    def test_corporate_not_reportable(self):
        lines = ["ヘッダー", "全社    △200"]
        result = classify_rows(lines, header_band_height=1)
        corp = [r for r in result.rows if r.role == RowRole.CORPORATE]
        assert len(corp) == 1
        assert corp[0].is_reportable_segment is False

    def test_adjustment_not_reportable(self):
        lines = ["ヘッダー", "調整額    △500"]
        result = classify_rows(lines, header_band_height=1)
        adj = [r for r in result.rows if r.role == RowRole.ADJUSTMENT]
        assert len(adj) == 1
        assert adj[0].is_reportable_segment is False

    def test_elimination(self):
        """消去又は全社 → elimination"""
        lines = ["ヘッダー", "消去又は全社    △1,000"]
        result = classify_rows(lines, header_band_height=1)
        elim = [r for r in result.rows if r.role == RowRole.ELIMINATION]
        assert len(elim) == 1
        assert elim[0].is_reportable_segment is False

    def test_segment_kan_elimination(self):
        lines = ["ヘッダー", "セグメント間消去    △200"]
        result = classify_rows(lines, header_band_height=1)
        elim = [r for r in result.rows if r.role == RowRole.ELIMINATION]
        assert len(elim) == 1

    def test_total(self):
        lines = ["ヘッダー", "合計    80,000"]
        result = classify_rows(lines, header_band_height=1)
        assert result.total_rows[0].is_reportable_segment is False

    def test_subtotal(self):
        lines = ["ヘッダー", "小計    60,000"]
        result = classify_rows(lines, header_band_height=1)
        sub = [r for r in result.rows if r.role == RowRole.SUBTOTAL]
        assert len(sub) == 1
        assert sub[0].is_reportable_segment is False

    def test_sonota(self):
        """その他 → other / False"""
        lines = ["ヘッダー", "その他    5,000"]
        result = classify_rows(lines, header_band_height=1)
        other = [r for r in result.rows if r.role == RowRole.OTHER]
        assert len(other) == 1
        assert other[0].is_reportable_segment is False

    def test_note(self):
        lines = ["ヘッダー", "（注）セグメント利益は営業利益ベースの数値であります。"]
        result = classify_rows(lines, header_band_height=1)
        note = [r for r in result.rows if r.role == RowRole.NOTE]
        assert len(note) == 1
        assert note[0].is_reportable_segment is False

    def test_non_reportable_count(self):
        lines = [
            "ヘッダー",
            "自動車    50,000    3,000",
            "電子      30,000    2,000",
            "全社      △200",
            "調整額    △500",
            "合計      80,000    4,500",
        ]
        result = classify_rows(lines, header_band_height=1)
        assert result.extractable_count == 2
        assert result.non_reportable_count >= 3  # 全社 + 調整 + 合計


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
