"""
EDINET → Canonical Writer ブリッジ

EdinetSegmentResult / EdinetSegmentRecord を
canonical_writer.write_segments_canonical() が期待する dict 形式に変換する。
"""
from __future__ import annotations

import logging
from typing import Optional

from src.segment.edinet_segment_extractor import (
    EdinetSegmentResult,
    EdinetSegmentRecord,
)
from lib.pipeline.canonical_writer import normalize_segment_key

logger = logging.getLogger("edinet_bridge")

# ============================================================
# segment_type の統一マッピング
# ============================================================
# edinet_segment_extractor の special_row_type →
# canonical の segment_type (ordinary/subtotal/total/adjustment/corporate/other)
_SEGMENT_TYPE_MAP = {
    "ordinary_segment": "ordinary",
    "subtotal": "subtotal",
    "total": "total",
    "adjustment": "adjustment",
    "corporate": "corporate",
    "other": "other",
}


def edinet_record_to_segment_dict(
    rec: EdinetSegmentRecord,
    *,
    ticker: str = "",
    period: str = "",
    quarter: str = "",
) -> dict:
    """EdinetSegmentRecord → write_segments_canonical 用 dict 1件。

    Returns:
        {
            "segment_name": ...,
            "sales": ...,
            "profit": ...,
            "segment_type": ...,
            "derivation_method": ...,
            "segment_name_raw": ...,
        }
    """
    return {
        "segment_name": rec.segment_name_norm or rec.segment_name_raw,
        "segment_name_raw": rec.segment_name_raw,
        "sales": rec.sales,
        "profit": rec.profit,
        "sales_ytd": rec.sales_ytd,
        "profit_ytd": rec.profit_ytd,
        "sales_qtd": rec.sales_qtd,
        "profit_qtd": rec.profit_qtd,
        "segment_type": _SEGMENT_TYPE_MAP.get(rec.special_row_type, "ordinary"),
        "derivation_method": rec.derivation_method,
        "concept_sales": rec.concept_sales,
        "concept_profit": rec.concept_profit,
        "member_name": rec.member_name,
    }


def edinet_result_to_canonical_segments(
    result: EdinetSegmentResult,
    *,
    include_non_ordinary: bool = False,
) -> list[dict]:
    """EdinetSegmentResult → canonical writer 用 segment dict list。

    Args:
        result: extract_edinet_segments() の戻り値
        include_non_ordinary: True の場合、total/adjustment/other も含む

    Returns:
        list of dicts for write_segments_canonical()
    """
    if result.status != "ok":
        return []

    segments: list[dict] = []
    unresolved: list[str] = []

    for rec in result.segments:
        seg_dict = edinet_record_to_segment_dict(
            rec,
            ticker=result.ticker,
            period=result.period,
            quarter=result.quarter,
        )

        seg_type = seg_dict["segment_type"]

        # ordinaly のみ canonical に書き込む (デフォルト)
        if not include_non_ordinary and seg_type not in ("ordinary",):
            continue

        # segment_name が空 or 短すぎる場合は unresolved
        name = seg_dict["segment_name"]
        if not name or len(name) < 2:
            unresolved.append(rec.member_name)
            continue

        segments.append(seg_dict)

    if unresolved:
        logger.info(
            f"[edinet_bridge] unresolved segment names: "
            f"ticker={result.ticker} names={unresolved}"
        )

    return segments


def edinet_result_to_raw_rows(
    result: EdinetSegmentResult,
    *,
    doc_id: str = "",
) -> list:
    """EdinetSegmentResult → SegmentRawRow list (models.py)。

    Raw 行は全 segment_type を含む (検証用)。
    """
    from src.segment.models import SegmentRawRow

    rows = []
    for rec in result.segments:
        row = SegmentRawRow(
            source="edinet_xbrl",
            source_system="edinet",
            source_doc_type=result.doc_type,
            source_document_id=doc_id,
            raw_ticker=result.ticker,
            normalized_ticker=result.ticker,
            period=result.period,
            quarter=result.quarter,
            raw_segment_name=rec.segment_name_raw,
            normalized_segment_name=rec.segment_name_norm,
            segment_type=_SEGMENT_TYPE_MAP.get(rec.special_row_type, "ordinary"),
            special_row_type=rec.special_row_type,
            sales=rec.sales,
            profit=rec.profit,
            sales_ytd=rec.sales_ytd,
            profit_ytd=rec.profit_ytd,
            sales_qtd=rec.sales_qtd,
            profit_qtd=rec.profit_qtd,
            extraction_method="edinet_xbrl",
            derivation_method=rec.derivation_method,
            confidence_score=0.9,
            is_consolidated=True,
        )
        rows.append(row)

    return rows
