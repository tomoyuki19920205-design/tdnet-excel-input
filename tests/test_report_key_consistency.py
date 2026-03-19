"""tests/test_report_key_consistency.py — レポート整合性テスト

estimator / reporting が filing-based metrics と整合していることを確認。
"""
from __future__ import annotations

import sys
from pathlib import Path
from dataclasses import dataclass, field

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.backfill.estimator import estimate_full_backfill, compute_retry_factor
from lib.backfill.reporting import generate_notes, build_report, build_markdown_report
from lib.backfill.metrics import BackfillMetrics


# ================================================================
# helper — sample100_db 実測相当の summary を生成
# ================================================================

def _sample100_summary():
    """filing_completed=100 の実測 summary dict。"""
    return {
        "total_filings": 100,
        "filing_completed": 100,
        "filing_ok": 91,
        "filing_ok_xbrl": 0,
        "filing_ok_pdf": 91,
        "filing_quarantined": 9,
        "filing_failed": 0,
        "filing_needs_pdf": 100,
        "xbrl_success_rate": "0.0%",
        "pdf_fallback_rate": "100.0%",
        "quarantine_rate": "9.0%",
        "failed_rate": "0.0%",
        "via_xbrl": 0,
        "via_pdf": 91,
        "xbrl_stage_events": 100,
        "pdf_stage_events": 100,
        "xbrl_stage_ok": 0,
        "xbrl_stage_needs_pdf": 100,
        "pdf_stage_ok": 91,
        "pdf_stage_failed": 0,
        "pdf_stage_quarantined": 9,
        "upserted": 91,
        "retried": 2,
        "timeouts": 0,
        "cache_hit_pdf": 0,
        "cache_hit_xbrl": 0,
        "total_segment_rows": 500,
        "avg_segments_per_filing": 5.5,
        "elapsed_sec": 411.2,
        "avg_sec_per_filing": 4.11,
        "avg_xbrl_sec": 0.0,
        "avg_pdf_sec": 3.8,
        "xbrl_stage_sec": 50.0,
        "pdf_stage_sec": 361.2,
        "avg_batch_size": 109.6,
        "upsert_inserted": 91,
        "upsert_updated": 0,
        "upsert_failed_batches": 0,
        "batch_count": 1,
        "current_extraction_mode": "pdf_only_effective",
    }


# ================================================================
# tests
# ================================================================


class TestEstimatorKeyConsistency:
    """filing_completed=100 なら sample_filings=100 で estimate > 0。"""

    def test_sample_filings_from_filing_completed(self):
        s = _sample100_summary()
        sample = s.get("filing_completed", s.get("completed", 0))
        assert sample == 100

    def test_base_case_positive(self):
        """avg_pdf_sec=3.8, total_filings=30000, pdf_fallback=100% → base_case > 0。"""
        est = estimate_full_backfill(
            estimated_total_filings=30000,
            sample_filings=100,
            avg_xbrl_sec=4.11,  # fallback to avg_sec
            avg_pdf_sec=3.8,
            xbrl_success_rate=0.0,
            pdf_fallback_rate=1.0,
            quarantine_rate=0.09,
            xbrl_workers=6,
            pdf_workers=3,
        )
        assert est.base_case_sec > 0
        assert est.base_case_hours > 0
        assert est.sample_filings == 100
        assert est.xbrl_path_sec > 0
        assert est.pdf_path_sec > 0

    def test_zero_sample_returns_zero(self):
        """sample_filings=0 → base_case=0 (旧バグを再現させない)。"""
        est = estimate_full_backfill(
            estimated_total_filings=30000,
            sample_filings=0,
            avg_xbrl_sec=1.0,
            avg_pdf_sec=3.0,
            xbrl_success_rate=0.0,
            pdf_fallback_rate=1.0,
        )
        assert est.base_case_sec == 0

    def test_json_report_estimate_nonzero(self):
        s = _sample100_summary()
        est = estimate_full_backfill(
            estimated_total_filings=30000,
            sample_filings=s["filing_completed"],
            avg_xbrl_sec=s["avg_sec_per_filing"],
            avg_pdf_sec=s["avg_pdf_sec"],
            xbrl_success_rate=0.0,
            pdf_fallback_rate=1.0,
            quarantine_rate=0.09,
        )
        d = est.to_dict()
        assert d["base_case_hours"] > 0
        assert d["sample_filings"] == 100


