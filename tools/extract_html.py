#!/usr/bin/env python3
# ============================================================
# extract_html.py — HTML形式IR文書からのテーブル数値抽出
# ============================================================
"""
HTML 形式の開示資料から表データを抽出する。

対象 doc_type: forecast_revision, monthly, kpi, supplement

CLI:
  python tools/extract_html.py --db decision_db.db
  python tools/extract_html.py --doc-type forecast_revision --limit 5
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
from io import StringIO
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _PROJECT_ROOT)

from src.common_ticker import normalize_ticker
from src.extraction.ir_doc_schema import ensure_tables, insert_facts

logger = logging.getLogger("ir_extraction")


# ============================================================
# 単位検出
# ============================================================

_UNIT_PATTERNS = [
    (re.compile(r"[（(]単位[：:]?\s*(百万円|億円|千円|円)[）)]"), None),
    (re.compile(r"単位[：:]\s*(百万円|億円|千円|円)"), None),
    (re.compile(r"[（(](百万円|億円|千円)[）)]"), None),
]

_UNIT_MULTIPLIER = {
    "百万円": 1_000_000,
    "億円": 100_000_000,
    "千円": 1_000,
    "円": 1,
}


def detect_unit(text: str) -> tuple[str, int]:
    """テキストから単位を検出し、(単位名, 乗数) を返す"""
    for pat, _ in _UNIT_PATTERNS:
        m = pat.search(text)
        if m:
            unit = m.group(1)
            return unit, _UNIT_MULTIPLIER.get(unit, 1)
    return "円", 1


# ============================================================
# メトリック名のパターンマッチ
# ============================================================

_METRIC_PATTERNS: list[tuple[str, list[str]]] = [
    ("sales", ["売上高", "売上", "営業収益", "売上収益"]),
    ("operating_profit", ["営業利益", "営業損益"]),
    ("ordinary_profit", ["経常利益", "経常損益"]),
    ("net_income", ["当期純利益", "親会社株主に帰属する当期純利益",
                     "四半期純利益", "当期利益"]),
    ("eps", ["1株当たり当期純利益", "EPS", "1株当たり四半期純利益"]),
    ("forecast_sales", ["売上高（予想）", "予想売上高", "売上高予想"]),
    ("forecast_operating_profit", ["営業利益（予想）", "予想営業利益", "営業利益予想"]),
    ("monthly_sales", ["月次売上", "月商"]),
    ("same_store_sales_yoy", ["既存店売上", "既存店前年比"]),
    ("customer_count", ["客数", "来客数", "来店客数"]),
    ("average_spend", ["客単価", "単価"]),
    ("arpu", ["ARPU"]),
    ("store_count", ["店舗数", "出店数"]),
    ("utilization_rate", ["稼働率"]),
    ("orders", ["受注高", "受注額", "受注"]),
    ("order_backlog", ["受注残高", "受注残"]),
    ("gross_profit", ["売上総利益", "粗利益"]),
]


def match_metric_name(label: str) -> str | None:
    """ラベルからメトリック名を推定する"""
    for metric, keywords in _METRIC_PATTERNS:
        for kw in keywords:
            if kw in label:
                return metric
    return None


# ============================================================
# 数値パース
# ============================================================

_NUM_RE = re.compile(r"[△▲\-]?\s*[\d,]+(?:\.\d+)?")


def parse_number(text: str) -> float | None:
    """テキストから数値を抽出する"""
    if not text or not text.strip():
        return None
    text = text.strip()
    text = text.replace("△", "-").replace("▲", "-")
    text = text.replace(",", "").replace("，", "").replace(" ", "")
    # ハイフンのみ（未定等）
    if text in ("-", "－", "―", "—", "–"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ============================================================
# HTML → facts 抽出 (抽出ロジック)
# ============================================================

def extract_facts_from_html(
    html_text: str,
    ticker: str,
    period: str = "",
    quarter: str = "",
    document_id: int | None = None,
) -> list[dict]:
    """
    HTML テキストからテーブルを解析し、facts のリストを返す。
    保存は行わない（純粋な抽出ロジック）。
    """
    facts: list[dict] = []
    soup = BeautifulSoup(html_text, "html.parser")
    full_text = soup.get_text()

    # 全体の単位を検出
    default_unit, default_mult = detect_unit(full_text)

    # pandas で table を抽出
    try:
        dfs = pd.read_html(StringIO(html_text), header=None)
    except Exception:
        dfs = []

    # table 要素を取得してタイトルを検出
    tables = soup.find_all("table")

    for i, df in enumerate(dfs):
        # テーブルタイトルを検出
        table_title = ""
        if i < len(tables):
            table_elem = tables[i]
            # 直前の兄弟要素からタイトルを取得
            prev = table_elem.find_previous_sibling(["p", "div", "h1", "h2", "h3", "h4", "h5"])
            if prev:
                table_title = prev.get_text(strip=True)[:80]

        # テーブル固有の単位チェック
        table_text = df.to_string()
        t_unit, t_mult = detect_unit(table_text)
        if t_unit != "円":
            unit, mult = t_unit, t_mult
        else:
            unit, mult = default_unit, default_mult

        row_found = False

        # === パターン A: 行ラベルにメトリック名 ===
        for _, row_data in df.iterrows():
            row_values = [str(v) for v in row_data.values]
            if not row_values:
                continue

            raw_label = row_values[0] if row_values else ""
            metric_name = match_metric_name(raw_label)

            if metric_name is None:
                continue

            row_found = True
            for col_idx, cell_val in enumerate(row_values[1:], 1):
                val = parse_number(cell_val)
                if val is None:
                    continue

                if metric_name not in ("eps", "same_store_sales_yoy",
                                        "utilization_rate", "arpu"):
                    val *= mult

                facts.append(_make_fact(
                    document_id, ticker, period, quarter,
                    metric_name, val, unit, mult, table_title, raw_label,
                ))

        # === パターン B: 列ヘッダーにメトリック名（横向きテーブル） ===
        if not row_found:
            col_metrics: dict[int, tuple[str, str]] = {}

            # (B-1) DataFrame のカラム名を走査（pandas が <th> をカラム名にした場合）
            col_names = [str(c) for c in df.columns]
            for col_idx, col_name in enumerate(col_names):
                mn = match_metric_name(col_name)
                if mn:
                    col_metrics[col_idx] = (mn, col_name)

            # (B-2) カラム名に無ければ、最初の1-2行をヘッダーとして走査
            if not col_metrics and len(df) >= 2:
                for header_row_idx in range(min(2, len(df))):
                    header_vals = [str(v) for v in df.iloc[header_row_idx].values]
                    for col_idx, cell in enumerate(header_vals):
                        mn = match_metric_name(cell)
                        if mn and col_idx not in col_metrics:
                            col_metrics[col_idx] = (mn, cell)

            if col_metrics:
                # カラム名ベースの場合は全行がデータ行
                # 行ベースの場合はヘッダー行の次からデータ行
                start_row = 0
                if not any(match_metric_name(str(c)) for c in df.columns):
                    # ヘッダーがデータ行にいる場合、最後のヘッダー行の次から
                    for hr in range(min(2, len(df))):
                        if any(match_metric_name(str(df.iloc[hr].values[c]))
                               for c in col_metrics):
                            start_row = hr + 1

                for row_idx in range(start_row, len(df)):
                    row_vals = [str(v) for v in df.iloc[row_idx].values]
                    row_label = row_vals[0] if row_vals else ""
                    for col_idx, (metric_name, raw_lbl) in col_metrics.items():
                        if col_idx >= len(row_vals):
                            continue
                        val = parse_number(row_vals[col_idx])
                        if val is None:
                            continue

                        if metric_name not in ("eps", "same_store_sales_yoy",
                                                "utilization_rate", "arpu"):
                            val *= mult

                        facts.append(_make_fact(
                            document_id, ticker, period, quarter,
                            metric_name, val, unit, mult, table_title,
                            f"{row_label}:{raw_lbl}",
                        ))

    return facts


def _make_fact(
    document_id, ticker, period, quarter,
    metric_name, val, unit, mult, table_title, raw_label,
) -> dict:
    """fact dict を生成する共通ヘルパー"""
    return {
        "document_id": document_id,
        "ticker": normalize_ticker(ticker),
        "period": period,
        "quarter": quarter,
        "metric_name": metric_name,
        "metric_value": val,
        "unit": "円" if metric_name not in ("eps", "same_store_sales_yoy",
                                             "utilization_rate") else unit,
        "segment_name": "",
        "source_type": "html",
        "confidence": "medium",
        "page_no": None,
        "table_title": table_title,
        "raw_label": str(raw_label)[:200],
        "normalized_label": metric_name,
    }


# ============================================================
# CLI — 保存ロジック
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="HTML形式IR文書から数値抽出")
    parser.add_argument("--db", default="decision_db.db", help="SQLiteパス")
    parser.add_argument("--doc-type", help="対象 doc_type のみ")
    parser.add_argument("--limit", type=int, default=50, help="処理上限")
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

    # HTML文書を取得
    where = "file_type = 'html' AND local_path IS NOT NULL AND local_path != ''"
    params: list = []
    if args.doc_type:
        where += " AND doc_type = ?"
        params.append(args.doc_type)

    rows = conn.execute(
        f"""SELECT id, ticker, pubdate, local_path, doc_type
            FROM documents
            WHERE {where}
            ORDER BY pubdate DESC LIMIT ?""",
        params + [args.limit],
    ).fetchall()

    logger.info(f"[HTML] 対象: {len(rows)} 件")

    total_facts = 0
    for row in rows:
        doc_id = row["id"]
        ticker = row["ticker"]
        local_path = row["local_path"]

        if not os.path.exists(local_path):
            logger.warning(f"  {ticker}: file not found: {local_path}")
            continue

        with open(local_path, "r", encoding="utf-8", errors="replace") as f:
            html_text = f.read()

        facts = extract_facts_from_html(
            html_text, ticker=ticker, document_id=doc_id,
        )

        if facts:
            inserted = insert_facts(conn, facts)
            total_facts += inserted
            logger.info(f"  {ticker}: {inserted} facts extracted")
        else:
            logger.debug(f"  {ticker}: no facts found")

    conn.close()
    print(f"\nHTML抽出完了: {total_facts} facts from {len(rows)} documents")


if __name__ == "__main__":
    main()
