"""
Segment Extraction データモデル

セグメント抽出パイプラインの共通データ構造を定義。
segment_raw / segment_canonical の両層で使用する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SegmentRawRow:
    """segment_raw テーブルに書き込む1行分のデータ。

    各 extractor (XBRL/HTML/PDF/EDINET) はこの構造体を返す。
    """

    # ソース
    source: str  # 'xbrl' | 'html' | 'pdf' | 'tdnet' | 'jquants' | 'edinet_xbrl'
    source_system: str = ""  # 'tdnet' | 'edinet' | 'jquants'
    source_doc_type: Optional[str] = None  # FYFinancialStatements_Consolidated_JP etc
    source_document_id: Optional[str] = None  # TDnet doc ID / EDINET doc_id etc
    doc_hash: Optional[str] = None

    # 銘柄
    raw_ticker: str = ""
    normalized_ticker: str = ""

    # 期間
    period: str = ""  # YYYY-MM-DD
    quarter: str = ""  # 1Q | 2Q | 3Q | FY | 1H

    # セグメント
    raw_segment_name: str = ""
    normalized_segment_name: Optional[str] = None
    segment_type: str = "ordinary"  # ordinary | subtotal | total | adjustment | corporate | other
    special_row_type: str = "ordinary_segment"  # legacy 互換: ordinary_segment | adjustment | total | corporate | other

    # 金額 (百万円)
    sales: Optional[int] = None
    profit: Optional[int] = None
    sales_ytd: Optional[int] = None
    profit_ytd: Optional[int] = None
    sales_qtd: Optional[int] = None
    profit_qtd: Optional[int] = None
    unit: str = "million_yen"

    # メタデータ
    extraction_method: str = ""  # 'xbrl' | 'html_table' | 'pdf_table' | 'edinet_xbrl'
    derivation_method: str = ""  # 'reported_qtd' | 'derived_from_ytd_diff' | 'ytd_only'
    confidence_score: float = 0.0  # 0.0 - 1.0
    is_consolidated: Optional[bool] = None
    accounting_standard: Optional[str] = None  # JP | IFRS | US
    disclosed_at: Optional[str] = None  # ISO datetime
    is_revised: bool = False
    revision_no: int = 0
    table_title: Optional[str] = None
    header_signature: Optional[str] = None
    raw_json: Optional[dict] = None


@dataclass
class SegmentCanonicalRow:
    """segment_canonical テーブルの1行。

    PK = (ticker, period, quarter, segment_name)
    """

    ticker: str
    period: str  # YYYY-MM-DD
    quarter: str  # 1Q | 2Q | 3Q | FY | 1H
    segment_name: str

    sales: Optional[int] = None  # 百万円
    profit: Optional[int] = None  # 百万円

    source: str = ""
    source_system: str = ""  # 'tdnet' | 'edinet' | 'jquants'
    source_doc_type: Optional[str] = None
    segment_type: str = "ordinary"  # ordinary | subtotal | total | adjustment | corporate | other
    derivation_method: str = ""  # 'reported_qtd' | 'derived_from_ytd_diff' | 'ytd_only'
    disclosed_at: Optional[str] = None
    is_consolidated: Optional[bool] = None
    accounting_standard: Optional[str] = None
    confidence_score: float = 0.0
    derived_from_raw_id: Optional[int] = None
