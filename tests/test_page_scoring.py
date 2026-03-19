#!/usr/bin/env python3
"""Phase A: ページスコアリングのテスト"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.page_scoring import score_segment_page, rank_candidate_pages, PageScore


class TestScoreSegmentPage:
    def test_segment_page_high_score(self):
        """セグメント情報ありページが高得点"""
        text = """
        報告セグメントごとの売上高及び利益又は損失の金額に関する情報
        セグメント情報
        売上高      営業利益
        建設事業    50,000    3,000
        開発事業    30,000    2,000
        調整額                 △500
        合計        80,000    4,500
        """
        ps = score_segment_page(text, 3)
        assert ps.score >= 0.3
        assert ps.page_no == 3

    def test_toc_page_low_score(self):
        """目次ページが低得点"""
        text = """
        目次
        連結貸借対照表………………1
        連結損益計算書………………3
        セグメント情報…………………5
        連結キャッシュ・フロー………8
        """
        ps = score_segment_page(text, 0)
        assert ps.score < 0.3

    def test_empty_page(self):
        """空ページ"""
        ps = score_segment_page("", 0)
        assert ps.score == 0.0

    def test_general_financial_page(self):
        """一般的な財務ページ (セグメントKWなし)"""
        text = """
        連結損益計算書
        売上高  1,000,000
        売上原価  700,000
        販売費及び一般管理費  200,000
        営業利益  100,000
        """
        ps = score_segment_page(text, 1)
        # 財務KWはあるがセグメントKWがない → 中程度
        assert ps.score < 0.3

    def test_score_breakdown_exists(self):
        """score_breakdown が詳細を含む"""
        text = "報告セグメント 売上高 営業利益"
        ps = score_segment_page(text, 0)
        assert len(ps.score_breakdown) > 0
        assert ps.reason  # reason が空でないこと


class TestRankCandidatePages:
    def test_ranking(self):
        pages = [
            PageScore(page_no=0, score=0.1),
            PageScore(page_no=1, score=0.5),
            PageScore(page_no=2, score=0.3),
            PageScore(page_no=3, score=0.8),
        ]
        ranked = rank_candidate_pages(pages, top_n=2, min_score=0.2)
        assert len(ranked) == 2
        assert ranked[0].page_no == 3
        assert ranked[1].page_no == 1

    def test_min_score_filter(self):
        pages = [
            PageScore(page_no=0, score=0.05),
            PageScore(page_no=1, score=0.1),
        ]
        ranked = rank_candidate_pages(pages, min_score=0.15)
        assert len(ranked) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
