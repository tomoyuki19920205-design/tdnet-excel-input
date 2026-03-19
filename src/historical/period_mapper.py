# ============================================================
# period_mapper.py — comparison basis → target period 変換
# ============================================================
"""
比較列の basis (yoy, yoy_end, prev_period_end) から
backfill 先の fiscal_year_end / quarter / period_type を決定する。

絶対条件:
  - basis 不明（None / 空文字 / "unknown"）→ None を返す（skip）
  - 逆算しない
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date


@dataclass
class TargetPeriod:
    """backfill 先の会計期間"""
    fiscal_year_end: str   # "2024-03-31"
    quarter: str           # "1Q"〜"4Q"
    period_type: str       # "quarterly" | "cumulative" | "point_in_time"


# basis不明の値
_UNKNOWN_VALUES = {None, "", "unknown", "不明"}


def _shift_fiscal_year_end(fiscal_year_end: str, delta_years: int) -> str:
    """fiscal_year_end の年部分を delta_years 分ずらす。

    "2025-03-31" + delta=-1 → "2024-03-31"
    "2025-12-31" + delta=-1 → "2024-12-31"
    """
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", fiscal_year_end)
    if not m:
        raise ValueError(f"Invalid fiscal_year_end format: {fiscal_year_end!r}")

    year = int(m.group(1)) + delta_years
    month = int(m.group(2))
    day = int(m.group(3))

    # 閏年対応: 2/29 → 2/28
    try:
        target_date = date(year, month, day)
    except ValueError:
        if month == 2 and day == 29:
            target_date = date(year, 2, 28)
        else:
            raise

    return target_date.isoformat()


def map_comparison_to_target(
    basis: str | None,
    current_fiscal_year_end: str,
    current_quarter: str,
    current_period_type: str,
) -> TargetPeriod | None:
    """comparison basis から backfill 先の period を決定する。

    Args:
        basis: "yoy" | "yoy_end" | "prev_period_end" | None
        current_fiscal_year_end: 当期の期末日 "2025-03-31"
        current_quarter: 当期の四半期 "1Q"〜"4Q"
        current_period_type: 当期の期間タイプ
            "quarterly" | "cumulative" | "point_in_time"

    Returns:
        TargetPeriod — backfill 先の期間情報
        None — basis 不明のため skip

    Mapping rules:
        yoy (前年同期比):
            fiscal_year_end = 年を -1
            quarter = そのまま
            period_type = current_period_type を引き継ぐ

        yoy_end (前年同期末):
            fiscal_year_end = 年を -1
            quarter = そのまま
            period_type = "point_in_time"

        prev_period_end (前期末):
            fiscal_year_end = 年を -1 した期末日
            quarter = "4Q" (既存DB規約に合わせる)
            period_type = "point_in_time"
    """
    if basis in _UNKNOWN_VALUES:
        return None

    normalized = basis.strip().lower() if isinstance(basis, str) else ""

    if normalized == "yoy":
        return TargetPeriod(
            fiscal_year_end=_shift_fiscal_year_end(current_fiscal_year_end, -1),
            quarter=current_quarter,
            period_type=current_period_type,
        )

    if normalized == "yoy_end":
        return TargetPeriod(
            fiscal_year_end=_shift_fiscal_year_end(current_fiscal_year_end, -1),
            quarter=current_quarter,
            period_type="point_in_time",
        )

    if normalized == "prev_period_end":
        return TargetPeriod(
            fiscal_year_end=_shift_fiscal_year_end(current_fiscal_year_end, -1),
            quarter="4Q",
            period_type="point_in_time",
        )

    # 未知の basis → skip
    return None
