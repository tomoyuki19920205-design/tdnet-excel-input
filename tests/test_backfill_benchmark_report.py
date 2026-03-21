"""tests/test_backfill_benchmark_report.py — reporting ユニットテスト"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.backfill.reporting import (
    generate_notes, compute_percentiles, build_report,
    save_json_report, build_markdown_report, save_markdown_report,
    build_comparison_table,
)


# ================================================================
# generate_notes
# ================================================================

class TestGenerateNotes:
    def test_pdf_dominant(self):
        m = {"elapsed_sec": 100, "pdf_stage_sec": 70, "xbrl_stage_sec": 20, "completed": 10,
             "pdf_fallback_rate": "15%", "quarantined": 0, "cache_hit_pdf": 0, "cache_hit_xbrl": 0,
             "batch_count": 0, "upserted": 0, "retried": 0, "timeouts": 0, "avg_sec_per_filing": 5}
        notes = generate_notes(m)
        assert any("PDF stage" in n for n in notes)

    def test_high_pdf_fallback(self):
        m = {"elapsed_sec": 100, "pdf_stage_sec": 30, "xbrl_stage_sec": 50, "completed": 10,
             "pdf_fallback_rate": "40%", "quarantined": 0, "cache_hit_pdf": 0, "cache_hit_xbrl": 0,
             "batch_count": 0, "upserted": 0, "retried": 0, "timeouts": 0, "avg_sec_per_filing": 5}
        notes = generate_notes(m)
        assert any("High PDF fallback" in n for n in notes)

    def test_high_quarantine(self):
        m = {"elapsed_sec": 100, "pdf_stage_sec": 30, "xbrl_stage_sec": 50, "completed": 10,
             "pdf_fallback_rate": "20%", "quarantined": 3, "cache_hit_pdf": 0, "cache_hit_xbrl": 0,
             "batch_count": 0, "upserted": 0, "retried": 0, "timeouts": 0, "avg_sec_per_filing": 5}
        notes = generate_notes(m)
        assert any("quarantine" in n.lower() for n in notes)

    def test_empty_metrics(self):
        notes = generate_notes({})
        assert isinstance(notes, list)

    def test_estimate_long(self):
        m = {"elapsed_sec": 10, "pdf_stage_sec": 3, "xbrl_stage_sec": 5, "completed": 10,
             "pdf_fallback_rate": "20%", "quarantined": 0, "cache_hit_pdf": 0, "cache_hit_xbrl": 0,
             "batch_count": 0, "upserted": 0, "retried": 0, "timeouts": 0, "avg_sec_per_filing": 1}
        est = {"base_case_hours": 48}
        notes = generate_notes(m, est)
        assert any("long" in n.lower() for n in notes)


# ================================================================
# compute_percentiles
# ================================================================

class TestComputePercentiles:
    def test_basic(self):
        p = compute_percentiles([100, 200, 300, 400, 500])
        assert p["p50_ms"] == 300
        assert p["count"] == 5
        assert p["min_ms"] == 100
        assert p["max_ms"] == 500

    def test_empty(self):
        p = compute_percentiles([])
        assert p["p50_ms"] == 0
        assert p["count"] == 0

    def test_single(self):
        p = compute_percentiles([42])
        assert p["p50_ms"] == 42
        assert p["p90_ms"] == 42

    def test_p90(self):
        durations = list(range(1, 101))  # 1..100
        p = compute_percentiles(durations)
        assert p["p90_ms"] == 91  # s[int(100*0.9)] = s[90] = 91


# ================================================================
# build_report
# ================================================================

class TestBuildReport:
    def test_shape(self):
        r = build_report(
            benchmark_name="test",
            phase2=True,
            xbrl_workers=6,
            pdf_workers=3,
            workers=4,
            metrics={"ok": 10},
            run_id="abc",
        )
        assert r["benchmark_name"] == "test"
        assert r["phase2"] is True
        assert r["workers"]["xbrl"] == 6
        assert r["workers"]["pdf"] == 3
        assert r["metrics"]["ok"] == 10
        assert "timestamp" in r
        assert isinstance(r["notes"], list)

    def test_with_estimate(self):
        r = build_report(
            benchmark_name="est",
            phase2=True,
            xbrl_workers=6, pdf_workers=3, workers=4,
            metrics={},
            estimate={"base_case_hours": 5},
        )
        assert r["estimate_3y"]["base_case_hours"] == 5


# ================================================================
# save_json_report
# ================================================================

class TestSaveJsonReport:
    def test_creates_file(self, tmp_path):
        report = {"benchmark_name": "test", "metrics": {"ok": 1}}
        path = str(tmp_path / "reports" / "bench.json")
        save_json_report(report, path)
        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert data["benchmark_name"] == "test"


# ================================================================
# Markdown
# ================================================================

class TestMarkdownReport:
    def test_generates_markdown(self):
        r = build_report(
            benchmark_name="md_test",
            phase2=True,
            xbrl_workers=6, pdf_workers=3, workers=4,
            metrics={"ok": 10, "elapsed_sec": 30},
            estimate={"base_case_hours": 2.5},
            notes=["Test note"],
            percentiles={"p50_ms": 200, "p90_ms": 500},
        )
        md = build_markdown_report(r)
        assert "# Backfill Benchmark Report" in md
        assert "md_test" in md
        assert "Metrics" in md
        assert "ok" in md
        assert "Test note" in md
        assert "Duration Percentiles" in md
        assert "3-Year Full Backfill Estimate" in md

    def test_save_markdown(self, tmp_path):
        report = {"benchmark_name": "save_test", "metrics": {"ok": 1}}
        path = str(tmp_path / "bench.md")
        save_markdown_report(report, path)
        assert os.path.exists(path)
        with open(path) as f:
            content = f.read()
        assert "save_test" in content


# ================================================================
# comparison table
# ================================================================

class TestComparisonTable:
    def test_empty(self):
        assert build_comparison_table([]) == ""

    def test_basic(self):
        runs = [
            {"workers": {"xbrl": 4, "pdf": 2}, "metrics": {"elapsed_sec": 60, "avg_sec_per_filing": 1.2, "ok_xbrl": 30, "ok_pdf": 10, "needs_pdf": 15, "xbrl_success_rate": "66%", "pdf_fallback_rate": "25%", "quarantined": 5}},
            {"workers": {"xbrl": 8, "pdf": 3}, "metrics": {"elapsed_sec": 40, "avg_sec_per_filing": 0.8, "ok_xbrl": 30, "ok_pdf": 10, "needs_pdf": 15, "xbrl_success_rate": "66%", "pdf_fallback_rate": "25%", "quarantined": 5}},
        ]
        md = build_comparison_table(runs)
        assert "Worker Comparison" in md
        assert "4/2" in md
        assert "8/3" in md


# ================================================================
# metrics summary fields (Step 5 additions)
# ================================================================

class TestMetricsSummaryStep5:
    def test_quarantine_rate_in_summary(self):
        from lib.backfill.metrics import BackfillMetrics
        from lib.backfill.worker import FilingResult
        m = BackfillMetrics(total_filings=10)
        # record_result で ok_count / quarantined_count を積む
        for i in range(8):
            m.record_result(FilingResult(
                filing_id=f"ok_{i}", status="ok", via="xbrl",
                segment_records=[{"s": 1}], metrics={},
            ))
        for i in range(2):
            m.record_result(FilingResult(
                filing_id=f"q_{i}", status="quarantined", metrics={},
            ))
        d = m.summary_dict()
        assert "quarantine_rate" in d
        assert d["quarantine_rate"] == "20.0%"

    def test_avg_xbrl_sec_in_summary(self):
        from lib.backfill.metrics import BackfillMetrics
        m = BackfillMetrics()
        m.xbrl_durations_ms = [500, 1000, 1500]
        d = m.summary_dict()
        assert d["avg_xbrl_sec"] == 1.0  # (500+1000+1500)/3/1000

    def test_avg_batch_size(self):
        from lib.backfill.metrics import BackfillMetrics
        m = BackfillMetrics()
        m.upsert_inserted = 100
        m.upsert_updated = 50
        m.batch_count = 5
        d = m.summary_dict()
        assert d["avg_batch_size"] == 30.0
