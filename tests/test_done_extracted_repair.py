"""tests/test_done_extracted_repair.py -- done/extracted 残骸修復テスト"""
from __future__ import annotations

import sqlite3
import tempfile
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.backfill.state_store import BackfillStateStore


def _make_store_with_done(n_done: int = 10, n_upserted: int = 20, n_queued: int = 5) -> BackfillStateStore:
    """done/extracted + upserted + queued の混在 state store を作る。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    store = BackfillStateStore(tmp.name)
    i = 0
    for _ in range(n_done):
        store.conn.execute(
            "INSERT INTO filing_state (filing_id, ticker, status, stage, disclosure_date) "
            "VALUES (?, ?, 'done', 'extracted', '2025-01-01')",
            (f"F{i:05d}", f"{1000 + i % 50}"),
        )
        i += 1
    for _ in range(n_upserted):
        store.conn.execute(
            "INSERT INTO filing_state (filing_id, ticker, status, stage, disclosure_date) "
            "VALUES (?, ?, 'upserted', 'completed', '2025-01-01')",
            (f"F{i:05d}", f"{1000 + i % 50}"),
        )
        i += 1
    for _ in range(n_queued):
        store.conn.execute(
            "INSERT INTO filing_state (filing_id, ticker, status, stage, disclosure_date) "
            "VALUES (?, ?, 'queued', 'listing', '2025-01-01')",
            (f"F{i:05d}", f"{1000 + i % 50}"),
        )
        i += 1
    store.conn.commit()
    return store


class TestDoneExtractedResume:
    """done/extracted が resume で拾われる。"""

    def test_resume_includes_done_by_default(self):
        store = _make_store_with_done(n_done=10, n_queued=5)
        cands = store.get_resume_candidates()
        statuses = set(c["status"] for c in cands)
        assert "done" in statuses
        assert len(cands) == 15  # 10 done + 5 queued
        store.close()

    def test_resume_excludes_upserted(self):
        store = _make_store_with_done(n_done=10, n_upserted=20, n_queued=0)
        cands = store.get_resume_candidates()
        statuses = set(c["status"] for c in cands)
        assert "upserted" not in statuses
        assert len(cands) == 10  # only done
        store.close()

    def test_resume_can_exclude_done(self):
        store = _make_store_with_done(n_done=10, n_queued=5)
        cands = store.get_resume_candidates(include_done_extracted=False)
        statuses = set(c["status"] for c in cands)
        assert "done" not in statuses
        assert len(cands) == 5  # only queued
        store.close()


class TestResetDoneToQueued:
    """reset_done_to_queued で done/extracted が queued に戻る。"""

    def test_reset_count(self):
        store = _make_store_with_done(n_done=10, n_upserted=20)
        count = store.reset_done_to_queued()
        assert count == 10
        store.close()

    def test_after_reset_all_queued(self):
        store = _make_store_with_done(n_done=10, n_upserted=20, n_queued=5)
        store.reset_done_to_queued()
        pending = store.get_pending()
        assert len(pending) == 15  # 10 reset + 5 original
        for p in pending:
            assert p["status"] == "queued"
        store.close()

    def test_reset_does_not_touch_upserted(self):
        store = _make_store_with_done(n_done=10, n_upserted=20)
        store.reset_done_to_queued()
        upserted = store.list_by_status("upserted")
        assert len(upserted) == 20
        store.close()

    def test_idempotent(self):
        store = _make_store_with_done(n_done=10)
        store.reset_done_to_queued()
        count2 = store.reset_done_to_queued()
        assert count2 == 0
        store.close()


class TestGetDoneExtracted:
    """get_done_extracted 専用メソッド。"""

    def test_returns_only_done(self):
        store = _make_store_with_done(n_done=10, n_upserted=20, n_queued=5)
        done = store.get_done_extracted()
        assert len(done) == 10
        for d in done:
            assert d["status"] == "done"
        store.close()

    def test_empty_when_no_done(self):
        store = _make_store_with_done(n_done=0, n_upserted=20, n_queued=5)
        done = store.get_done_extracted()
        assert len(done) == 0
        store.close()
