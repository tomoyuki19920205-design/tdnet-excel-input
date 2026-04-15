#!/usr/bin/env python3
"""common_models.py — イベント検知共通データモデル"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional


# ============================================================
# イベント種別定数
# ============================================================
class EventType:
    BUYBACK = "buyback"
    FORECAST_REVISION = "forecast_revision"
    DIVIDEND_REVISION = "dividend_revision"


# ============================================================
# DocumentMeta — パイプライン入力用のドキュメントメタ情報
# ============================================================
@dataclass
class DocumentMeta:
    """TDNET文書のメタ情報（event_pipeline への入力）"""
    doc_id: str
    ticker: str
    company_name: str = ""
    title: str = ""
    disclosure_datetime: str = ""
    doc_url: str = ""
    pdf_path: str = ""
    text_body: str = ""  # 取得済みの場合


# ============================================================
# EventRecord — 共通イベント保存構造
# ============================================================
@dataclass
class EventRecord:
    """全イベント種別共通の保存用構造体"""
    event_id: str = ""
    source_doc_id: str = ""
    ticker: str = ""
    company_name: str = ""
    disclosure_datetime: str = ""
    title: str = ""
    doc_url: str = ""               # 開示書類の元URL
    event_type: str = ""          # buyback / forecast_revision / dividend_revision
    subtype: str = ""             # resolution / upward / increase etc.
    importance: int = 50
    summary_text: str = ""
    raw_payload_json: str = ""    # 元テキスト/分類結果
    extracted_payload_json: str = ""  # 抽出結果
    fingerprint: str = ""         # 重複検知用のハッシュ
    status: str = "new"
    first_seen_at: str = ""
    last_seen_at: str = ""
    notified_at: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        if not self.event_id:
            self.event_id = str(uuid.uuid4())

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)


# ============================================================
# ClassificationResult — 共通分類結果
# ============================================================
@dataclass
class ClassificationResult:
    """文書分類の共通結果"""
    is_target: bool = False
    event_type: str = ""
    subtype_hint: str = ""
    confidence: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
# PipelineResult — パイプライン実行結果
# ============================================================
@dataclass
class PipelineResult:
    """イベントパイプライン実行結果サマリ"""
    processed: int = 0
    detected: int = 0
    saved: int = 0
    filtered: int = 0
    notified: int = 0
    errors: int = 0
    skipped: int = 0
    supabase_saved: int = 0
    supabase_dedup_skipped: int = 0
    supabase_errors: int = 0
    details: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
