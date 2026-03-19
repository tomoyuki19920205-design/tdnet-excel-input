#!/usr/bin/env python3
# ============================================================
# previous_doc_resolver.py — 比較対象短信の自動特定
# ============================================================
"""
新しい決算短信に対して、同一tickerの比較対象となる
直前の決算短信を自動で選定する。

比較ルール:
  2Q → 同年度1Q
  3Q → 同年度2Q
  FY → 同年度3Q
  1Q → 前年度FY（年度を1年戻す）
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field

logger = logging.getLogger("filing_diff")


@dataclass
class PreviousDocResult:
    """比較対象特定の結果"""
    current_doc_id: str
    previous_doc_id: str | None
    current_title: str
    previous_title: str | None
    comparison_rule: str          # e.g. "3Q->2Q"
    comparison_confidence: str    # high / medium / low
    comparison_note: str = ""


# ================================================================
# 比較対象Q解決
# ================================================================

def resolve_comparison_target(
    period: str,
    quarter: str,
) -> tuple[str, str] | None:
    """
    (period, quarter) から比較対象の (target_period, target_quarter) を返す。

    period: "2026-03-31" 形式
    quarter: "1Q"/"2Q"/"3Q"/"4Q"/"FY"

    Returns: (target_period, target_quarter) or None
    """
    q = quarter.upper().strip()

    if q == "2Q":
        return (period, "1Q")
    elif q == "3Q":
        return (period, "2Q")
    elif q in ("4Q", "FY"):
        return (period, "3Q")
    elif q == "1Q":
        # 前年度FY: 期末年を1年戻す
        prev_period = _shift_period_year(period, -1)
        if prev_period:
            return (prev_period, "4Q")
        return None
    return None


def _shift_period_year(period: str, delta: int) -> str | None:
    """period文字列（YYYY-MM-DD）の年を delta 分シフト"""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", period)
    if not m:
        return None
    y, mo, d = int(m.group(1)), m.group(2), m.group(3)
    return f"{y + delta}-{mo}-{d}"


# ================================================================
# DB検索
# ================================================================

def find_previous_earnings_doc(
    ticker: str,
    period: str,
    quarter: str,
    db_conn: sqlite3.Connection,
) -> PreviousDocResult | None:
    """
    同一tickerの比較対象短信を quarterly_results から検索する。

    source_doc_id が NULL でない行のみ対象。
    """
    # 5桁コードに対応（DB内は5桁で格納されている可能性）
    ticker_variants = [ticker]
    if len(ticker) == 4:
        ticker_variants.append(ticker + "0")
    elif len(ticker) == 5 and ticker.endswith("0"):
        ticker_variants.append(ticker[:-1])

    target = resolve_comparison_target(period, quarter)
    if target is None:
        return PreviousDocResult(
            current_doc_id="",
            previous_doc_id=None,
            current_title="",
            previous_title=None,
            comparison_rule=f"{quarter}->none",
            comparison_confidence="low",
            comparison_note="比較対象Qを解決できません",
        )

    target_period, target_quarter = target
    rule = f"{quarter}->{target_quarter}"

    # DB内の quarter は "1Q"/"2Q"/"3Q"/"4Q" の可能性
    target_q_variants = [target_quarter]
    if target_quarter == "FY":
        target_q_variants.append("4Q")
    elif target_quarter == "4Q":
        target_q_variants.append("FY")

    placeholders_t = ",".join(["?"] * len(ticker_variants))
    placeholders_q = ",".join(["?"] * len(target_q_variants))

    query = f"""
        SELECT company_code, fiscal_year_end, quarter,
               source_doc_id, source_url
        FROM quarterly_results
        WHERE company_code IN ({placeholders_t})
          AND fiscal_year_end = ?
          AND quarter IN ({placeholders_q})
          AND source_doc_id IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT 1
    """
    params = [*ticker_variants, target_period, *target_q_variants]

    row = db_conn.execute(query, params).fetchone()

    if row:
        return PreviousDocResult(
            current_doc_id="",
            previous_doc_id=row["source_doc_id"] if isinstance(row, sqlite3.Row) else row[3],
            current_title="",
            previous_title=None,
            comparison_rule=rule,
            comparison_confidence="high",
            comparison_note=f"period={target_period} q={target_quarter}",
        )

    # フォールバック: 同一tickerの直近の決算短信（quarterを問わない）
    query_fb = f"""
        SELECT company_code, fiscal_year_end, quarter,
               source_doc_id, source_url
        FROM quarterly_results
        WHERE company_code IN ({placeholders_t})
          AND source_doc_id IS NOT NULL
          AND (fiscal_year_end < ? OR
               (fiscal_year_end = ? AND quarter < ?))
        ORDER BY fiscal_year_end DESC, quarter DESC
        LIMIT 1
    """
    params_fb = [*ticker_variants, period, period, quarter]
    row_fb = db_conn.execute(query_fb, params_fb).fetchone()

    if row_fb:
        fb_q = row_fb["quarter"] if isinstance(row_fb, sqlite3.Row) else row_fb[2]
        return PreviousDocResult(
            current_doc_id="",
            previous_doc_id=row_fb["source_doc_id"] if isinstance(row_fb, sqlite3.Row) else row_fb[3],
            current_title="",
            previous_title=None,
            comparison_rule=f"{quarter}->{fb_q}(fallback)",
            comparison_confidence="medium",
            comparison_note="理想対象が見つからずフォールバック",
        )

    return PreviousDocResult(
        current_doc_id="",
        previous_doc_id=None,
        current_title="",
        previous_title=None,
        comparison_rule=rule,
        comparison_confidence="low",
        comparison_note="比較対象が見つかりません",
    )
