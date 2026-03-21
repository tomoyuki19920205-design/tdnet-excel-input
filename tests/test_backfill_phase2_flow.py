"""tests/test_backfill_phase2_flow.py — Phase 2 (XBRL/PDF 分離) テスト

xbrl_first / pdf_only / needs_pdf / state 遷移 / metrics を検証。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from unittest.mock import patch, MagicMock

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.backfill.listing_sources.base import FilingInfo
from lib.backfill.worker import (
    process_one_filing_xbrl_first,
    process_one_filing_pdf_only,
    FilingResult,
)
from lib.backfill.state_store import BackfillStateStore
from lib.backfill.metrics import BackfillMetrics


def _make_filing(**kwargs) -> FilingInfo:
    defaults = dict(
        filing_id="test_fid_001",
        ticker="6750",
        title="2025年3月期 決算短信",
        disclosure_date="2025-05-15",
        doc_url="https://example.com/doc.pdf",
        xbrl_url="https://example.com/xbrl.zip",
        doc_type="financial_statement",
        company_name="テスト株式会社",
        published_at="2025-05-15 15:00",
        listing_source="tdnet_html",
        has_xbrl=True,
    )
    defaults.update(kwargs)
    return FilingInfo(**defaults)


@dataclass
class MockExtracted:
    fiscal_year: str = "2025-03-31"
    quarter: str = "4Q"
    sales: float = 1000000
    operating_profit: float = 200000
    gross_profit: float = None
    cost_of_sales: float = None
    source: str = "xbrl"


@dataclass
class MockSegment:
    segment_name: str = "セグメントA"
    segment_order: int = 1
    segment_sales: float = 500000
    segment_profit: float = 100000
    raw_profit_label: str = "営業利益"


class TestXbrlFirstSuccess:
    """XBRL で segment 取得成功。"""

    @patch("src.extractor.extract_segment_financials")
    @patch("src.extractor.extract_financials")
    @patch("src.downloader.download_document")
    def test_ok_via_xbrl(self, mock_dl, mock_fin, mock_seg, tmp_path):
        cache = str(tmp_path / "cache")
        fid_dir = os.path.join(cache, "test_fid_001")
        os.makedirs(fid_dir, exist_ok=True)
        pdf_path = os.path.join(fid_dir, "source.pdf")
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF test")
        # XBRL ZIP cache を作成 (download_document_ex の実呼び出しを回避)
        xbrl_path = os.path.join(fid_dir, "xbrl.zip")
        with open(xbrl_path, "wb") as f:
            f.write(b"PK fake xbrl")

        mock_dl.return_value = pdf_path
        mock_fin.return_value = (MockExtracted(), "")
        mock_seg.return_value = ([MockSegment(), MockSegment(segment_name="SegB", segment_order=2)], "")

        result = process_one_filing_xbrl_first(
            _make_filing(), cache_root=cache, sleep_fn=lambda _: None,
        )
        assert result.status == "ok"
        assert result.via == "xbrl"
        assert len(result.segment_records) == 2


class TestXbrlFirstNeedsPdf:
    """XBRL で segment 取得失敗 → needs_pdf。"""

    @patch("src.extractor.extract_segment_financials")
    @patch("src.extractor.extract_financials")
    @patch("src.downloader.download_document")
    def test_needs_pdf_no_segments(self, mock_dl, mock_fin, mock_seg, tmp_path):
        cache = str(tmp_path / "cache")
        fid_dir = os.path.join(cache, "test_fid_001")
        os.makedirs(fid_dir, exist_ok=True)
        pdf_path = os.path.join(fid_dir, "source.pdf")
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF test")
        # XBRL ZIP cache を作成
        xbrl_path = os.path.join(fid_dir, "xbrl.zip")
        with open(xbrl_path, "wb") as f:
            f.write(b"PK fake xbrl")

        mock_dl.return_value = pdf_path
        mock_fin.return_value = (MockExtracted(), "")
        mock_seg.return_value = ([], "no_segment_table")

        result = process_one_filing_xbrl_first(
            _make_filing(), cache_root=cache, sleep_fn=lambda _: None,
        )
        assert result.status == "needs_pdf"
        assert result.via is None
        assert (result.quarantine or {}).get("review_hint") == "xbrl_segment_parse_failed"

    @patch("src.extractor.extract_financials")
    @patch("src.downloader.download_document")
    def test_needs_pdf_no_xbrl_url(self, mock_dl, mock_fin, tmp_path):
        """xbrl_url がない場合も needs_pdf。"""
        cache = str(tmp_path / "cache")
        pdf_path = os.path.join(cache, "test_fid_001", "source.pdf")
        os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF test")

        mock_dl.return_value = pdf_path
        mock_fin.return_value = (MockExtracted(source="pdf"), "")

        result = process_one_filing_xbrl_first(
            _make_filing(xbrl_url=None, has_xbrl=False),
            cache_root=cache, sleep_fn=lambda _: None,
        )
        assert result.status == "needs_pdf"
        assert (result.quarantine or {}).get("review_hint") == "xbrl_zip_not_available"

    def test_needs_pdf_is_not_failed(self, tmp_path):
        """needs_pdf は failed ではなく正常な中間状態。"""
        result = FilingResult(filing_id="x", status="needs_pdf")
        assert result.status != "failed"
        assert result.status != "quarantined"


class TestPdfOnlySuccess:
    """PDF-only で segment 取得成功。"""

    @patch("src.extractor.extract_segment_financials")
    def test_ok_via_pdf(self, mock_seg, tmp_path):
        cache = str(tmp_path / "cache")
        fid = "test_fid_pdf"
        pdf_path = os.path.join(cache, fid, "source.pdf")
        os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF test")

        # financials result cache
        fin_path = os.path.join(cache, fid, "extract_financials_result.json")
        with open(fin_path, "w", encoding="utf-8") as f:
            json.dump({"period": "2025-03-31", "quarter": "4Q"}, f)

        mock_seg.return_value = ([MockSegment()], "")

        result = process_one_filing_pdf_only(
            _make_filing(filing_id=fid),
            cache_root=cache, sleep_fn=lambda _: None,
        )
        assert result.status == "ok"
        assert result.via == "pdf"
        assert len(result.segment_records) == 1

    @patch("src.extractor.extract_segment_financials")
    def test_quarantined_pdf(self, mock_seg, tmp_path):
        """PDF segment 抽出失敗 → quarantined。"""
        cache = str(tmp_path / "cache")
        fid = "test_fid_pdf2"
        pdf_path = os.path.join(cache, fid, "source.pdf")
        os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF test")

        mock_seg.return_value = ([], "no table found")

        result = process_one_filing_pdf_only(
            _make_filing(filing_id=fid),
            cache_root=cache, sleep_fn=lambda _: None,
        )
        assert result.status == "quarantined"
        assert "pdf" in (result.quarantine or {}).get("review_hint", "")


class TestPdfOnlyCacheReuse:
    """PDF-only は cache を再利用し、再 download しない。"""

    @patch("src.extractor.extract_segment_financials")
    def test_no_redownload(self, mock_seg, tmp_path):
        cache = str(tmp_path / "cache")
        fid = "test_fid_cache"
        fid_dir = os.path.join(cache, fid)
        os.makedirs(fid_dir, exist_ok=True)
        pdf_path = os.path.join(fid_dir, "source.pdf")
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF test")
        # extract_financials_result.json cache (period/quarter 取得に必要)
        fin_path = os.path.join(fid_dir, "extract_financials_result.json")
        with open(fin_path, "w", encoding="utf-8") as f:
            json.dump({"period": "2025-03-31", "quarter": "4Q"}, f)

        mock_seg.return_value = ([MockSegment()], "")

        # download_document は呼ばれない
        result = process_one_filing_pdf_only(
            _make_filing(filing_id=fid),
            cache_root=cache, sleep_fn=lambda _: None,
        )
        assert result.status == "ok"


class TestStateNeedsPdf:
    """state_store の mark_needs_pdf / list_needs_pdf。"""

    def test_mark_and_list(self, tmp_path):
        store = BackfillStateStore(str(tmp_path / "s.db"))
        from lib.backfill.listing_sources.base import FilingInfo

        filings = [
            FilingInfo(filing_id="f1", ticker="6750", title="T1", disclosure_date="2025-05-15",
                       doc_url="u", xbrl_url=None, doc_type="fs", company_name="C",
                       published_at="t", listing_source="html", has_xbrl=False),
            FilingInfo(filing_id="f2", ticker="4062", title="T2", disclosure_date="2025-05-15",
                       doc_url="u", xbrl_url=None, doc_type="fs", company_name="C",
                       published_at="t", listing_source="html", has_xbrl=False),
        ]
        store.register_filings(filings)
        store.mark_needs_pdf("f1", review_hint="xbrl_missing_segment_data")

        needs = store.list_needs_pdf()
        assert len(needs) == 1
        assert needs[0]["filing_id"] == "f1"
        assert needs[0]["review_hint"] == "xbrl_missing_segment_data"
        store.close()

    def test_needs_pdf_in_resume_candidates(self, tmp_path):
        store = BackfillStateStore(str(tmp_path / "s.db"))
        from lib.backfill.listing_sources.base import FilingInfo

        filings = [
            FilingInfo(filing_id="f1", ticker="6750", title="T1", disclosure_date="2025-05-15",
                       doc_url="u", xbrl_url=None, doc_type="fs", company_name="C",
                       published_at="t", listing_source="html", has_xbrl=False),
        ]
        store.register_filings(filings)
        store.mark_needs_pdf("f1")

        candidates = store.get_resume_candidates(limit=100, include_needs_pdf=True)
        fids = [c["filing_id"] for c in candidates]
        assert "f1" in fids
        store.close()


class TestMetricsPhase2:
    """Phase 2 metrics: ok_xbrl / ok_pdf / needs_pdf / rates。"""

    def test_record_xbrl_ok(self):
        m = BackfillMetrics()
        r = FilingResult(filing_id="a", status="ok", via="xbrl", segment_records=[{"s": 1}], metrics={})
        m.record_result(r)
        assert m.ok_xbrl_count == 1
        assert m.ok_pdf_count == 0

    def test_record_pdf_ok(self):
        m = BackfillMetrics()
        r = FilingResult(filing_id="a", status="ok", via="pdf", segment_records=[{"s": 1}], metrics={})
        m.record_result(r)
        assert m.ok_xbrl_count == 0
        assert m.ok_pdf_count == 1

    def test_record_needs_pdf(self):
        m = BackfillMetrics()
        r = FilingResult(filing_id="a", status="needs_pdf", metrics={})
        m.record_result(r)
        assert m.needs_pdf_count == 1
        assert m.ok_count == 0

    def test_xbrl_success_rate(self):
        m = BackfillMetrics(total_filings=10)
        for i in range(8):
            m.record_xbrl_result(
                FilingResult(filing_id=f"x{i}", status="ok", via="xbrl",
                            segment_records=[{"s": 1}], metrics={})
            )
        for i in range(8, 10):
            m.record_xbrl_result(
                FilingResult(filing_id=f"x{i}", status="needs_pdf", metrics={})
            )
        assert abs(m.xbrl_success_rate - 0.8) < 0.001

    def test_pdf_fallback_rate(self):
        m = BackfillMetrics(total_filings=10)
        for i in range(3):
            m.record_xbrl_result(
                FilingResult(filing_id=f"x{i}", status="needs_pdf", metrics={})
            )
        for i in range(3, 10):
            m.record_xbrl_result(
                FilingResult(filing_id=f"x{i}", status="ok", via="xbrl",
                            segment_records=[{"s": 1}], metrics={})
            )
        assert abs(m.pdf_fallback_rate - 0.3) < 0.001

    def test_summary_has_phase2_fields(self):
        m = BackfillMetrics(total_filings=10)
        for i in range(5):
            m.record_xbrl_result(
                FilingResult(filing_id=f"x{i}", status="ok", via="xbrl",
                            segment_records=[{"s": 1}], metrics={})
            )
        for i in range(5, 8):
            m.record_xbrl_result(
                FilingResult(filing_id=f"x{i}", status="needs_pdf", metrics={})
            )
        for i in range(5, 7):
            m.record_pdf_result(
                FilingResult(filing_id=f"x{i}", status="ok", via="pdf",
                            segment_records=[{"s": 1}], metrics={})
            )
        m.xbrl_stage_elapsed = 10.0
        m.pdf_stage_elapsed = 5.0
        d = m.summary_dict()
        assert "filing_ok_xbrl" in d
        assert "filing_ok_pdf" in d
        assert "filing_needs_pdf" in d
        assert "xbrl_success_rate" in d
        assert "pdf_fallback_rate" in d
        assert "xbrl_stage_sec" in d
        assert "pdf_stage_sec" in d
