#!/usr/bin/env python3
"""forecast_classifier.py — 業績予想修正関連文書の分類器"""
from __future__ import annotations

import re
import unicodedata

from .common_models import ClassificationResult, EventType


# ============================================================
# 正規化
# ============================================================
def _normalize(title: str) -> str:
    s = title.replace("\n", "").replace("\r", "")
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", "", s)
    return s.lower()


# ============================================================
# 分類パターン
# ============================================================
_STRONG_KEYWORDS = [
    "業績予想の修正",
    "業績予想修正",
    "通期業績予想の修正",
    "連結業績予想の修正",
    "個別業績予想の修正",
    "通期連結業績予想の修正",
    "業績予想及び配当予想の修正",
    "業績予想並びに配当予想の修正",
]

_DIFFERENCE_KEYWORDS = [
    "業績予想と実績との差異",
    "通期業績予想と実績との差異",
    "四半期業績予想と実績との差異",
    "連結業績予想と実績値との差異",
]

_REVISION_WORDS = ["修正", "変更", "上方修正", "下方修正", "差異"]
_BASE_WORDS = ["業績", "予想"]

_EXCLUDE_PATTERNS = [
    "月次",
    "速報",
    "補足説明資料",
    "説明会資料",
    "プレゼンテーション",
    "ir資料",
]


# ============================================================
# メイン分類関数
# ============================================================
def classify_forecast(title: str, body_head: str = "") -> ClassificationResult:
    """タイトルと本文冒頭から業績予想修正かを判定する。"""
    n = _normalize(title)
    n_body = _normalize(body_head[:1000]) if body_head else ""
    combined = n + " " + n_body

    matched: list[str] = []
    confidence = 0.0
    subtype_hint = ""

    # 1. 除外チェック
    for excl in _EXCLUDE_PATTERNS:
        if excl in n:
            # 「業績」が含まれていなければ除外
            if "業績" not in n and "予想" not in n:
                return ClassificationResult(
                    is_target=False,
                    event_type=EventType.FORECAST_REVISION,
                    matched_keywords=[f"EXCLUDED:{excl}"],
                )

    # 2. 強マッチ（差異開示）
    for kw in _DIFFERENCE_KEYWORDS:
        if kw in n or kw in n_body:
            matched.append(kw)
            confidence += 0.50
            subtype_hint = "difference"

    # 3. 強マッチ（予想修正）
    if not matched:
        for kw in _STRONG_KEYWORDS:
            if kw in n or kw in n_body:
                matched.append(kw)
                confidence += 0.50
                break

    # 4. 組み合わせマッチ
    if not matched:
        has_base = any(w in n for w in _BASE_WORDS)
        has_revision = any(w in n for w in _REVISION_WORDS)
        if has_base and has_revision:
            # 「配当」のみの場合は forecast_revision ではない
            if "業績" not in n and "配当" in n:
                return ClassificationResult(is_target=False, event_type=EventType.FORECAST_REVISION)
            matched.append("組合せ:" + "+".join(w for w in _BASE_WORDS if w in n) +
                          "+" + "+".join(w for w in _REVISION_WORDS if w in n))
            confidence += 0.40

    # 5. subtype ヒント
    if not subtype_hint:
        if "上方修正" in n or "上方修正" in n_body:
            subtype_hint = "upward"
        elif "下方修正" in n or "下方修正" in n_body:
            subtype_hint = "downward"
        elif "差異" in n:
            subtype_hint = "difference"

    # 6. 本文キーワードボーナス
    body_kws = ["前回発表予想", "今回修正予想", "修正予想", "実績値", "前回予想"]
    body_hit = sum(1 for kw in body_kws if kw in n_body)
    if body_hit >= 2:
        confidence += 0.15
        matched.append(f"body_kw_hits={body_hit}")

    confidence = min(confidence, 1.0)
    is_target = confidence >= 0.30 and len(matched) > 0

    return ClassificationResult(
        is_target=is_target,
        event_type=EventType.FORECAST_REVISION,
        subtype_hint=subtype_hint,
        confidence=round(confidence, 2),
        matched_keywords=matched,
    )
