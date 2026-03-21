"""tests/test_backfill_resume.py — resume / stale_running / mark_upserted テスト"""
from __future__ import annotations

import os
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.backfill.state_store import BackfillStateStore
from lib.backfill.listing_sources.base import FilingInfo


def _make_filings(n: int, **overrides) -> list[FilingInfo]:
    return [
        FilingInfo(
            filing_id=f"fid_{i:03d}",
            ticker=overrides.get("ticker", "6750"),
            title=f"Test filing {i}",
            disclosure_date="2025-05-15",
            doc_url=f"https://example.com/doc_{i}.pdf",
            xbrl_url=None,
            doc_type="financial_statement",
            company_name="Test",
            published_at="2025-05-15 15:00",
            listing_source="tdnet_html",
            has_xbrl=False,
        )
        for i in range(n)
    ]


class TestResumeCandidates:
    def test_done_included_in_resume(self, tmp_path):
        """done = segment抽出完了/upsert待ち → resume候補に含まれる"""
        store = BackfillStateStore(str(tmp_path / "s.db"))
        filings = _make_filings(3)
        store.register_filings(filings)
        store.mark_done("fid_000", via="xbrl", segment_count=2)

        candidates = store.get_resume_candidates(limit=100)
        fids = [c["filing_id"] for c in candidates]
        # done はまだ upsert されていないため resume 対象
        assert "fid_000" in fids
        assert "fid_001" in fids
        assert "fid_002" in fids
        store.close()

    def test_upserted_excluded(self, tmp_path):
        store = BackfillStateStore(str(tmp_path / "s.db"))
        filings = _make_filings(2)
        store.register_filings(filings)
        store.mark_done("fid_000", via="xbrl")
        store.mark_upserted("fid_000")

        candidates = store.get_resume_candidates(limit=100)
        fids = [c["filing_id"] for c in candidates]
        assert "fid_000" not in fids
        store.close()

    def test_queued_included(self, tmp_path):
        store = BackfillStateStore(str(tmp_path / "s.db"))
        store.register_filings(_make_filings(3))

        candidates = store.get_resume_candidates(limit=100)
        assert len(candidates) == 3
        store.close()

    def test_running_included(self, tmp_path):
        """前回異常終了した running は resume 対象。"""
        store = BackfillStateStore(str(tmp_path / "s.db"))
        store.register_filings(_make_filings(2))
        store.update_status("fid_000", "running", stage="downloading")

        candidates = store.get_resume_candidates(limit=100)
        fids = [c["filing_id"] for c in candidates]
        assert "fid_000" in fids
        store.close()

    def test_quarantined_excluded_by_default(self, tmp_path):
        store = BackfillStateStore(str(tmp_path / "s.db"))
        store.register_filings(_make_filings(2))
        store.mark_quarantined("fid_000", error="test")

        candidates = store.get_resume_candidates(limit=100, include_quarantined=False)
        fids = [c["filing_id"] for c in candidates]
        assert "fid_000" not in fids
        store.close()

    def test_quarantined_included_with_flag(self, tmp_path):
        store = BackfillStateStore(str(tmp_path / "s.db"))
        store.register_filings(_make_filings(2))
        store.mark_quarantined("fid_000", error="test")

        candidates = store.get_resume_candidates(limit=100, include_quarantined=True)
        fids = [c["filing_id"] for c in candidates]
        assert "fid_000" in fids
        store.close()

    def test_failed_included_with_flag(self, tmp_path):
        store = BackfillStateStore(str(tmp_path / "s.db"))
        store.register_filings(_make_filings(2))
        store.mark_failed("fid_000", error="crash")

        candidates = store.get_resume_candidates(limit=100, include_failed=True)
        fids = [c["filing_id"] for c in candidates]
        assert "fid_000" in fids
        store.close()


class TestStaleRunning:
    def test_reset_stale_running(self, tmp_path):
        store = BackfillStateStore(str(tmp_path / "s.db"))
        store.register_filings(_make_filings(2))
        # fid_000 を running にして、last_attempt_at を古く設定
        store.update_status("fid_000", "running", stage="downloading")
        store.conn.execute(
            "UPDATE filing_state SET last_attempt_at = '2025-01-01T00:00:00+09:00' WHERE filing_id = 'fid_000'"
        )
        store.conn.commit()

        reset = store.reset_stale_running(max_age_hours=1)
        assert reset == 1

        row = dict(store.conn.execute(
            "SELECT status FROM filing_state WHERE filing_id = 'fid_000'"
        ).fetchone())
        assert row["status"] == "queued"
        store.close()


class TestMarkUpserted:
    def test_upserted_status(self, tmp_path):
        store = BackfillStateStore(str(tmp_path / "s.db"))
        store.register_filings(_make_filings(1))
        store.mark_done("fid_000", via="xbrl", segment_count=3)
        store.mark_upserted("fid_000")

        row = dict(store.conn.execute(
            "SELECT status, stage FROM filing_state WHERE filing_id = 'fid_000'"
        ).fetchone())
        assert row["status"] == "upserted"
        assert row["stage"] == "completed"
        store.close()

    def test_stats_includes_upserted(self, tmp_path):
        store = BackfillStateStore(str(tmp_path / "s.db"))
        store.register_filings(_make_filings(3))
        store.mark_done("fid_000", via="xbrl")
        store.mark_upserted("fid_000")
        store.mark_quarantined("fid_001", error="e")

        s = store.stats()
        assert s["upserted"] == 1
        assert s["quarantined"] == 1
        assert s["queued"] == 1
        store.close()
