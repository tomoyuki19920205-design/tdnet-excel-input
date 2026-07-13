"""lib/backfill/state_store.py — バックフィル状態管理 (SQLite)

filing 単位の処理状態を管理する。resume / retry / quarantine の基盤。
Step 3: resume, stale_running, mark_upserted。
Step 4: mark_needs_pdf, list_needs_pdf, Phase 2 対応。
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("backfill.state")
JST = timezone(timedelta(hours=9))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS filing_state (
    filing_id         TEXT PRIMARY KEY,
    ticker            TEXT NOT NULL,
    disclosure_date   TEXT,
    doc_type          TEXT,
    title             TEXT,
    period            TEXT,
    quarter           TEXT,
    has_xbrl          BOOLEAN DEFAULT 0,
    has_pdf           BOOLEAN DEFAULT 1,
    status            TEXT NOT NULL DEFAULT 'queued',
    stage             TEXT NOT NULL DEFAULT 'listing',
    attempt_count     INTEGER DEFAULT 0,
    last_error        TEXT,
    last_error_stage  TEXT,
    source_url        TEXT,
    xbrl_url          TEXT,
    doc_path          TEXT,
    xbrl_path         TEXT,
    cache_dir         TEXT,
    via               TEXT,
    segment_count     INTEGER DEFAULT 0,
    listing_source    TEXT,
    result_fingerprint TEXT,
    review_hint       TEXT,
    retryable         BOOLEAN DEFAULT 1,
    last_attempt_at   TEXT,
    first_seen_at     TEXT,
    last_seen_at      TEXT,
    started_at        TEXT,
    finished_at       TEXT,
    duration_ms       INTEGER DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS ix_filing_state_status
    ON filing_state(status);
CREATE INDEX IF NOT EXISTS ix_filing_state_ticker
    ON filing_state(ticker);
CREATE INDEX IF NOT EXISTS ix_filing_state_date
    ON filing_state(disclosure_date);
"""


