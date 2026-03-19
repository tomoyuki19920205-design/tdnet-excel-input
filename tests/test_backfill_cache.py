"""tests/test_backfill_cache.py — cache モジュールのテスト"""
from __future__ import annotations

import json
import os
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.backfill.cache import (
    ensure_cache_layout, write_metadata, has_pdf, has_xbrl,
    save_extract_financials_result, save_extract_segments_result,
    save_quarantine, append_filing_log,
)
from lib.backfill.listing_sources.base import FilingInfo


def _make_filing() -> FilingInfo:
    return FilingInfo(
        filing_id="test123",
        ticker="6750",
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


class TestEnsureCacheLayout:
    def test_creates_directory(self, tmp_path):
        root = str(tmp_path / "cache")
        paths = ensure_cache_layout(root, "abc123")
        assert paths.cache_dir.exists()
        assert paths.cache_dir.name == "abc123"

    def test_all_paths_under_cache_dir(self, tmp_path):
        paths = ensure_cache_layout(str(tmp_path), "xyz")
        for field_name in ["metadata_json", "source_pdf", "xbrl_zip",
                          "extract_financials_result_json", "extract_segments_result_json",
                          "quarantine_json", "logs_jsonl"]:
            p = getattr(paths, field_name)
            assert str(p).startswith(str(paths.cache_dir))


class TestWriteMetadata:
    def test_write_from_filing_info(self, tmp_path):
        paths = ensure_cache_layout(str(tmp_path), "m1")
        filing = _make_filing()
        write_metadata(paths, filing)
        data = json.loads(paths.metadata_json.read_text(encoding="utf-8"))
        assert data["filing_id"] == "test123"
        assert data["ticker"] == "6750"
        assert "created_at" in data

    def test_write_from_dict(self, tmp_path):
        paths = ensure_cache_layout(str(tmp_path), "m2")
        write_metadata(paths, {"filing_id": "d1", "ticker": "4062"})
        data = json.loads(paths.metadata_json.read_text(encoding="utf-8"))
        assert data["filing_id"] == "d1"


class TestHasPdfXbrl:
    def test_has_pdf_false_initially(self, tmp_path):
        paths = ensure_cache_layout(str(tmp_path), "p1")
        assert has_pdf(paths) is False

    def test_has_pdf_true_after_write(self, tmp_path):
        paths = ensure_cache_layout(str(tmp_path), "p2")
        paths.source_pdf.write_bytes(b"%PDF-1.4 test")
        assert has_pdf(paths) is True

    def test_has_xbrl_false_initially(self, tmp_path):
        paths = ensure_cache_layout(str(tmp_path), "x1")
        assert has_xbrl(paths) is False

    def test_has_xbrl_true_after_write(self, tmp_path):
        paths = ensure_cache_layout(str(tmp_path), "x2")
        paths.xbrl_zip.write_bytes(b"PK\x03\x04 test zip")
        assert has_xbrl(paths) is True

    def test_has_pdf_false_for_empty(self, tmp_path):
        paths = ensure_cache_layout(str(tmp_path), "p3")
        paths.source_pdf.write_bytes(b"")
        assert has_pdf(paths) is False


class TestSaveResults:
    def test_save_extract_financials(self, tmp_path):
        paths = ensure_cache_layout(str(tmp_path), "r1")
        save_extract_financials_result(paths, {"sales": 1000000})
        data = json.loads(paths.extract_financials_result_json.read_text(encoding="utf-8"))
        assert data["sales"] == 1000000

    def test_save_extract_segments(self, tmp_path):
        paths = ensure_cache_layout(str(tmp_path), "r2")
        save_extract_segments_result(paths, [{"segment_name": "Seg1"}])
        data = json.loads(paths.extract_segments_result_json.read_text(encoding="utf-8"))
        assert data[0]["segment_name"] == "Seg1"

    def test_save_quarantine(self, tmp_path):
        paths = ensure_cache_layout(str(tmp_path), "r3")
        save_quarantine(paths, {"reason": "test"})
        data = json.loads(paths.quarantine_json.read_text(encoding="utf-8"))
        assert data["reason"] == "test"


class TestAppendFilingLog:
    def test_appends_lines(self, tmp_path):
        paths = ensure_cache_layout(str(tmp_path), "log1")
        append_filing_log(paths, {"event": "start"})
        append_filing_log(paths, {"event": "end"})
        lines = paths.logs_jsonl.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["event"] == "start"
        assert json.loads(lines[1])["event"] == "end"
