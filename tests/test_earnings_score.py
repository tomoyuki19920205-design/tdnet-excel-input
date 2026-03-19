#!/usr/bin/env python3
"""tests/test_earnings_score.py — 決算アルファスコアのテスト"""
import json
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.alerts.earnings_score import (
    calculate_earnings_score,
    calculate_growth_score,
    calculate_tone_score,
    detect_guidance_score,
    detect_risk_penalty,
    rank_from_score,
    build_score_reason,
    ScoreResult,
)


# ============================================================
# Growth Score
# ============================================================

class TestGrowthScore:
    def test_high_growth(self):
        """高成長: 売上 YOY+50%, OP YOY+70%"""
        alert = {"sales_yoy": 50, "op_yoy": 70, "sales_qoq": 25, "op_qoq": 25}
        score, pos, neg = calculate_growth_score(alert)
        assert score == 50  # clamped at 50
        assert any("売上" in r for r in pos)
        assert any("利益" in r for r in pos)

    def test_moderate_growth(self):
        """中程度成長"""
        alert = {"sales_yoy": 12, "op_yoy": 20, "sales_qoq": 5, "op_qoq": 5}
        score, pos, neg = calculate_growth_score(alert)
        assert 10 <= score <= 30

    def test_negative_growth(self):
        """マイナス成長"""
        alert = {"sales_yoy": -5, "op_yoy": -10, "sales_qoq": -3, "op_qoq": -5}
        score, pos, neg = calculate_growth_score(alert)
        assert score == 0  # clamped at 0
        assert len(neg) >= 2

    def test_none_values(self):
        """None 値はスキップ"""
        alert = {"sales_yoy": None, "op_yoy": None}
        score, pos, neg = calculate_growth_score(alert)
        assert score == 0
        assert pos == []
        assert neg == []


# ============================================================
# Tone Score
# ============================================================

class TestToneScore:
    def test_stronger_positive(self):
        score, pos, neg = calculate_tone_score({"tone_change": "stronger_positive"})
        assert score == 20
        assert any("強気" in r for r in pos)

    def test_slightly_negative(self):
        score, pos, neg = calculate_tone_score({"tone_change": "slightly_negative"})
        assert score == -10
        assert any("慎重" in r for r in neg)

    def test_none_summary(self):
        score, pos, neg = calculate_tone_score(None)
        assert score == 0

    def test_empty_tone(self):
        score, pos, neg = calculate_tone_score({"tone_change": ""})
        assert score == 0


# ============================================================
# Guidance Score
# ============================================================

class TestGuidanceScore:
    def test_positive_guidance(self):
        score, reason = detect_guidance_score("通期見通しを上方修正")
        assert score == 20
        assert "前向き" in reason

    def test_negative_guidance(self):
        score, reason = detect_guidance_score("業績見通しを下方修正")
        assert score == -20
        assert "下方" in reason

    def test_neutral_guidance(self):
        score, reason = detect_guidance_score("通期業績予想は据え置き")
        assert score == 5

    def test_empty_guidance(self):
        score, reason = detect_guidance_score("")
        assert score == 0
        assert reason == ""


# ============================================================
# Risk Penalty
# ============================================================

class TestRiskPenalty:
    def test_severe_risks(self):
        """重いリスク複数"""
        summary = {
            "risk_change": "減損リスクが高まっている",
            "new_keywords_json": json.dumps(["在庫調整", "原材料高"]),
        }
        penalty, reasons = detect_risk_penalty(summary)
        assert penalty <= -14  # 3 hits x -7 = -21, clamped to -20
        assert len(reasons) >= 2

    def test_mild_risks(self):
        """軽いリスクのみ"""
        summary = {
            "risk_change": "",
            "new_keywords_json": json.dumps(["コスト増", "人件費増"]),
        }
        penalty, reasons = detect_risk_penalty(summary)
        assert -10 <= penalty <= -3
        assert len(reasons) >= 1

    def test_no_risk(self):
        penalty, reasons = detect_risk_penalty({"risk_change": "", "new_keywords_json": "[]"})
        assert penalty == 0
        assert reasons == []

    def test_none_summary(self):
        penalty, reasons = detect_risk_penalty(None)
        assert penalty == 0


# ============================================================
# Rank / Emoji
# ============================================================

class TestRank:
    def test_s_rank(self):
        assert rank_from_score(70) == ("S", "🔥")
        assert rank_from_score(100) == ("S", "🔥")

    def test_a_rank(self):
        assert rank_from_score(50) == ("A", "🚀")
        assert rank_from_score(69) == ("A", "🚀")

    def test_b_rank(self):
        assert rank_from_score(30) == ("B", "📈")
        assert rank_from_score(49) == ("B", "📈")

    def test_c_rank(self):
        assert rank_from_score(10) == ("C", "⚠️")
        assert rank_from_score(29) == ("C", "⚠️")

    def test_d_rank(self):
        assert rank_from_score(9) == ("D", "❌")
        assert rank_from_score(0) == ("D", "❌")

    def test_boundary_values(self):
        """境界値テスト"""
        for threshold, expected_rank, emoji in [(70, "S", "🔥"), (50, "A", "🚀"),
                                                  (30, "B", "📈"), (10, "C", "⚠️")]:
            r, e = rank_from_score(threshold)
            assert r == expected_rank, f"score={threshold}"
            r2, e2 = rank_from_score(threshold - 1)
            assert r2 != expected_rank, f"score={threshold - 1} should not be {expected_rank}"


