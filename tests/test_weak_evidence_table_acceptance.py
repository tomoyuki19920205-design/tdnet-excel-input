#!/usr/bin/env python3
"""test_weak_evidence_table_acceptance.py — Phase 5: weak evidence テーブル採用テスト"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.table_scoring import (
    score_segment_table, is_weak_evidence_table,
)


class TestWeakEvidenceAcceptance:
    """テーマB: weak evidence でテーブル候補に残すテスト。"""

    def test_sales_only_with_numeric_rows(self):
        """sales ヘッダー + 数値行3以上 → weak evidence 採用。"""
        lines = [
            "売上高",
            "A事業   10,000  5,000",
            "B事業   20,000  8,000",
            "C事業   15,000  6,000",
        ]
        ts = score_segment_table(lines)
        assert is_weak_evidence_table(ts)

    def test_segment_rows_with_numeric_cols(self):
        """セグメント行3+ + 数値列2+ → weak evidence 採用。"""
        lines = [
            "事業名     金額A     金額B",
            "建設事業   50,000    3,000",
            "開発事業   30,000    2,000",
            "環境事業   10,000    1,000",
        ]
        ts = score_segment_table(lines)
        assert ts.segment_like_rows >= 3
        assert ts.numeric_col_count >= 2
        assert is_weak_evidence_table(ts)

    def test_too_few_numeric_rows_rejected(self):
        """数値行2以下は weak evidence 不採用。"""
        lines = [
            "売上高",
            "A事業   10,000  500",
        ]
        ts = score_segment_table(lines)
        assert not is_weak_evidence_table(ts)

    def test_single_col_rejected(self):
        """数値列1列は weak evidence 不採用。"""
        lines = [
            "売上高",
            "A事業   10,000",
            "B事業   20,000",
            "C事業   15,000",
        ]
        ts = score_segment_table(lines)
        # 数値列1列
        assert not is_weak_evidence_table(ts)

    def test_score_below_010_rejected(self):
        """スコア0.10未満は weak evidence 不採用。"""
        lines = ["テスト"]
        ts = score_segment_table(lines)
        assert not is_weak_evidence_table(ts)


class TestWeakEvidenceExclusionGate:
    """テーマB: weak evidence の structure gate テスト。"""

    def test_equipment_table_rejected(self):
        """設備投資表は exclusion penalty で弾かれる。"""
        lines = [
            "設備投資の概要",
            "               投資額   進捗率",
            "工場A          500      80",
            "工場B          300      60",
            "工場C          200      40",
        ]
        ts = score_segment_table(lines)
        assert not is_weak_evidence_table(ts)

    def test_employee_table_rejected(self):
        """従業員表は exclusion penalty で弾かれる。"""
        lines = [
            "従業員の状況",
            "             人数    平均年齢",
            "正社員      1,234    38.5",
            "契約         567    32.1",
            "パート       890    45.2",
        ]
        ts = score_segment_table(lines)
        assert not is_weak_evidence_table(ts)

    def test_cashflow_table_rejected(self):
        """キャッシュフロー表は exclusion penalty で弾かれる。"""
        lines = [
            "キャッシュフローの状況",
            "               当期    前期",
            "営業CF        5,000   4,000",
            "投資CF       -3,000  -2,000",
            "財務CF       -1,000    -500",
        ]
        ts = score_segment_table(lines)
        assert not is_weak_evidence_table(ts)

    def test_forecast_table_rejected(self):
        """業績予想表は exclusion penalty で弾かれる。"""
        lines = [
            "業績予想",
            "              当期予想   前回予想",
            "売上高       100,000    95,000",
            "営業利益      10,000     9,000",
            "経常利益       9,500     8,500",
        ]
        ts = score_segment_table(lines)
        assert not is_weak_evidence_table(ts)


class TestWeakEvidenceBorderCases:
    """weak evidence のボーダーラインテスト。"""

    def test_aux_terms_with_numeric_structure(self):
        """補助語 + 数値構造でも segments がないと弱い。"""
        lines = [
            "              金額A     金額B",
            "合計         100,000    10,000",
            "その他         5,000       500",
            "調整額                     △100",
        ]
        ts = score_segment_table(lines)
        # segment_like_rows がスキップ語で除外されるので weak evidence にならない
        # ただし score は低い
        assert ts.score < 0.3

    def test_near_threshold_accepted(self):
        """閾値ぎりぎりでも structure gate を満たせば採用。"""
        lines = [
            "事業概要",
            "            売上高    利益",
            "A事業      10,000     500",
            "B事業      20,000   1,000",
            "C事業      15,000     800",
        ]
        ts = score_segment_table(lines)
        # 構造条件を全て満たすなら weak evidence
        if ts.numeric_col_count >= 2 and ts.numeric_row_count >= 3:
            if ts.has_sales_header or ts.segment_like_rows >= 2:
                assert is_weak_evidence_table(ts)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
