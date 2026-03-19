#!/usr/bin/env python3
# ============================================================
# classify_documents.py — IR文書のタイプ分類
# ============================================================
"""
タイトルベースで開示資料の doc_type と file_type を判定する。

CLI:
  python tools/classify_documents.py --db decision_db.db
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

from src.extraction.ir_doc_schema import ensure_tables, insert_document

logger = logging.getLogger("ir_extraction")


# ============================================================
# 文書タイプ分類
# ============================================================

_DOC_TYPE_RULES: list[tuple[list[str], str]] = [
    # 予想修正（earnings より先に判定）
    (["業績予想の修正", "配当予想の修正", "業績予想及び", "配当の修正",
      "業績・配当予想の修正", "修正に関するお知らせ"], "forecast_revision"),
    # 月次
    (["月次", "月商", "月別", "月間"], "monthly"),
    # 決算説明資料（earnings より先に判定）
    (["決算説明資料", "決算説明会資料", "決算説明会", "説明会資料"], "presentation"),
    # 決算短信
    (["決算短信", "四半期決算", "中間決算"], "earnings"),
    # KPI
    (["KPI", "経営指標", "主要指標"], "kpi"),
    # セグメント
    (["セグメント情報", "セグメント別", "事業別"], "segment"),
    # 補足資料
    (["補足資料", "補足説明", "補足情報", "参考資料"], "supplement"),
]


def classify_doc_type(title: str) -> str:
    """タイトルからドキュメントタイプを判定する"""
    if not title:
        return "other"
    for keywords, doc_type in _DOC_TYPE_RULES:
        for kw in keywords:
            if kw in title:
                return doc_type
    return "other"


def classify_file_type(url: str) -> str:
    """URLの拡張子からファイルタイプを判定する"""
    if not url:
        return "other"
    url_lower = url.lower().split("?")[0]
    if url_lower.endswith(".pdf"):
        return "pdf"
    if url_lower.endswith((".htm", ".html")):
        return "html"
    if url_lower.endswith((".xbrl", ".xml")):
        return "xbrl"
    if url_lower.endswith(".zip"):
        return "xbrl"  # TDNETのZIPは通常XBRL
    return "other"


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="IR文書分類")
    parser.add_argument("--db", default="decision_db.db", help="SQLiteパス")
    parser.add_argument("--limit", type=int, default=100, help="処理上限")
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

    # quarterly_results から未分類の文書を取得
    rows = conn.execute(
        """SELECT DISTINCT company_code, source_url, title, updated_at
           FROM quarterly_results
           WHERE source_url IS NOT NULL AND source_url != ''
           ORDER BY updated_at DESC
           LIMIT ?""",
        (args.limit,),
    ).fetchall()

    stats = {"total": len(rows), "classified": 0, "skipped": 0}
    for row in rows:
        ticker = row["company_code"]
        url = row["source_url"] or ""
        title = row["title"] or ""
        pubdate = row["updated_at"] or ""

        doc_type = classify_doc_type(title)
        file_type = classify_file_type(url)

        doc_id = insert_document(
            conn, ticker=ticker, pubdate=pubdate, title=title,
            doc_type=doc_type, file_type=file_type, url=url,
        )
        if doc_id:
            stats["classified"] += 1
        else:
            stats["skipped"] += 1

        logger.debug(f"  {ticker}: {doc_type}/{file_type} | {title[:40]}")

    conn.close()

    print(f"\n分類完了: total={stats['total']} classified={stats['classified']} "
          f"skipped={stats['skipped']}")


if __name__ == "__main__":
    main()
