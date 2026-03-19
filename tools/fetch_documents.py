#!/usr/bin/env python3
# ============================================================
# fetch_documents.py — IR文書のダウンロード
# ============================================================
"""
documents テーブルに登録済みの文書をダウンロードする。

CLI:
  python tools/fetch_documents.py --db decision_db.db
  python tools/fetch_documents.py --doc-type forecast_revision
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _PROJECT_ROOT)

from src.extraction.ir_doc_schema import ensure_tables

logger = logging.getLogger("ir_extraction")

# ダウンロード先ディレクトリ
_DOCS_DIR = os.path.join(_PROJECT_ROOT, "data", "ir_docs")

# ダウンロード間隔 (秒)
_DOWNLOAD_DELAY = 1.0

# User-Agent
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; tdnet-excel-input/1.0)",
}


# ============================================================
# ダウンロード
# ============================================================

def download_document(url: str, save_dir: str) -> str | None:
    """
    URL からファイルをダウンロードし、ローカルパスを返す。
    既にダウンロード済みの場合はそのパスを返す。
    """
    if not url:
        return None

    # ファイル名を URL から生成
    fname = re.sub(r"[^\w.\-]", "_", url.split("/")[-1].split("?")[0])
    if not fname:
        fname = "unknown"
    save_path = os.path.join(save_dir, fname)

    # 既存チェック
    if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
        return save_path

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
        os.makedirs(save_dir, exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(resp.content)
        logger.info(f"  DL OK: {fname} ({len(resp.content)} bytes)")
        return save_path
    except Exception as e:
        logger.warning(f"  DL FAIL: {url} -> {e}")
        return None


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="IR文書ダウンロード")
    parser.add_argument("--db", default="decision_db.db", help="SQLiteパス")
    parser.add_argument("--doc-type", help="対象 doc_type のみ")
    parser.add_argument("--limit", type=int, default=50, help="処理上限")
    parser.add_argument("--output-dir", default=_DOCS_DIR, help="保存先")
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

    # ダウンロード対象を取得
    where = "local_path IS NULL OR local_path = ''"
    params: list = []
    if args.doc_type:
        where += " AND doc_type = ?"
        params.append(args.doc_type)

    rows = conn.execute(
        f"""SELECT id, ticker, url, doc_type, file_type
            FROM documents
            WHERE {where}
            ORDER BY pubdate DESC
            LIMIT ?""",
        params + [args.limit],
    ).fetchall()

    logger.info(f"[FETCH] 対象: {len(rows)} 件")

    downloaded = 0
    for row in rows:
        doc_id = row["id"]
        ticker = row["ticker"]
        url = row["url"]
        doc_type = row["doc_type"]
        file_type = row["file_type"]

        # ファイルタイプに応じたサブディレクトリ
        save_dir = os.path.join(args.output_dir, doc_type)
        local_path = download_document(url, save_dir)

        if local_path:
            conn.execute(
                "UPDATE documents SET local_path=? WHERE id=?",
                (local_path, doc_id),
            )
            conn.commit()
            downloaded += 1

        time.sleep(_DOWNLOAD_DELAY)

    conn.close()
    print(f"\nダウンロード完了: {downloaded}/{len(rows)}")


if __name__ == "__main__":
    main()
