# ============================================================
# db.py — SQLite冪等性管理
# ============================================================
from __future__ import annotations

import sqlite3
from pathlib import Path

from .utils import now_jst_str

# テーブル作成SQL
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS processing_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    disclosure_id TEXT UNIQUE NOT NULL,
    code TEXT NOT NULL,
    year TEXT NOT NULL,
    quarter TEXT NOT NULL,
    start_row INTEGER,
    term_row INTEGER,
    target_row INTEGER,
    old_sales TEXT,
    old_gross_profit TEXT,
    old_operating_profit TEXT,
    new_sales TEXT,
    new_gross_profit TEXT,
    new_operating_profit TEXT,
    status TEXT NOT NULL,
    error_detail TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);
"""


class StateDB:
    """SQLiteによる処理状態管理（冪等性保証）"""

    def __init__(self, db_path: str):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_TABLE)
        self._conn.commit()

    # retryable skip ステータス一覧
    # これらのステータスで記録された開示は「処理済み」とは見なさず、
    # 次回 ingest で再試行される。
    _RETRYABLE_STATUSES = frozenset([
        "skipped_not_tanshin",  # Status.SKIPPED_NOT_TANSHIN
    ])

    def is_processed(self, disclosure_id: str) -> bool:
        """同一disclosure_idが既に処理済みかどうか。
        retryable skip ステータスの場合は False を返す（再処理対象）。
        """
        cur = self._conn.execute(
            "SELECT status FROM processing_log WHERE disclosure_id = ?",
            (disclosure_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        # retryable skip は未処理扱い
        return row[0] not in self._RETRYABLE_STATUSES

    def record(
        self,
        disclosure_id: str,
        code: str,
        year: str,
        quarter: str,
        status: str,
        start_row: int | None = None,
        term_row: int | None = None,
        target_row: int | None = None,
        old_values: dict | None = None,
        new_values: dict | None = None,
        error_detail: str = "",
    ) -> None:
        """処理結果をDBに記録する"""
        old = old_values or {}
        new = new_values or {}
        now = now_jst_str()

        self._conn.execute(
            """
            INSERT OR REPLACE INTO processing_log
            (disclosure_id, code, year, quarter,
             start_row, term_row, target_row,
             old_sales, old_gross_profit, old_operating_profit,
             new_sales, new_gross_profit, new_operating_profit,
             status, error_detail, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                disclosure_id, code, year, quarter,
                start_row, term_row, target_row,
                str(old.get("sales", "")),
                str(old.get("gross_profit", "")),
                str(old.get("operating_profit", "")),
                str(new.get("sales", "")),
                str(new.get("gross_profit", "")),
                str(new.get("operating_profit", "")),
                status, error_detail, now, now,
            ),
        )
        self._conn.commit()

    def get_log(self, disclosure_id: str) -> dict | None:
        """disclosure_idの処理ログを取得"""
        cur = self._conn.execute(
            "SELECT * FROM processing_log WHERE disclosure_id = ?",
            (disclosure_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))

    def close(self) -> None:
        self._conn.close()
