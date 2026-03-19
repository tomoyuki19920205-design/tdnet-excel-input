#!/usr/bin/env python3
# ============================================================
# earnings_score.py — 決算アルファスコア算出
# ============================================================
"""
決算発表のスコアをルールベースで算出し、S/A/B/C/D ランクを付ける。

スコア構成 (100点満点):
  - Growth Score      最大 50点  (YOY/QOQ 数値成長)
  - AI Tone Score     最大 20点  (AI差分要約のトーン)
  - Guidance Score    最大 20点  (ガイダンス変化)
  - Risk Penalty      最大-20点  (リスク要因)

使い方:
  from src.alerts.earnings_score import calculate_earnings_score, build_score_reason
  result = calculate_earnings_score(alert_row, diff_summary_row)
  reason = build_score_reason(result)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


# ============================================================
# ScoreResult
# ============================================================

@dataclass
class ScoreResult:
    total_score: int = 0
    rank: str = "D"
    emoji: str = "❌"
    growth_score: int = 0
    tone_score: int = 0
    guidance_score: int = 0
    risk_penalty: int = 0
    reason_positive: list[str] = field(default_factory=list)
    reason_negative: list[str] = field(default_factory=list)


# ============================================================
# ランク定義
# ============================================================

_RANK_TABLE = [
    (70, "S", "🔥"),
    (50, "A", "🚀"),
    (30, "B", "📈"),
    (10, "C", "⚠️"),
]
_DEFAULT_RANK = ("D", "❌")


def rank_from_score(score: int) -> tuple[str, str]:
    """スコアからランクと絵文字を返す"""
    for threshold, rank, emoji in _RANK_TABLE:
        if score >= threshold:
            return rank, emoji
    return _DEFAULT_RANK


# ============================================================
# 1. Growth Score (最大50点)
# ============================================================

def calculate_growth_score(alert: dict) -> tuple[int, list[str], list[str]]:
    """
    YOY/QOQ 数値から成長スコアを算出する。

    Returns: (score, reasons_positive, reasons_negative)
    """
    score = 0
    pos: list[str] = []
    neg: list[str] = []

    # --- 売上 YOY ---
    sales_yoy = alert.get("sales_yoy")
    if sales_yoy is not None:
        if sales_yoy >= 40:
            score += 20
            pos.append("売上成長が非常に高い")
        elif sales_yoy >= 25:
            score += 15
            pos.append("売上成長が高い")
        elif sales_yoy >= 15:
            score += 10
            pos.append("売上成長が堅調")
        elif sales_yoy >= 10:
            score += 7
        elif sales_yoy >= 5:
            score += 3
        elif sales_yoy < 0:
            score -= 5
            neg.append("売上が前年比マイナス")

    # --- 営業利益 YOY ---
    op_yoy = alert.get("op_yoy")
    if op_yoy is not None:
        if op_yoy >= 60:
            score += 30
            pos.append("利益成長が非常に高い")
        elif op_yoy >= 40:
            score += 24
            pos.append("利益成長が高い")
        elif op_yoy >= 25:
            score += 18
            pos.append("利益成長が強い")
        elif op_yoy >= 15:
            score += 12
            pos.append("利益成長が堅調")
        elif op_yoy >= 5:
            score += 6
        elif op_yoy < 0:
            score -= 10
            neg.append("利益が前年比マイナス")

    # --- 売上 QOQ (補助) ---
    sales_qoq = alert.get("sales_qoq")
    if sales_qoq is not None:
        if sales_qoq >= 20:
            score += 5
        elif sales_qoq >= 10:
            score += 3
        elif sales_qoq < 0:
            score -= 2

    # --- 営業利益 QOQ (補助) ---
    op_qoq = alert.get("op_qoq")
    if op_qoq is not None:
        if op_qoq >= 20:
            score += 8
        elif op_qoq >= 10:
            score += 5
        elif op_qoq < 0:
            score -= 3

    # clamp 0-50
    score = max(0, min(50, score))
    return score, pos, neg


# ============================================================
# 2. AI Tone Score (最大20点)
# ============================================================

_TONE_SCORES = {
    "stronger_positive": 20,
    "slightly_positive": 15,
    "neutral": 5,
    "mixed": 0,
    "slightly_negative": -10,
    "stronger_negative": -20,
}

_TONE_REASON = {
    "stronger_positive": "トーン強気",
    "slightly_positive": "トーンやや強気",
    "neutral": None,
    "mixed": None,
    "slightly_negative": "慎重トーン",
    "stronger_negative": "トーンネガティブ",
}


def calculate_tone_score(diff_summary: dict | None) -> tuple[int, list[str], list[str]]:
    """tone_change からトーンスコアを算出"""
    if diff_summary is None:
        return 0, [], []

    tone = (diff_summary.get("tone_change") or "").strip()
    score = _TONE_SCORES.get(tone, 0)
    pos: list[str] = []
    neg: list[str] = []
    reason = _TONE_REASON.get(tone)
    if reason:
        if score > 0:
            pos.append(reason)
        else:
            neg.append(reason)
    return score, pos, neg


# ============================================================
# 3. Guidance Score (最大20点)
# ============================================================

_GUIDANCE_POSITIVE = [
    "上方修正", "増額", "増益見通し", "見通し改善",
    "前向き", "強い需要継続", "配当増額",
]
_GUIDANCE_NEUTRAL = [
    "据え置き", "維持", "変更なし",
]
_GUIDANCE_NEGATIVE = [
    "下方修正", "減額", "未定", "慎重",
    "先行き不透明", "合理的算定困難",
]


def detect_guidance_score(text: str | None) -> tuple[int, str]:
    """
    guidance_change テキストからスコアと理由を返す。

    Returns: (score, reason_text)
    """
    if not text:
        return 0, ""

    text_lower = text.strip()

    # ポジティブ判定
    for kw in _GUIDANCE_POSITIVE:
        if kw in text_lower:
            return 20, "ガイダンス前向き"

    # ネガティブ判定
    for kw in _GUIDANCE_NEGATIVE:
        if kw in text_lower:
            return -20, "ガイダンス下方修正"

    # 中立判定
    for kw in _GUIDANCE_NEUTRAL:
        if kw in text_lower:
            return 5, ""

    return 0, ""


# ============================================================
# 4. Risk Penalty (最大 -20点)
# ============================================================

_RISK_SEVERE = [
    "減損", "在庫調整", "受注減", "需要減退", "赤字", "特損",
    "稼働率低下", "原材料高", "価格下落", "先行き不透明",
    "関税影響", "為替悪化",
]
_RISK_MILD = [
    "コスト増", "人件費増", "立ち上げ費用", "一過性費用", "在庫増",
]


def detect_risk_penalty(diff_summary: dict | None) -> tuple[int, list[str]]:
    """
    risk_change と new_keywords_json からリスクペナルティを算出。

    Returns: (penalty, risk_reasons)
    """
    if diff_summary is None:
        return 0, []

    # 検査対象テキストを結合
    risk_text = (diff_summary.get("risk_change") or "").strip()
    kw_json = diff_summary.get("new_keywords_json") or "[]"
    try:
        keywords = json.loads(kw_json)
        if isinstance(keywords, list):
            kw_text = " ".join(str(k) for k in keywords)
        else:
            kw_text = ""
    except (json.JSONDecodeError, TypeError):
        kw_text = ""

    combined = (risk_text + " " + kw_text).strip()
    if not combined:
        return 0, []

    penalty = 0
    reasons: list[str] = []

    # 強いネガティブ: -7 per hit, max -20
    severe_penalty = 0
    for kw in _RISK_SEVERE:
        if kw in combined:
            severe_penalty -= 7
            reasons.append(kw)
    severe_penalty = max(-20, severe_penalty)

    # 軽いネガティブ: -3 per hit, max -10
    mild_penalty = 0
    for kw in _RISK_MILD:
        if kw in combined:
            mild_penalty -= 3
            if kw not in reasons:
                reasons.append(kw)
    mild_penalty = max(-10, mild_penalty)

    penalty = max(-20, severe_penalty + mild_penalty)
    return penalty, reasons


# ============================================================
# 5. 総合スコア算出
# ============================================================

def calculate_earnings_score(
    alert: dict,
    diff_summary: dict | None = None,
) -> ScoreResult:
    """
    決算スコアを算出する。

    Args:
        alert: compute_alert() の返り値 (sales_yoy, op_yoy, etc.)
        diff_summary: filing_diff_summaries の行 (None可)

    Returns:
        ScoreResult
    """
    result = ScoreResult()

    # Growth
    g_score, g_pos, g_neg = calculate_growth_score(alert)
    result.growth_score = g_score
    result.reason_positive.extend(g_pos)
    result.reason_negative.extend(g_neg)

    # Tone
    t_score, t_pos, t_neg = calculate_tone_score(diff_summary)
    result.tone_score = t_score

    result.reason_positive.extend(t_pos)
    result.reason_negative.extend(t_neg)

    # Guidance
    guidance_text = (diff_summary or {}).get("guidance_change", "")
    g_score_val, g_reason = detect_guidance_score(guidance_text)
    result.guidance_score = g_score_val
    if g_reason:
        if g_score_val > 0:
            result.reason_positive.append(g_reason)
        else:
            result.reason_negative.append(g_reason)

    # Risk
    r_penalty, r_reasons = detect_risk_penalty(diff_summary)
    result.risk_penalty = r_penalty
    result.reason_negative.extend(r_reasons)

    # Total (clamped 0-100)
    raw = result.growth_score + result.tone_score + result.guidance_score + result.risk_penalty
    result.total_score = max(0, min(100, raw))

    # Rank
    result.rank, result.emoji = rank_from_score(result.total_score)

    return result


# ============================================================
# 6. スコア理由の生成
# ============================================================

def build_score_reason(result: ScoreResult) -> str:
    """
    スコア理由を短文で生成する。
    Discord 表示用に1-2行に収める。
    """
    parts: list[str] = []

    if result.reason_positive:
        # 重複除去して最大3個
        seen = set()
        unique = []
        for r in result.reason_positive:
            if r not in seen:
                seen.add(r)
                unique.append(r)
        parts.extend(unique[:3])

    if result.reason_negative:
        seen = set()
        unique = []
        for r in result.reason_negative:
            if r not in seen:
                seen.add(r)
                unique.append(r)
        # ネガティブは最大2個、「注意」を付ける
        for r in unique[:2]:
            parts.append(f"{r}に注意")

    if not parts:
        if result.total_score >= 50:
            parts.append("堅調な決算")
        elif result.total_score >= 30:
            parts.append("標準的な決算")
        else:
            parts.append("注目度低い")

    return " / ".join(parts)
