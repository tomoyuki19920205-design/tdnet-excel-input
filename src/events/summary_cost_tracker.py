#!/usr/bin/env python3
"""summary_cost_tracker.py — AI要約のコスト監視

ai_summaries テーブルの usage 実測値から日次/モデル別のトークン使用量とコストを集計する。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone, timedelta

from .summary_storage import get_daily_token_stats

logger = logging.getLogger("summary_cost_tracker")

JST = timezone(timedelta(hours=9))

# ============================================================
# モデル別コスト単価 (USD per 1M tokens)
# ============================================================
# 2026-03 時点の参考価格。変更時はここを更新。
_MODEL_COSTS = {
    "gpt-5.4-mini": {"input": 0.40, "output": 1.60},
    "gpt-5-mini":   {"input": 0.15, "output": 0.60},
    "gpt-5.4":      {"input": 2.00, "output": 8.00},
}

# フォールバック単価
_DEFAULT_COST = {"input": 1.00, "output": 4.00}


def _estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """トークン数からコストを推定する (USD)"""
    costs = _MODEL_COSTS.get(model, _DEFAULT_COST)
    input_cost = (input_tokens / 1_000_000) * costs["input"]
    output_cost = (output_tokens / 1_000_000) * costs["output"]
    return round(input_cost + output_cost, 6)


def get_daily_cost_summary(
    conn: sqlite3.Connection,
    target_date: str | None = None,
) -> dict:
    """日次のコスト集計を取得する。

    Parameters
    ----------
    conn : sqlite3.Connection
    target_date : str | None
        YYYY-MM-DD 形式。None の場合は全期間。

    Returns
    -------
    {
        "date": "2026-03-20" or "ALL",
        "total_requests": 15,
        "total_input_tokens": 12000,
        "total_output_tokens": 3000,
        "estimated_cost_usd": 0.015,
        "by_model": {
            "gpt-5.4-mini": {
                "count": 12,
                "input_tokens": 10000,
                "output_tokens": 2500,
                "cost_usd": 0.008
            }, ...
        }
    }
    """
    stats = get_daily_token_stats(conn, target_date)

    total_requests = 0
    total_input = 0
    total_output = 0
    total_cost = 0.0
    by_model: dict[str, dict] = {}

    for row in stats:
        model = row.get("model_used", "unknown")
        count = row.get("count", 0)
        inp = row.get("total_input_tokens", 0) or 0
        out = row.get("total_output_tokens", 0) or 0
        cost = _estimate_cost_usd(model, inp, out)

        total_requests += count
        total_input += inp
        total_output += out
        total_cost += cost

        if model not in by_model:
            by_model[model] = {
                "count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
            }
        by_model[model]["count"] += count
        by_model[model]["input_tokens"] += inp
        by_model[model]["output_tokens"] += out
        by_model[model]["cost_usd"] += cost

    # cost_usd を丸める
    for m in by_model.values():
        m["cost_usd"] = round(m["cost_usd"], 6)

    return {
        "date": target_date or "ALL",
        "total_requests": total_requests,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "estimated_cost_usd": round(total_cost, 6),
        "by_model": by_model,
    }


def print_cost_report(conn: sqlite3.Connection, target_date: str | None = None) -> None:
    """コストレポートをコンソールに出力する"""
    summary = get_daily_cost_summary(conn, target_date)

    print()
    print("=" * 55)
    print("  AI SUMMARY COST REPORT")
    print("=" * 55)
    print(f"  対象        : {summary['date']}")
    print(f"  総リクエスト : {summary['total_requests']}")
    print(f"  入力トークン : {summary['total_input_tokens']:,}")
    print(f"  出力トークン : {summary['total_output_tokens']:,}")
    print(f"  推定コスト   : ${summary['estimated_cost_usd']:.4f}")
    print()

    if summary["by_model"]:
        print("  [モデル別]")
        for model, data in sorted(summary["by_model"].items()):
            print(
                f"    {model:20s} : "
                f"{data['count']:4d}件  "
                f"input={data['input_tokens']:>8,}  "
                f"output={data['output_tokens']:>8,}  "
                f"${data['cost_usd']:.4f}"
            )

    print("=" * 55)
    print()
