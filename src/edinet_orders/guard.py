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


def _has_period_qualifier(cell: str) -> bool:
    """
    テキストに前期/当期などの期間区分キーワードが含まれるか判定する。
    前連結会計年度・当連結会計年度・前事業年度・当事業年度 等の正常な横並び表ヘッダーを識別するために使用。
    """
    _PERIOD_QW = [
        "前連結", "当連結", "前事業", "当事業",
        "前連結会計年度", "当連結会計年度",
        "前事業年度", "当事業年度",
        "前期", "当期",
        "前年同期", "当年同期",
        "前連結累計", "当連結累計",
        "第\\d+期",  # 「第X期」形式
    ]
    for kw in _PERIOD_QW:
        if re.search(kw, cell):
            return True
    return False


def _has_repeated_orders_header(row: dict[str, Any]) -> bool:
    """
    3列ヘッダーなどの列ずれ疑いを検知するため、
    source_header, source_label, snippet内に「受注高」が複数回出現するか判定する。

    ただし、前期・当期の横並び正常テーブル（例: 前連結会計年度 受注高 / 当連結会計年度 受注高）は
    期間区分キーワードが各セルに含まれる場合に限り除外する。
    曖昧な3列ヘッダー（期間区分なしで同一metricが複数列）は引き続きTHREE_COLUMN_HEADER_REVIEWにする。
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
            if line_clean.count("受注高") < 2:
                continue

            # 「受注高」が2回以上出現する行：
            # パイプ(|)でセル分割して各セルを精査する。
            # 各セルが期間区分KW（前連結・当連結等）を持っており、
            # かつ1セル内で「受注高」が1回だけなら「前期/当期 横並び正常テーブル」と判断してスキップ。
            cells = [c.strip() for c in line_clean.split("|")]
            order_cells = [c for c in cells if "受注高" in c]

            if len(order_cells) >= 2:
                # 全セルが期間区分KWを持っているかチェック
                all_have_period = all(_has_period_qualifier(c) for c in order_cells)
                # 全セル内での「受注高」が1回ずつかチェック（1セル内に2回は真の3列ヘッダー）
                all_single_occurrence = all(c.count("受注高") == 1 for c in order_cells)
                if all_have_period and all_single_occurrence:
                    # 正常な前期/当期 横並び表 → スキップ（THREE_COLUMN_HEADER_REVIEWにしない）
                    continue

            # 上記条件を満たさない場合は従来通りreview対象
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

    # 1b. A segment row must never be persisted as the company total.
    if normalized_segment is not None:
        row["save_candidate"] = False
        row["classification"] = "SEGMENT_TOTAL_REVIEW"
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

    # 2b. Annual order rows may only come from annual filings.
    if row.get("_document_period_mismatch"):
        row["save_candidate"] = False
        row["classification"] = "DOCUMENT_PERIOD_TYPE_MISMATCH_REJECT"
        return row

    if row.get("_selected_percentage_leaf") or row.get("_selected_subcomponent_leaf"):
        row["save_candidate"] = False
        row["classification"] = "AMBIGUOUS_HEADER_REVIEW"
        return row

    if row.get("_has_total_row") is False:
        row["save_candidate"] = False
        row["classification"] = "SEGMENT_TOTAL_REVIEW"
        return row

    if row.get("_previous_period_selected_while_current_exists"):
        row["save_candidate"] = False
        row["classification"] = "PREVIOUS_PERIOD_TABLE_REVIEW"
        return row

    # 3. Semantic metric coverage.  Ending construction carryover is a valid
    # companion to orders_received and must not be duplicated into backlog.
    has_orders = row.get("orders_received") is not None
    has_backlog = row.get("order_backlog") is not None
    has_carryover = row.get("construction_carryover") is not None
    has_rpo = row.get("rpo") is not None
    if not has_orders and not has_backlog and not has_carryover and not has_rpo:
        row["save_candidate"] = False
        row["classification"] = "BOTH_NULL_REJECT"
        return row
    semantic_construction_complete = has_orders and has_carryover
    semantic_rpo_complete = has_rpo and not has_orders and not has_backlog and not has_carryover
    if not semantic_construction_complete and not semantic_rpo_complete and (not has_orders or not has_backlog):
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
    if source_unit not in ("million_yen", "thousand_yen", "billion_yen", "yen"):
        row["save_candidate"] = False
        row["classification"] = "UNKNOWN_UNIT_REVIEW"
        return row

    if row.get("_source_unit_consistent") is False:
        row["save_candidate"] = False
        row["classification"] = "SOURCE_UNIT_REVIEW"
        return row

    arithmetic_status = str(row.get("_arithmetic_status") or "")
    if arithmetic_status in {"FAIL", "REVIEW", "ARITHMETIC_REVIEW"}:
        row["save_candidate"] = False
        row["classification"] = "ARITHMETIC_REVIEW"
        return row
    source_table_exception = arithmetic_status == "SOURCE_TABLE_EXCEPTION"

    # 5. Same Value チェック (orders == backlog)
    if has_orders and has_backlog and row["orders_received"] == row["order_backlog"]:
        row["save_candidate"] = False
        row["classification"] = "SAME_VALUE_SOURCE_REVIEW"
        return row

    # 6. 3列ヘッダー・列ずれ疑いチェック
    if row.get("_ambiguous_header") or (
        row.get("source_tag") != "semantic_table_v2" and _has_repeated_orders_header(row)
    ):
        row["save_candidate"] = False
        row["classification"] = "AMBIGUOUS_HEADER_REVIEW"
        return row

    # すべてのガードを通過した場合は保存候補
    row["save_candidate"] = True
    if source_table_exception:
        row["classification"] = "SOURCE_TABLE_EXCEPTION"
    elif row.get("classification") != "PARTIAL_METRIC_REVIEW":
        row["classification"] = "PASS_SAVE_CANDIDATE"
        
    return row
