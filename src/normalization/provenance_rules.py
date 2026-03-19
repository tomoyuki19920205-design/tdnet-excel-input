# ============================================================
# provenance_rules.py — source 信頼度・ペナルティ定義
# ============================================================
from __future__ import annotations

from .field_metadata import SourceType

# ============================================================
# Base confidence (source_type × field_name)
# ============================================================

BASE_CONFIDENCE: dict[str, dict[str, float]] = {
    SourceType.TDNET_SUMMARY_XBRL: {
        "sales": 0.92, "gross_profit": 0.92,
        "operating_profit": 0.92, "cost_of_sales": 0.92,
    },
    SourceType.TDNET_ATTACHMENT_XBRL: {
        "sales": 0.88, "gross_profit": 0.95,
        "operating_profit": 0.88, "cost_of_sales": 0.90,
    },
    SourceType.JQUANTS: {
        "sales": 0.85, "gross_profit": 0.85,
        "operating_profit": 0.85, "cost_of_sales": 0.50,
    },
    SourceType.PDF_EXTRACTED: {
        "sales": 0.70, "gross_profit": 0.70,
        "operating_profit": 0.70, "cost_of_sales": 0.50,
    },
    SourceType.INFERRED: {
        "sales": 0.55, "gross_profit": 0.55,
        "operating_profit": 0.55, "cost_of_sales": 0.55,
    },
    SourceType.UNKNOWN: {
        "sales": 0.50, "gross_profit": 0.50,
        "operating_profit": 0.50, "cost_of_sales": 0.50,
    },
}

# ============================================================
# Penalties
# ============================================================

PENALTY_UNIT_UNKNOWN = 0.10
PENALTY_CONTEXT_UNKNOWN = 0.05
PENALTY_HEURISTIC_CONVERSION = 0.15
PENALTY_PER_ANOMALY_FLAG = 0.05

# ============================================================
# Source 優先順 (tie-break 用; index が小さい方が優先)
# ============================================================

SOURCE_PRIORITY: list[str] = [
    SourceType.TDNET_SUMMARY_XBRL,
    SourceType.TDNET_ATTACHMENT_XBRL,
    SourceType.JQUANTS,
    SourceType.PDF_EXTRACTED,
    SourceType.INFERRED,
    SourceType.UNKNOWN,
]


def get_base_confidence(source_type: str, field_name: str) -> float:
    """base confidence を取得。未知の組み合わせは 0.50。"""
    src_map = BASE_CONFIDENCE.get(source_type, {})
    return src_map.get(field_name, 0.50)


def compute_confidence(
    source_type: str,
    field_name: str,
    *,
    unit_unknown: bool = False,
    context_unknown: bool = False,
    heuristic_conversion: bool = False,
    anomaly_count: int = 0,
) -> float:
    """ペナルティ適用後の confidence を計算。"""
    c = get_base_confidence(source_type, field_name)
    if unit_unknown:
        c -= PENALTY_UNIT_UNKNOWN
    if context_unknown:
        c -= PENALTY_CONTEXT_UNKNOWN
    if heuristic_conversion:
        c -= PENALTY_HEURISTIC_CONVERSION
    c -= PENALTY_PER_ANOMALY_FLAG * anomaly_count
    return max(0.0, min(1.0, round(c, 4)))


def source_priority_index(source_type: str) -> int:
    """SOURCE_PRIORITY でのインデックス。見つからなければ末尾扱い。"""
    try:
        return SOURCE_PRIORITY.index(source_type)
    except ValueError:
        return len(SOURCE_PRIORITY)
