"""tests/test_metrics_filing_based.py — BackfillMetrics filing ベーステスト"""
from __future__ import annotations

import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.backfill.metrics import BackfillMetrics


@dataclass
class _MockResult:
    """FilingResult のモック。"""
    filing_id: str = "F001"
    status: str = "ok"
    via: str | None = "xbrl"
    segment_records: list = field(default_factory=list)
    financial_records: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    quarantine: dict | None = None
    result_fingerprint: str = ""


# ================================================================
# filing ベースカウント
# ================================================================


class TestFilingBasedCounting:
    """100 filings で completed が 100 を超えないこと。"""

    def test_100_all_xbrl_ok(self):
        m = BackfillMetrics(total_filings=100)
        for i in range(100):
            r = _MockResult(filing_id=f"F{i:03d}", status="ok", via="xbrl",
                           segment_records=[{"s": 1}])
            m.record_xbrl_result(r)
        assert m.completed_filings == 100
        assert m.ok_count == 100
        assert m.ok_xbrl_count == 100
        assert m.ok_pdf_count == 0

    def test_100_all_needs_pdf_then_pdf_ok(self):
        """100件全部 needs_pdf → PDF で 95 成功, 5 quarantined。
        completed = 100 (200 ではない)"""
        m = BackfillMetrics(total_filings=100)

        # Stage B: 100 filings → all needs_pdf
        for i in range(100):
            r = _MockResult(filing_id=f"F{i:03d}", status="needs_pdf", via=None)
            m.record_xbrl_result(r)

        # needs_pdf は中間状態 → completed は 0
        assert m.completed_filings == 0
        assert m.xbrl_stage_needs_pdf == 100

        # Stage C: 95 ok + 5 quarantined
        for i in range(95):
            r = _MockResult(filing_id=f"F{i:03d}", status="ok", via="pdf",
                           segment_records=[{"s": 1}])
            m.record_pdf_result(r)

        for i in range(95, 100):
            r = _MockResult(filing_id=f"F{i:03d}", status="quarantined", via=None)
            m.record_pdf_result(r)

        # completed = 100 (200 ではない)
        assert m.completed_filings == 100
        assert m.ok_count == 95
        assert m.ok_pdf_count == 95
        assert m.ok_xbrl_count == 0
        assert m.quarantined_count == 5

    def test_via_pdf_equals_ok_pdf(self):
        """via_pdf と ok_pdf は一致する。"""
        m = BackfillMetrics(total_filings=10)
        for i in range(10):
            m.record_xbrl_result(
                _MockResult(filing_id=f"F{i:03d}", status="needs_pdf", via=None)
            )
        for i in range(8):
            m.record_pdf_result(
                _MockResult(filing_id=f"F{i:03d}", status="ok", via="pdf",
                           segment_records=[{"s": 1}])
            )
        for i in range(8, 10):
            m.record_pdf_result(
                _MockResult(filing_id=f"F{i:03d}", status="quarantined", via=None)
            )
        assert m.via_pdf_count == m.ok_pdf_count == 8


# ================================================================
# rate 計算
# ================================================================


class TestRates:
    def test_pdf_fallback_rate_100_percent(self):
        """needs_pdf=100 → pdf_fallback_rate=100%"""
        m = BackfillMetrics(total_filings=100)
        for i in range(100):
            m.record_xbrl_result(
                _MockResult(filing_id=f"F{i:03d}", status="needs_pdf", via=None)
            )
        assert m.pdf_fallback_rate == pytest.approx(1.0)

    def test_xbrl_success_rate_0(self):
        """ok_xbrl=0 → xbrl_success_rate=0%"""
        m = BackfillMetrics(total_filings=100)
        for i in range(100):
            m.record_xbrl_result(
                _MockResult(filing_id=f"F{i:03d}", status="needs_pdf", via=None)
            )
        assert m.xbrl_success_rate == 0.0

    def test_xbrl_success_rate_50_percent(self):
        """50/100 → xbrl_success_rate=50%"""
        m = BackfillMetrics(total_filings=100)
        for i in range(50):
            m.record_xbrl_result(
                _MockResult(filing_id=f"F{i:03d}", status="ok", via="xbrl",
                           segment_records=[{"s": 1}])
            )
        for i in range(50, 100):
            m.record_xbrl_result(
                _MockResult(filing_id=f"F{i:03d}", status="needs_pdf", via=None)
            )
        assert m.xbrl_success_rate == pytest.approx(0.5)

    def test_quarantine_rate_filing_based(self):
        m = BackfillMetrics(total_filings=100)
        for i in range(95):
            m.record_xbrl_result(
                _MockResult(filing_id=f"F{i:03d}", status="ok", via="xbrl",
                           segment_records=[{"s": 1}])
            )
        for i in range(95, 100):
            m.record_xbrl_result(
                _MockResult(filing_id=f"F{i:03d}", status="quarantined", via=None)
            )
        assert m.quarantine_rate == pytest.approx(0.05)


