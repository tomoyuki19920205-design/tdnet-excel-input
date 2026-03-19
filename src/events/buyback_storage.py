#!/usr/bin/env python3
"""buyback_storage.py — 自社株買いイベントの SQLite 保存"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

from .buyback_models import BuybackEvent

logger = logging.getLogger("buyback_storage")

JST = timezone(timedelta(hours=9))

# ============================================================
# テーブル定義
# ============================================================
_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS buyback_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    disclosure_date TEXT,
    event_type TEXT NOT NULL,
    title TEXT,
    source_type TEXT,
    source_path TEXT,
    source_doc_id TEXT,
    source_url TEXT,
    raw_text_hash TEXT,
    shares_limit INTEGER,
    shares_acquired INTEGER,
    shares_cancelled INTEGER,
    amount_limit_million_yen REAL,
    amount_acquired_million_yen REAL,
    ratio_to_outstanding REAL,
    start_date TEXT,
    end_date TEXT,
    cancel_date TEXT,
    acquisition_method TEXT,
    board_resolution_date TEXT,
    status_period_label TEXT,
    status_notes TEXT,
    extracted_json TEXT,
    extraction_confidence REAL,
    extractor_version TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_CREATE_INDEX = """\
CREATE INDEX IF NOT EXISTS idx_buyback_ticker_date
ON buyback_events(ticker, disclosure_date);
"""


def ensure_buyback_table(conn: sqlite3.Connection) -> None:
    """buyback_events テーブルが存在しなければ作成する。"""
    conn.execute(_CREATE_TABLE)
    conn.execute(_CREATE_INDEX)
    conn.commit()
    logger.debug("buyback_events テーブル確認/作成完了")


# ============================================================
# 重複チェック
# ============================================================
def _find_existing_id(conn: sqlite3.Connection, event: BuybackEvent) -> Optional[int]:
    """既存レコードの id を探す。

    優先順位:
    1. source_doc_id 一致
    2. (ticker, disclosure_date, event_type, raw_text_hash) 一致
    """
    if event.source_doc_id:
        row = conn.execute(
            "SELECT id FROM buyback_events WHERE source_doc_id = ?",
            (event.source_doc_id,),
        ).fetchone()
        if row:
            return row[0]

    row = conn.execute(
        """SELECT id FROM buyback_events
           WHERE ticker = ? AND disclosure_date = ?
             AND event_type = ? AND raw_text_hash = ?""",
        (event.ticker, event.disclosure_date, event.event_type, event.raw_text_hash),
    ).fetchone()
    if row:
        return row[0]

    return None


# ============================================================
# UPSERT
# ============================================================
_COLUMNS = [
    "ticker", "disclosure_date", "event_type", "title",
    "source_type", "source_path", "source_doc_id", "source_url",
    "raw_text_hash",
    "shares_limit", "shares_acquired", "shares_cancelled",
    "amount_limit_million_yen", "amount_acquired_million_yen",
    "ratio_to_outstanding",
    "start_date", "end_date", "cancel_date",
    "acquisition_method", "board_resolution_date",
    "status_period_label", "status_notes",
    "extracted_json",
    "extraction_confidence", "extractor_version",
    "created_at", "updated_at",
]


def _event_to_values(event: BuybackEvent, now: str) -> dict:
    """BuybackEvent → INSERT/UPDATE 用辞書"""
    return {
        "ticker": event.ticker,
        "disclosure_date": event.disclosure_date,
        "event_type": event.event_type,
        "title": event.title,
        "source_type": event.source_type,
        "source_path": event.source_path,
        "source_doc_id": event.source_doc_id,
        "source_url": event.source_url,
        "raw_text_hash": event.raw_text_hash,
        "shares_limit": event.shares_limit,
        "shares_acquired": event.shares_acquired,
        "shares_cancelled": event.shares_cancelled,
        "amount_limit_million_yen": event.amount_limit_million_yen,
        "amount_acquired_million_yen": event.amount_acquired_million_yen,
        "ratio_to_outstanding": event.ratio_to_outstanding,
        "start_date": event.start_date,
        "end_date": event.end_date,
        "cancel_date": event.cancel_date,
        "acquisition_method": event.acquisition_method,
        "board_resolution_date": event.board_resolution_date,
        "status_period_label": event.status_period_label,
        "status_notes": event.status_notes,
        "extracted_json": event.extracted_json,
        "extraction_confidence": event.extraction_confidence,
        "extractor_version": event.extractor_version,
        "created_at": now,
        "updated_at": now,
    }


def upsert_buyback_event(conn: sqlite3.Connection, event: BuybackEvent) -> int:
    """BuybackEvent を保存する。既存レコードがあれば UPDATE、なければ INSERT。

    Returns
    -------
    int
        保存されたレコードの id
    """
    now = datetime.now(JST).isoformat()
    existing_id = _find_existing_id(conn, event)

    if existing_id is not None:
        # UPDATE（created_at は変えない）
        sets = ", ".join(f"{c} = ?" for c in _COLUMNS if c != "created_at")
        vals = _event_to_values(event, now)
        params = [vals[c] for c in _COLUMNS if c != "created_at"]
        params.append(existing_id)
        conn.execute(
            f"UPDATE buyback_events SET {sets} WHERE id = ?",
            params,
        )
        conn.commit()
        logger.info(f"UPDATE buyback_events id={existing_id} ticker={event.ticker}")
        return existing_id
    else:
        # INSERT
        placeholders = ", ".join("?" for _ in _COLUMNS)
        col_names = ", ".join(_COLUMNS)
        vals = _event_to_values(event, now)
        params = [vals[c] for c in _COLUMNS]
        cursor = conn.execute(
            f"INSERT INTO buyback_events ({col_names}) VALUES ({placeholders})",
            params,
        )
        conn.commit()
        new_id = cursor.lastrowid
        logger.info(f"INSERT buyback_events id={new_id} ticker={event.ticker}")
        return new_id
