#!/usr/bin/env python3
# ============================================================
# ir_doc_schema.py — IR文書 / 抽出ファクト DBスキーマ
# ============================================================
"""
documents / extracted_facts テーブルの作成・管理。
既存の decision_db.db に追加する補完テーブル。
"""
from __future__ import annotations

import sqlite3

from ..persist_policy import should_persist_intermediates

# ============================================================
# documents テーブル
# ============================================================

_CREATE_DOCUMENTS_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    pubdate TEXT,
    title TEXT,
    doc_type TEXT NOT NULL,
    file_type TEXT NOT NULL,
    url TEXT,
    local_path TEXT,
    tdnet_doc_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

_DOCUMENTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_documents_ticker ON documents(ticker);",
    "CREATE INDEX IF NOT EXISTS idx_documents_doc_type ON documents(doc_type);",
    "CREATE INDEX IF NOT EXISTS idx_documents_pubdate ON documents(pubdate);",
    # UNIQUE(ticker, url): 同一 ticker + 同一 URL の重複登録を防止
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_ticker_url "
    "  ON documents(ticker, url);",
]

# ============================================================
# extracted_facts テーブル
# ============================================================

_CREATE_EXTRACTED_FACTS_SQL = """
CREATE TABLE IF NOT EXISTS extracted_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER REFERENCES documents(id),
    ticker TEXT NOT NULL,
    period TEXT,
    quarter TEXT,
    metric_name TEXT NOT NULL,
    metric_value REAL,
    unit TEXT,
    segment_name TEXT,
    source_type TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'medium',
    page_no INTEGER,
    table_title TEXT,
    raw_label TEXT,
    normalized_label TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

_EXTRACTED_FACTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ef_ticker ON extracted_facts(ticker);",
    "CREATE INDEX IF NOT EXISTS idx_ef_metric ON extracted_facts(metric_name);",
    "CREATE INDEX IF NOT EXISTS idx_ef_document_id ON extracted_facts(document_id);",
    # UNIQUE: document_id + metric_name + period + quarter + segment_name + raw_label
    # raw_label を含めることで、同一テーブル内の異なる行ラベルの重複を許容
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_ef_doc_metric_period "
    "  ON extracted_facts(document_id, metric_name, period, quarter, "
    "  segment_name, raw_label);",
]


# ============================================================
# API
# ============================================================

def ensure_tables(conn: sqlite3.Connection):
    """テーブルとインデックスが無ければ作成する"""
    conn.execute(_CREATE_DOCUMENTS_SQL)
    for idx_sql in _DOCUMENTS_INDEXES:
        conn.execute(idx_sql)
    conn.execute(_CREATE_EXTRACTED_FACTS_SQL)
    for idx_sql in _EXTRACTED_FACTS_INDEXES:
        conn.execute(idx_sql)
    conn.commit()


def insert_document(
    conn: sqlite3.Connection,
    ticker: str,
    pubdate: str,
    title: str,
    doc_type: str,
    file_type: str,
    url: str = "",
    local_path: str = "",
    tdnet_doc_id: str = "",
) -> int | None:
    """
    documents に INSERT（重複時は無視して既存IDを返す）。
    Returns: document id or None
    """
    try:
        cur = conn.execute(
            """INSERT INTO documents
               (ticker, pubdate, title, doc_type, file_type, url, local_path, tdnet_doc_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker, pubdate, title, doc_type, file_type, url, local_path, tdnet_doc_id),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        # 重複 → 既存IDを返す
        row = conn.execute(
            "SELECT id FROM documents WHERE ticker=? AND url=?",
            (ticker, url),
        ).fetchone()
        return row[0] if row else None


def insert_facts(
    conn: sqlite3.Connection,
    facts: list[dict],
) -> int:
    """
    extracted_facts に一括 INSERT（重複は無視）。
    Returns: 挿入された行数
    """
    inserted = 0
    if not should_persist_intermediates():
        return inserted
    for f in facts:
        try:
            conn.execute(
                """INSERT INTO extracted_facts
                   (document_id, ticker, period, quarter, metric_name,
                    metric_value, unit, segment_name, source_type,
                    confidence, page_no, table_title, raw_label, normalized_label)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f.get("document_id"),
                    f.get("ticker", ""),
                    f.get("period", ""),
                    f.get("quarter", ""),
                    f.get("metric_name", ""),
                    f.get("metric_value"),
                    f.get("unit", ""),
                    f.get("segment_name", ""),
                    f.get("source_type", ""),
                    f.get("confidence", "medium"),
                    f.get("page_no"),
                    f.get("table_title", ""),
                    f.get("raw_label", ""),
                    f.get("normalized_label", ""),
                ),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            pass  # 重複スキップ
    conn.commit()
    return inserted
