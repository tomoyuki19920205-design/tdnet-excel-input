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

_CREATE_LOCK_TABLE = """
CREATE TABLE IF NOT EXISTS process_locks (
    lock_id TEXT PRIMARY KEY,
    process_name TEXT UNIQUE NOT NULL,
    pid INTEGER,
    status TEXT NOT NULL,
    started_at TEXT,
    heartbeat_at TEXT,
    released_at TEXT,
    stale_after_sec INTEGER,
    current_step TEXT,
    processed_count INTEGER DEFAULT 0,
    total_candidates INTEGER DEFAULT 0
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
        self.ensure_process_locks_table()
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

    # --- Lock management ---
    def ensure_process_locks_table(self) -> None:
        self._conn.execute(_CREATE_LOCK_TABLE)
        self._conn.commit()

    def get_active_process_lock(self, process_name: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM process_locks WHERE process_name = ? AND status = 'running'",
            (process_name,)
        )
        row = cur.fetchone()
        if row:
            cols = [desc[0] for desc in cur.description]
            return dict(zip(cols, row))
        return None

    def acquire_process_lock(self, lock_id: str, process_name: str, pid: int, stale_after_sec: int, total_candidates: int = 0) -> bool:
        now = now_jst_str()
        try:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO process_locks
                (lock_id, process_name, pid, status, started_at, heartbeat_at, released_at, stale_after_sec, current_step, processed_count, total_candidates)
                VALUES (?, ?, ?, 'running', ?, ?, NULL, ?, '', 0, ?)
                """,
                (lock_id, process_name, pid, now, now, stale_after_sec, total_candidates)
            )
            self._conn.commit()
            return True
        except Exception:
            return False

    def update_process_lock_heartbeat(self, process_name: str, processed_count: int = 0, current_step: str = "", total_candidates: int | None = None) -> None:
        now = now_jst_str()
        if total_candidates is not None:
            self._conn.execute(
                """
                UPDATE process_locks
                SET heartbeat_at = ?, processed_count = ?, current_step = ?, total_candidates = ?
                WHERE process_name = ? AND status = 'running'
                """,
                (now, processed_count, current_step, total_candidates, process_name)
            )
        else:
            self._conn.execute(
                """
                UPDATE process_locks
                SET heartbeat_at = ?, processed_count = ?, current_step = ?
                WHERE process_name = ? AND status = 'running'
                """,
                (now, processed_count, current_step, process_name)
            )
        self._conn.commit()

    def release_process_lock(self, process_name: str, status: str = "completed") -> None:
        now = now_jst_str()
        self._conn.execute(
            """
            UPDATE process_locks
            SET status = ?, released_at = ?
            WHERE process_name = ? AND status = 'running'
            """,
            (status, now, process_name)
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
