# src/edinet_orders/transformer.py
"""
抽出結果 (extractor.py の出力) を edinet_order_data テーブルの
INSERT形式へ変換するモジュール。

百万円変換・source_unit 付与・null_reason 付与を行う。
segment_name_key は generated column のため出力しない。
"""
from __future__ import annotations

import math
from typing import Any

# 単位 -> source_unit 変換
_UNIT_MAP: dict[str, str] = {
    "百万円": "million_yen",
    "千円": "thousand_yen",
    "億円": "billion_yen",
    "円": "yen",
}

# source_unit -> 百万円変換係数
_FACTORS: dict[str, float] = {
    "million_yen": 1.0,
    "billion_yen": 100.0,
    "thousand_yen": 1 / 1000,
    "yen": 1 / 1_000_000,
}


def _to_million(value: int | float | None, source_unit: str) -> int | None:
    """
    原単位の値を百万円に変換する（切捨て）。
    source_unit が 'unknown' の場合は None を返す。
    """
    if value is None or source_unit not in _FACTORS:
        return None
    factor = _FACTORS[source_unit]
    return math.floor(value * factor)


def transform_to_db_row(
    extracted: dict[str, Any],
    fiscal_end: str | None,
) -> dict[str, Any]:
    """
    extract_from_company() の出力を edinet_order_data INSERT形式に変換する。

    Parameters
    ----------
    extracted : dict
        extractor.extract_from_company() の戻り値
    fiscal_end : str | None
        決算期末日 "YYYY-MM-DD"。survey_detail.json の fiscal_end。
        None の場合は _dryrun_period_error フラグを立てる。

    Returns
    -------
    dict
        edinet_order_data の INSERT 対象カラムにマッピングされた dict。
        segment_name_key は含まない（generated column）。
    """
    raw_unit = extracted.get("unit")
    source_unit = _UNIT_MAP.get(raw_unit, "unknown") if raw_unit else "unknown"

    is_low = extracted.get("confidence") == "low"

    # null_reason
    if is_low:
        null_reason = "no_table_found"
    elif source_unit == "unknown":
        null_reason = "unit_unclear"
    else:
        null_reason = None

    # fiscal_year
    fiscal_year = int(fiscal_end[:4]) if fiscal_end else None

    row: dict[str, Any] = {
        "ticker": extracted["ticker"],
        "company_name": extracted["company"],
        "doc_id": extracted.get("doc_id"),
        "period": fiscal_end,           # YYYY-MM-DD
        "fiscal_year": fiscal_year,     # integer
        "segment_name": None,           # 連結全体
        # segment_name_key: generated column — INSERT対象外
        "source_type": "edinet_yuho",
        "source_tag": extracted.get("source_tag"),
        "confidence": extracted.get("confidence", "low"),
        "null_reason": null_reason,
        "source_unit": source_unit,     # 常に文字列（NOT NULL対応）
        # raw_* : 原単位の値
        "raw_orders_received": extracted.get("orders_received"),
        "raw_order_backlog": extracted.get("order_backlog"),
        "raw_construction_carryover": extracted.get("construction_carryover"),
        "raw_completed_construction": extracted.get("completed_construction"),
        "raw_rpo": extracted.get("rpo"),
        # 百万円変換後の値
        "orders_received": _to_million(extracted.get("orders_received"), source_unit),
        "order_backlog": _to_million(extracted.get("order_backlog"), source_unit),
        "construction_carryover": _to_million(extracted.get("construction_carryover"), source_unit),
        "completed_construction": _to_million(extracted.get("completed_construction"), source_unit),
        "rpo": _to_million(extracted.get("rpo"), source_unit),
        "snippet": (extracted.get("snippet") or "")[:2000] or None,
        # DRY RUN補助フィールド（DB INSERT時は無視）
        "_dryrun_period_error": "fiscal_end_missing" if not fiscal_end else None,
    }

    return row
