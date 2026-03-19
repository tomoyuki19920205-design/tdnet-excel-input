# ============================================================
# schemas.py — Historical backfill 共通スキーマ
# ============================================================
"""
開示本文・表中の比較列から生成する historical record の共通データ型。

絶対条件:
  - 比率だけでは record を作らない（expression_type == "absolute" のみ）
  - basis 不明なら skip
  - 逆算しない
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ComparisonColumn:
    """比較列から抽出された1件分のデータ"""
    basis: str = ""               # "yoy" | "yoy_end" | "prev_period_end"
    expression_type: str = ""     # "absolute" | "rate" | "change_value"
    metric_name: str = ""
    value: float | None = None
    raw_text: str = ""
    segment_name: str | None = None  # セグメント用


@dataclass
class HistoricalRecord:
    """historical backfill 用の1レコード（DB書込単位）"""
    company_code: str
    target_fiscal_year_end: str   # backfill先の期末日 "2024-03-31"
    target_quarter: str           # backfill先の四半期 "1Q"〜"4Q"
    target_period_type: str       # "quarterly" | "cumulative" | "point_in_time"
    metric_name: str              # "orders_total", "backlog_total", "segment_sales" 等
    value: float                  # 百万円正規化後の絶対値
    unit: str = "百万円"
    source_basis: str = ""        # "yoy" | "yoy_end" | "prev_period_end"
    source_doc_id: str = ""       # 抽出元開示の ID
    source_expression_type: str = "absolute"  # "absolute" のみ
    confidence: str = "medium"    # "high" | "medium" | "low"
    segment_name: str | None = None  # セグメント用


@dataclass
class ExtractResult:
    """extract系関数の共通返却型

    current_records:   当期レコード（従来の extract 結果）
    historical_records: 比較列から生成した record
    stats:             集計情報
    """
    current_records: list[HistoricalRecord] = field(default_factory=list)
    historical_records: list[HistoricalRecord] = field(default_factory=list)
    stats: dict = field(default_factory=lambda: {
        "extracted": 0,
        "skipped_ratio_only": 0,
        "skipped_unknown_basis": 0,
        "skipped_existing": 0,
    })


# ============================================================
# 残高系メトリクス判定
# ============================================================

# 残高系（ストック型）メトリクス — period_type は常に point_in_time
BALANCE_METRIC_NAMES = frozenset({
    "backlog_total",
    "carryover_construction_total",
    # セグメント系で将来追加する場合はここに
})

# フロー型メトリクス — period_type は cumulative / quarterly を引き継ぐ
FLOW_METRIC_NAMES = frozenset({
    "orders_total",
    "segment_sales",
    "segment_profit",
})


def is_balance_metric(metric_name: str) -> bool:
    """残高系（ストック型）メトリクスかどうかを判定する。

    残高系: backlog_total, carryover_construction_total
      → period_type は常に "point_in_time"

    フロー系: orders_total, segment_sales, segment_profit
      → period_type は cumulative / quarterly を引き継ぐ

    Returns:
        True = 残高系（point_in_time）
        False = フロー系（period_type を引き継ぐ）
    """
    return metric_name in BALANCE_METRIC_NAMES


def resolve_period_type(metric_name: str, current_period_type: str) -> str:
    """メトリクスに応じた period_type を返す。

    残高系 → "point_in_time" 固定
    フロー系 → current_period_type を引き継ぐ
    """
    if is_balance_metric(metric_name):
        return "point_in_time"
    return current_period_type
