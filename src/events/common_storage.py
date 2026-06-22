#!/usr/bin/env python3
"""common_storage.py — イベント共通テーブルの SQLite 操作"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

from .common_models import EventRecord

logger = logging.getLogger("event_storage")

JST = timezone(timedelta(hours=9))


def _now_jst() -> str:
    return datetime.now(JST).isoformat()


# ============================================================
# テーブル定義
# ============================================================
_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    source_doc_id TEXT NOT NULL,
    ticker TEXT,
    company_name TEXT,
    disclosure_datetime TEXT,
    title TEXT,
    event_type TEXT NOT NULL,
    subtype TEXT,
    importance INTEGER DEFAULT 50,
    summary_text TEXT,
    raw_payload_json TEXT,
    extracted_payload_json TEXT,
    fingerprint TEXT NOT NULL UNIQUE,
    status TEXT DEFAULT 'new',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    notified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    doc_url TEXT
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_events_ticker ON events(ticker);",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);",
    "CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);",
    "CREATE INDEX IF NOT EXISTS idx_events_notified ON events(notified_at);",
    "CREATE INDEX IF NOT EXISTS idx_events_fp ON events(fingerprint);",
]


def ensure_events_table(conn: sqlite3.Connection) -> None:
    """events テーブルが存在しなければ作成する"""
    conn.execute(_CREATE_TABLE)
    for idx_sql in _CREATE_INDEXES:
        conn.execute(idx_sql)
    conn.commit()
    
    # Existing DB migration: safely add doc_url if it doesn't exist
    try:
        conn.execute("ALTER TABLE events ADD COLUMN doc_url TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass
        
    logger.debug("events テーブル確認/作成完了")


# ============================================================
# UPSERT — fingerprint ベース
# ============================================================
_INSERT_COLS = [
    "event_id", "source_doc_id", "ticker", "company_name",
    "disclosure_datetime", "title", "event_type", "subtype",
    "importance", "summary_text", "raw_payload_json",
    "extracted_payload_json", "fingerprint", "status",
    "first_seen_at", "last_seen_at", "notified_at",
    "created_at", "updated_at", "doc_url",
]


def upsert_event(
    conn: sqlite3.Connection,
    event: EventRecord,
) -> tuple[str, str]:
    """EventRecord を保存する。fingerprint で重複検知。

    Returns
    -------
    (action, event_id)
        action = "inserted" | "updated" | "no_change"
    """
    now = _now_jst()

    # fingerprint で既存チェック
    row = conn.execute(
        "SELECT id, event_id, extracted_payload_json FROM events WHERE fingerprint = ?",
        (event.fingerprint,),
    ).fetchone()

    if row is not None:
        existing_id = row[0]
        existing_event_id = row[1]
        # 内容が同一なら no_change
        if row[2] == event.extracted_payload_json:
            conn.execute(
                "UPDATE events SET last_seen_at = ?, updated_at = ? WHERE id = ?",
                (now, now, existing_id),
            )
            conn.commit()
            logger.debug(f"no_change events id={existing_id} fp={event.fingerprint[:12]}")
            return "no_change", existing_event_id
        # 内容が異なるなら update
        conn.execute(
            """UPDATE events SET
                source_doc_id=?, ticker=?, company_name=?,
                disclosure_datetime=?, title=?, event_type=?, subtype=?,
                importance=?, summary_text=?, raw_payload_json=?,
                extracted_payload_json=?, doc_url=?,
                last_seen_at=?, updated_at=?
            WHERE id = ?""",
            (
                event.source_doc_id, event.ticker, event.company_name,
                event.disclosure_datetime, event.title, event.event_type, event.subtype,
                event.importance, event.summary_text, event.raw_payload_json,
                event.extracted_payload_json, event.doc_url,
                now, now, existing_id,
            ),
        )
        conn.commit()
        logger.info(f"UPDATE events id={existing_id} fp={event.fingerprint[:12]}")
        return "updated", existing_event_id

    # INSERT
    event.first_seen_at = now
    event.last_seen_at = now
    event.created_at = now
    event.updated_at = now
    event.status = "new"

    placeholders = ", ".join("?" for _ in _INSERT_COLS)
    col_names = ", ".join(_INSERT_COLS)
    vals = [getattr(event, c) for c in _INSERT_COLS]
    conn.execute(
        f"INSERT INTO events ({col_names}) VALUES ({placeholders})",
        vals,
    )
    conn.commit()
    logger.info(
        f"INSERT events event_id={event.event_id[:12]} "
        f"type={event.event_type} ticker={event.ticker} fp={event.fingerprint[:12]}"
    )
    return "inserted", event.event_id


# ============================================================
# 未通知イベント取得
# ============================================================
def get_unnotified_events(
    conn: sqlite3.Connection,
    event_type: str | None = None,
    since: str | None = None,
) -> list[EventRecord]:
    """notified_at が NULL かつ status='new' or 'filtered' のイベントを取得。

    filtered も含めることで、ルール変更時に再評価が可能。
    通知判定は呼び出し元 (notify_rules) で行う。
    """
    sql = "SELECT * FROM events WHERE notified_at IS NULL AND status IN ('new', 'filtered')"
    params: list = []
    if event_type:
        sql += " AND event_type = ?"
        params.append(event_type)
    if since:
        sql += " AND first_seen_at >= ?"
        params.append(since)
    sql += " ORDER BY importance DESC, first_seen_at ASC"

    cursor = conn.execute(sql, params)
    cols = [d[0] for d in cursor.description]
    rows = cursor.fetchall()

    result = []
    for row in rows:
        d = dict(zip(cols, row))
        result.append(EventRecord(**{k: v for k, v in d.items() if k in EventRecord.__dataclass_fields__}))
    return result


def mark_notified(conn: sqlite3.Connection, event_id: str) -> None:
    """通知済みマーク"""
    now = _now_jst()
    conn.execute(
        "UPDATE events SET notified_at = ?, status = 'notified', updated_at = ? WHERE event_id = ?",
        (now, now, event_id),
    )
    conn.commit()


def mark_filtered(conn: sqlite3.Connection, event_id: str) -> None:
    """非通知対象マーク (ルール変更時に再評価可能)"""
    now = _now_jst()
    conn.execute(
        "UPDATE events SET status = 'filtered', updated_at = ? WHERE event_id = ? AND status != 'notified'",
        (now, event_id),
    )
    conn.commit()


def mark_skipped(conn: sqlite3.Connection, event_id: str) -> None:
    """通知条件外としてスキップマーク"""
    now = _now_jst()
    conn.execute(
        "UPDATE events SET status = 'skipped', updated_at = ? WHERE event_id = ? AND status != 'notified'",
        (now, event_id),
    )
    conn.commit()


def mark_discord_send_failed(conn: sqlite3.Connection, event_id: str) -> None:
    """Discord送信失敗 (要マニュアルレビュー) マーク"""
    now = _now_jst()
    conn.execute(
        "UPDATE events SET status = 'discord_send_failed_manual_review', updated_at = ? WHERE event_id = ? AND status != 'notified'",
        (now, event_id),
    )
    conn.commit()


# ============================================================
# リスト取得
# ============================================================
def list_events(
    conn: sqlite3.Connection,
    event_type: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[EventRecord]:
    """イベント一覧取得"""
    sql = "SELECT * FROM events WHERE 1=1"
    params: list = []
    if event_type:
        sql += " AND event_type = ?"
        params.append(event_type)
    if since:
        sql += " AND first_seen_at >= ?"
        params.append(since)
    sql += " ORDER BY first_seen_at DESC LIMIT ?"
    params.append(limit)

    cursor = conn.execute(sql, params)
    cols = [d[0] for d in cursor.description]
    rows = cursor.fetchall()

    result = []
    for row in rows:
        d = dict(zip(cols, row))
        result.append(EventRecord(**{k: v for k, v in d.items() if k in EventRecord.__dataclass_fields__}))
    return result
