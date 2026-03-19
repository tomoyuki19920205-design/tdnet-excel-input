"""tests/test_state_store.py — BackfillStateStore テスト"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.backfill.state_store import BackfillStateStore
from lib.backfill.listing_sources.base import FilingInfo


def _make_filing(filing_id: str = "abc123", ticker: str = "6750") -> FilingInfo:
    return FilingInfo(
        filing_id=filing_id,
        ticker=ticker,
        title="2025年3月期 決算短信",
        disclosure_date="2025-05-15",
        doc_url="https://example.com/doc.pdf",
        xbrl_url=None,
        doc_type="financial_statement",
        company_name="テスト株式会社",
        published_at="2025-05-15 15:00",
        listing_source="tdnet_html",
        has_xbrl=False,
    )


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test_state.db")
    s = BackfillStateStore(db_path)
    yield s
    s.close()


class TestRegister:
    def test_register_new(self, store):
        f = _make_filing()
        result = store.register_filings([f])
        assert result["new"] == 1
        assert result["existing"] == 0

    def test_register_existing_updates_last_seen(self, store):
        f = _make_filing()
        store.register_filings([f])
        result = store.register_filings([f])
        assert result["new"] == 0
        assert result["existing"] == 1

    def test_register_multiple(self, store):
        filings = [_make_filing(f"id_{i}", f"{6750+i}") for i in range(5)]
        result = store.register_filings(filings)
        assert result["new"] == 5


class TestGetPending:
    def test_get_pending(self, store):
        store.register_filings([_make_filing()])
        pending = store.get_pending()
        assert len(pending) == 1
        assert pending[0]["status"] == "queued"

    def test_get_pending_with_ticker_filter(self, store):
        store.register_filings([
            _make_filing("a", "6750"),
            _make_filing("b", "4062"),
        ])
        pending = store.get_pending(tickers=["6750"])
        assert len(pending) == 1
        assert pending[0]["ticker"] == "6750"

    def test_get_pending_limit(self, store):
        filings = [_make_filing(f"id_{i}") for i in range(10)]
        store.register_filings(filings)
        pending = store.get_pending(limit=3)
        assert len(pending) == 3


class TestStatusUpdates:
    def test_mark_running(self, store):
        store.register_filings([_make_filing()])
        store.update_status("abc123", "running", stage="downloading")
        rows = store.get_pending(statuses=["running"])
        assert len(rows) == 1
        assert rows[0]["stage"] == "downloading"
        assert rows[0]["attempt_count"] == 1

    def test_mark_done(self, store):
        store.register_filings([_make_filing()])
        store.mark_done("abc123", via="xbrl", segment_count=3)
        rows = store.get_pending(statuses=["done"])
        assert len(rows) == 1
        assert rows[0]["via"] == "xbrl"
        assert rows[0]["segment_count"] == 3

    def test_mark_quarantined(self, store):
        store.register_filings([_make_filing()])
        store.mark_quarantined("abc123", error="parse error", stage="extracting_xbrl")
        rows = store.get_pending(statuses=["quarantined"])
        assert len(rows) == 1
        assert "parse error" in rows[0]["last_error"]

    def test_mark_failed(self, store):
        store.register_filings([_make_filing()])
        store.mark_failed("abc123", error="timeout", stage="downloading")
        rows = store.get_pending(statuses=["failed"])
        assert len(rows) == 1


class TestRetry:
    def test_reset_for_retry(self, store):
        store.register_filings([_make_filing()])
        store.mark_quarantined("abc123", error="test")
        count = store.reset_for_retry(statuses=["quarantined"])
        assert count == 1
        pending = store.get_pending()
        assert len(pending) == 1

    def test_non_retryable_not_reset(self, store):
        store.register_filings([_make_filing()])
        store.mark_quarantined("abc123", error="test", retryable=False)
        count = store.reset_for_retry(statuses=["quarantined"])
        assert count == 0


class TestStats:
    def test_stats(self, store):
        store.register_filings([
            _make_filing("a", "6750"),
            _make_filing("b", "4062"),
        ])
        store.mark_done("a", via="xbrl")
        stats = store.stats()
        assert stats["done"] == 1
        assert stats["queued"] == 1
        assert stats["total"] == 2

    def test_count_by_listing_source(self, store):
        store.register_filings([_make_filing()])
        counts = store.count_by_listing_source()
        assert counts["tdnet_html"] == 1


class TestListingProvider:
    """CompositeProviderの基本テスト。"""

    def test_composite_dedup(self):
        from lib.backfill.listing_provider import CompositeListingProvider

        class MockProvider:
            name = "mock"
            def list_filings(self, *a, **kw):
                return [_make_filing("id1"), _make_filing("id1")]

        provider = CompositeListingProvider([MockProvider()])
        filings = provider.list_filings("2025-01-01", "2025-01-02")
        assert len(filings) == 1

    def test_composite_fallback(self):
        from lib.backfill.listing_provider import CompositeListingProvider

        class FailProvider:
            name = "fail"
            def list_filings(self, *a, **kw):
                raise RuntimeError("fail")

        class OkProvider:
            name = "ok"
            def list_filings(self, *a, **kw):
                return [_make_filing()]

        provider = CompositeListingProvider([FailProvider(), OkProvider()])
        filings = provider.list_filings("2025-01-01", "2025-01-02")
        assert len(filings) == 1


class TestUpdateReviewHint:
    """update_review_hint テスト。"""

    def test_update_review_hint_persists(self, store):
        """review_hint が DB に永続反映されること。"""
        store.register_filings([_make_filing()])
        store.mark_quarantined("abc123", error="parse error",
                               review_hint="pdf_table_parse_failed")
        # 新 hint に更新
        store.update_review_hint("abc123", "pdf_no_sales_profit_columns")
        rows = store.get_pending(statuses=["quarantined"])
        assert len(rows) == 1
        assert rows[0]["review_hint"] == "pdf_no_sales_profit_columns"

    def test_old_hint_replaced_in_db(self, store):
        """旧 pdf_table_parse_failed が新 hint に置き換わり DB 集計が変わること。"""
        filings = [_make_filing(f"id_{i}") for i in range(5)]
        store.register_filings(filings)
        for f in filings:
            store.mark_quarantined(
                f.filing_id, error="parse error",
                review_hint="pdf_table_parse_failed",
            )

        # 3件を新 hint に更新
        store.update_review_hint("id_0", "pdf_no_sales_profit_columns")
        store.update_review_hint("id_1", "pdf_no_segment_page_candidate")
        store.update_review_hint("id_2", "pdf_no_rows_extracted")

        rows = store.get_pending(statuses=["quarantined"])
        hints = {r["filing_id"]: r["review_hint"] for r in rows}
        assert hints["id_0"] == "pdf_no_sales_profit_columns"
        assert hints["id_1"] == "pdf_no_segment_page_candidate"
        assert hints["id_2"] == "pdf_no_rows_extracted"
        assert hints["id_3"] == "pdf_table_parse_failed"
        assert hints["id_4"] == "pdf_table_parse_failed"

    def test_status_unchanged_after_hint_update(self, store):
        """hint 更新後も status は quarantined のまま変わらないこと。"""
        store.register_filings([_make_filing()])
        store.mark_quarantined("abc123", error="err",
                               review_hint="pdf_table_parse_failed")
        store.update_review_hint("abc123", "pdf_no_rows_extracted")
        rows = store.get_pending(statuses=["quarantined"])
        assert len(rows) == 1
        assert rows[0]["status"] == "quarantined"
        assert rows[0]["review_hint"] == "pdf_no_rows_extracted"

