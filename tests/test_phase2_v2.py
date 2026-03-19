"""tests/test_phase2_v2.py — Phase2 V2 runner + metrics + JSONL のテスト

runner / metrics / logger を mock で検証する。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.backfill.listing_sources.base import FilingInfo
from lib.backfill.metrics import BackfillMetricsV2
from lib.backfill.jsonl_logger import RunLogger
from lib.backfill.worker_v2 import FilingResultV2, SourceCandidate


# ============================================================
# ヘルパー
# ============================================================

def _make_filing(fid="v2_001", **kwargs) -> FilingInfo:
    defaults = dict(
        filing_id=fid,
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


def _make_result_v2(
    fid="v2_001", status="ok", source="xbrl", **kwargs
) -> FilingResultV2:
    defaults = dict(
        filing_id=fid,
        status=status,
        source=source,
        selected_path=source,
        confidence=0.9,
        reason=f"[{source}] success",
        hard_fail_reason="",
        quarantine_reason="",
        fallback_used=False,
        fallback_reason="",
        raw_segment_count=3,
        valid_segment_count=3,
        invalid_segment_count=0,
        sales_non_null_count=3,
        profit_non_null_count=3,
        invalid_names=[],
        account_like_ratio=0.0,
        narrative_contamination=False,
        segment_records=[
            {"segment_name": "A", "segment_sales": 100, "segment_profit": 10},
            {"segment_name": "B", "segment_sales": 200, "segment_profit": 20},
            {"segment_name": "C", "segment_sales": 300, "segment_profit": 30},
        ],
        financial_records=[],
        via=source,
        metrics={"total_ms": 500},
        cache_paths={"cache_dir": "/tmp/test"},
        quarantine=None,
        result_fingerprint="abc123",
        candidates=[],
        candidate_summary=f"{source}:success",
    )
    defaults.update(kwargs)
    return FilingResultV2(**defaults)


def _mock_store():
    store = MagicMock()
    store.mark_done = MagicMock()
    store.mark_quarantined = MagicMock()
    store.mark_failed = MagicMock()
    store.mark_needs_pdf = MagicMock()
    return store


# ============================================================
# 1. phase2_runner が worker_version=v2 で worker_v2 を呼ぶ
# ============================================================

class TestRunPhase2V2:
    @patch("lib.backfill.worker_v2.process_one_filing_v2")
    def test_runner_calls_v2_worker(self, mock_v2, tmp_path):
        from lib.backfill.phase2_runner import run_phase2_v2

        result = _make_result_v2()
        mock_v2.return_value = result
        store = _mock_store()
        metrics = BackfillMetricsV2()
        log_path = str(tmp_path / "test.jsonl")
        rl = RunLogger(path=log_path, run_id="test_run")

        filing = _make_filing()
        pending = [{"filing_id": "v2_001"}]
        filing_map = {"v2_001": filing}

        results = run_phase2_v2(
            pending, filing_map,
            store=store, metrics=metrics, run_logger=rl, run_id="test_run",
            workers=1,
        )
        rl.close()

        assert len(results) == 1
        assert results[0].status == "ok"
        mock_v2.assert_called_once()
        store.mark_done.assert_called_once()


# ============================================================
# 2. JSONL に新キーが出る
# ============================================================

class TestJsonlV2Keys:
    def test_jsonl_has_v2_keys(self, tmp_path):
        log_path = str(tmp_path / "v2_test.jsonl")
        rl = RunLogger(path=log_path, run_id="test_keys")
        filing = _make_filing()
        result = _make_result_v2()
        rl.log_filing_result_v2(result, filing)
        rl.close()

        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()

        # 2行目 (1行目は run_start)
        event = json.loads(lines[1])
        required_keys = [
            "event", "worker_version", "filing_id", "ticker",
            "status", "source", "selected_path", "selected_source",
            "selected_status", "selected_confidence", "confidence",
            "fallback_used", "fallback_reason",
            "hard_fail_reason", "quarantine_reason",
            "valid_segment_count", "sales_non_null_count", "profit_non_null_count",
            "raw_segment_count", "invalid_segment_count",
            "account_like_ratio", "narrative_contamination",
            "candidate_summary",
        ]
        for key in required_keys:
            assert key in event, f"Missing JSONL key: {key}"
        assert event["worker_version"] == "v2"
        assert event["event"] == "filing_result"

    def test_jsonl_has_candidate_level_detail(self, tmp_path):
        """各 source の validator_status が JSONL に含まれる。"""
        from src.segment.extraction_result_validator import (
            ExtractionValidationResult, ExtractionStatus, HardFailReason,
        )
        log_path = str(tmp_path / "v2_cand.jsonl")
        rl = RunLogger(path=log_path, run_id="test_cand")

        # candidate を持つ result
        v = ExtractionValidationResult(
            status=ExtractionStatus.SUCCESS, confidence=0.95,
            reason="ok", hard_fail_reason=HardFailReason.NONE,
            raw_segment_count=3, valid_segment_count=3, invalid_segment_count=0,
            sales_non_null_count=3, profit_non_null_count=3,
            invalid_names=[], account_like_ratio=0.0, narrative_contamination=False,
        )
        candidates = [
            SourceCandidate(source="xbrl", attempted=True, available=True, validation=v),
            SourceCandidate(source="html", attempted=False, available=False, skip_reason="not_implemented"),
            SourceCandidate(source="pdf", attempted=True, available=True, error="pdf_no_segments"),
        ]
        result = _make_result_v2(candidates=candidates)
        rl.log_filing_result_v2(result, _make_filing())
        rl.close()

        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
        event = json.loads(lines[1])

        assert event.get("xbrl_attempted") is True
        assert event.get("xbrl_validator_status") == "success"
        assert event.get("html_attempted") is False
        assert event.get("html_skip_reason") == "not_implemented"
        assert event.get("pdf_attempted") is True
        assert event.get("pdf_validator_status") == "error"


# ============================================================
# 3. metrics に fallback_reason / quarantine_reason breakdown
# ============================================================

class TestMetricsV2Breakdown:
    def test_metrics_breakdown(self):
        m = BackfillMetricsV2()
        m.record_v2_result(_make_result_v2(fid="ok1", status="ok"))
        m.record_v2_result(_make_result_v2(fid="ok2", status="ok", source="pdf",
            fallback_used=True, fallback_reason="primary_quarantine"))
        m.record_v2_result(_make_result_v2(fid="partial1", status="partial", source="pdf",
            fallback_used=True, fallback_reason="primary_partial",
            profit_non_null_count=0))
        m.record_v2_result(_make_result_v2(fid="q1", status="quarantined", source="xbrl",
            hard_fail_reason="too_few_sales", quarantine_reason="too_few_sales",
            segment_records=[]))
        m.record_v2_result(_make_result_v2(fid="q2", status="quarantined", source="pdf",
            hard_fail_reason="narrative_contamination", quarantine_reason="narrative_contamination",
            narrative_contamination=True, segment_records=[]))
        m.record_v2_result(_make_result_v2(fid="f1", status="failed", source="",
            selected_path="none", segment_records=[]))

        d = m.summary_dict()

        # status counts
        assert d["filing_ok"] == 2
        assert d["filing_partial"] == 1
        assert d["filing_quarantined"] == 2
        assert d["filing_failed"] == 1
        assert d["total_filings"] == 6

        # fallback
        assert d["fallback_used_count"] == 2
        assert "primary_quarantine" in d["fallback_reason_breakdown"]
        assert "primary_partial" in d["fallback_reason_breakdown"]

        # quarantine reason
        assert "too_few_sales" in d["quarantine_reason_breakdown"]
        assert "narrative_contamination" in d["quarantine_reason_breakdown"]

        # hard_fail
        assert "too_few_sales" in d["hard_fail_reason_breakdown"]
        assert "narrative_contamination" in d["hard_fail_reason_breakdown"]

        # narrative
        assert d["narrative_contamination_count"] == 1

        # worker_version
        assert d["worker_version"] == "v2"


# ============================================================
# 4. v1/v2 切替で既存挙動が壊れない
# ============================================================

class TestV1Compat:
    def test_v1_metrics_still_work(self):
        """既存 BackfillMetrics が壊れないこと。"""
        from lib.backfill.metrics import BackfillMetrics
        m = BackfillMetrics()
        m.total_filings = 1
        d = m.summary_dict()
        assert "total_filings" in d
        assert "filing_ok" in d

    def test_v1_logger_still_works(self, tmp_path):
        """既存 log_filing_result が壊れないこと。"""
        from lib.backfill.worker import FilingResult
        log_path = str(tmp_path / "v1.jsonl")
        rl = RunLogger(path=log_path, run_id="v1_test")
        result = FilingResult(
            filing_id="old_001", status="ok", via="pdf",
            segment_records=[{"segment_name": "A", "segment_sales": 100}],
            metrics={"total_ms": 100},
        )
        rl.log_filing_result(result, _make_filing(fid="old_001"))
        rl.close()

        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
        event = json.loads(lines[1])
        assert event["status"] == "ok"
        assert event["via"] == "pdf"
        # v1 には worker_version がない
        assert "worker_version" not in event


# ============================================================
# 5. selected_path breakdown が正しく集計される
# ============================================================

class TestSelectedPathBreakdown:
    def test_selected_path_breakdown(self):
        m = BackfillMetricsV2()
        m.record_v2_result(_make_result_v2(fid="x1", source="xbrl"))
        m.record_v2_result(_make_result_v2(fid="x2", source="xbrl"))
        m.record_v2_result(_make_result_v2(fid="p1", source="pdf"))
        m.record_v2_result(_make_result_v2(fid="n1", status="failed", source="",
            selected_path="none", segment_records=[]))

        d = m.summary_dict()
        assert d["selected_path_xbrl"] == 2
        assert d["selected_path_pdf"] == 1
        assert d["selected_path_none"] == 1
        assert d["selected_path_html"] == 0


# ============================================================
# 6. partial が run summary に含まれる
# ============================================================

class TestPartialInSummary:
    def test_partial_counted(self):
        m = BackfillMetricsV2()
        m.record_v2_result(_make_result_v2(fid="p1", status="partial", source="pdf"))
        d = m.summary_dict()
        assert d["filing_partial"] == 1
        assert d["filing_ok"] == 0


# ============================================================
# 7. 混在しても集計が壊れない
# ============================================================

class TestMixedStatuses:
    def test_all_four_statuses_mixed(self):
        m = BackfillMetricsV2()
        m.record_v2_result(_make_result_v2(fid="ok", status="ok"))
        m.record_v2_result(_make_result_v2(fid="p", status="partial"))
        m.record_v2_result(_make_result_v2(fid="q", status="quarantined",
            hard_fail_reason="too_few_sales", quarantine_reason="too_few_sales",
            segment_records=[]))
        m.record_v2_result(_make_result_v2(fid="f", status="failed", source="",
            selected_path="none", segment_records=[]))

        d = m.summary_dict()
        assert d["total_filings"] == 4
        assert d["filing_ok"] == 1
        assert d["filing_partial"] == 1
        assert d["filing_quarantined"] == 1
        assert d["filing_failed"] == 1
        # avg / median should not crash
        assert d["avg_valid_segment_count"] >= 0
        assert d["median_valid_segment_count"] >= 0


# ============================================================
# 8. avg / median 計算の正確性
# ============================================================

class TestAvgMedian:
    def test_avg_median_values(self):
        m = BackfillMetricsV2()
        # valid_segment_count: 2, 4, 6 → avg=4, median=4
        m.record_v2_result(_make_result_v2(fid="a", valid_segment_count=2))
        m.record_v2_result(_make_result_v2(fid="b", valid_segment_count=4))
        m.record_v2_result(_make_result_v2(fid="c", valid_segment_count=6))
        d = m.summary_dict()
        assert d["avg_valid_segment_count"] == 4.0
        assert d["median_valid_segment_count"] == 4.0

    def test_empty_metrics(self):
        m = BackfillMetricsV2()
        d = m.summary_dict()
        assert d["total_filings"] == 0
        assert d["avg_valid_segment_count"] == 0.0
