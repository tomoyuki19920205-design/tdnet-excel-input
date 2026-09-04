# ============================================================
# field_metadata.py — field 単位メタデータ構造体
# ============================================================
from __future__ import annotations

from dataclasses import dataclass, field


# ============================================================
# 許容値定数 (enum 的に固定)
# ============================================================

class SourceType:
    """抽出元のタイプ"""
    JQUANTS = "jquants"
    TDNET_SUMMARY_XBRL = "tdnet_summary_xbrl"
    TDNET_ATTACHMENT_XBRL = "tdnet_attachment_xbrl"
    PDF_EXTRACTED = "pdf_extracted"
    INFERRED = "inferred"
    UNKNOWN = "unknown"

    ALL = (
        JQUANTS, TDNET_SUMMARY_XBRL, TDNET_ATTACHMENT_XBRL,
        PDF_EXTRACTED, INFERRED, UNKNOWN,
    )


class RawUnit:
    """元データの単位"""
    YEN = "yen"
    THOUSAND_YEN = "thousand_yen"
    MILLION_YEN = "million_yen"
    UNKNOWN = "unknown"

    ALL = (YEN, THOUSAND_YEN, MILLION_YEN, UNKNOWN)


# field_sources の文字列 → SourceType マッピング
_FIELD_SOURCE_TO_SOURCE_TYPE = {
    "summary_xbrl": SourceType.TDNET_SUMMARY_XBRL,
    "attachment_xbrl": SourceType.TDNET_ATTACHMENT_XBRL,
    "xbrl": SourceType.TDNET_SUMMARY_XBRL,  # 旧形式
    "pdf": SourceType.PDF_EXTRACTED,
    "pdf_table": SourceType.PDF_EXTRACTED,
    "pdf_fallback": SourceType.PDF_EXTRACTED,
}

# source_unit 文字列 → RawUnit マッピング
_SOURCE_UNIT_TO_RAW_UNIT = {
    "円": RawUnit.YEN,
    "千円": RawUnit.THOUSAND_YEN,
    "百万円": RawUnit.MILLION_YEN,
}


def map_field_source_to_source_type(field_source: str) -> str:
    """field_sources JSON の値を SourceType に変換"""
    # A producer may append structured provenance, e.g.
    # ``summary_xbrl|bank_proxy|OrdinaryIncome``.  The first token remains the
    # physical source while the remaining tokens explain the proxy decision.
    physical_source = str(field_source or "").split("|", 1)[0]
    return _FIELD_SOURCE_TO_SOURCE_TYPE.get(physical_source, SourceType.UNKNOWN)


def map_source_unit_to_raw_unit(source_unit: str) -> str:
    """ExtractedFinancials.source_unit を RawUnit に変換"""
    return _SOURCE_UNIT_TO_RAW_UNIT.get(source_unit, RawUnit.UNKNOWN)


# ============================================================
# データクラス
# ============================================================

@dataclass
class FieldMeta:
    """1 financial field のメタデータ"""
    source_type: str          # SourceType.*
    raw_unit: str             # RawUnit.*
    normalized_unit: str      # 常に "million_yen"
    context_kind: str | None  # "consolidated" / "non_consolidated" / None
    period_kind: str | None   # "cumulative" / "standalone" / "fy" / None
    confidence: float         # 0.0〜1.0 (ペナルティ適用後)
    normalization_note: str | None = None
    anomaly_flags: list[str] = field(default_factory=list)


@dataclass
class NormalizedField:
    """正規化済み field = 値 + メタデータ"""
    raw_value: float | None
    normalized_value: int | None  # 百万円 int
    meta: FieldMeta
