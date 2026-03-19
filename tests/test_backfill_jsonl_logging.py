"""tests/test_backfill_jsonl_logging.py — JSONL ログテスト"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.backfill.jsonl_logger import RunLogger, make_filing_event, generate_run_id


@dataclass
class MockResult:
    filing_id: str = "test_fid"
    status: str = "ok"
    via: str | None = "xbrl"
    segment_records: list = field(default_factory=lambda: [{"seg": 1}])
    metrics: dict = field(default_factory=lambda: {"total_ms": 500, "pdf_cache_hit": True})
    quarantine: dict | None = None
    result_fingerprint: str | None = "abc123"


@dataclass
class MockFiling:
    ticker: str = "6750"
    listing_source: str = "tdnet_html"


class TestRunLogger:
    def test_writes_jsonl(self, tmp_path):
        path = str(tmp_path / "run.jsonl")
        rl = RunLogger(path, run_id="test_run")
        rl.log_filing_result(MockResult(), MockFiling())
        rl.close()

        lines = open(path, encoding="utf-8").readlines()
        # lines[0] = run_start, lines[1] = filing_result
        assert len(lines) >= 2
        data = json.loads(lines[1])
        assert data["event"] == "filing_result"
        assert data["run_id"] == "test_run"
        assert data["filing_id"] == "test_fid"
        assert data["status"] == "ok"
        assert data["via"] == "xbrl"
        assert data["segment_count"] == 1

    def test_writes_summary(self, tmp_path):
        path = str(tmp_path / "run.jsonl")
        rl = RunLogger(path, run_id="r2")
        rl.log_summary({"total": 10, "ok": 8})
        rl.close()

        data = json.loads(open(path, encoding="utf-8").readlines()[-1])
        assert data["event"] == "run_summary"
        assert data["total"] == 10

    def test_appends_multiple(self, tmp_path):
        path = str(tmp_path / "run.jsonl")
        rl = RunLogger(path, run_id="r3")
        rl.log_filing_result(MockResult(filing_id="a"))
        rl.log_filing_result(MockResult(filing_id="b"))
        rl.close()

        lines = open(path, encoding="utf-8").readlines()
        # run_start + 2 filings + close summary = 4 lines
        filing_lines = [l for l in lines if '"filing_result"' in l]
        assert len(filing_lines) == 2

    def test_no_path_no_error(self):
        rl = RunLogger(None, run_id="r4")
        rl.log_filing_result(MockResult())
        rl.close()  # no error

    def test_quarantine_event(self, tmp_path):
        path = str(tmp_path / "run.jsonl")
        rl = RunLogger(path, run_id="r5")
        result = MockResult(
            status="quarantined", via=None, segment_records=[],
            quarantine={"review_hint": "pdf_timeout"},
        )
        rl.log_filing_result(result, MockFiling())
        rl.close()

        data = json.loads(open(path, encoding="utf-8").readlines()[1])
        assert data["status"] == "quarantined"
        assert data["review_hint"] == "pdf_timeout"


class TestMakeFilingEvent:
    def test_minimal(self):
        ev = make_filing_event("start", "fid_001")
        assert ev["event"] == "start"
        assert ev["filing_id"] == "fid_001"
        assert "ts" in ev

    def test_full(self):
        ev = make_filing_event(
            "quarantined", "fid_002",
            stage="extracting_pdf",
            status="quarantined",
            error="table parse failed",
            review_hint="pdf_table_parse_failed",
            via="pdf",
            segment_count=0,
            attempt=2,
            duration_ms=5000,
        )
        assert ev["stage"] == "extracting_pdf"
        assert ev["review_hint"] == "pdf_table_parse_failed"
        assert ev["attempt"] == 2


class TestGenerateRunId:
    def test_unique(self):
        ids = {generate_run_id() for _ in range(100)}
        assert len(ids) == 100

    def test_length(self):
        assert len(generate_run_id()) == 12
