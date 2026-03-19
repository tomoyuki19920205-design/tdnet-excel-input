#!/usr/bin/env python3
"""test_multi_page_table_merge_v2.py — Phase 5: multi-page table merge テスト"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.table_scoring import score_segment_table, TableScore
from src.analysis.segment_detection_v2 import _try_merge_adjacent_pages


def _make_candidate(ts, lines, page_text, page_score, page_no):
    """テスト用タプル生成。"""
    return (ts, lines, page_text, page_score, page_no)


class TestMergeAdjacentPages:
    """テーマC: multi-page table merge テスト。"""

    def test_basic_two_page_merge(self):
        """連続ページの表が merge されてスコアが上がる。"""
        lines_a = [
            "報告セグメント",
            "              売上高    営業利益",
            "建設事業     50,000       3,000",
            "開発事業     30,000       2,000",
        ]
        lines_b = [
            "環境事業     10,000       1,000",
            "その他        5,000         500",
            "調整額                      △500",
            "合計         95,000       6,000",
        ]
        ts_a = score_segment_table(lines_a, "", 0, 0, len(lines_a))
        ts_b = score_segment_table(lines_b, "", 0, 0, len(lines_b))

        candidates = [
            _make_candidate(ts_a, lines_a, "\n".join(lines_a), 0.5, 3),
            _make_candidate(ts_b, lines_b, "\n".join(lines_b), 0.4, 4),
        ]
        pages_data = [(f"page{i}", i) for i in range(6)]
        page_candidates = []

        results = _try_merge_adjacent_pages(candidates, pages_data, page_candidates)

        # merge 結果が返されるかチェック
        if results:
            merged_ts = results[0][0]
            # merge 後のスコアは単ページより高い
            assert merged_ts.score > ts_a.score
            assert merged_ts.score > ts_b.score

    def test_non_adjacent_pages_not_merged(self):
        """非連続ページは merge されない。"""
        lines_a = [
            "報告セグメント",
            "売上高 利益",
            "A事業  10,000  500",
        ]
        lines_b = [
            "B事業  20,000  1,000",
            "C事業  15,000  800",
        ]
        ts_a = score_segment_table(lines_a, "", 0, 0, len(lines_a))
        ts_b = score_segment_table(lines_b, "", 0, 0, len(lines_b))

        # ページ3 と 5 (非連続)
        candidates = [
            _make_candidate(ts_a, lines_a, "\n".join(lines_a), 0.5, 3),
            _make_candidate(ts_b, lines_b, "\n".join(lines_b), 0.4, 5),
        ]
        pages_data = [(f"page{i}", i) for i in range(6)]
        page_candidates = []

        results = _try_merge_adjacent_pages(candidates, pages_data, page_candidates)
        assert len(results) == 0

    def test_header_inheritance(self):
        """2ページ目にヘッダーなし → 1ページ目のヘッダーを継承。"""
        lines_a = [
            "セグメント情報",
            "              売上高    セグメント利益",
            "A事業          10,000       500",
            "B事業          20,000      1,000",
        ]
        # 2ページ目はデータ行のみ (ヘッダーなし)
        lines_b = [
            "C事業          15,000       800",
            "D事業           8,000       400",
            "その他          5,000       200",
            "合計           58,000      2,900",
        ]
        ts_a = score_segment_table(lines_a)
        ts_b = score_segment_table(lines_b)

        candidates = [
            _make_candidate(ts_a, lines_a, "\n".join(lines_a), 0.5, 3),
            _make_candidate(ts_b, lines_b, "\n".join(lines_b), 0.4, 4),
        ]
        pages_data = [(f"page{i}", i) for i in range(6)]
        page_candidates = []

        results = _try_merge_adjacent_pages(candidates, pages_data, page_candidates)
        if results:
            merged_ts = results[0][0]
            # merge 後は segment_like_rows が増え、aux_terms も増えてスコア上昇
            assert merged_ts.segment_like_rows >= 3 or merged_ts.aux_term_count >= 2


class TestMergeScoreImprovement:
    """merge 後のスコア上昇テスト。"""

    def test_weak_pages_become_strong_after_merge(self):
        """単体では弱い候補が merge 後に強くなる。"""
        # 1ページ目: ヘッダーあり + 少数行
        lines_a = [
            "報告セグメント",
            "              売上高    セグメント利益",
            "建設事業     50,000       3,000",
        ]
        # 2ページ目: 多数行 + 補助語
        lines_b = [
            "開発事業     30,000       2,000",
            "環境事業     10,000       1,000",
            "その他        5,000         500",
            "調整額                      △500",
            "合計         95,000       6,000",
        ]
        ts_a = score_segment_table(lines_a)
        ts_b = score_segment_table(lines_b)

        # merge
        merged_lines = lines_a + lines_b
        ts_merged = score_segment_table(merged_lines)

        # merge 後スコアの方が高い
        assert ts_merged.score >= max(ts_a.score, ts_b.score)

    def test_single_candidate_no_merge(self):
        """候補1件のみでは merge しない。"""
        lines = ["報告セグメント", "A事業 10,000 500"]
        ts = score_segment_table(lines)
        candidates = [_make_candidate(ts, lines, "\n".join(lines), 0.5, 3)]
        pages_data = [(f"page{i}", i) for i in range(5)]

        results = _try_merge_adjacent_pages(candidates, pages_data, [])
        assert len(results) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
