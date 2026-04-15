"""lib/pipeline/unit_convert.py — 単位変換ユーティリティ

J-Quants / TDnet 等の円単位データを百万円単位に変換する共通モジュール。
"""
from __future__ import annotations

MILLIONS_DIVISOR: int = 1_000_000


def to_millions(value) -> int | None:
    """円単位の数値を百万円単位に変換する。

    - None はそのまま返す
    - 整数演算で端数を切り捨て (truncation toward zero)
    - int / float / 数値文字列を受け付ける

    Examples:
        >>> to_millions(32_000_000)
        32
        >>> to_millions(-1_500_000)
        -1
        >>> to_millions(None) is None
        True
    """
    if value is None:
        return None
    # str → float → int で安全に整数化; int/float はそのまま int()
    n: int = int(value) if not isinstance(value, str) else int(float(value))
    # 負値は truncate toward zero (Python の // は floor なので符号を分離)
    if n >= 0:
        return n // MILLIONS_DIVISOR
    else:
        return -((-n) // MILLIONS_DIVISOR)