# ================================================================
# stage event 分離
# ================================================================


class TestStageEventsSeparation:
    def test_stage_events_separate(self):
        """stage events は filing count と独立。"""
        m = BackfillMetrics(total_filings=100)

        # Stage B: 100 events
        for i in range(100):
            m.record_xbrl_result(
                _MockResult(filing_id=f"F{i:03d}", status="needs_pdf", via=None)
            )

        # Stage C: 100 events
        for i in range(100):
            m.record_pdf_result(
                _MockResult(filing_id=f"F{i:03d}", status="ok", via="pdf",
                           segment_records=[{"s": 1}])
            )

        # stage events = 100 + 100 = 200
        assert m.xbrl_stage_events == 100
        assert m.pdf_stage_events == 100

        # filing completed = 100 (200 ではない!)
        assert m.completed_filings == 100

    def test_summary_dict_has_both_metrics(self):
        """summary_dict に filing ベースと stage ベースの両方が含まれる。"""
        m = BackfillMetrics(total_filings=10)
        for i in range(10):
            m.record_xbrl_result(
                _MockResult(filing_id=f"F{i:03d}", status="ok", via="xbrl",
                           segment_records=[{"s": 1}])
            )
        d = m.summary_dict()

        # filing ベース
        assert "filing_completed" in d
        assert "filing_ok" in d
        assert "filing_ok_xbrl" in d
        assert "filing_ok_pdf" in d
        assert "filing_needs_pdf" in d

        # stage ベース
        assert "xbrl_stage_events" in d
        assert "xbrl_stage_ok" in d
        assert "pdf_stage_events" in d


# ================================================================
# backward compat
# ================================================================


class TestBackwardCompat:
    def test_record_result_still_works(self):
        """旧 record_result API が動く。"""
        m = BackfillMetrics(total_filings=1)
        r = _MockResult(filing_id="F001", status="ok", via="xbrl",
                       segment_records=[{"s": 1}])
        m.record_result(r)  # 旧 API
        assert m.ok_count == 1

    def test_record_result_with_stage_pdf(self):
        m = BackfillMetrics(total_filings=1)
        m.record_result(
            _MockResult(filing_id="F001", status="ok", via="pdf",
                       segment_records=[{"s": 1}]),
            stage="pdf"
        )
        assert m.ok_pdf_count == 1
        assert m.pdf_stage_events == 1


# ================================================================
# xbrl_ok only when segment success
# ================================================================


class TestXbrlSegmentSuccess:
    def test_xbrl_ok_only_with_segments(self):
        """XBRL worker が segment success の場合のみ ok_xbrl カウント。"""
        m = BackfillMetrics(total_filings=2)

        # Filing 1: XBRL ok with segments
        m.record_xbrl_result(
            _MockResult(filing_id="F001", status="ok", via="xbrl",
                       segment_records=[{"s": 1}])
        )
        # Filing 2: XBRL needs_pdf (no segments)
        m.record_xbrl_result(
            _MockResult(filing_id="F002", status="needs_pdf", via=None)
        )

        assert m.ok_xbrl_count == 1
        assert m.needs_pdf_count == 1
