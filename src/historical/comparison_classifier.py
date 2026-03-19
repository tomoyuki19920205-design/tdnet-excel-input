# ============================================================
# comparison_classifier.py — 比較列・比較行の basis / expression_type 判定
# ============================================================
"""
PDF テキストのラベルやヘッダーから:
  - comparison basis (yoy / yoy_end / prev_period_end)
  - expression_type  (absolute / rate / change_value)
を判定する。

判定ルール:
  basis:
    「前年同期」「前年同四半期」 → yoy
    「前年同期末」「前年同四半期末」 → yoy_end
    「前期末」 → prev_period_end
    不明 → None

  expression_type:
    金額（カンマ区切り数値）→ "absolute"
    ％付き → "rate"
    増減 / 差額 → "change_value"
"""
from __future__ import annotations

import re


# ============================================================
# Basis detection
# ============================================================

# 順序重要: longer match first
_BASIS_PATTERNS: list[tuple[str, str]] = [
    # yoy_end (前年同期末) — must match BEFORE yoy
    ("前年同期末", "yoy_end"),
    ("前年同四半期末", "yoy_end"),
    ("前年度末", "yoy_end"),
    # prev_period_end
    ("前期末", "prev_period_end"),
    ("前連結会計年度末", "prev_period_end"),
    ("前連結会計年度", "prev_period_end"),
    # yoy (前年同期) — matched after yoy_end
    ("前年同期", "yoy"),
    ("前年同四半期", "yoy"),
    ("前年度同期", "yoy"),
]

# ヘッダ用: 比較列を示すフレーズ
_HEADER_COMPARISON_PATTERNS: list[tuple[str, str]] = [
    ("前年同四半期累計期間", "yoy"),
    ("前第", "yoy"),          # "前第3四半期累計期間"
    ("前連結会計年度末", "prev_period_end"),
    ("前連結会計年度", "prev_period_end"),
    ("前期実績", "yoy"),      # "前期実績" — 通期/期末対比
    ("前期末", "prev_period_end"),
]


def detect_basis_from_label(label: str) -> str | None:
    """行ラベルから basis を検出する。

    例:
        "前年同期受注高" → "yoy"
        "前年同期末受注残高" → "yoy_end"
        "前期末受注残高" → "prev_period_end"
        "受注高" → None (当期)

    Returns:
        basis string or None
    """
    if not label:
        return None

    for pattern, basis in _BASIS_PATTERNS:
        if pattern in label:
            return basis

    return None


def detect_basis_from_header(header: str) -> str | None:
    """列ヘッダーから basis を検出する。

    例:
        "前年同四半期累計期間" → "yoy"
        "当第3四半期累計期間" → None (当期)

    Returns:
        basis string or None
    """
    if not header:
        return None

    # yoy_end patterns first
    for pattern, basis in _BASIS_PATTERNS:
        if pattern in header:
            return basis

    for pattern, basis in _HEADER_COMPARISON_PATTERNS:
        if pattern in header:
            return basis

    return None


# ============================================================
# Expression type detection
# ============================================================

# 増減・差額を示すキーワード
_CHANGE_KEYWORDS = [
    "増減", "差額", "増加", "減少", "変動",
    "増△減", "増(減)", "増（減）",
]

# 率を示すキーワード
_RATE_KEYWORDS = [
    "増減率", "変動率", "比率", "構成比",
    "前年同期比", "前年比", "前期比",
]


def detect_expression_type(label: str, value_text: str = "") -> str:
    """ラベルとテキストから expression_type を判定する。

    Args:
        label: 列ヘッダーまたは行ラベル
        value_text: 値のテキスト表現（"105.2%", "18,000" 等）

    Returns:
        "absolute" | "rate" | "change_value"
    """
    combined = (label or "") + " " + (value_text or "")

    # rate - ％を含むか、率系キーワード
    if "%" in combined or "％" in combined:
        return "rate"
    for kw in _RATE_KEYWORDS:
        if kw in combined:
            return "rate"

    # change_value - 増減系キーワード
    for kw in _CHANGE_KEYWORDS:
        if kw in combined:
            return "change_value"

    # default: absolute
    return "absolute"


def is_comparison_column_header(header: str) -> bool:
    """ヘッダーが比較列（前年同期等）であるかを判定する。"""
    return detect_basis_from_header(header) is not None


def is_current_column_header(header: str) -> bool:
    """ヘッダーが当期列であるかを判定する。"""
    if not header:
        return False
    # 当期比(%) 等はレート列なので除外
    if is_change_column_header(header):
        return False
    current_keywords = [
        "当第", "当四半期", "当連結", "当期",
        "当中間", "当期実績",
    ]
    for kw in current_keywords:
        if kw in header:
            return True
    return False


def is_change_column_header(header: str) -> bool:
    """ヘッダーが増減列・予想列であるかを判定する。"""
    if not header:
        return False
    _skip_keywords = _CHANGE_KEYWORDS + _RATE_KEYWORDS + [
        "次期予想", "予想", "計画",
    ]
    for kw in _skip_keywords:
        if kw in header:
            return True
    return False
