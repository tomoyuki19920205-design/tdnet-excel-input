# ============================================================
# normalize_field.py — field 単位正規化関数
# ============================================================
from __future__ import annotations

import logging
import math

from .field_metadata import (
    FieldMeta, NormalizedField, RawUnit, SourceType,
)
from .provenance_rules import compute_confidence

logger = logging.getLogger("normalize")

# 値が百万円を超えた場合に「円建て」と判断するヒューリスティック閾値
_HEURISTIC_YEN_THRESHOLD = 1_000_000_000  # 10億

_MAX_SAFE_VALUE = 9_000_000_000_000  # 百万円単位で 9千兆


def normalize_financial_field(
    raw_value: float | None,
    *,
    field_name: str,
    source_type: str,
    raw_unit: str,
    origin: str,
    context_kind: str | None = None,
    period_kind: str | None = None,
) -> NormalizedField:
    """
    1 financial field を百万円に正規化し、メタデータを付与する。

    Args:
        raw_value: DB/API からの生値
        field_name: sales / gross_profit / cost_of_sales / operating_profit
        source_type: SourceType.*
        raw_unit: RawUnit.*
        origin: 行全体の由来 (tdnet / jquants / unknown)
        context_kind: consolidated / non_consolidated / None
        period_kind: cumulative / standalone / fy / None

    Returns:
        NormalizedField (normalized_value は百万円 int)
    """
    anomaly_flags: list[str] = []
    note: str | None = None
    heuristic_conversion = False
    normalized_value: int | None = None

    if raw_value is None or (isinstance(raw_value, float) and (math.isnan(raw_value) or math.isinf(raw_value))):
        meta = FieldMeta(
            source_type=source_type,
            raw_unit=raw_unit,
            normalized_unit="million_yen",
            context_kind=context_kind,
            period_kind=period_kind,
            confidence=0.0,
            anomaly_flags=["null_value"],
        )
        return NormalizedField(raw_value=raw_value, normalized_value=None, meta=meta)

    fv = float(raw_value)

    # ── 単位別変換 ──
    if raw_unit == RawUnit.YEN:
        # 円 → 百万円
        normalized_value = round(fv / 1_000_000)
        if fv % 1_000_000 != 0:
            note = f"yen_to_million: {fv} -> {normalized_value} (rounded)"
        else:
            note = f"yen_to_million: {fv} -> {normalized_value}"
        anomaly_flags.append("unit_converted")

    elif raw_unit == RawUnit.THOUSAND_YEN:
        # 千円 → 百万円
        normalized_value = round(fv / 1_000)
        note = f"thousand_yen_to_million: {fv} -> {normalized_value}"
        anomaly_flags.append("unit_converted")

    elif raw_unit == RawUnit.MILLION_YEN:
        # 百万円 → そのまま
        normalized_value = int(fv)

    elif raw_unit == RawUnit.UNKNOWN:
        # ヒューリスティック判定
        if origin == "jquants":
            # J-Quants は百万円
            normalized_value = int(fv)
            note = "unknown_unit: assumed million_yen (jquants)"
        elif origin == "tdnet":
            # TDnet は円
            normalized_value = round(fv / 1_000_000)
            note = f"unknown_unit: assumed yen (tdnet): {fv} -> {normalized_value}"
            anomaly_flags.append("unit_inferred")
            heuristic_conversion = True
        else:
            # unknown origin — ヒューリスティック
            if abs(fv) > _HEURISTIC_YEN_THRESHOLD:
                normalized_value = round(fv / 1_000_000)
                note = f"heuristic: large value -> yen_to_million: {fv} -> {normalized_value}"
                anomaly_flags.append("unit_inferred")
                heuristic_conversion = True
            else:
                normalized_value = int(fv)
                note = "heuristic: small value -> assumed million_yen"
                heuristic_conversion = True

    # ── 範囲チェック ──
    if normalized_value is not None and abs(normalized_value) > _MAX_SAFE_VALUE:
        anomaly_flags.append("value_overflow")
        normalized_value = None

    # ── context 不明フラグ ──
    unit_unknown = (raw_unit == RawUnit.UNKNOWN)
    context_unknown = (context_kind is None)

    # ── confidence 計算 ──
    conf = compute_confidence(
        source_type, field_name,
        unit_unknown=unit_unknown,
        context_unknown=context_unknown,
        heuristic_conversion=heuristic_conversion,
        anomaly_count=len([f for f in anomaly_flags if f not in ("unit_converted", "null_value")]),
    )

    meta = FieldMeta(
        source_type=source_type,
        raw_unit=raw_unit,
        normalized_unit="million_yen",
        context_kind=context_kind,
        period_kind=period_kind,
        confidence=conf,
        normalization_note=note,
        anomaly_flags=anomaly_flags,
    )

    return NormalizedField(
        raw_value=fv,
        normalized_value=normalized_value,
        meta=meta,
    )