class BackfillStateStore:
    """バックフィル状態を SQLite で管理する。"""

    def __init__(self, db_path: str = "data/backfill_state.db") -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        """明示的トランザクション。"""
        self.conn.execute("BEGIN")
        try:
            yield
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ================================================================
    # Register
    # ================================================================

    def register_filings(self, filings: list) -> dict[str, int]:
        """filing を state store に登録 (既存は listing_source / last_seen_at のみ更新)。"""
        now = _now_iso()
        new_count = 0
        existing_count = 0

        for f in filings:
            existing = self.conn.execute(
                "SELECT filing_id FROM filing_state WHERE filing_id = ?",
                (f.filing_id,),
            ).fetchone()

            if existing:
                self.conn.execute(
                    "UPDATE filing_state SET last_seen_at = ? WHERE filing_id = ?",
                    (now, f.filing_id),
                )
                existing_count += 1
            else:
                self.conn.execute(
                    """INSERT INTO filing_state (
                        filing_id, ticker, disclosure_date, doc_type, title,
                        has_xbrl, source_url, xbrl_url, listing_source,
                        first_seen_at, last_seen_at, status, stage
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'listing')""",
                    (
                        f.filing_id, f.ticker, f.disclosure_date,
                        f.doc_type, f.title, f.has_xbrl,
                        f.doc_url, f.xbrl_url, f.listing_source,
                        now, now,
                    ),
                )
                new_count += 1

        self.conn.commit()
        logger.info(f"[state] registered: new={new_count} existing={existing_count}")
        return {"new": new_count, "existing": existing_count}

    # ================================================================
    # Query
    # ================================================================

    def get_pending(
        self,
        *,
        limit: int = 0,
        tickers: list[str] | None = None,
        statuses: list[str] | None = None,
    ) -> list[dict]:
        """処理待ちの filing を取得する。limit=0 で全件。"""
        statuses = statuses or ["queued"]
        placeholders = ",".join("?" * len(statuses))
        sql = f"""
            SELECT * FROM filing_state
            WHERE status IN ({placeholders})
        """
        params: list[Any] = list(statuses)

        if tickers:
            ticker_ph = ",".join("?" * len(tickers))
            sql += f" AND ticker IN ({ticker_ph})"
            params.extend(tickers)

        sql += " ORDER BY disclosure_date ASC, ticker ASC"
        if limit and limit > 0:
            sql += f" LIMIT {limit}"

        rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_pending_for_filing_ids(
        self,
        filing_ids: list[str] | set[str] | tuple[str, ...],
        *,
        limit: int = 0,
        tickers: list[str] | None = None,
        statuses: list[str] | None = None,
    ) -> list[dict]:
        """Return pending rows restricted to an explicit filing-ID set.

        The filing-ID restriction is applied by SQLite, not by filtering an
        unscoped pending result in Python. IDs are chunked for large manifests.
        """
        unique_ids = list(dict.fromkeys(str(fid) for fid in filing_ids if str(fid)))
        if not unique_ids:
            return []

        statuses = statuses or ["queued"]
        rows_by_id: dict[str, dict] = {}
        id_chunk_size = 500
        for offset in range(0, len(unique_ids), id_chunk_size):
            id_chunk = unique_ids[offset:offset + id_chunk_size]
            status_ph = ",".join("?" * len(statuses))
            id_ph = ",".join("?" * len(id_chunk))
            sql = f"""
                SELECT * FROM filing_state
                WHERE status IN ({status_ph})
                  AND filing_id IN ({id_ph})
            """
            params: list[Any] = [*statuses, *id_chunk]
            if tickers:
                ticker_ph = ",".join("?" * len(tickers))
                sql += f" AND ticker IN ({ticker_ph})"
                params.extend(tickers)
            for row in self.conn.execute(sql, params).fetchall():
                row_dict = dict(row)
                rows_by_id[row_dict["filing_id"]] = row_dict

        rows = sorted(
            rows_by_id.values(),
            key=lambda row: (row.get("disclosure_date") or "", row.get("ticker") or ""),
        )
        if limit and limit > 0:
            rows = rows[:limit]
        return rows

    def get_resume_candidates(
        self,
        *,
        limit: int = 0,
        tickers: list[str] | None = None,
        include_quarantined: bool = False,
        include_failed: bool = False,
        include_needs_pdf: bool = True,
        include_done_extracted: bool = True,
    ) -> list[dict]:
        """resume 対象の filing を取得する。

        include_done_extracted=True: done/extracted (upsert 前にクラッシュした残骸) を含む。
        """
        statuses = ["queued", "running"]
        if include_needs_pdf:
            statuses.append("needs_pdf")
        if include_quarantined:
            statuses.append("quarantined")
        if include_failed:
            statuses.append("failed")
        if include_done_extracted:
            statuses.append("done")

        return self.get_pending(
            limit=limit, tickers=tickers, statuses=statuses,
        )

    def get_done_extracted(
        self,
        *,
        limit: int = 0,
        tickers: list[str] | None = None,
    ) -> list[dict]:
        """status='done' and stage='extracted' の filing を取得する (repair 用)。"""
        return self.get_pending(
            limit=limit, tickers=tickers, statuses=["done"],
        )

    def reset_done_to_queued(self) -> int:
        """done/extracted を queued に戻す (再抽出用)。"""
        self.conn.execute(
            """UPDATE filing_state
               SET status = 'queued', stage = 'listing'
               WHERE status = 'done' AND stage = 'extracted'""",
        )
        count = self.conn.execute("SELECT changes()").fetchone()[0]
        self.conn.commit()
        if count > 0:
            logger.info(f"[state] reset_done_to_queued: {count} rows")
        return count

    def reset_stale_running(self, max_age_hours: int = 2) -> int:
        """異常終了で running のまま残った行を queued に戻す。

        Returns:
            reset した行数
        """
        cutoff = datetime.now(JST) - timedelta(hours=max_age_hours)
        self.conn.execute(
            """UPDATE filing_state
               SET status = 'queued', stage = 'listing'
               WHERE status = 'running'
                 AND last_attempt_at < ?""",
            (cutoff.isoformat(),),
        )
        count = self.conn.execute("SELECT changes()").fetchone()[0]
        self.conn.commit()
        if count > 0:
            logger.info(f"[state] reset_stale_running: {count} rows (>{max_age_hours}h)")
        return count

    def list_by_status(self, status: str, limit: int = 100) -> list[dict]:
        """指定 status の行を返す。"""
        rows = self.conn.execute(
            "SELECT * FROM filing_state WHERE status = ? LIMIT ?",
            (status, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # ================================================================
    # Status Update
    # ================================================================

    def update_status(
        self,
        filing_id: str,
        status: str,
        *,
        stage: str | None = None,
        error: str | None = None,
        error_stage: str | None = None,
        **kwargs: Any,
    ) -> None:
        """状態を更新する。"""
        parts = ["status = ?"]
        params: list[Any] = [status]

        if stage:
            parts.append("stage = ?")
            params.append(stage)

        if status == "running":
            parts.append("started_at = ?")
            params.append(_now_iso())
            parts.append("attempt_count = attempt_count + 1")
            parts.append("last_attempt_at = ?")
            params.append(_now_iso())

        if error:
            parts.append("last_error = ?")
            params.append(error[:2000])
        if error_stage:
            parts.append("last_error_stage = ?")
            params.append(error_stage)

        for key, value in kwargs.items():
            parts.append(f"{key} = ?")
            params.append(value)

        params.append(filing_id)
        sql = f"UPDATE filing_state SET {', '.join(parts)} WHERE filing_id = ?"
        self.conn.execute(sql, params)

    def mark_done(
        self,
        filing_id: str,
        *,
        via: str | None = None,
        segment_count: int = 0,
        result_fingerprint: str | None = None,
        duration_ms: int = 0,
        **kwargs: Any,
    ) -> None:
        """抽出完了 (upsert 前)。"""
        now = _now_iso()
        extra = {
            "via": via,
            "segment_count": segment_count,
            "finished_at": now,
            "duration_ms": duration_ms,
        }
        if result_fingerprint:
            extra["result_fingerprint"] = result_fingerprint
        extra.update(kwargs)
        self.update_status(filing_id, "done", stage="extracted", **extra)

    def mark_upserted(self, filing_id: str) -> None:
        """DB upsert 完了。done → upserted に昇格。"""
        self.update_status(filing_id, "upserted", stage="completed")

    def mark_needs_pdf(
        self,
        filing_id: str,
        *,
        review_hint: str = "needs_pdf",
    ) -> None:
        """XBRL で segment が取れず PDF fallback が必要。正常な中間状態。"""
        extra: dict[str, Any] = {"review_hint": review_hint}
        self.update_status(filing_id, "needs_pdf", stage="needs_pdf", **extra)

    def list_needs_pdf(self, limit: int = 500) -> list[dict]:
        """needs_pdf 状態の filing を返す。"""
        return self.list_by_status("needs_pdf", limit=limit)

    def mark_quarantined(
        self,
        filing_id: str,
        *,
        error: str = "",
        stage: str = "unknown",
        review_hint: str | None = None,
        retryable: bool = True,
    ) -> None:
        """quarantine にする。"""
        extra: dict[str, Any] = {"finished_at": _now_iso()}
        if review_hint:
            extra["review_hint"] = review_hint
        extra["retryable"] = retryable

        self.update_status(
            filing_id, "quarantined",
            stage=stage, error=error, error_stage=stage,
            **extra,
        )

    def update_review_hint(self, filing_id: str, review_hint: str) -> None:
        """review_hint のみを更新する (status は変えない)。

        retry 後に still_quarantined の案件に新 hint を永続反映するために使う。
        """
        self.conn.execute(
            "UPDATE filing_state SET review_hint = ? WHERE filing_id = ?",
            (review_hint, filing_id),
        )
        self.conn.commit()

    def mark_failed(
        self,
        filing_id: str,
        *,
        error: str = "",
        stage: str = "unknown",
    ) -> None:
        """失敗にする。"""
        self.update_status(
            filing_id, "failed",
            stage=stage, error=error, error_stage=stage,
            finished_at=_now_iso(),
        )

    def reset_for_retry(
        self,
        filing_id: str | None = None,
        *,
        statuses: list[str] | None = None,
    ) -> int:
        """retry 対象を queued に戻す。"""
        if filing_id:
            self.conn.execute(
                "UPDATE filing_state SET status='queued', stage='listing' "
                "WHERE filing_id = ? AND retryable = 1",
                (filing_id,),
            )
        elif statuses:
            placeholders = ",".join("?" * len(statuses))
            self.conn.execute(
                f"UPDATE filing_state SET status='queued', stage='listing' "
                f"WHERE status IN ({placeholders}) AND retryable = 1",
                statuses,
            )
        else:
            return 0

        count = self.conn.execute("SELECT changes()").fetchone()[0]
        self.conn.commit()
        logger.info(f"[state] reset_for_retry: {count} rows")
        return count

    def reset_filing(self, filing_id: str) -> bool:
        """指定 filing を強制的に queued に戻す (retryable 制約なし)。

        --reset-target / 固定母集団テストで使用。
        Returns:
            True if row was updated.
        """
        self.conn.execute(
            "UPDATE filing_state SET status='queued', stage='listing', "
            "attempt_count=0, last_error=NULL, last_error_stage=NULL, "
            "review_hint=NULL "
            "WHERE filing_id = ?",
            (filing_id,),
        )
        count = self.conn.execute("SELECT changes()").fetchone()[0]
        self.conn.commit()
        return count > 0

    def requeue_single_filing(
        self,
        filing_id: str,
        *,
        expected_status: str = "quarantined",
        expected_stage: str | None = None,
        expected_error: str | None = None,
    ) -> dict[str, dict]:
        """Atomically requeue one exact filing while preserving audit history."""
        with self.transaction():
            before_row = self.conn.execute(
                "SELECT * FROM filing_state WHERE filing_id = ?",
                (filing_id,),
            ).fetchone()
            if before_row is None:
                raise RuntimeError("STOP_REQUEUE_SOURCE_STATE_CHANGED: state row not found")
            before = dict(before_row)
            if before.get("status") != expected_status:
                raise RuntimeError(
                    "STOP_REQUEUE_SOURCE_STATE_CHANGED: "
                    f"expected status={expected_status}, actual={before.get('status')}"
                )
            if expected_stage is not None and before.get("stage") != expected_stage:
                raise RuntimeError(
                    "STOP_REQUEUE_SOURCE_STATE_CHANGED: "
                    f"expected stage={expected_stage}, actual={before.get('stage')}"
                )
            if expected_error is not None and before.get("last_error") != expected_error:
                raise RuntimeError(
                    "STOP_REQUEUE_SOURCE_STATE_CHANGED: "
                    f"expected error={expected_error}, actual={before.get('last_error')}"
                )

            cursor = self.conn.execute(
                "UPDATE filing_state SET status='queued', stage='listing', "
                "last_error=NULL, last_error_stage=NULL, review_hint=NULL, "
                "started_at=NULL, finished_at=NULL, duration_ms=NULL "
                "WHERE filing_id = ? AND status = ?",
                (filing_id, expected_status),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "STOP_REQUEUE_SOURCE_STATE_CHANGED: "
                    f"expected one updated row, actual={cursor.rowcount}"
                )

            after_row = self.conn.execute(
                "SELECT * FROM filing_state WHERE filing_id = ?",
                (filing_id,),
            ).fetchone()
            if after_row is None:
                raise RuntimeError("STOP_REQUEUE_SOURCE_STATE_CHANGED: readback missing")
            after = dict(after_row)
            expected_cleared = (
                "last_error", "last_error_stage", "review_hint",
                "started_at", "finished_at", "duration_ms",
            )
            if after.get("status") != "queued" or after.get("stage") != "listing":
                raise RuntimeError("STOP_REQUEUE_SOURCE_STATE_CHANGED: readback status mismatch")
            if any(after.get(field) is not None for field in expected_cleared):
                raise RuntimeError("STOP_REQUEUE_SOURCE_STATE_CHANGED: readback clear mismatch")
            if after.get("attempt_count") != before.get("attempt_count"):
                raise RuntimeError("STOP_REQUEUE_SOURCE_STATE_CHANGED: attempt_count changed")

        logger.info(
            "[state] requeue_single_filing: filing_id=%s before=%s after=%s "
            "changed=%s attempt_count=%s",
            filing_id,
            before.get("status"),
            after.get("status"),
            [
                "status", "stage", "last_error", "last_error_stage", "review_hint",
                "started_at", "finished_at", "duration_ms",
            ],
            after.get("attempt_count"),
        )
        return {"before": before, "after": after}

    # ================================================================
    # Stats
    # ================================================================

    def stats(self) -> dict[str, int]:
        """status 別件数を返す。"""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM filing_state GROUP BY status"
        ).fetchall()
        result = {row["status"]: row["cnt"] for row in rows}
        result["total"] = sum(result.values())
        return result

    def count_by_listing_source(self) -> dict[str, int]:
        """listing_source 別件数。"""
        rows = self.conn.execute(
            "SELECT listing_source, COUNT(*) as cnt "
            "FROM filing_state GROUP BY listing_source"
        ).fetchall()
        return {(row["listing_source"] or "unknown"): row["cnt"] for row in rows}


def _now_iso() -> str:
    return datetime.now(JST).isoformat()
