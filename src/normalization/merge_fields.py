# ============================================================
# merge_fields.py — field 単位 confidence ベースマージ
# ============================================================
from __future__ import annotations

from .field_metadata import NormalizedField
from .provenance_rules import source_priority_index


def choose_best_field(
    existing: NormalizedField | None,
    new: NormalizedField | None,
) -> NormalizedField | None:
    """
    2つの NormalizedField から最良候補を選択する。

    Tie-break ルール (4段階):
      1. confidence が高い方
      2. source 優先順 (SOURCE_PRIORITY index が小さい方)
      3. anomaly_flags が少ない方
      4. 既存維持 (変更しない)

    None の場合は非 None を返す。両方 None なら None。
    """
    if existing is None and new is None:
        return None
    if existing is None:
        return new
    if new is None:
        return existing

    # normalized_value が None の方は不採用 (もう一方に値がある場合)
    if existing.normalized_value is None and new.normalized_value is not None:
        return new
    if new.normalized_value is None and existing.normalized_value is not None:
        return existing

    # 1. confidence 比較
    if new.meta.confidence > existing.meta.confidence + 0.001:
        return new
    if existing.meta.confidence > new.meta.confidence + 0.001:
        return existing

    # 2. source 優先順
    new_pri = source_priority_index(new.meta.source_type)
    ext_pri = source_priority_index(existing.meta.source_type)
    if new_pri < ext_pri:
        return new
    if ext_pri < new_pri:
        return existing

    # 3. anomaly_flags が少ない方
    new_flags = len(new.meta.anomaly_flags)
    ext_flags = len(existing.meta.anomaly_flags)
    if new_flags < ext_flags:
        return new
    if ext_flags < new_flags:
        return existing

    # 4. 既存維持
    return existing


_AMOUNT_FIELDS = ("sales", "gross_profit", "operating_profit", "cost_of_sales")


def merge_row_fields(
    existing: dict[str, NormalizedField | None],
    new: dict[str, NormalizedField | None],
) -> dict[str, NormalizedField | None]:
    """
    既存行と新規行の field を field 単位でマージする。

    Args:
        existing: {field_name: NormalizedField | None}
        new: {field_name: NormalizedField | None}

    Returns:
        マージ結果の dict
    """
    result: dict[str, NormalizedField | None] = {}
    for field_name in _AMOUNT_FIELDS:
        ext = existing.get(field_name)
        nw = new.get(field_name)
        result[field_name] = choose_best_field(ext, nw)
    return result
