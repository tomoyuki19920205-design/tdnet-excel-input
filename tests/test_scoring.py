#!/usr/bin/env python3
"""スコアリングユーティリティのテスト"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.scoring import (
    SemanticScore,
    normalize_text,
    score_toc_line,
    is_toc_line,
    score_row_role,
    score_table_candidate,
)


class TestNormalizeText:
    def test_fullwidth_to_halfwidth(self):
        assert normalize_text("Ａ Ｂ Ｃ") == "ABC"

    def test_spaces_removed(self):
        assert normalize_text("売 上 高") == "売上高"

    def test_mixed(self):
        assert normalize_text("営業　利益") == "営業利益"


class TestScoreTocLine:
    def test_toc_with_dots(self):
        result = score_toc_line("（セグメント情報等）………8")
        assert result.role == "toc"
        assert result.score >= 0.3

    def test_toc_with_keyword(self):
        result = score_toc_line("目次")
        assert result.score >= 0.5

    def test_normal_line(self):
        result = score_toc_line("建設事業  50,000  3,000")
        assert result.score < 0.3

    def test_is_toc_line_helper(self):
        assert is_toc_line("（セグメント情報等）………8") is True
        assert is_toc_line("建設事業  50,000") is False


class TestScoreRowRole:
    def test_total_row(self):
        scores = score_row_role("合計")
        assert scores["total"] >= 0.8
        assert scores["skip"] >= 0.7

    def test_adjustment_row(self):
        scores = score_row_role("調整額")
        assert scores["skip"] >= 0.7
        assert scores["adjustment"] >= 0.5

    def test_segment_name(self):
        scores = score_row_role("建設事業")
        assert scores["segment_name"] >= 0.5
        assert scores["skip"] < 0.3

    def test_partial_skip(self):
        scores = score_row_role("消去又は全社")
        assert scores["skip"] >= 0.7

    def test_elimination_partial(self):
        scores = score_row_role("セグメント間消去")
        assert scores["skip"] >= 0.5


class TestScoreTableCandidate:
    def test_segment_table(self):
        lines = [
            "報告セグメントの概要",
            "売上高  セグメント利益",
            "建設事業  50,000  3,000",
            "開発事業  30,000  2,000",
        ]
        result = score_table_candidate(lines, 0, len(lines), "segment")
        assert result.score >= 0.5
        assert "セグメント" in result.reason

    def test_order_table(self):
        lines = [
            "受注高の状況",
            "建築  100,000",
            "土木   50,000",
            "合計  150,000",
        ]
        result = score_table_candidate(lines, 0, len(lines), "order")
        assert result.score >= 0.3
        assert "受注" in result.reason

    def test_low_score_unrelated(self):
        lines = [
            "連結損益計算書",
            "売上高  1,000,000",
        ]
        result = score_table_candidate(lines, 0, len(lines), "segment")
        assert result.score < 0.5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