# ============================================================
# 総合スコア (E2E)
# ============================================================

class TestCalculateEarningsScore:
    def test_s_rank_full(self):
        """高成長 + ポジティブトーン + 前向きガイダンス → S"""
        alert = {"sales_yoy": 45, "op_yoy": 65, "sales_qoq": 15, "op_qoq": 15}
        diff = {
            "tone_change": "stronger_positive",
            "guidance_change": "通期見通しを上方修正",
            "risk_change": "",
            "new_keywords_json": "[]",
        }
        result = calculate_earnings_score(alert, diff)
        assert result.rank == "S"
        assert result.total_score >= 70
        assert result.emoji == "🔥"
        assert result.growth_score > 0
        assert result.tone_score == 20
        assert result.guidance_score == 20

    def test_moderate_growth_no_ai(self):
        """中程度成長 + AI要約なし → C (Growth のみで 22点)"""
        alert = {"sales_yoy": 15, "op_yoy": 20, "sales_qoq": 5, "op_qoq": 5}
        result = calculate_earnings_score(alert, None)
        assert result.rank in ("C", "B")
        assert result.tone_score == 0
        assert result.guidance_score == 0

    def test_strong_growth_high_risk(self):
        """数値は強いがリスク大 → ランクが下がる"""
        alert = {"sales_yoy": 40, "op_yoy": 50}
        diff = {
            "tone_change": "neutral",
            "guidance_change": "",
            "risk_change": "減損リスクが高まっている。在庫調整が必要。",
            "new_keywords_json": json.dumps(["原材料高", "赤字"]),
        }
        result = calculate_earnings_score(alert, diff)
        assert result.risk_penalty < 0
        # リスクが大きいのでSにはならないはず
        assert result.total_score < 70

    def test_guidance_downward(self):
        """下方修正で大きく減点"""
        alert = {"sales_yoy": 10, "op_yoy": 10}
        diff = {
            "tone_change": "slightly_negative",
            "guidance_change": "通期業績予想を下方修正",
            "risk_change": "",
            "new_keywords_json": "[]",
        }
        result = calculate_earnings_score(alert, diff)
        assert result.guidance_score == -20
        assert result.total_score < 30

    def test_no_data(self):
        """すべてなし"""
        alert = {}
        result = calculate_earnings_score(alert, None)
        assert result.total_score == 0
        assert result.rank == "D"

    def test_score_clamped_0_100(self):
        """スコアが 0-100 に収まる"""
        # 最悪ケース
        alert = {"sales_yoy": -20, "op_yoy": -30, "sales_qoq": -10, "op_qoq": -10}
        diff = {
            "tone_change": "stronger_negative",
            "guidance_change": "下方修正",
            "risk_change": "減損、在庫調整、原材料高、赤字",
            "new_keywords_json": "[]",
        }
        result = calculate_earnings_score(alert, diff)
        assert result.total_score >= 0
        assert result.total_score <= 100

        # 最高ケース
        alert2 = {"sales_yoy": 100, "op_yoy": 100, "sales_qoq": 50, "op_qoq": 50}
        diff2 = {
            "tone_change": "stronger_positive",
            "guidance_change": "上方修正",
            "risk_change": "",
            "new_keywords_json": "[]",
        }
        result2 = calculate_earnings_score(alert2, diff2)
        assert result2.total_score <= 100


# ============================================================
# Score Reason
# ============================================================

class TestBuildScoreReason:
    def test_reason_with_positive_and_negative(self):
        result = ScoreResult(
            reason_positive=["利益成長が高い", "トーンやや強気"],
            reason_negative=["原材料高"],
        )
        reason = build_score_reason(result)
        assert "利益成長が高い" in reason
        assert "原材料高に注意" in reason

    def test_reason_positive_only(self):
        result = ScoreResult(
            reason_positive=["売上成長が高い"],
        )
        reason = build_score_reason(result)
        assert "売上成長が高い" in reason

    def test_reason_empty(self):
        """理由が空 → デフォルトメッセージ"""
        result = ScoreResult(total_score=55)
        reason = build_score_reason(result)
        assert len(reason) > 0

    def test_reason_truncated(self):
        """正の理由が多い場合、最大3個に制限"""
        result = ScoreResult(
            reason_positive=["a", "b", "c", "d", "e"],
            reason_negative=["x", "y", "z"],
        )
        reason = build_score_reason(result)
        parts = reason.split(" / ")
        assert len(parts) <= 5  # max 3 positive + 2 negative
