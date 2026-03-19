#!/usr/bin/env python3
"""test_table_candidate_scoring.py — Phase 5: テーブル候補スコアリング強化テスト"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.table_scoring import (
    score_segment_table, TableScore, is_weak_evidence_table,
)


class TestScoreCategories:
    """5カテゴリ分離のテスト。"""

    def test_categories_present(self):
        """score_categories に5カテゴリが含まれる。"""
        lines = ["報告セグメント", "売上高 利益", "A事業 1,000 100"]
        ts = score_segment_table(lines)
        assert "header" in ts.score_categories
        assert "numeric_layout" in ts.score_categories
        assert "segment_row" in ts.score_categories
        assert "exclusion" in ts.score_categories
        assert "sequence" in ts.score_categories

    def test_empty_table_all_zero(self):
        ts = score_segment_table([])
        assert ts.score == 0.0
        # header, numeric_layout, segment_row, sequence はすべて 0
        assert ts.score_categories["header"] == 0
        assert ts.score_categories["segment_row"] == 0
        assert ts.score_categories["sequence"] == 0


class TestHeaderScoring:
    """テーマA: ヘッダー加点テスト。"""

    def test_sales_header_adds_score(self):
        lines = ["売上高  営業利益", "A事業 1,000 100"]
        ts = score_segment_table(lines)
        assert ts.has_sales_header
        assert ts.score_breakdown.get("sales_header", 0) > 0

    def test_profit_header_adds_score(self):
        lines = ["セグメント利益", "A事業 100"]
        ts = score_segment_table(lines)
        assert ts.has_profit_header
        assert ts.score_breakdown.get("profit_header", 0) > 0

    def test_segment_kw_adds_score(self):
        lines = ["報告セグメントの概要", "売上高"]
        ts = score_segment_table(lines)
        assert ts.score_categories["header"] > 0.1

    def test_english_headers(self):
        lines = ["Revenue  Profit", "Div A 10,000 1,000"]
        ts = score_segment_table(lines)
        assert ts.has_sales_header
        assert ts.has_profit_header


class TestNumericLayoutScoring:
    """テーマA: 数値構造加点テスト。"""

    def test_numeric_cols_two_plus(self):
        lines = [
            "事業名  売上高  営業利益",
            "A事業   10,000   500",
            "B事業   20,000  1,000",
            "C事業   15,000   800",
        ]
        ts = score_segment_table(lines)
        assert ts.numeric_col_count >= 2
        assert ts.score_breakdown.get("numeric_cols", 0) > 0

    def test_numeric_rows_five_plus(self):
        lines = [
            "セグメント情報",
            "売上高  利益",
            "A  1,000  100",
            "B  2,000  200",
            "C  3,000  300",
            "D  4,000  400",
            "E  5,000  500",
        ]
        ts = score_segment_table(lines)
        assert ts.numeric_row_count >= 5
        assert "num_rows_5plus" in ts.score_breakdown

    def test_single_numeric_col_penalized(self):
        lines = [
            "項目",
            "売上高   100,000",
            "営業利益  15,000",
            "経常利益  14,000",
            "当期純利益 10,000",
        ]
        ts = score_segment_table(lines)
        # 数値列1列 → 減点
        assert ts.score_breakdown.get("single_num_col", 0) < 0

    def test_too_few_rows_penalized(self):
        lines = ["A 1,000 100"]
        ts = score_segment_table(lines)
        assert ts.score_breakdown.get("too_few_rows", 0) < 0


class TestExclusionPenalty:
    """テーマA: 除外減点テスト。"""

    def test_equipment_investment_penalized(self):
        lines = [
            "設備投資の概要",
            "本社  500百万円",
            "工場  300百万円",
        ]
        ts = score_segment_table(lines)
        assert ts.score_categories["exclusion"] < 0

    def test_employee_table_penalized(self):
        lines = [
            "従業員の状況",
            "正社員  1,234名",
            "契約  567名",
        ]
        ts = score_segment_table(lines)
        assert ts.score_categories["exclusion"] < 0

    def test_cashflow_penalized(self):
        lines = [
            "キャッシュフローの状況",
            "営業CF  5,000",
            "投資CF  -3,000",
        ]
        ts = score_segment_table(lines)
        assert ts.score_categories["exclusion"] < 0

    def test_forecast_penalized(self):
        lines = [
            "業績予想",
            "売上高  100,000",
            "営業利益  10,000",
        ]
        ts = score_segment_table(lines)
        assert ts.score_categories["exclusion"] < 0


class TestSequenceBonus:
    """テーマA: 三拍子ボーナステスト (条件付き)。"""

    def test_full_segment_table_gets_bonus(self):
        """売上+利益+補助語+セグメント行 → ボーナス。"""
        lines = [
            "報告セグメント",
            "              売上高    セグメント利益",
            "建設事業     50,000       3,000",
            "開発事業     30,000       2,000",
            "環境事業     10,000       1,000",
            "その他        5,000         500",
            "調整額                      △500",
            "合計         95,000       6,000",
        ]
        ts = score_segment_table(lines)
        assert ts.score_categories["sequence"] > 0
        assert "三拍子" in ts.reason

    def test_no_bonus_without_segment_rows(self):
        """セグメント行がなければ三拍子ボーナスなし。"""
        lines = [
            "売上高  営業利益",
            "合計  100,000  10,000",
        ]
        ts = score_segment_table(lines)
        assert ts.score_categories["sequence"] == 0

    def test_no_bonus_for_total_only_table(self):
        """合計のみの表は三拍子ボーナスなし。"""
        lines = [
            "売上高  営業利益",
            "合計  100,000  10,000",
            "その他      0       0",
            "調整額  -1,000    -100",
        ]
        ts = score_segment_table(lines)
        # segment_like_rows がスキップラベルなので 0
        assert ts.score_categories["sequence"] == 0


class TestStructuralFields:
    """TableScore の構造情報フィールドテスト。"""

    def test_segment_like_rows_counted(self):
        lines = [
            "セグメント別",
            "事業名  売上高  営業利益",
            "建設    50,000  3,000",
            "開発    30,000  2,000",
            "環境    10,000  1,000",
        ]
        ts = score_segment_table(lines)
        assert ts.segment_like_rows >= 3

    def test_aux_term_count(self):
        lines = [
            "A事業  10,000  500",
            "B事業  20,000  1,000",
            "その他  5,000  200",
            "調整額       △100",
            "合計   35,000  1,600",
        ]
        ts = score_segment_table(lines)
        assert ts.aux_term_count >= 3


class TestFalsePositivePrevention:
    """false positive 抑制テスト。"""

    def test_pl_table_low_score(self):
        """会社全体PL表はセグメント表より低スコア。"""
        pl_lines = [
            "連結損益計算書",
            "売上高      100,000",
            "売上原価     60,000",
            "販管費       25,000",
            "営業利益     15,000",
            "経常利益     14,000",
            "当期純利益   10,000",
        ]
        seg_lines = [
            "報告セグメント",
            "              売上高    営業利益",
            "建設事業     50,000       3,000",
            "開発事業     30,000       2,000",
            "環境事業     10,000       1,000",
        ]
        pl_ts = score_segment_table(pl_lines)
        seg_ts = score_segment_table(seg_lines)
        assert seg_ts.score > pl_ts.score

    def test_equipment_table_not_weak_evidence(self):
        """設備投資表は weak evidence で採用されない。"""
        lines = [
            "設備投資の概要",
            "工場A  500  300  200",
            "工場B  300  200  100",
            "工場C  200  100   50",
        ]
        ts = score_segment_table(lines)
        assert not is_weak_evidence_table(ts)

    def test_cashflow_table_not_weak_evidence(self):
        """キャッシュフロー表は weak evidence で採用されない。"""
        lines = [
            "キャッシュフローの状況",
            "営業CF  5,000  3,000",
            "投資CF  -3,000  -2,000",
            "財務CF  -1,000  -500",
        ]
        ts = score_segment_table(lines)
        assert not is_weak_evidence_table(ts)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
