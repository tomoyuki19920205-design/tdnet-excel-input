#!/usr/bin/env python3
"""Phase B: テーブルスコアリングのテスト"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.table_scoring import (
    score_segment_table, find_table_regions, TableScore,
)


class TestScoreSegmentTable:
    def test_segment_table_high_score(self):
        """売上/利益/セグメント名あり表が高得点"""
        lines = [
            "報告セグメントの概要",
            "              売上高    セグメント利益",
            "建設事業     50,000       3,000",
            "開発事業     30,000       2,000",
            "環境事業     10,000       1,000",
            "調整額                      △500",
            "合計         90,000       5,500",
        ]
        ts = score_segment_table(lines)
        assert ts.score >= 0.4

    def test_ratio_table_low_score(self):
        """比率中心表が低得点"""
        lines = [
            "セグメント情報",
            "         構成比%    前年比%",
            "事業A    30.5%     101.2%",
            "事業B    25.3%      98.5%",
            "事業C    44.2%     103.8%",
        ]
        ts = score_segment_table(lines)
        # 比率中心は減点が入る
        assert ts.score < 0.5

    def test_empty_table(self):
        ts = score_segment_table([])
        assert ts.score == 0.0

    def test_nearby_context_boosts(self):
        """周辺テキストにセグメントKWがあると加点"""
        lines = [
            "              売上高    営業利益",
            "事業A        50,000     3,000",
        ]
        nearby = "報告セグメントごとの売上高及び利益又は損失"
        ts = score_segment_table(lines, nearby_text=nearby)
        assert ts.score > score_segment_table(lines).score


class TestFindTableRegions:
    def test_finds_region(self):
        lines = [
            "連結損益計算書",
            "",
            "報告セグメントの概要",
            "売上高  利益",
            "建設   50,000  3,000",
            "開発   30,000  2,000",
            "",
            "",
            "連結貸借対照表",
        ]
        regions = find_table_regions(lines)
        assert len(regions) >= 1
        start, end, nearby = regions[0]
        assert start == 2

    def test_no_region(self):
        lines = [
            "連結損益計算書",
            "売上高  1,000,000",
        ]
        regions = find_table_regions(lines)
        assert len(regions) == 0

    def test_skips_toc(self):
        """目次行 (ドットリーダー) をスキップ"""
        lines = [
            "セグメント情報………………5",
            "連結損益計算書………………3",
        ]
        regions = find_table_regions(lines)
        assert len(regions) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
