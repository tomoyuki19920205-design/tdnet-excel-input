#!/usr/bin/env python3
"""summary_prioritizer.py — AI要約の優先度分類

event_type / subtype を最優先に使い、title をフォールバック判定に利用する。
"""
from __future__ import annotations

import re
import unicodedata

from .summary_models import SummaryPriority
from .common_models import EventType


# ============================================================
# HIGH 判定ルール
# ============================================================
# (event_type, subtype) の組み合わせ
_HIGH_EVENT_RULES: list[tuple[str, str | None]] = [
    # 自社株買い決議
    (EventType.BUYBACK, "resolution"),
    # 上方修正
    (EventType.FORECAST_REVISION, "upward"),
    # 下方修正
    (EventType.FORECAST_REVISION, "downward"),
    # 増配
    (EventType.DIVIDEND_REVISION, "increase"),
]

# タイトルベースの HIGH 判定キーワード（event_type が空の場合のフォールバック）
_HIGH_TITLE_KEYWORDS = [
    "決算短信",
    "四半期決算",
    "通期決算",
    "上方修正",
    "下方修正",
    "増配",
    "自己株式の取得に係る事項の決定",
    "自社株買い",
    "自己株式取得",
]

# ============================================================
# NORMAL 判定ルール
# ============================================================
_NORMAL_EVENT_RULES: list[tuple[str, str | None]] = [
    # 業績差異
    (EventType.FORECAST_REVISION, "difference"),
    (EventType.FORECAST_REVISION, "neutral"),
    (EventType.FORECAST_REVISION, "undecided"),
    # 配当修正（増配以外）
    (EventType.DIVIDEND_REVISION, "decrease"),
    (EventType.DIVIDEND_REVISION, "special_dividend"),
    (EventType.DIVIDEND_REVISION, "commemorative_dividend"),
    (EventType.DIVIDEND_REVISION, "maintain"),
    (EventType.DIVIDEND_REVISION, "undecided"),
    # 自社株買い（決議以外）
    (EventType.BUYBACK, "status"),
    (EventType.BUYBACK, "result"),
    (EventType.BUYBACK, "cancellation"),
]

_NORMAL_TITLE_KEYWORDS = [
    "業績予想",
    "差異",
    "修正",
    "配当予想",
    "自己株式の取得状況",
    "自己株式の取得結果",
]


def _normalize_title(title: str) -> str:
    """タイトルを正規化（全角→半角、小文字化、スペース除去）"""
    s = title.replace("\n", "").replace("\r", "")
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", "", s)
    s = s.lower()
    return s


def classify_priority(
    title: str,
    event_type: str = "",
    subtype: str = "",
) -> str:
    """タイトルとイベント種別から優先度を判定する。

    判定順序:
    1. event_type + subtype の組み合わせで HIGH/NORMAL を判定
    2. event_type のみで HIGH/NORMAL を判定
    3. タイトルのキーワードで HIGH/NORMAL を判定
    4. いずれにも該当しなければ LOW

    Parameters
    ----------
    title : str
        開示タイトル
    event_type : str
        イベント種別（buyback / forecast_revision / dividend_revision）
    subtype : str
        サブタイプ（resolution / upward / increase etc.）

    Returns
    -------
    SummaryPriority.HIGH | SummaryPriority.NORMAL | SummaryPriority.LOW
    """
    # 1. event_type + subtype で HIGH 判定
    if event_type:
        for et, st in _HIGH_EVENT_RULES:
            if event_type == et:
                if st is None or subtype == st:
                    return SummaryPriority.HIGH

        # 2. event_type + subtype で NORMAL 判定
        for et, st in _NORMAL_EVENT_RULES:
            if event_type == et:
                if st is None or subtype == st:
                    return SummaryPriority.NORMAL

        # event_type があるが上記に該当しない → NORMAL (安全側に倒す)
        return SummaryPriority.NORMAL

    # 3. タイトルフォールバック
    n = _normalize_title(title)

    for kw in _HIGH_TITLE_KEYWORDS:
        if kw in n:
            return SummaryPriority.HIGH

    for kw in _NORMAL_TITLE_KEYWORDS:
        if kw in n:
            return SummaryPriority.NORMAL

    # 4. いずれにも該当しなければ LOW
    return SummaryPriority.LOW
