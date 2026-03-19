"""tests/test_phase4_page_candidate.py — Phase 4 ページ候補検出強化テスト"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.analysis.page_scoring import score_segment_page, apply_sequence_boost, PageScore


class TestPageScoringNewKeywords:
    """テーマA: 新 KW による加点テスト。"""

    def test_category_betsu(self):
        ps = score_segment_page("カテゴリ別\n事業A 1,000 100", 0)
        assert ps.score > 0.05

    def test_youto_betsu(self):
        ps = score_segment_page("用途別\n製品X 5,000 500", 0)
        assert ps.score > 0.05

    def test_segment_results_en(self):
        ps = score_segment_page("segment results\nDivision A 10,000 1,000", 0)
        assert ps.score > 0.05

    def test_jigyou_goto(self):
        ps = score_segment_page("事業ごと\n事業A 1,000 100", 0)
        assert ps.score > 0.05

    def test_chiiki_betsu_gyoseki(self):
        ps = score_segment_page("地域別業績\n日本 10,000 1,000", 0)
        assert ps.score > 0.1


class TestDeductionPhase4:
    """テーマA: 新減点テスト。"""

    def test_juugyouin_deduction(self):
        ps = score_segment_page("従業員の状況\n正社員 1,234名", 0)
        assert "従業員" in ps.reason

    def test_cashflow_deduction(self):
        ps = score_segment_page("キャッシュフローの状況\n営業CF 5,000", 0)
        assert "キャッシュフロー" in ps.reason

    def test_zaiseijotai_deduction(self):
        ps = score_segment_page("財政状態の分析\n総資産 100,000", 0)
        assert "財政状態" in ps.reason


class TestRowPatternScoring:
    """テーマC: 行パターン分析テスト。"""

    def test_region_names_bonus(self):
        text = "\n".join([
            "地域別業績",
            "日本    10,000  1,000",
            "北米    8,000   800",
            "欧州    6,000   600",
            "アジア  4,000   400",
        ])
        ps = score_segment_page(text, 0)
        assert ps.score_breakdown.get("region_names", 0) > 0
        assert ps.score > 0.3

    def test_industry_names_bonus(self):
        text = "\n".join([
            "事業別業績",
            "不動産事業   50,000  5,000",
            "建設事業     30,000  3,000",
            "金融事業     20,000  2,000",
        ])
        ps = score_segment_page(text, 0)
        assert ps.score_breakdown.get("industry_names", 0) > 0

    def test_multi_num_table_bonus(self):
        text = "\n".join([
            "セグメント別",
            "事業名  売上高  営業利益",
            "A事業   10,000  500",
            "B事業   20,000  1,000",
            "C事業   15,000  800",
            "合計    45,000  2,300",
        ])
        ps = score_segment_page(text, 0)
        assert ps.score_breakdown.get("multi_num_table", 0) > 0
        assert ps.score > 0.4

    def test_no_bonus_for_few_rows(self):
        """行が少なすぎる場合は加点なし。"""
        text = "事業A  1,000  100"
        ps = score_segment_page(text, 0)
        assert ps.score_breakdown.get("multi_num_table", 0) == 0


class TestSequenceBoost:
    """テーマB: page sequence boost テスト。"""

    def test_adjacent_page_boost(self):
        """隣接ページに強い候補があれば弱いページも加点。"""
        scores = [
            PageScore(page_no=0, score=0.05),
            PageScore(page_no=1, score=0.40),
            PageScore(page_no=2, score=0.05),
        ]
        apply_sequence_boost(scores)
        # page 0 は page 1 の近隣として加点される
        assert scores[0].score > 0.05
        # page 2 も page 1 の近隣として加点される
        assert scores[2].score > 0.05

    def test_no_boost_if_all_low(self):
        """全ページが弱い場合はほぼ加点なし。"""
        scores = [
            PageScore(page_no=0, score=0.01),
            PageScore(page_no=1, score=0.02),
            PageScore(page_no=2, score=0.01),
        ]
        apply_sequence_boost(scores)
        assert scores[0].score < 0.05
        assert scores[1].score < 0.05

    def test_two_page_boost(self):
        """±2 ページも弱く加点。"""
        scores = [
            PageScore(page_no=0, score=0.02),
            PageScore(page_no=1, score=0.02),
            PageScore(page_no=2, score=0.50),
            PageScore(page_no=3, score=0.02),
            PageScore(page_no=4, score=0.02),
        ]
        apply_sequence_boost(scores)
        # page 0 は page 2 の ±2 として弱加点
        assert scores[0].score > 0.02
        # page 1 は ±1 として強加点
        assert scores[1].score > scores[0].score

    def test_sequence_boost_single_page(self):
        """1ページだけなら加点なし。"""
        scores = [PageScore(page_no=0, score=0.10)]
        apply_sequence_boost(scores)
        assert scores[0].score == 0.10

    def test_entry_page_protected(self):
        """前ページが説明ページ、次ページが表ページ → 説明ページも候補に残る。"""
        scores = [
            PageScore(page_no=0, score=0.12),  # 説明ページ (min_score=0.15 未満)
            PageScore(page_no=1, score=0.45),   # 本体ページ
        ]
        apply_sequence_boost(scores)
        assert scores[0].score >= 0.15  # sequence boost で 0.15 を超える


class TestPageCandidateIntegration:
    """統合的なページ候補テスト。"""

    def test_typical_segment_page_high_score(self):
        """典型的なセグメント表ページは高スコア。"""
        text = "\n".join([
            "セグメント別業績",
            "                売上高    営業利益",
            "国内事業      50,000      5,000",
            "海外事業      30,000      3,000",
            "その他        10,000      1,000",
            "調整額        -2,000       -200",
            "合計          88,000      8,800",
        ])
        ps = score_segment_page(text, 0)
        assert ps.score > 0.5

    def test_segment_page_without_keyword(self):
        """明示的なセグメントKWがなくても文脈で候補化。"""
        text = "\n".join([
            "事業別",
            "            売上高    営業利益",
            "不動産      50,000      5,000",
            "建設        30,000      3,000",
            "金融        20,000      2,000",
            "その他      10,000      1,000",
            "合計       110,000     11,000",
        ])
        ps = score_segment_page(text, 0)
        assert ps.score > 0.3

    def test_non_segment_financial_page(self):
        """PL 全体のページはセグメント表ページより低スコア。"""
        text = "\n".join([
            "連結損益計算書",
            "売上高      100,000",
            "売上原価     60,000",
            "販管費       25,000",
            "営業利益     15,000",
            "経常利益     14,000",
            "当期純利益   10,000",
        ])
        ps = score_segment_page(text, 0)
        # セグメントKWがないので、典型的なセグメント表ページ(0.5+)より有意に低い
        # ただし売上+利益共存で加点されるのは正常動作
        assert ps.score < 0.7
        assert "kw:" not in str(ps.score_breakdown)  # セグメント系KWはマッチしない
