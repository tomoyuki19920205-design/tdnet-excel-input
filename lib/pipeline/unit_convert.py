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


def gross_profit_to_millions(value) -> int | float | None:
    """J-Quants details由来の粗利を、端数を保って百万円へ変換する。

    ``to_millions`` は既存のSales/OP等が依存する整数百万円契約なので変更しない。
    details APIが返す円単位の粗利だけは、累計値同士の減算前に精度を失わない
    よう百万未満の端数を保持する。百万で割り切れる値は従来どおりintを返す。
    """
    if value is None:
        return None
    n: int = int(value) if not isinstance(value, str) else int(float(value))
    if n % MILLIONS_DIVISOR == 0:
        return n // MILLIONS_DIVISOR
    return n / MILLIONS_DIVISOR
