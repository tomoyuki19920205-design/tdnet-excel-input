#!/usr/bin/env python3
"""summary_models.py — AI要約データモデル

速報通知用の短いAI要約を管理するためのデータクラス定義。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional


# ============================================================
# 優先度定数
# ============================================================
class SummaryPriority:
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


# ============================================================
# ジョブステータス定数
# ============================================================
class JobStatus:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


# ============================================================
# 要約タイプ定数
# ============================================================
class SummaryType:
    FLASH = "flash"          # 速報要約（初回）
    DETAILED = "detailed"    # 詳細要約（将来用）
    RESUMMARY = "resummary"  # 再要約（将来用）


# ============================================================
# トーン定数
# ============================================================
class Tone:
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    MIXED = "mixed"
    CAUTIOUS = "cautious"

    ALL = ("positive", "negative", "neutral", "mixed", "cautious")


# ============================================================
# プロンプトバージョン
# ============================================================
CURRENT_PROMPT_VERSION = "v1.0"


# ============================================================
# SummaryJob — ジョブキュー用
# ============================================================
@dataclass
class SummaryJob:
    """AI要約ジョブ管理用データクラス"""
    doc_id: str
    fingerprint: str
    ticker: str = ""
    company_name: str = ""
    title: str = ""
    event_type: str = ""     # buyback / forecast_revision / dividend_revision / ""
    subtype: str = ""        # resolution / upward / increase etc.
    priority: str = SummaryPriority.NORMAL
    status: str = JobStatus.PENDING
    retry_count: int = 0
    error_msg: str = ""
    # イベント抽出済み構造化データ (JSON文字列)
    extracted_payload_json: str = ""
    created_at: str = ""
    updated_at: str = ""


# ============================================================
# AISummary — AI要約結果
# ============================================================
@dataclass
class AISummary:
    """AI要約結果データクラス"""
    summary_id: str = ""
    doc_id: str = ""
    fingerprint: str = ""
    ticker: str = ""
    company_name: str = ""
    title: str = ""
    priority: str = ""
    summary_type: str = SummaryType.FLASH
    prompt_version: str = CURRENT_PROMPT_VERSION

    # AI出力フィールド
    headline: str = ""
    bullet_1: str = ""
    bullet_2: str = ""
    bullet_3: str = ""
    tone: str = ""
    needs_review: bool = False

    # API利用量（実測値）
    model_used: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    # Discord通知済みフラグ
    notified_at: Optional[str] = None

    created_at: str = ""

    def __post_init__(self):
        if not self.summary_id:
            self.summary_id = str(uuid.uuid4())

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def bullets(self) -> list[str]:
        """3つのbulletsをリストで返す"""
        return [b for b in [self.bullet_1, self.bullet_2, self.bullet_3] if b]

    @classmethod
    def from_api_response(
        cls,
        doc_id: str,
        fingerprint: str,
        ticker: str,
        company_name: str,
        title: str,
        priority: str,
        api_result: dict,
        usage: dict,
    ) -> "AISummary":
        """API応答からAISummaryを構築"""
        bullets = api_result.get("bullets", [])
        return cls(
            doc_id=doc_id,
            fingerprint=fingerprint,
            ticker=ticker,
            company_name=company_name,
            title=title,
            priority=priority,
            headline=api_result.get("headline", ""),
            bullet_1=bullets[0] if len(bullets) > 0 else "",
            bullet_2=bullets[1] if len(bullets) > 1 else "",
            bullet_3=bullets[2] if len(bullets) > 2 else "",
            tone=api_result.get("tone", "neutral"),
            needs_review=api_result.get("needs_review", False),
            model_used=usage.get("model_used", ""),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )
