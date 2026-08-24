#!/usr/bin/env python3
"""dividend_models.py — 配当予想修正イベントのデータモデル"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Optional
import json


@dataclass
class DividendRevisionEvent:
    """配当予想修正の抽出結果"""
    fiscal_period: str = ""            # "2026年3月期"
    dividend_basis: str = ""           # "中間" / "期末" / "年間"

    previous_dividend_per_share: Optional[float] = None
    revised_dividend_per_share: Optional[float] = None
    delta_dividend_per_share: Optional[float] = None

    special_dividend_per_share: Optional[float] = None
    commemorative_dividend_per_share: Optional[float] = None

    annual_total_previous: Optional[float] = None
    annual_total_revised: Optional[float] = None

    payout_ratio: Optional[float] = None

    # Durable dividend/shareholder-return policy change (secondary event).
    # The primary event remains dividend_revision so one disclosure produces
    # one notification card.
    policy_change_detected: bool = False
    policy_change_scope: str = ""
    policy_change_label: str = ""
    policy_change_action: str = ""
    policy_change_summary: str = ""
    policy_change_before: str = ""
    policy_change_after: str = ""
    policy_change_metrics: list[dict] = field(default_factory=list)
    policy_change_evidence: list[str] = field(default_factory=list)

    subtype: str = "undecided"  # increase/decrease/special_dividend/commemorative_dividend/maintain/undecided
    importance: int = 50
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)
