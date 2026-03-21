#!/usr/bin/env python3
"""dividend_extractor.py — 配当予想修正テキストからの値抽出"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from .dividend_models import DividendRevisionEvent
from .common_normalizers import normalize_jp_number, parse_number

logger = logging.getLogger("dividend_extractor")


# ============================================================
# 期間・基準判定
# ============================================================
_FY_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*期")
_BASIS_KEYWORDS = {
    "期末": "期末",
    "中間": "中間",
    "年間": "年間",
    "第2四半期末": "中間",
    "第3四半期末": "第3四半期末",
}


def _detect_fiscal_period(text: str) -> str:
    m = _FY_RE.search(text)
    return f"{m.group(1)}年{m.group(2)}月期" if m else ""


def _detect_dividend_basis(text: str) -> str:
    for kw, basis in _BASIS_KEYWORDS.items():
        if kw in text:
            return basis
    return ""


# ============================================================
# 配当額抽出
# ============================================================
_DIVIDEND_AMOUNT_RE = re.compile(r"([\d,.]+)\s*円")
_NEGATIVE_MARK = re.compile(r"[△▲\-]")


def _extract_dividend_per_share(text: str, anchors: list[str], window: int = 200) -> Optional[float]:
    """アンカー近傍から1株当たり配当額を抽出"""
    for anchor in anchors:
        idx = text.find(anchor)
        if idx >= 0:
            snippet = text[idx:idx + len(anchor) + window]
            s = normalize_jp_number(snippet)
            m = _DIVIDEND_AMOUNT_RE.search(s)
            if m:
                try:
                    return float(m.group(1).replace(",", ""))
                except ValueError:
                    pass
    return None


def _find_dividend_table_values(text: str) -> dict:
    """テーブル形式の配当情報から前回/修正後の値を抽出する。

    一般的な配当予想修正PDFの表形式:
                      中間  期末  年間
    前回予想(A)        XX円  XX円  XX円
    今回修正予想(B)    XX円  XX円  XX円
    """
    result = {}
    lines = text.split("\n")

    prev_line = None
    revised_line = None

    for i, line in enumerate(lines):
        clean = normalize_jp_number(line).strip()
        if not clean:
            continue

        if any(kw in clean for kw in ["前回発表予想", "前回予想"]):
            prev_line = clean
        elif any(kw in clean for kw in ["今回修正予想", "今回予想", "修正予想", "修正後"]):
            revised_line = clean

    # 数値抽出
    def _extract_yen_values(line: str) -> list[float]:
        s = normalize_jp_number(line)
        vals = []
        for m in re.finditer(r"([\d,.]+)\s*(?:円|\.)", s):
            try:
                vals.append(float(m.group(1).replace(",", "")))
            except ValueError:
                pass
        return vals

    if prev_line:
        prev_vals = _extract_yen_values(prev_line)
        if prev_vals:
            result["previous_values"] = prev_vals

    if revised_line:
        revised_vals = _extract_yen_values(revised_line)
        if revised_vals:
            result["revised_values"] = revised_vals

    return result


# ============================================================
# subtype 判定
# ============================================================
def _determine_subtype(
    event: DividendRevisionEvent,
    title: str = "",
) -> str:
    """配当修正の subtype を決定"""
    # 優先順: commemorative > special > increase > decrease > maintain > undecided
    if event.commemorative_dividend_per_share and event.commemorative_dividend_per_share > 0:
        return "commemorative_dividend"
    if event.special_dividend_per_share and event.special_dividend_per_share > 0:
        return "special_dividend"

    if event.revised_dividend_per_share is not None and event.previous_dividend_per_share is not None:
        if event.revised_dividend_per_share > event.previous_dividend_per_share:
            return "increase"
        elif event.revised_dividend_per_share < event.previous_dividend_per_share:
            return "decrease"
        else:
            return "maintain"

    # タイトルからのヒント
    if "増配" in title:
        return "increase"
    if "減配" in title:
        return "decrease"
    if "記念配当" in title:
        return "commemorative_dividend"
    if "特別配当" in title:
        return "special_dividend"

    return "undecided"


# ============================================================
# importance 算出
# ============================================================
def _calc_importance(event: DividendRevisionEvent) -> int:
    score = 50

    if event.subtype in ("commemorative_dividend", "special_dividend"):
        score = 75

    if event.previous_dividend_per_share is not None and event.revised_dividend_per_share is not None:
        prev = event.previous_dividend_per_share
        rev = event.revised_dividend_per_share
        if prev > 0:
            change_pct = (rev - prev) / prev * 100
            if change_pct >= 50:
                score = 85
            elif change_pct >= 20:
                score = 75
            elif change_pct > 0:
                score = 70
            elif change_pct <= -50:
                score = 80
            elif change_pct < 0:
                score = 70
        elif prev == 0 and rev > 0:
            score = 80  # 無配→復配

    return score


# ============================================================
# メイン抽出関数
# ============================================================
def extract_dividend_revision(
    text: str,
    title: str = "",
) -> DividendRevisionEvent:
    """テキストから配当予想修正イベントを抽出する。"""
    event = DividendRevisionEvent()
    event.fiscal_period = _detect_fiscal_period(text) or _detect_fiscal_period(title)
    event.dividend_basis = _detect_dividend_basis(text) or _detect_dividend_basis(title)

    confidence = 0.0

    # テーブル値抽出
    table_vals = _find_dividend_table_values(text)
    prev_vals = table_vals.get("previous_values", [])
    revised_vals = table_vals.get("revised_values", [])

    # 期末/年間を優先（配列の後ろの方）
    if prev_vals and revised_vals:
        confidence += 0.40

        if event.dividend_basis == "中間":
            # 中間配当 = 最初の値
            event.previous_dividend_per_share = prev_vals[0] if prev_vals else None
            event.revised_dividend_per_share = revised_vals[0] if revised_vals else None
        elif len(prev_vals) >= 2 and len(revised_vals) >= 2:
            # 期末 = 2番目, 年間 = 3番目（あれば）
            event.previous_dividend_per_share = prev_vals[-2] if len(prev_vals) >= 2 else prev_vals[-1]
            event.revised_dividend_per_share = revised_vals[-2] if len(revised_vals) >= 2 else revised_vals[-1]
            if len(prev_vals) >= 3:
                event.annual_total_previous = prev_vals[-1]
            if len(revised_vals) >= 3:
                event.annual_total_revised = revised_vals[-1]
        else:
            event.previous_dividend_per_share = prev_vals[-1]
            event.revised_dividend_per_share = revised_vals[-1]
    elif revised_vals:
        confidence += 0.20
        event.revised_dividend_per_share = revised_vals[-1] if revised_vals else None

    # delta 計算
    if event.previous_dividend_per_share is not None and event.revised_dividend_per_share is not None:
        event.delta_dividend_per_share = round(
            event.revised_dividend_per_share - event.previous_dividend_per_share, 2
        )

    # アンカーベースのフォールバック
    if event.revised_dividend_per_share is None:
        val = _extract_dividend_per_share(text, ["修正後", "今回修正予想", "今回予想"])
        if val is not None:
            event.revised_dividend_per_share = val
            confidence += 0.15

    if event.previous_dividend_per_share is None:
        val = _extract_dividend_per_share(text, ["前回予想", "前回発表予想"])
        if val is not None:
            event.previous_dividend_per_share = val
            confidence += 0.10

    # 特別配当/記念配当
    special = _extract_dividend_per_share(text, ["特別配当"])
    if special:
        event.special_dividend_per_share = special
        confidence += 0.10

    commemorative = _extract_dividend_per_share(text, ["記念配当"])
    if commemorative:
        event.commemorative_dividend_per_share = commemorative
        confidence += 0.10

    # 配当性向
    payout_match = re.search(r"配当性向\s*[:：]?\s*([\d.]+)\s*[%％]", normalize_jp_number(text))
    if payout_match:
        try:
            event.payout_ratio = float(payout_match.group(1))
        except ValueError:
            pass

    event.confidence = min(round(confidence, 2), 1.0)
    event.subtype = _determine_subtype(event, title)
    event.importance = _calc_importance(event)

    return event
