#!/usr/bin/env python3
# ============================================================
# normalize_extracted_facts.py — 抽出ファクトの正規化
# ============================================================
"""
extracted_facts テーブルの値を正規化する。
- ticker 正規化 (4桁)
- metric_name 正規化
- unit 正規化 → 円単位に統一
- confidence 付与
- quarter 正規化

CLI:
  python tools/normalize_extracted_facts.py --db decision_db.db
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _PROJECT_ROOT)

from src.common_ticker import normalize_ticker
from src.extraction.ir_doc_schema import ensure_tables

logger = logging.getLogger("ir_extraction")


# ============================================================
# メトリック名正規化
# ============================================================

_METRIC_NORMALIZE_MAP: dict[str, list[str]] = {
    "sales": ["売上高", "売上", "営業収益", "売上収益", "revenue"],
    "gross_profit": ["売上総利益", "粗利益"],
    "operating_profit": ["営業利益", "営業損益", "operating profit"],
    "ordinary_profit": ["経常利益", "経常損益", "ordinary profit"],
    "net_income": ["当期純利益", "当期利益", "四半期純利益",
                    "親会社株主に帰属する当期純利益", "net income"],
    "eps": ["EPS", "1株当たり当期純利益", "1株当たり四半期純利益"],
    "forecast_sales": ["予想売上高", "売上高予想", "修正後売上高"],
    "forecast_operating_profit": ["予想営業利益", "営業利益予想", "修正後営業利益"],
    "monthly_sales": ["月次売上", "月商"],
    "same_store_sales_yoy": ["既存店売上", "既存店前年比"],
    "customer_count": ["客数", "来客数", "来店客数"],
    "average_spend": ["客単価", "単価"],
    "arpu": ["ARPU"],
    "store_count": ["店舗数", "出店数"],
    "utilization_rate": ["稼働率"],
    "orders": ["受注高", "受注額", "受注"],
    "order_backlog": ["受注残高", "受注残"],
    "segment_sales": ["セグメント売上", "部門別売上"],
    "segment_operating_profit": ["セグメント利益", "部門別利益"],
}

# フラット化した逆引きマップ
_LABEL_TO_METRIC: dict[str, str] = {}
for metric, labels in _METRIC_NORMALIZE_MAP.items():
    for label in labels:
        _LABEL_TO_METRIC[label] = metric


def normalize_metric_name(raw_label: str) -> str:
    """ラベルから正規化されたメトリック名を返す"""
    if not raw_label:
        return ""
    raw = raw_label.strip()

    # 完全一致
    if raw in _LABEL_TO_METRIC:
        return _LABEL_TO_METRIC[raw]

    # 部分一致 (長い順に)
    for label, metric in sorted(_LABEL_TO_METRIC.items(), key=lambda x: -len(x[0])):
        if label in raw:
            return metric

    return raw


# ============================================================
# 単位正規化
# ============================================================

def normalize_unit(raw_unit: str, value: float) -> tuple[str, float]:
    """
    単位を円に正規化する。

    Returns: (normalized_unit, normalized_value)
    """
    if not raw_unit:
        return "円", value

    unit = raw_unit.strip()
    if unit == "百万円":
        return "円", value * 1_000_000
    elif unit == "億円":
        return "円", value * 100_000_000
    elif unit == "千円":
        return "円", value * 1_000
    elif unit == "%":
        return "%", value
    else:
        return unit, value


# ============================================================
# Quarter 正規化
# ============================================================

_QUARTER_MAP = {
    "第1四半期": "1Q", "1Q": "1Q", "Q1": "1Q",
    "第2四半期": "2Q", "2Q": "2Q", "Q2": "2Q",
    "第3四半期": "3Q", "3Q": "3Q", "Q3": "3Q",
    "第4四半期": "4Q", "通期": "4Q", "4Q": "4Q", "Q4": "4Q",
    "中間": "2Q",
}


def normalize_quarter(raw: str) -> str:
    """quarter を正規化する"""
    if not raw:
        return ""
    raw = raw.strip()
    for k, v in _QUARTER_MAP.items():
        if k in raw:
            return v
    return raw


# ============================================================
# Confidence 判定
# ============================================================

# 高信頼: XBRL由来 or テーブルからの明確な抽出
_HIGH_CONFIDENCE_SOURCES = {"xbrl"}
# 中信頼: HTML テーブル
_MEDIUM_CONFIDENCE_SOURCES = {"html"}
# 低信頼: PDF テーブル
_LOW_CONFIDENCE_SOURCES = {"pdf"}

# メトリック名の信頼度補正
_HIGH_CONFIDENCE_METRICS = {
    "sales", "operating_profit", "ordinary_profit", "net_income", "eps",
}


def determine_confidence(
    source_type: str,
    metric_name: str,
    has_table_title: bool = False,
) -> str:
    """confidence を判定する"""
    if source_type in _HIGH_CONFIDENCE_SOURCES:
        return "high"
    if source_type in _MEDIUM_CONFIDENCE_SOURCES:
        if metric_name in _HIGH_CONFIDENCE_METRICS:
            return "high"
        return "medium"
    if source_type in _LOW_CONFIDENCE_SOURCES:
        if metric_name in _HIGH_CONFIDENCE_METRICS and has_table_title:
            return "medium"
        return "low"
    return "low"


# ============================================================
# 一括正規化
# ============================================================

def normalize_facts(facts: list[dict]) -> list[dict]:
    """
    facts リストを正規化する。

    - ticker: 4桁正規化
    - metric_name: ラベルマッチング
    - unit: 円単位に統一
    - quarter: 正規化
    - confidence: 付与
    """
    result = []
    for f in facts:
        nf = dict(f)

        # ticker
        nf["ticker"] = normalize_ticker(nf.get("ticker", ""))

        # metric_name
        raw_label = nf.get("raw_label", "")
        norm_metric = normalize_metric_name(raw_label)
        if norm_metric:
            nf["normalized_label"] = norm_metric
            if not nf.get("metric_name") or nf["metric_name"] == raw_label:
                nf["metric_name"] = norm_metric

        # quarter
        nf["quarter"] = normalize_quarter(nf.get("quarter", ""))

        # confidence
        nf["confidence"] = determine_confidence(
            source_type=nf.get("source_type", ""),
            metric_name=nf.get("metric_name", ""),
            has_table_title=bool(nf.get("table_title")),
        )

        result.append(nf)

    return result


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="抽出ファクトの正規化")
    parser.add_argument("--db", default="decision_db.db", help="SQLiteパス")
    parser.add_argument("--limit", type=int, default=500, help="処理上限")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S",
    )

    db_path = args.db
    if not os.path.isabs(db_path):
        db_path = os.path.join(_PROJECT_ROOT, db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_tables(conn)

    # 未正規化レコードを取得
    rows = conn.execute(
        """SELECT id, document_id, ticker, period, quarter,
                  metric_name, metric_value, unit, segment_name,
                  source_type, confidence, page_no, table_title,
                  raw_label, normalized_label
           FROM extracted_facts
           ORDER BY id ASC
           LIMIT ?""",
        (args.limit,),
    ).fetchall()

    logger.info(f"[NORM] 対象: {len(rows)} 件")
    updated = 0

    for row in rows:
        row_dict = dict(row)
        old_confidence = row_dict.get("confidence", "")

        # 正規化
        norm = normalize_facts([row_dict])[0]

        # 変更があれば UPDATE
        if (norm.get("normalized_label") != row_dict.get("normalized_label") or
                norm.get("confidence") != old_confidence or
                norm.get("ticker") != row_dict.get("ticker") or
                norm.get("quarter") != row_dict.get("quarter")):
            conn.execute(
                """UPDATE extracted_facts
                   SET normalized_label=?, confidence=?, ticker=?, quarter=?,
                       metric_name=?
                   WHERE id=?""",
                (
                    norm.get("normalized_label", ""),
                    norm.get("confidence", "medium"),
                    norm.get("ticker", ""),
                    norm.get("quarter", ""),
                    norm.get("metric_name", ""),
                    row_dict["id"],
                ),
            )
            updated += 1

    conn.commit()
    conn.close()

    print(f"\n正規化完了: updated={updated}/{len(rows)}")


if __name__ == "__main__":
    main()
