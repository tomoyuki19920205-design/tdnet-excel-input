# ============================================================
# existing_check.py — DB書込前の既存値チェック（必須化）
# ============================================================
"""
historical backfill で書込む前に既存値を確認し、
既存値がある場合は skip する。

絶対条件:
  - 既存値は上書きしない
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..migration.migration_db import MigrationDB
    from .schemas import HistoricalRecord

logger = logging.getLogger("historical.existing_check")


def check_existing_order_metric(
    db: MigrationDB,
    company_code: str,
    fiscal_year_end: str,
    quarter: str,
    metric_name: str,
    period_type: str = "",
) -> bool:
    """既存の order_metric レコードがあるか確認する。

    Args:
        db: MigrationDB インスタンス
        company_code: 企業コード "1801"
        fiscal_year_end: 期末日 "2025-03-31"
        quarter: 四半期 "1Q"〜"4Q"
        metric_name: "orders_total" 等
        period_type: "quarterly" | "cumulative" | "point_in_time"
            現時点では order_metrics テーブルに period_type カラムが
            ないため、まだフィルタに含めない。
            将来カラム追加時にここを拡張する。

    Returns:
        True = 既存値あり（skip する）
        False = 既存値なし（書込可）
    """
    rows = db.get_order_metrics(company_code, fiscal_year_end, quarter)
    for row in rows:
        if row.get("metric_name") == metric_name:
            return True
    return False


def check_existing_segment(
    db: MigrationDB,
    company_code: str,
    fiscal_year_end: str,
    quarter: str,
    segment_name: str,
    metric_name: str = "",
    period_type: str = "",
) -> bool:
    """既存の segment_financials レコードがあるか確認する。

    Args:
        db: MigrationDB インスタンス
        company_code: 企業コード
        fiscal_year_end: 期末日
        quarter: 四半期
        segment_name: セグメント名
        metric_name: "segment_sales" | "segment_profit"
            metric_name が指定された場合、該当カラムが非Noneかを確認。
        period_type: "quarterly" | "cumulative" | "point_in_time"
            現時点では segment_financials テーブルに period_type カラムが
            ないため、まだフィルタに含めない。
            将来カラム追加時にここを拡張する。

    Returns:
        True = 既存値あり（skip する）
        False = 既存値なし（書込可）
    """
    segments = db.get_segments(company_code, fiscal_year_end, quarter)
    for seg in segments:
        if seg.get("segment_name") != segment_name:
            continue

        # metric_name 指定がない場合は、segment_name 一致で skip
        if not metric_name:
            return True

        # metric_name 指定がある場合は、該当カラムに値があるか確認
        if metric_name == "segment_sales" and seg.get("segment_sales") is not None:
            return True
        if metric_name == "segment_profit" and seg.get("segment_profit") is not None:
            return True

    return False


def filter_skip_existing(
    records: list[HistoricalRecord],
    db: MigrationDB,
) -> tuple[list[HistoricalRecord], int]:
    """既存値がないレコードのみ返す。

    Args:
        records: HistoricalRecord のリスト
        db: MigrationDB インスタンス

    Returns:
        (書込対象リスト, skip した件数)
    """
    writable: list[HistoricalRecord] = []
    skipped = 0

    for rec in records:
        if rec.segment_name is not None:
            # セグメント系
            exists = check_existing_segment(
                db,
                rec.company_code,
                rec.target_fiscal_year_end,
                rec.target_quarter,
                rec.segment_name,
                metric_name=rec.metric_name,
                period_type=rec.target_period_type,
            )
        else:
            # 受注系
            exists = check_existing_order_metric(
                db,
                rec.company_code,
                rec.target_fiscal_year_end,
                rec.target_quarter,
                rec.metric_name,
                period_type=rec.target_period_type,
            )

        if exists:
            logger.debug(
                "skip_existing: %s/%s/%s/%s (既存値あり)",
                rec.company_code, rec.target_fiscal_year_end,
                rec.target_quarter, rec.metric_name,
            )
            skipped += 1
        else:
            writable.append(rec)

    return writable, skipped
