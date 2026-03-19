"""tests/test_backfill_estimator.py — estimator ユニットテスト"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.backfill.estimator import estimate_full_backfill, compute_retry_factor, EstimateResult


class TestEstimateFullBackfill:
    """3年外挿のコア計算をテスト。"""

    def test_basic_estimate(self):
        r = estimate_full_backfill(
            estimated_total_filings=10000,
            sample_filings=100,
            avg_xbrl_sec=0.5,
            avg_pdf_sec=3.0,
            xbrl_success_rate=0.7,
            pdf_fallback_rate=0.3,
            xbrl_workers=6,
            pdf_workers=3,
        )
        assert isinstance(r, EstimateResult)
        assert r.base_case_sec > 0
        assert r.optimistic_sec < r.base_case_sec
        assert r.pessimistic_sec > r.base_case_sec
        assert r.base_case_hours == r.base_case_sec / 3600

    def test_optimistic_is_80_pct(self):
        r = estimate_full_backfill(
            estimated_total_filings=10000, sample_filings=100,
            avg_xbrl_sec=1.0, avg_pdf_sec=5.0,
            xbrl_success_rate=0.8, pdf_fallback_rate=0.2,
            xbrl_workers=6, pdf_workers=3,
        )
        assert abs(r.optimistic_sec - r.base_case_sec * 0.8) < 0.1

    def test_pessimistic_is_130_pct(self):
        r = estimate_full_backfill(
            estimated_total_filings=10000, sample_filings=100,
            avg_xbrl_sec=1.0, avg_pdf_sec=5.0,
            xbrl_success_rate=0.8, pdf_fallback_rate=0.2,
            xbrl_workers=6, pdf_workers=3,
        )
        assert abs(r.pessimistic_sec - r.base_case_sec * 1.3) < 0.1

    def test_zero_filings(self):
        r = estimate_full_backfill(
            estimated_total_filings=0, sample_filings=0,
            avg_xbrl_sec=1.0, avg_pdf_sec=3.0,
            xbrl_success_rate=0.7, pdf_fallback_rate=0.3,
            xbrl_workers=6, pdf_workers=3,
        )
        assert r.base_case_sec == 0.0
        assert r.optimistic_hours == 0.0

    def test_negative_total(self):
        r = estimate_full_backfill(
            estimated_total_filings=-100, sample_filings=50,
            avg_xbrl_sec=1.0, avg_pdf_sec=3.0,
            xbrl_success_rate=0.7, pdf_fallback_rate=0.3,
            xbrl_workers=6, pdf_workers=3,
        )
        assert r.base_case_sec == 0.0

    def test_quarantine_reduces_xbrl_path(self):
        without_q = estimate_full_backfill(
            estimated_total_filings=10000, sample_filings=100,
            avg_xbrl_sec=1.0, avg_pdf_sec=5.0,
            xbrl_success_rate=0.8, pdf_fallback_rate=0.2,
            quarantine_rate=0.0,
            xbrl_workers=6, pdf_workers=3,
        )
        with_q = estimate_full_backfill(
            estimated_total_filings=10000, sample_filings=100,
            avg_xbrl_sec=1.0, avg_pdf_sec=5.0,
            xbrl_success_rate=0.8, pdf_fallback_rate=0.2,
            quarantine_rate=0.1,
            xbrl_workers=6, pdf_workers=3,
        )
        # Quarantine reduces processable → xbrl_path shrinks
        assert with_q.xbrl_path_sec < without_q.xbrl_path_sec

    def test_retry_factor_increases_estimate(self):
        base = estimate_full_backfill(
            estimated_total_filings=10000, sample_filings=100,
            avg_xbrl_sec=1.0, avg_pdf_sec=5.0,
            xbrl_success_rate=0.8, pdf_fallback_rate=0.2,
            xbrl_workers=6, pdf_workers=3,
            retry_factor=1.0,
        )
        retried = estimate_full_backfill(
            estimated_total_filings=10000, sample_filings=100,
            avg_xbrl_sec=1.0, avg_pdf_sec=5.0,
            xbrl_success_rate=0.8, pdf_fallback_rate=0.2,
            xbrl_workers=6, pdf_workers=3,
            retry_factor=1.2,
        )
        assert retried.base_case_sec > base.base_case_sec

    def test_to_dict_shape(self):
        r = estimate_full_backfill(
            estimated_total_filings=5000, sample_filings=50,
            avg_xbrl_sec=0.5, avg_pdf_sec=3.0,
            xbrl_success_rate=0.7, pdf_fallback_rate=0.3,
            xbrl_workers=6, pdf_workers=3,
        )
        d = r.to_dict()
        assert "base_case_hours" in d
        assert "optimistic_hours" in d
        assert "pessimistic_hours" in d
        assert "estimation_method" in d
        assert d["estimation_method"] == "phase2_dual_path"


class TestComputeRetryFactor:
    def test_no_retry(self):
        assert compute_retry_factor(0, 100) == 1.0

    def test_some_retries(self):
        f = compute_retry_factor(10, 100)
        assert 1.0 < f < 1.5

    def test_many_retries(self):
        f = compute_retry_factor(100, 100)
        assert f == 1.5  # capped

    def test_zero_completed(self):
        assert compute_retry_factor(5, 0) == 1.0
