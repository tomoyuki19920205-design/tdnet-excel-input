#!/usr/bin/env python3
# ============================================================
# extract_pdf.py — PDF形式IR文書からのテーブル数値抽出
# ============================================================
"""
テキスト PDF から表データを抽出する (OCR不要)。

対象 doc_type: presentation, supplement, segment, kpi

CLI:
  python tools/extract_pdf.py --db decision_db.db
  python tools/extract_pdf.py --doc-type presentation --limit 5

依存: pdfplumber (pip install pdfplumber)
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
from src.extraction.ir_doc_schema import ensure_tables, insert_facts

logger = logging.getLogger("ir_extraction")

# extract_html から共通ユーティリティを再利用
from tools.extract_html import (
    detect_unit,
    match_metric_name,
    parse_number,
)


# ============================================================
# PDF → facts 抽出 (抽出ロジック)
# ============================================================

def extract_facts_from_pdf(
    pdf_path: str,
    ticker: str,
    period: str = "",
    quarter: str = "",
    document_id: int | None = None,
) -> list[dict]:
    """
    PDF ファイルからテーブルを解析し、facts のリストを返す。
    保存は行わない（純粋な抽出ロジック）。

    pdfplumber が未インストールの場合は空リストを返す。
    """
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber is not installed. Skipping PDF extraction.")
        return []

    facts: list[dict] = []

    try:
        pdf = pdfplumber.open(pdf_path)
    except Exception as e:
        logger.warning(f"  PDF open failed: {pdf_path} -> {e}")
        return []

    for page_no, page in enumerate(pdf.pages, 1):
        page_text = page.extract_text() or ""

        # ページの単位検出
        unit_name, unit_mult = detect_unit(page_text)

        # テーブル抽出
        tables = page.extract_tables() or []

        for table_idx, table in enumerate(tables):
            if not table or len(table) < 2:
                continue

            # テーブルタイトル: テーブルの直前のテキスト行を推定
            table_title = _detect_table_title(page_text, table)

            # テーブル固有の単位検出
            table_text = " ".join(
                " ".join(str(c) for c in row if c) for row in table
            )
            t_unit, t_mult = detect_unit(table_text)
            if t_unit != "円":
                curr_unit, curr_mult = t_unit, t_mult
            else:
                curr_unit, curr_mult = unit_name, unit_mult

            # 各行を処理
            for row_idx, row in enumerate(table):
                if not row or len(row) < 2:
                    continue

                raw_label = str(row[0] or "").strip()
                metric_name = match_metric_name(raw_label)
                if not metric_name:
                    continue

                for col_idx, cell in enumerate(row[1:], 1):
                    val = parse_number(str(cell or ""))
                    if val is None:
                        continue

                    # 金額系メトリックなら単位変換
                    if metric_name not in ("eps", "same_store_sales_yoy",
                                            "utilization_rate", "arpu"):
                        val *= curr_mult

                    facts.append({
                        "document_id": document_id,
                        "ticker": normalize_ticker(ticker),
                        "period": period,
                        "quarter": quarter,
                        "metric_name": metric_name,
                        "metric_value": val,
                        "unit": "円" if metric_name not in (
                            "eps", "same_store_sales_yoy",
                            "utilization_rate",
                        ) else curr_unit,
                        "segment_name": "",
                        "source_type": "pdf",
                        "confidence": "medium",
                        "page_no": page_no,
                        "table_title": table_title[:80] if table_title else "",
                        "raw_label": raw_label[:200],
                        "normalized_label": metric_name,
                    })

    pdf.close()
    return facts


def _detect_table_title(page_text: str, table: list) -> str:
    """テーブルの最初の行のテキストを含む行の直前行をタイトルとして推定"""
    if not table or not table[0]:
        return ""

    first_cell = str(table[0][0] or "")
    if not first_cell:
        return ""

    lines = page_text.split("\n")
    for i, line in enumerate(lines):
        if first_cell[:10] in line and i > 0:
            return lines[i - 1].strip()
    return ""


# ============================================================
# CLI — 保存ロジック
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="PDF形式IR文書から数値抽出")
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

    # PDF文書を取得
    where = "file_type = 'pdf' AND local_path IS NOT NULL AND local_path != ''"
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

    logger.info(f"[PDF] 対象: {len(rows)} 件")

    total_facts = 0
    for row in rows:
        doc_id = row["id"]
        ticker = row["ticker"]
        local_path = row["local_path"]

        if not os.path.exists(local_path):
            logger.warning(f"  {ticker}: file not found: {local_path}")
            continue

        facts = extract_facts_from_pdf(
            local_path, ticker=ticker, document_id=doc_id,
        )

        if facts:
            inserted = insert_facts(conn, facts)
            total_facts += inserted
            logger.info(f"  {ticker}: {inserted} facts extracted")
        else:
            logger.debug(f"  {ticker}: no facts found")

    conn.close()
    print(f"\nPDF抽出完了: {total_facts} facts from {len(rows)} documents")


if __name__ == "__main__":
    main()
