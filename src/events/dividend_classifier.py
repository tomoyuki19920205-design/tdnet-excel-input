#!/usr/bin/env python3
"""dividend_classifier.py — 配当予想修正関連文書の分類器"""
from __future__ import annotations

import re
import unicodedata

from .common_models import ClassificationResult, EventType


def _normalize(title: str) -> str:
    s = title.replace("\n", "").replace("\r", "")
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", "", s)
    return s.lower()


# ============================================================
# 分類パターン
# ============================================================
_STRONG_KEYWORDS = [
    "配当予想の修正",
    "配当予想修正",
    "期末配当予想の修正",
    "中間配当予想の修正",
    "剰余金の配当",
    "配当方針の変更",
    "増配",
    "減配",
]

_SPECIAL_KEYWORDS = [
    "記念配当",
    "特別配当",
]

_COMBINED_BASE = ["配当"]
_COMBINED_ACTION = ["修正", "変更", "増配", "減配"]

_EXCLUDE_PATTERNS = [
    "株主優待",
    "役員人事",
    "定款変更",
    "配当落ち日",
]


# ============================================================
# メイン分類関数
# ============================================================
def classify_dividend(title: str, body_head: str = "") -> ClassificationResult:
    """タイトルと本文冒頭から配当予想修正かを判定する。"""
    n = _normalize(title)
    n_body = _normalize(body_head[:1000]) if body_head else ""
    combined = n + " " + n_body

    matched: list[str] = []
    confidence = 0.0
    subtype_hint = ""

    # 1. 除外チェック
    for excl in _EXCLUDE_PATTERNS:
        if excl in n and "配当" not in n:
            return ClassificationResult(
                is_target=False,
                event_type=EventType.DIVIDEND_REVISION,
                matched_keywords=[f"EXCLUDED:{excl}"],
            )

    # 「業績予想及び配当予想の修正」→ 配当としても検知
    # ただし forecast_classifier.py 側でも検知されるので、
    # 本文に配当修正テーブルがあるときのみ対象にする
    if "業績" in n and "配当" in n and "修正" in n:
        # 業績予想修正で配当も含む場合 → 配当部分を検知対象にする
        matched.append("業績予想及び配当予想の修正")
        confidence += 0.40
        subtype_hint = ""

    # 2. 特別配当/記念配当
    for kw in _SPECIAL_KEYWORDS:
        if kw in n or kw in n_body:
            matched.append(kw)
            confidence += 0.50
            subtype_hint = "commemorative_dividend" if "記念" in kw else "special_dividend"

    # 3. 強マッチ
    if not matched:
        for kw in _STRONG_KEYWORDS:
            if kw in n or kw in n_body:
                matched.append(kw)
                confidence += 0.50
                if kw == "増配":
                    subtype_hint = "increase"
                elif kw == "減配":
                    subtype_hint = "decrease"
                break

    # 4. 組み合わせマッチ
    if not matched:
        has_base = any(w in n for w in _COMBINED_BASE)
        has_action = any(w in n for w in _COMBINED_ACTION)
        if has_base and has_action:
            matched.append("組合せ:配当+" + "+".join(w for w in _COMBINED_ACTION if w in n))
            confidence += 0.40
            if "増配" in n:
                subtype_hint = "increase"
            elif "減配" in n:
                subtype_hint = "decrease"

    # 5. 決算短信の配当欄だけでは対象外
    if not matched:
        if "決算短信" in n:
            return ClassificationResult(is_target=False, event_type=EventType.DIVIDEND_REVISION)

    # 6. 本文キーワードボーナス
    body_kws = ["前回予想", "修正後", "1株当たり配当金", "年間配当金", "配当金の額"]
    body_hit = sum(1 for kw in body_kws if kw in n_body)
    if body_hit >= 2:
        confidence += 0.15

    confidence = min(confidence, 1.0)
    is_target = confidence >= 0.30 and len(matched) > 0

    return ClassificationResult(
        is_target=is_target,
        event_type=EventType.DIVIDEND_REVISION,
        subtype_hint=subtype_hint,
        confidence=round(confidence, 2),
        matched_keywords=matched,
    )
