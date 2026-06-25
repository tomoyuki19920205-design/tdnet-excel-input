# src/edinet_orders/guard.py
"""
抽出・変換後のEDINET受注データに対する保存前ガード（安全ルール）。
行単体（row-level）の検証を行い、save_candidate と classification を付与する。
DBの重複確認などDBコンテキストに依存するガードはここには含まれない。
"""
from __future__ import annotations

import re
from typing import Any


def _normalize_segment_name(val: str | None) -> str | None:
    """segment_name の不要な文字を SQL null (None) に正規化する。"""
    if val is None:
        return None
    val_clean = str(val).strip()
    if val_clean.lower() in ("", "none", "null"):
        return None
    return val


def _has_repeated_orders_header(row: dict[str, Any]) -> bool:
    """
    3列ヘッダーなどの列ずれ疑いを検知するため、
    source_header, source_label, snippet内に「受注高」が複数回出現するか判定する。
    """
    targets = [
        row.get("source_header", ""),
        row.get("source_label", ""),
        row.get("snippet", "")
    ]
    for target in targets:
        if not target:
            continue
        # 改行で分割して行ごとにチェック
        lines = str(target).split("\n")
        for line in lines:
            if line.count("受注高") >= 2:
                return True
    return False


def apply_pre_save_guard(row: dict[str, Any]) -> dict[str, Any]:
    """
    変換済みの db_row に対して安全ルールを適用し、
    `save_candidate` (bool) と `classification` (str) を付与して返す。
    
    Parameters
    ----------
    row : dict
        transformer.transform_to_db_row() が生成した辞書
        
    Returns
    -------
    dict
        安全判定結果が付与された辞書
    """
    # 1. Segment name 正規化
    normalized_segment = _normalize_segment_name(row.get("segment_name"))
    row["segment_name"] = normalized_segment
    row["segment_name_insert_value"] = normalized_segment

    # 1b. 非null segment_name は全社値か不明なため原則SEGMENT_REVIEW
    if normalized_segment is not None:
        row["save_candidate"] = False
        row["classification"] = "SEGMENT_REVIEW"
        return row

    # 2. 必須フィールドの欠損チェック
    if not row.get("period"):
        row["save_candidate"] = False
        row["classification"] = "PERIOD_NULL_REJECT"
        return row
        
    if not row.get("fiscal_year") or not row.get("ticker") or not row.get("doc_id") or not row.get("source_type"):
        row["save_candidate"] = False
        row["classification"] = "OTHER_REVIEW"
        return row

    # 3. orders_received / order_backlog の両方/片方nullチェック
    has_orders = row.get("orders_received") is not None
    has_backlog = row.get("order_backlog") is not None
    if not has_orders and not has_backlog:
        row["save_candidate"] = False
        row["classification"] = "BOTH_NULL_REJECT"
        return row
    if not has_orders or not has_backlog:
        row["save_candidate"] = False
        row["classification"] = "PARTIAL_METRIC_REVIEW"
        return row

    # 4. Unit の不明チェック (未知単位はPASS禁止)
    source_unit = str(row.get("source_unit") or "").strip().lower()
    if source_unit not in ("million_yen", "thousand_yen"):
        row["save_candidate"] = False
        row["classification"] = "UNKNOWN_UNIT_REVIEW"
        return row

    # 5. Same Value チェック (orders == backlog)
    if has_orders and has_backlog and row["orders_received"] == row["order_backlog"]:
        row["save_candidate"] = False
        row["classification"] = "SAME_VALUE_SOURCE_REVIEW"
        return row

    # 6. 3列ヘッダー・列ずれ疑いチェック
    if _has_repeated_orders_header(row):
        row["save_candidate"] = False
        row["classification"] = "THREE_COLUMN_HEADER_REVIEW"
        return row

    # すべてのガードを通過した場合は保存候補
    row["save_candidate"] = True
    row["classification"] = "PASS_SAVE_CANDIDATE"
    return row
