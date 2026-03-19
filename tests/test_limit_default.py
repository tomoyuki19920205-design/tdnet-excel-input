"""tests/test_limit_default.py -- limit=None でフル実行になることの確認"""
from __future__ import annotations

import sqlite3
import tempfile
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.backfill.state_store import BackfillStateStore


def _make_state_store(n_filings: int) -> BackfillStateStore:
    """n_filings 件の queued filing を持つ一時 state store を作る。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    store = BackfillStateStore(tmp.name)
    for i in range(n_filings):
        store.conn.execute(
            "INSERT INTO filing_state (filing_id, ticker, status, disclosure_date) "
            "VALUES (?, ?, 'queued', '2025-01-01')",
            (f"F{i:05d}", f"{1000 + i % 50}"),
        )
    store.conn.commit()
    return store


class TestLimitDefault:
    """state_store のデフォルト limit が unlimited になっていること。"""

    def test_get_pending_default_returns_all(self):
        """limit 未指定 (default=0) → 全件返す。"""
        store = _make_state_store(300)
        pending = store.get_pending()
        assert len(pending) == 300
        store.close()

    def test_get_pending_limit_100(self):
        """limit=100 → 100件のみ。"""
        store = _make_state_store(300)
        pending = store.get_pending(limit=100)
        assert len(pending) == 100
        store.close()

    def test_get_pending_limit_0_returns_all(self):
        """limit=0 → 全件。"""
        store = _make_state_store(300)
        pending = store.get_pending(limit=0)
        assert len(pending) == 300
        store.close()

    def test_get_resume_candidates_default_returns_all(self):
        """resume candidates でも default=0 → 全件。"""
        store = _make_state_store(300)
        cands = store.get_resume_candidates()
        assert len(cands) == 300
        store.close()

    def test_get_resume_candidates_limit_50(self):
        """resume candidates limit=50 → 50件。"""
        store = _make_state_store(300)
        cands = store.get_resume_candidates(limit=50)
        assert len(cands) == 50
        store.close()

    def test_get_pending_limit_none_equivalent(self):
        """limit=None は 0 と同様に全件返す (run_backfill の limit or 0)。"""
        store = _make_state_store(200)
        # simulates: _limit_for_query = applied_limit or 0
        applied_limit = None
        _limit_for_query = applied_limit or 0
        pending = store.get_pending(limit=_limit_for_query)
        assert len(pending) == 200
        store.close()
