#!/usr/bin/env python3
"""summary_storage.py — AI要約テーブルの SQLite 操作

summary_jobs テーブルと ai_summaries テーブルの CRUD 操作を提供する。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

from .summary_models import SummaryJob, AISummary, JobStatus

logger = logging.getLogger("summary_storage")

JST = timezone(timedelta(hours=9))


def _now_jst() -> str:
    return datetime.now(JST).isoformat()


# ============================================================
# テーブル定義
# ============================================================
_CREATE_SUMMARY_JOBS = """\
CREATE TABLE IF NOT EXISTS summary_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    ticker TEXT,
    company_name TEXT,
    title TEXT,
    event_type TEXT,
    subtype TEXT,
    priority TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    error_msg TEXT,
    extracted_payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_CREATE_AI_SUMMARIES = """\
CREATE TABLE IF NOT EXISTS ai_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_id TEXT NOT NULL UNIQUE,
    doc_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    ticker TEXT,
    company_name TEXT,
    title TEXT,
    priority TEXT,
    summary_type TEXT NOT NULL DEFAULT 'flash',
    prompt_version TEXT NOT NULL DEFAULT 'v1.0',
    headline TEXT,
    bullet_1 TEXT,
    bullet_2 TEXT,
    bullet_3 TEXT,
    tone TEXT,
    needs_review INTEGER DEFAULT 0,
    model_used TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    notified_at TEXT,
    created_at TEXT NOT NULL
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_sjobs_fingerprint ON summary_jobs(fingerprint);",
    "CREATE INDEX IF NOT EXISTS idx_sjobs_status ON summary_jobs(status);",
    "CREATE INDEX IF NOT EXISTS idx_sjobs_priority ON summary_jobs(priority);",
    "CREATE INDEX IF NOT EXISTS idx_aisummaries_fingerprint ON ai_summaries(fingerprint);",
    "CREATE INDEX IF NOT EXISTS idx_aisummaries_ticker ON ai_summaries(ticker);",
    "CREATE INDEX IF NOT EXISTS idx_aisummaries_created ON ai_summaries(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_aisummaries_notified ON ai_summaries(notified_at);",
]


def ensure_summary_tables(conn: sqlite3.Connection) -> None:
    """summary_jobs / ai_summaries テーブルが存在しなければ作成する"""
    conn.execute(_CREATE_SUMMARY_JOBS)
    conn.execute(_CREATE_AI_SUMMARIES)
    for idx_sql in _CREATE_INDEXES:
        conn.execute(idx_sql)
    conn.commit()
    logger.debug("summary_jobs / ai_summaries テーブル確認/作成完了")


# ============================================================
# summary_jobs 操作
# ============================================================
def insert_summary_job(
    conn: sqlite3.Connection,
    job: SummaryJob,
) -> str:
    """SummaryJob を挿入する。fingerprint 重複時は 'already_exists' を返す。

    Returns
    -------
    "inserted" | "already_exists"
    """
    now = _now_jst()

    # fingerprint で既存チェック
    row = conn.execute(
        "SELECT id FROM summary_jobs WHERE fingerprint = ?",
        (job.fingerprint,),
    ).fetchone()
    if row is not None:
        logger.info(
            f"summary_job skip_reason=already_summarized fp={job.fingerprint[:12]} "
            f"ticker={job.ticker}"
        )
        return "already_exists"

    job.created_at = now
    job.updated_at = now

    conn.execute(
        """INSERT INTO summary_jobs
           (doc_id, fingerprint, ticker, company_name, title,
            event_type, subtype, priority, status, retry_count,
            error_msg, extracted_payload_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job.doc_id, job.fingerprint, job.ticker, job.company_name, job.title,
            job.event_type, job.subtype, job.priority, job.status, job.retry_count,
            job.error_msg, job.extracted_payload_json, job.created_at, job.updated_at,
        ),
    )
    conn.commit()
    logger.info(f"INSERT summary_job fp={job.fingerprint[:12]} priority={job.priority}")
    return "inserted"


def get_pending_jobs(
    conn: sqlite3.Connection,
    priority: str | None = None,
    exclude_low: bool = True,
) -> list[SummaryJob]:
    """pending ステータスのジョブを優先度順に取得"""
    sql = "SELECT * FROM summary_jobs WHERE status = 'pending'"
    params: list = []
    if priority:
        sql += " AND priority = ?"
        params.append(priority)
    elif exclude_low:
        sql += " AND priority != 'low'"
    sql += """ ORDER BY
        CASE priority
            WHEN 'high' THEN 0
            WHEN 'normal' THEN 1
            WHEN 'low' THEN 2
            ELSE 3
        END ASC,
        created_at ASC"""

    cursor = conn.execute(sql, params)
    cols = [d[0] for d in cursor.description]
    rows = cursor.fetchall()

    result = []
    for row in rows:
        d = dict(zip(cols, row))
        result.append(SummaryJob(**{k: v for k, v in d.items() if k in SummaryJob.__dataclass_fields__}))
    return result


