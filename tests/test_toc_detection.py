"""tests/test_toc_detection.py — TOC ページ検出のテスト"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from analysis.toc_detection import (
    classify_toc_line, detect_toc_page, detect_toc_candidate,
)


# ============================================================
# 9-1: TOC page detection tests
# ============================================================
class TestTocLineDetection:
    """個別行の TOC 判定"""

    def test_paren_note_with_dots(self):
        """（セグメント情報等の注記）…………… 10 → TOC 行"""
        r = classify_toc_line("（セグメント情報等の注記）…………… 10")
        assert r.is_toc_line is True
        assert r.trailing_page_number == 10
        assert r.has_note_keyword is True

    def test_paren_cashflow_with_dots(self):
        """（四半期連結キャッシュ・フロー計算書に関する注記）…… 8 → TOC"""
        r = classify_toc_line("（四半期連結キャッシュ・フロー計算書に関する注記）…… 8")
        assert r.is_toc_line is True
        assert r.trailing_page_number == 8

    def test_paren_kohatsu_with_dots(self):
        """（重要な後発事象）…………………… 11 → TOC"""
        r = classify_toc_line("（重要な後発事象）…………………… 11")
        assert r.is_toc_line is True
        assert r.trailing_page_number == 11

    def test_regular_segment_name_not_toc(self):
        """物流事業 → TOC 行ではない"""
        r = classify_toc_line("物流事業")
        assert r.is_toc_line is False

    def test_financial_row_not_toc(self):
        """物流事業  135,959  12,345 → TOC 行ではない"""
        r = classify_toc_line("物流事業  135,959  12,345")
        assert r.is_toc_line is False

    def test_dotted_leader_with_page_number(self):
        """セグメント情報 ・・・・・・・ 12"""
        r = classify_toc_line("セグメント情報 ・・・・・・・ 12")
        assert r.is_toc_line is True


class TestTocPageDetection:
    """ページ全体の TOC 判定"""

    def test_typical_toc_page(self):
        """3行以上の TOC 行 → TOC ページ"""
        lines = [
            "（セグメント情報等の注記）…………… 10",
            "（四半期連結キャッシュ・フロー計算書に関する注記）…… 8",
            "（重要な後発事象）…………………… 11",
            "（株主資本等変動計算書に関する注記）…… 9",
        ]
        r = detect_toc_page(lines)
        assert r.is_toc_page is True
        assert r.toc_line_count >= 3

    def test_financial_table_page_not_toc(self):
        """財務表のマトリクス → TOC ではない"""
        lines = [
            "報告セグメント  売上高  利益",
            "物流事業  135,959  12,345",
            "不動産事業  8,232  456",
            "情報処理事業  15,000  3,200",
        ]
        r = detect_toc_page(lines)
        assert r.is_toc_page is False

    def test_two_toc_lines_not_enough(self):
        """2行だけ → TOC ページ判定しない"""
        lines = [
            "（セグメント情報等の注記）…………… 10",
            "（重要な後発事象）…………………… 11",
        ]
        r = detect_toc_page(lines)
        assert r.is_toc_page is False

    def test_mokuji_heading_boosts(self):
        """「目次」見出しがあると加点"""
        lines = [
            "目 次",
            "（セグメント情報）…………… 10",
            "（継続企業の前提）…………… 5",
            "（重要な後発事象）…… 11",
        ]
        r = detect_toc_page(lines)
        assert r.is_toc_page is True
        assert r.has_mokuji_heading is True


# ============================================================
# 9-2: candidate-level TOC reject tests
# ============================================================
class TestTocCandidateDetection:
    """candidate テーブル単位の TOC 判定"""

    def test_toc_candidate_rejected(self):
        """TOC 行5件 + ページ番号 → reject"""
        lines = [
            "（セグメント情報等の注記）…………… 10",
            "（キャッシュ・フロー計算書に関する注記）…… 8",
            "（重要な後発事象）…………………… 11",
            "（継続企業の前提に関する注記）…… 5",
            "（追加情報）…………………… 6",
        ]
        r = detect_toc_candidate(lines)
        assert r.is_toc_candidate is True
        assert r.reject_reason != ""

    def test_toc_candidate_with_one_valid_segment(self):
        """TOC 行3件 + valid segment 1件 → まだ reject"""
        lines = [
            "（セグメント情報等の注記）…………… 10",
            "物流事業 135,959 12,345",
            "（重要な後発事象）…………………… 11",
            "（継続企業の前提に関する注記）…… 5",
        ]
        r = detect_toc_candidate(lines)
        assert r.is_toc_candidate is True


# ============================================================
# 9-3: 本物の表を TOC 扱いしないテスト
# ============================================================
class TestRealTableNotToc:
    """本物のセグメント表は TOC 扱いしない"""

    def test_segment_table_not_toc(self):
        """セグメント売上/利益表 → TOC ではない"""
        lines = [
            "報告セグメント  売上高  利益",
            "物流事業  135,959  12,345",
            "不動産事業  8,232  456",
            "情報処理事業  15,000  3,200",
            "合計  159,191  16,001",
        ]
        r = detect_toc_candidate(lines)
        assert r.is_toc_candidate is False

    def test_table_with_notes_header(self):
        """ヘッダーに注記があるセグメント表 → TOC ではない"""
        lines = [
            "セグメント情報",
            "報告セグメント  売上高  営業利益",
            "食品事業  50,000  5,000",
            "化学品事業  30,000  3,000",
            "機械事業  20,000  2,000",
        ]
        r = detect_toc_candidate(lines)
        assert r.is_toc_candidate is False


# ============================================================
# 9-4: dotted leader 単独では TOC にしない
# ============================================================
class TestDottedLeaderAlone:
    """dotted leader があるだけでは TOC にしない"""

    def test_dotted_leader_with_large_values_not_toc(self):
        """大きな財務数値がある → TOC ではない"""
        lines = [
            "物流事業 ······ 135,959 12,345",
            "不動産事業 ······ 8,232 456",
            "合計 ······ 144,191 12,801",
        ]
        r = detect_toc_candidate(lines)
        # dotted leader はあるがページ番号パターンではないので TOC にならない
        assert r.toc_line_count < 3


# ============================================================
# 9-5: regression tests
# ============================================================
class TestTocRegressionNoFalsePositive:
    """既存テスト対象を TOC 扱いしない"""

    def test_narrative_block_not_toc(self):
        """本文ブロック (2331型) は TOC ではなく narrative"""
        lines = [
            "当第3四半期連結累計期間におけるわが国経済は、",
            "景気は緩やかに回復しており、",
            "セグメントの業績は以下のとおりである。",
            "主に増加が影響している。",
        ]
        r = detect_toc_candidate(lines)
        assert r.is_toc_candidate is False

    def test_pl_table_not_toc(self):
        """PL テーブルは TOC ではない"""
        lines = [
            "売上高  135,959,000",
            "売上原価  100,000,000",
            "売上総利益  35,959,000",
            "販売費及び一般管理費  20,000,000",
            "営業利益  15,959,000",
        ]
        r = detect_toc_candidate(lines)
        assert r.is_toc_candidate is False
