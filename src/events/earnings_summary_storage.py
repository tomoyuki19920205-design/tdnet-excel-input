#!/usr/bin/env python3
"""earnings_summary_storage.py — 決算短信V2 要約テーブルの SQLite 操作

earnings_summaries テーブルの CRUD 操作を提供する。
全件保存、通知のみ条件付きフィルタ。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("earnings_storage")

JST = timezone(timedelta(hours=9))


def _now_jst() -> str:
    return datetime.now(JST).isoformat()


# ============================================================
# テーブル定義
# ============================================================
_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS earnings_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    company_name TEXT,
    fiscal_year TEXT,
    quarter TEXT,
    title TEXT,
    disclosure_date TEXT,
    sales_value REAL,
    sales_yoy REAL,
    op_value REAL,
    op_yoy REAL,
    segment_summary_json TEXT,
    overall_reason_summary TEXT,
    segment_reason_summary TEXT,
    summary_short TEXT,
    summary_full TEXT,
    fingerprint TEXT NOT NULL UNIQUE,
    source_url TEXT,
    archive_path TEXT,
    notified_at TEXT,
    created_at TEXT NOT NULL,
    guidance_sales REAL,
    guidance_op REAL,
    guidance_eps REAL,
    guidance_sales_yoy REAL,
    guidance_op_yoy REAL,
    guidance_eps_yoy REAL,
    outlook_summary TEXT
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_es_ticker ON earnings_summaries(ticker);",
    "CREATE INDEX IF NOT EXISTS idx_es_fiscal ON earnings_summaries(fiscal_year, quarter);",
    "CREATE INDEX IF NOT EXISTS idx_es_disclosure ON earnings_summaries(disclosure_date);",
    "CREATE INDEX IF NOT EXISTS idx_es_fingerprint ON earnings_summaries(fingerprint);",
    "CREATE INDEX IF NOT EXISTS idx_es_notified ON earnings_summaries(notified_at);",
]


# 既存DBでのカラム追加用
_GUIDANCE_COLUMNS = [
    ("guidance_sales", "REAL"),
    ("guidance_op", "REAL"),
    ("guidance_eps", "REAL"),
    ("guidance_sales_yoy", "REAL"),
    ("guidance_op_yoy", "REAL"),
    ("guidance_eps_yoy", "REAL"),
    ("outlook_summary", "TEXT"),
]


def ensure_earnings_summary_table(conn: sqlite3.Connection) -> None:
    """テーブルが存在しなければ作成、既存ならカラム追加"""
    conn.execute(_CREATE_TABLE)
    for idx_sql in _CREATE_INDEXES:
        conn.execute(idx_sql)

    # 既存DB: ガイダンスカラムがなければ追加
    cursor = conn.execute("PRAGMA table_info(earnings_summaries)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    for col_name, col_type in _GUIDANCE_COLUMNS:
        if col_name not in existing_cols:
            conn.execute(
                f"ALTER TABLE earnings_summaries ADD COLUMN {col_name} {col_type}"
            )
            logger.info(f"ALTER TABLE earnings_summaries ADD COLUMN {col_name} {col_type}")

    conn.commit()
    logger.debug("テーブル確認/作成完了")


# ============================================================
# 保存
# ============================================================
_INSERT_COLS = [
    "ticker", "company_name", "fiscal_year", "quarter", "title",
    "disclosure_date", "sales_value", "sales_yoy", "op_value", "op_yoy",
    "segment_summary_json", "overall_reason_summary", "segment_reason_summary",
    "summary_short", "summary_full",
    "fingerprint", "source_url", "archive_path",
    "notified_at", "created_at",
    "guidance_sales", "guidance_op", "guidance_eps",
    "guidance_sales_yoy", "guidance_op_yoy", "guidance_eps_yoy",
    "outlook_summary",
]


def save_earnings_summary(
    conn: sqlite3.Connection,
    data: dict,
) -> str:
    """決算短信要約を保存する。fingerprint 重複時は 'already_exists' を返す。

    Parameters
    ----------
    data : dict
        _INSERT_COLS のキーを含む辞書

    Returns
    -------
    "inserted" | "already_exists"
    """
    fp = data.get("fingerprint", "")

    # fingerprint 重複チェック
    row = conn.execute(
        "SELECT id FROM earnings_summaries WHERE fingerprint = ?",
        (fp,),
    ).fetchone()
    if row is not None:
        logger.debug(f"earnings_summary already exists: fp={fp[:12]}")
        return "already_exists"

    now = _now_jst()
    if not data.get("created_at"):
        data["created_at"] = now

    placeholders = ", ".join("?" for _ in _INSERT_COLS)
    col_names = ", ".join(_INSERT_COLS)
    vals = [data.get(c) for c in _INSERT_COLS]

    conn.execute(
        f"INSERT INTO earnings_summaries ({col_names}) VALUES ({placeholders})",
        vals,
    )
    conn.commit()
    logger.info(
        f"INSERT earnings_summary fp={fp[:12]} ticker={data.get('ticker')}"
    )
    return "inserted"


# ============================================================
# 取得
# ============================================================
def get_earnings_summaries_by_ticker(
    conn: sqlite3.Connection,
    ticker: str,
) -> list[dict]:
    """指定ticker の要約一覧を取得（新しい順）"""
    cursor = conn.execute(
        "SELECT * FROM earnings_summaries WHERE ticker = ? ORDER BY disclosure_date DESC, created_at DESC",
        (ticker,),
    )
    cols = [d[0] for d in cursor.description]
    rows = cursor.fetchall()
    return [dict(zip(cols, row)) for row in rows]


def get_unnotified_earnings_summaries(
    conn: sqlite3.Connection,
) -> list[dict]:
    """未通知の要約を取得"""
    cursor = conn.execute(
        "SELECT * FROM earnings_summaries WHERE notified_at IS NULL ORDER BY created_at ASC"
    )
    cols = [d[0] for d in cursor.description]
    rows = cursor.fetchall()
    return [dict(zip(cols, row)) for row in rows]


def mark_earnings_notified(
    conn: sqlite3.Connection,
    fingerprint: str,
) -> None:
    """通知済みマーク"""
    now = _now_jst()
    conn.execute(
        "UPDATE earnings_summaries SET notified_at = ? WHERE fingerprint = ?",
        (now, fingerprint),
    )
    conn.commit()


# ============================================================
# 通知条件判定
# ============================================================
def should_notify_earnings(sales_yoy: float | None, op_yoy: float | None) -> bool:
    """通知条件: sales_yoy >= 25% or op_yoy >= 25% (内部実値で判定)

    全件保存、通知のみ条件付き。
    YOYがNoneの場合はもう一方で判定。両方Noneは通知しない。
    """
    return (
        (sales_yoy is not None and sales_yoy >= 0.25)
        or (op_yoy is not None and op_yoy >= 0.25)
    )
