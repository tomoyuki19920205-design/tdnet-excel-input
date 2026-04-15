#!/usr/bin/env python3
"""
candidate_guard 救済ロジックのテスト

テストケース:
1. narrative_guard で落ちるが表シグナルが強いケースが rescue されること
2. bs_cf_guard で落ちるが表シグナルが強いケースが rescue されること
3. header+numeric で anchor=0 でも通るケースをテスト
4. 明確な文章ブロックが引き続き reject されること (回帰防止)
5. compute_candidate_table_signals の基本テスト
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.row_classifier import (
    evaluate_candidate_guard,
    compute_candidate_table_signals,
    CandidateGuardResult,
)


# ============================================================
# compute_candidate_table_signals テスト
# ============================================================

class TestComputeCandidateTableSignals:
    def test_basic_table(self):
        """数値2個以上の行が3行 → repeated_numeric_rows=3, density > 0"""
        lines = [
            "建設事業  50,000  3,000",
            "不動産事業  20,000  1,500",
            "その他  5,000  500",
            "合計  75,000  5,000",
        ]
        density, rep_rows, _dnp = compute_candidate_table_signals(lines)
        assert rep_rows == 4
        assert density > 0.10

    def test_narrative_block(self):
        """文章ブロックは数値密度が低い"""
        lines = [
            "当連結会計年度における経営成績の概況は以下のとおりです。",
            "売上高は前年同期比で増加しました。",
            "営業利益は原材料費高騰の影響により減少しました。",
            "この結果、経常利益は前年同期比で減少しました。",
        ]
        density, rep_rows, _dnp = compute_candidate_table_signals(lines)
        assert rep_rows == 0
        assert density < 0.10

    def test_empty(self):
        density, rep_rows, _dnp = compute_candidate_table_signals([])
        assert density == 0.0
        assert rep_rows == 0
        assert _dnp == 0


# ============================================================
# narrative_guard 救済テスト
# ============================================================

class TestNarrativeGuardRescue:
    def test_narrative_rejected_without_signal(self):
        """表シグナルなし → narrative_guard で reject"""
        labels = [
            "当連結会計年度における経営成績の概況は以下のとおりです。",
            "売上高は前年同期比で増加しました。",
            "営業利益は原材料費高騰の影響により減少しました。",
            "この結果、経常利益は前年同期比で減少しました。",
            "建設事業",
        ]
        result = evaluate_candidate_guard(labels)
        assert not result.accepted
        assert result.reject_reason == "narrative_guard"

    def test_narrative_rescued_with_strong_signal(self):
        """表シグナルが強い → narrative_guard を回避"""
        # narrative 行 3 > valid 1 だが header+numeric が強い
        labels = [
            "当連結会計年度における経営成績の概況は以下のとおりです。",
            "売上高は前年同期比で増加しました。",
            "営業利益は減少しました。",
            "建設事業",
        ]
        table_lines = [
            "売上高  営業利益",
            "建設事業  50,000  3,000",
            "不動産事業  20,000  1,500",
            "その他  5,000  500",
            "合計  75,000  5,000",
        ]
        result = evaluate_candidate_guard(
            labels,
            candidate_lines=table_lines,
            header_keyword_hits=2,  # 売上+利益
            anchor_hits=0,
            segment_name_like_rows=3,
        )
        assert result.accepted
        assert "narrative" in result.rescued_by

    def test_narrative_rescued_with_weak_signal(self):
        """弱い表シグナル → narrative_guard を回避"""
        labels = [
            "当連結会計年度における経営成績の概況は以下のとおりです。",
            "売上高は増加しました。",
            "営業利益は減少しました。",
            "建設事業",
        ]
        table_lines = [
            "売上高  営業利益",
            "建設事業  50,000  3,000",
            "不動産事業  20,000  1,500",
        ]
        result = evaluate_candidate_guard(
            labels,
            candidate_lines=table_lines,
            header_keyword_hits=1,  # 弱いシグナル
            anchor_hits=0,
            segment_name_like_rows=2,
        )
        assert result.accepted
        assert "table_signal:narrative" in result.rescued_by


# ============================================================
# bs_cf_guard 救済テスト
# ============================================================

class TestBsCfGuardRescue:
    def test_bscf_rejected_without_signal(self):
        """表シグナルなし → bs_cf_guard で reject"""
        labels = [
            "流動資産",
            "建設事業",
        ]
        result = evaluate_candidate_guard(labels)
        assert not result.accepted
        assert result.reject_reason == "bs_cf_guard"

    def test_bscf_rescued_with_signal(self):
        """表シグナルあり → bs_cf_guard を回避"""
        labels = [
            "流動資産",
            "建設事業",
        ]
        table_lines = [
            "売上高  セグメント利益",
            "建設事業  50,000  3,000",
            "不動産事業  20,000  1,500",
            "その他  5,000  500",
        ]
        result = evaluate_candidate_guard(
            labels,
            candidate_lines=table_lines,
            header_keyword_hits=1,
            anchor_hits=0,
            segment_name_like_rows=2,
        )
        assert result.accepted
        assert "bs_cf" in result.rescued_by


# ============================================================
# no_valid_segment_rows 救済テスト
# ============================================================

class TestNoValidSegmentRescue:
    def test_no_valid_rejected_without_signal(self):
        """valid < 2 かつ表シグナルなし → reject"""
        labels = ["日本", "合計"]
        result = evaluate_candidate_guard(labels)
        assert not result.accepted
        assert result.reject_reason == "no_valid_segment_rows"

    def test_no_valid_rescued_with_signal(self):
        """valid < 2 だが header+numeric+segrows が強い → reject しない"""
        # 注: 「日本」はvalid_segment_like判定されるが1件のみ。
        # total_metric_dominant (t>=v and v<=1) に引っかからないよう
        # valid 1件 + total_or_metric 0件のケースで検証
        labels = ["日本"]
        table_lines = [
            "売上高  営業利益",
            "日本  50,000  3,000",
            "北米  20,000  1,500",
            "欧州  10,000  800",
        ]
        result = evaluate_candidate_guard(
            labels,
            candidate_lines=table_lines,
            header_keyword_hits=2,
            anchor_hits=0,
            segment_name_like_rows=3,
        )
        assert result.accepted
        assert "no_valid_segment" in result.rescued_by


# ============================================================
# 回帰防止テスト
# ============================================================

class TestRegressionPrevention:
    def test_pure_narrative_still_rejected(self):
        """純粋な文章ブロックは表シグナルがあっても reject (表シグナルなし)"""
        labels = [
            "当社グループは以下の事業を展開しております。",
            "建設事業につきましては、受注は好調に推移しました。",
            "不動産事業につきましては、マンション販売が増加しました。",
            "この結果、売上高は前年同期比で増加しました。",
            "営業利益は前年同期比で減少しました。",
        ]
        result = evaluate_candidate_guard(labels)
        assert not result.accepted
        assert result.reject_reason == "narrative_guard"

    def test_pl_table_not_rescued(self):
        """PL テーブルは表シグナルでは rescue されない"""
        labels = [
            "売上原価",
            "売上総利益",
            "販売費及び一般管理費",
            "営業利益",
        ]
        table_lines = [
            "売上高  100,000",
            "売上原価  70,000",
            "売上総利益  30,000",
            "販売費及び一般管理費  20,000",
            "営業利益  10,000",
        ]
        result = evaluate_candidate_guard(
            labels,
            candidate_lines=table_lines,
            header_keyword_hits=2,
            anchor_hits=0,
        )
        assert not result.accepted
        assert result.reject_reason == "pl_guard"

    def test_existing_accept_unchanged(self):
        """既存の正常ケースは引き続き accept"""
        labels = [
            "建設事業",
            "不動産事業",
            "エネルギー事業",
            "その他",
            "合計",
        ]
        result = evaluate_candidate_guard(labels)
        assert result.accepted
        assert result.reject_reason == ""

    def test_candidate_guard_result_has_signals(self):
        """CandidateGuardResult に表シグナルフィールドが存在する"""
        labels = ["建設事業", "不動産事業", "その他"]
        table_lines = [
            "建設事業  50,000  3,000",
            "不動産事業  20,000  1,500",
            "その他  5,000  500",
        ]
        result = evaluate_candidate_guard(
            labels,
            candidate_lines=table_lines,
            header_keyword_hits=2,
            anchor_hits=1,
            segment_name_like_rows=3,
        )
        assert result.numeric_density > 0
        assert result.repeated_numeric_rows > 0
        assert result.header_keyword_hits == 2
        assert result.candidate_score > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
