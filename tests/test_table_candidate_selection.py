#!/usr/bin/env python3
"""test_table_candidate_selection.py — Phase 5: ページ内複数テーブル選別テスト"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.table_scoring import score_segment_table, find_table_regions


class TestIntraPageSelection:
    """同ページ内の複数テーブルから正しい表を選ぶテスト。"""

    def test_segment_table_over_pl_table(self):
        """セグメント表は会社全体PL表より高スコア。"""
        seg_lines = [
            "報告セグメント",
            "              売上高    セグメント利益",
            "建設事業     50,000       3,000",
            "開発事業     30,000       2,000",
            "環境事業     10,000       1,000",
            "その他        5,000         500",
            "調整額                      △500",
            "合計         95,000       6,000",
        ]
        pl_lines = [
            "連結損益計算書",
            "売上高       100,000",
            "売上原価      60,000",
            "売上総利益    40,000",
            "販管費        25,000",
            "営業利益      15,000",
        ]
        seg_score = score_segment_table(seg_lines)
        pl_score = score_segment_table(pl_lines)
        assert seg_score.score > pl_score.score

    def test_large_table_over_small_table(self):
        """行数が多い表は行数が少ない表より高スコア (同等の内容なら)。"""
        large_lines = [
            "セグメント情報",
            "売上高  利益",
            "A事業   10,000   500",
            "B事業   20,000  1,000",
            "C事業   15,000   800",
            "D事業   12,000   600",
            "E事業    8,000   400",
            "その他   5,000   200",
            "調整額            △100",
            "合計    70,000  3,400",
        ]
        small_lines = [
            "セグメント 売上高",
            "A  1,000",
            "B  2,000",
        ]
        large_score = score_segment_table(large_lines)
        small_score = score_segment_table(small_lines)
        assert large_score.score > small_score.score

    def test_multi_numeric_col_preferred(self):
        """数値列が多い表が数値列1列の表より優先される。"""
        multi_col_lines = [
            "事業名  売上高  営業利益",
            "A事業   10,000   500",
            "B事業   20,000  1,000",
            "C事業   15,000   800",
        ]
        single_col_lines = [
            "事業名  売上高",
            "A事業   10,000",
            "B事業   20,000",
            "C事業   15,000",
        ]
        multi_score = score_segment_table(multi_col_lines)
        single_score = score_segment_table(single_col_lines)
        assert multi_score.score > single_score.score

    def test_aux_terms_boost_selection(self):
        """その他/調整額/合計が含まれる表が優先される。"""
        with_aux = [
            "セグメント別売上高",
            "A事業  10,000  500",
            "B事業  20,000  1,000",
            "その他  5,000   200",
            "調整額          △100",
            "合計   35,000  1,600",
        ]
        without_aux = [
            "セグメント別売上高",
            "A事業  10,000  500",
            "B事業  20,000  1,000",
            "C事業  15,000  800",
        ]
        with_score = score_segment_table(with_aux)
        without_score = score_segment_table(without_aux)
        assert with_score.score > without_score.score


class TestFindTableRegionInPage:
    """ページ内テーブル領域検出テスト。"""

    def test_multiple_regions(self):
        """同ページ内に2つのセグメント領域がある場合。"""
        lines = [
            "報告セグメントの売上高",
            "A事業  10,000",
            "B事業  20,000",
            "",
            "",
            "事業セグメントの利益",
            "A事業  500",
            "B事業  1,000",
        ]
        regions = find_table_regions(lines)
        assert len(regions) >= 1

    def test_non_segment_region_skipped(self):
        """セグメントKWを含まない領域は検出されない。"""
        lines = [
            "連結損益計算書",
            "売上高  100,000",
            "営業利益  10,000",
        ]
        regions = find_table_regions(lines)
        assert len(regions) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
