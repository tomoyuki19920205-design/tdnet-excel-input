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
            # 芝浦機械型などの「受注高(百万円)前年同期比(%)」による誤検知を防ぐため、
            # 「前年同期比」や「増減率」等の直前にある「受注高」はカウントから除外する
            line_clean = re.sub(r"受注残?高[^|]*?(?:前年同期|増減|比較|比)", "", line)
            if line_clean.count("受注高") >= 2:
                return True
    return False


def apply_pre_save_guard(row: dict[str, Any], enable_partial_save: bool = False) -> dict[str, Any]:
    """
    変換済みの db_row に対して安全ルールを適用し、
    `save_candidate` (bool) と `classification` (str) を付与して返す。
    
    Parameters
    ----------
    row : dict
        transformer.transform_to_db_row() が生成した辞書
    enable_partial_save : bool
        Trueの場合、PARTIAL_METRIC_REVIEW を保存候補として扱う（除外ルールを通過したもののみ）
        
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
        # 除外ルールの判定
        snippet = str(row.get("snippet") or "")
        
        # 明らかな誤抽出パターン
        false_patterns = ["販売実績", "生産実績", "仕入実績", "受注損失引当金", "履行義務", "契約負債", "契約残高"]
        is_false_positive = False
        for p in false_patterns:
            if p in snippet:
                is_false_positive = True
                break
        
        if is_false_positive:
            # もし「受注」が含まれていれば要注意だが今回は安全側に倒して全部落としてもよい。
            # しかし指示では Category C「受注も含まれている要注意」は保存する方針だったので、
            # 「受注」を含まない場合、または引当金等確定NGワードが含まれる場合を明確なNGとする。
            is_strict_false = False
            if "受注" not in snippet:
                is_strict_false = True
            elif any(p in snippet for p in ["受注損失引当金", "履行義務", "契約負債", "契約残高"]):
                is_strict_false = True
                
            if is_strict_false:
                row["save_candidate"] = False
                row["classification"] = "PARTIAL_METRIC_REVIEW_REJECT"
                return row
                
        row["classification"] = "PARTIAL_METRIC_REVIEW"
        if enable_partial_save:
            row["is_partial"] = True
            row["partial_type"] = "orders_received_only" if has_orders else "order_backlog_only"
            row["missing_metric"] = "order_backlog" if has_orders else "orders_received"
            row["review_label"] = "受注残未開示" if has_orders else "受注高未開示"
            # save_candidate は最後まで通過すれば True になるためここでは return せず後続のチェックを受けさせる
        else:
            row["save_candidate"] = False
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
    if row.get("classification") != "PARTIAL_METRIC_REVIEW":
        row["classification"] = "PASS_SAVE_CANDIDATE"
        
    return row
