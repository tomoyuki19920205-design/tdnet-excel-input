#!/usr/bin/env python3
"""forecast_models.py — 業績予想修正イベントのデータモデル"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional
import json


@dataclass
class ForecastRevisionEvent:
    """業績予想修正の抽出結果"""
    period_label: str = ""          # "2026年3月期 通期"
    basis: str = ""                 # "連結" / "個別"

    previous_sales: Optional[float] = None
    revised_sales: Optional[float] = None
    delta_sales: Optional[float] = None
    change_sales_pct: Optional[float] = None

    previous_op: Optional[float] = None
    revised_op: Optional[float] = None
    delta_op: Optional[float] = None
    change_op_pct: Optional[float] = None

    previous_ordinary: Optional[float] = None
    revised_ordinary: Optional[float] = None
    delta_ordinary: Optional[float] = None
    change_ordinary_pct: Optional[float] = None

    previous_net_income: Optional[float] = None
    revised_net_income: Optional[float] = None
    delta_net_income: Optional[float] = None
    change_net_income_pct: Optional[float] = None

    previous_eps: Optional[float] = None
    revised_eps: Optional[float] = None
    delta_eps: Optional[float] = None
    change_eps_pct: Optional[float] = None

    # 専用抽出器が返す「今回の通期EPS予想」（補助フィールド、None は取得不能を意味する）
    latest_full_year_eps: Optional[float] = None
    eps_validated: bool = False

    is_difference_disclosure: bool = False
    extraction_source: str = "fallback"    # pdf_text / html_text / fallback
    extracted_metrics_count: int = 0
    raw_table_text: str = ""

    subtype: str = "undecided"      # upward/downward/difference/neutral/undecided
    importance: int = 50
    confidence: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        # raw_table_text は大きいので payload に入れない
        d.pop("raw_table_text", None)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)