class TestObservationBatchSize:
    """avg_batch_size=109.6 なら underutilized 注意は出ない。"""

    def test_no_underutilized_warning_for_large_batch(self):
        s = _sample100_summary()
        notes = generate_notes(s)
        for note in notes:
            assert "underutilized" not in note, f"Unexpected underutilized warning: {note}"

    def test_underutilized_warning_for_small_batch(self):
        s = _sample100_summary()
        s["avg_batch_size"] = 8.0
        notes = generate_notes(s)
        assert any("underutilized" in n for n in notes)

    def test_observation_uses_summary_avg_batch(self):
        """avg_batch_size=200 → underutilized は出ない。"""
        s = _sample100_summary()
        s["avg_batch_size"] = 200
        notes = generate_notes(s)
        assert not any("underutilized" in n for n in notes)


class TestMarkdownReportEstimate:
    """Markdown レポートの estimate セクションが 0 にならない。"""

    def test_markdown_has_nonzero_estimate(self):
        s = _sample100_summary()
        est = estimate_full_backfill(
            estimated_total_filings=30000,
            sample_filings=s["filing_completed"],
            avg_xbrl_sec=s["avg_sec_per_filing"],
            avg_pdf_sec=s["avg_pdf_sec"],
            xbrl_success_rate=0.0,
            pdf_fallback_rate=1.0,
            quarantine_rate=0.09,
        )
        notes = generate_notes(s, est.to_dict())
        report = build_report(
            benchmark_name="sample100_db",
            phase2=True,
            xbrl_workers=6,
            pdf_workers=3,
            workers=6,
            metrics=s,
            estimate=est.to_dict(),
            notes=notes,
        )
        md = build_markdown_report(report)
        assert "base_case_hours" in md
        # hours should not be 0
        assert "| base_case_hours | 0 |" not in md
        assert "| sample_filings | 0 |" not in md


class TestExtractionMode:
    """current_extraction_mode が正しく設定される。"""

    def test_pdf_only_effective(self):
        m = BackfillMetrics(total_filings=100)
        for i in range(100):
            from lib.backfill.metrics import BackfillMetrics as _BM
            from dataclasses import dataclass as _dc

            @_dc
            class _R:
                filing_id: str = f"F{i:03d}"
                status: str = "needs_pdf"
                via: str = None
                segment_records: list = None
                metrics: dict = None
                quarantine: dict = None
                result_fingerprint: str = ""

                def __post_init__(self):
                    self.segment_records = self.segment_records or []
                    self.metrics = self.metrics or {}

            m.record_xbrl_result(_R())
        d = m.summary_dict()
        assert d["current_extraction_mode"] == "pdf_only_effective"


class TestFilingBasedKeysInNotes:
    """generate_notes が filing_completed を使う。"""

    def test_uses_filing_completed(self):
        # only filing_completed set, no old "completed" key
        s = {"filing_completed": 100, "filing_quarantined": 20,
             "elapsed_sec": 100, "pdf_stage_sec": 80, "xbrl_stage_sec": 10}
        notes = generate_notes(s)
        assert any("quarantine" in n.lower() for n in notes)

    def test_fallback_to_old_completed(self):
        """旧キー 'completed' も fallback で読める。"""
        s = {"completed": 50, "quarantined": 10,
             "elapsed_sec": 50, "pdf_stage_sec": 40, "xbrl_stage_sec": 5}
        notes = generate_notes(s)
        assert any("quarantine" in n.lower() for n in notes)