def update_job_status(
    conn: sqlite3.Connection,
    fingerprint: str,
    status: str,
    error_msg: str = "",
    increment_retry: bool = False,
) -> None:
    """ジョブステータスを更新する"""
    now = _now_jst()
    if increment_retry:
        conn.execute(
            """UPDATE summary_jobs
               SET status = ?, error_msg = ?, retry_count = retry_count + 1, updated_at = ?
               WHERE fingerprint = ?""",
            (status, error_msg, now, fingerprint),
        )
    else:
        conn.execute(
            "UPDATE summary_jobs SET status = ?, error_msg = ?, updated_at = ? WHERE fingerprint = ?",
            (status, error_msg, now, fingerprint),
        )
    conn.commit()


def get_job_retry_count(conn: sqlite3.Connection, fingerprint: str) -> int:
    """ジョブのリトライ回数を取得"""
    row = conn.execute(
        "SELECT retry_count FROM summary_jobs WHERE fingerprint = ?",
        (fingerprint,),
    ).fetchone()
    return row[0] if row else 0


# ============================================================
# ai_summaries 操作
# ============================================================
def save_ai_summary(
    conn: sqlite3.Connection,
    summary: AISummary,
) -> str:
    """AISummary を保存する。fingerprint 重複時は 'already_exists' を返す。

    Returns
    -------
    "inserted" | "already_exists"
    """
    now = _now_jst()

    row = conn.execute(
        "SELECT id FROM ai_summaries WHERE fingerprint = ?",
        (summary.fingerprint,),
    ).fetchone()
    if row is not None:
        logger.debug(f"ai_summary already exists: fp={summary.fingerprint[:12]}")
        return "already_exists"

    summary.created_at = now

    conn.execute(
        """INSERT INTO ai_summaries
           (summary_id, doc_id, fingerprint, ticker, company_name, title,
            priority, summary_type, prompt_version,
            headline, bullet_1, bullet_2, bullet_3,
            tone, needs_review,
            model_used, input_tokens, output_tokens,
            notified_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            summary.summary_id, summary.doc_id, summary.fingerprint,
            summary.ticker, summary.company_name, summary.title,
            summary.priority, summary.summary_type, summary.prompt_version,
            summary.headline, summary.bullet_1, summary.bullet_2, summary.bullet_3,
            summary.tone, 1 if summary.needs_review else 0,
            summary.model_used, summary.input_tokens, summary.output_tokens,
            summary.notified_at, summary.created_at,
        ),
    )
    conn.commit()
    logger.info(
        f"INSERT ai_summary fp={summary.fingerprint[:12]} "
        f"model={summary.model_used} tokens={summary.input_tokens}+{summary.output_tokens}"
    )
    return "inserted"


def get_summary_by_fingerprint(
    conn: sqlite3.Connection,
    fingerprint: str,
) -> Optional[AISummary]:
    """fingerprint で要約を取得"""
    cursor = conn.execute(
        "SELECT * FROM ai_summaries WHERE fingerprint = ?",
        (fingerprint,),
    )
    cols = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    if not row:
        return None
    d = dict(zip(cols, row))
    # needs_review を bool に変換
    d["needs_review"] = bool(d.get("needs_review", 0))
    return AISummary(**{k: v for k, v in d.items() if k in AISummary.__dataclass_fields__})


def get_unnotified_summaries(
    conn: sqlite3.Connection,
) -> list[AISummary]:
    """未通知の要約を取得（優先度順）"""
    cursor = conn.execute(
        """SELECT * FROM ai_summaries
           WHERE notified_at IS NULL
           ORDER BY
               CASE priority
                   WHEN 'high' THEN 0
                   WHEN 'normal' THEN 1
                   WHEN 'low' THEN 2
                   ELSE 3
               END ASC,
               created_at ASC"""
    )
    cols = [d[0] for d in cursor.description]
    rows = cursor.fetchall()

    result = []
    for row in rows:
        d = dict(zip(cols, row))
        d["needs_review"] = bool(d.get("needs_review", 0))
        result.append(AISummary(**{k: v for k, v in d.items() if k in AISummary.__dataclass_fields__}))
    return result


def mark_summary_notified(
    conn: sqlite3.Connection,
    summary_id: str,
) -> None:
    """要約を通知済みにマーク"""
    now = _now_jst()
    conn.execute(
        "UPDATE ai_summaries SET notified_at = ? WHERE summary_id = ?",
        (now, summary_id),
    )
    conn.commit()


# ============================================================
# コスト集計用クエリ
# ============================================================
def get_daily_token_stats(
    conn: sqlite3.Connection,
    target_date: str | None = None,
) -> list[dict]:
    """日次のトークン使用量統計を取得

    Returns
    -------
    [{"date": "2026-03-20", "model_used": "gpt-5.4-mini",
      "count": 15, "total_input_tokens": 12000, "total_output_tokens": 3000}, ...]
    """
    if target_date:
        date_filter = f"AND created_at LIKE '{target_date}%'"
    else:
        date_filter = ""

    cursor = conn.execute(
        f"""SELECT
               SUBSTR(created_at, 1, 10) AS date,
               model_used,
               COUNT(*) AS count,
               SUM(input_tokens) AS total_input_tokens,
               SUM(output_tokens) AS total_output_tokens
           FROM ai_summaries
           WHERE 1=1 {date_filter}
           GROUP BY date, model_used
           ORDER BY date DESC, model_used"""
    )
    cols = [d[0] for d in cursor.description]
    rows = cursor.fetchall()
    return [dict(zip(cols, row)) for row in rows]
