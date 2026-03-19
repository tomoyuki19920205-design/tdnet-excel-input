#!/usr/bin/env python3
"""buyback_models.py — 自社株買いイベント抽出のデータモデル"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional
import json


# ============================================================
# event_type 定数
# ============================================================
BUYBACK_DECISION = "buyback_decision"
BUYBACK_STATUS = "buyback_status"
BUYBACK_RESULT = "buyback_result"
TREASURY_CANCEL = "treasury_cancel"

VALID_EVENT_TYPES = {BUYBACK_DECISION, BUYBACK_STATUS, BUYBACK_RESULT, TREASURY_CANCEL}

EXTRACTOR_VERSION = "1.0.0"


# ============================================================
# ClassificationResult
# ============================================================
@dataclass
class ClassificationResult:
    """文書分類の結果"""
    is_buyback_related: bool
    event_type_candidate: str = ""
    confidence: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
# BuybackEvent
# ============================================================
@dataclass
class BuybackEvent:
    """抽出された自社株買いイベント"""
    ticker: str
    disclosure_date: str
    event_type: str

    title: str = ""

    # 決定系
    shares_limit: Optional[int] = None
    amount_limit_million_yen: Optional[float] = None

    # 取得実績系
    shares_acquired: Optional[int] = None
    amount_acquired_million_yen: Optional[float] = None

    # 消却系
    shares_cancelled: Optional[int] = None
    cancel_date: Optional[str] = None

    # 共通
    ratio_to_outstanding: Optional[float] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    acquisition_method: Optional[str] = None
    board_resolution_date: Optional[str] = None

    # ステータス系
    status_period_label: Optional[str] = None
    status_notes: Optional[str] = None

    # メタデータ
    source_type: str = ""
    source_path: str = ""
    source_doc_id: Optional[str] = None
    source_url: Optional[str] = None
    raw_text_hash: str = ""

    extraction_confidence: float = 0.0
    extractor_version: str = EXTRACTOR_VERSION

    # 抽出中間情報
    extracted_json: str = ""

    def to_dict(self) -> dict:
        """JSON 出力用辞書"""
        d = asdict(self)
        # extracted_json を dict に戻してネスト
        if d.get("extracted_json"):
            try:
                d["extracted_json"] = json.loads(d["extracted_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)
