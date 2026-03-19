"""lib/backfill/estimator.py — 3年フルバックフィル所要時間の外挿推定

実測ベンチ結果から、Phase 2 構造を反映して所要時間を見積もる。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EstimateResult:
    """外挿推定の結果。"""
    estimated_total_filings: int
    sample_filings: int
    avg_xbrl_sec: float
    avg_pdf_sec: float
    xbrl_success_rate: float
    pdf_fallback_rate: float
    quarantine_rate: float
    xbrl_workers: int
    pdf_workers: int
    retry_factor: float

    base_case_sec: float
    optimistic_sec: float
    pessimistic_sec: float

    xbrl_path_sec: float
    pdf_path_sec: float
    db_overhead_sec: float

    @property
    def base_case_hours(self) -> float:
        return self.base_case_sec / 3600

    @property
    def optimistic_hours(self) -> float:
        return self.optimistic_sec / 3600

    @property
    def pessimistic_hours(self) -> float:
        return self.pessimistic_sec / 3600

    def to_dict(self) -> dict:
        return {
            "estimated_total_filings": self.estimated_total_filings,
            "sample_filings": self.sample_filings,
            "avg_xbrl_sec": round(self.avg_xbrl_sec, 3),
            "avg_pdf_sec": round(self.avg_pdf_sec, 3),
            "xbrl_success_rate": f"{self.xbrl_success_rate:.1%}",
            "pdf_fallback_rate": f"{self.pdf_fallback_rate:.1%}",
            "quarantine_rate": f"{self.quarantine_rate:.1%}",
            "xbrl_workers": self.xbrl_workers,
            "pdf_workers": self.pdf_workers,
            "retry_factor": round(self.retry_factor, 2),
            "xbrl_path_sec": round(self.xbrl_path_sec, 1),
            "pdf_path_sec": round(self.pdf_path_sec, 1),
            "db_overhead_sec": round(self.db_overhead_sec, 1),
            "base_case_sec": round(self.base_case_sec, 1),
            "base_case_hours": round(self.base_case_hours, 2),
            "optimistic_sec": round(self.optimistic_sec, 1),
            "optimistic_hours": round(self.optimistic_hours, 2),
            "pessimistic_sec": round(self.pessimistic_sec, 1),
            "pessimistic_hours": round(self.pessimistic_hours, 2),
            "estimation_method": "phase2_dual_path",
            "note": "Sample-based extrapolation; actual times may vary.",
        }


def estimate_full_backfill(
    *,
    estimated_total_filings: int,
    sample_filings: int,
    avg_xbrl_sec: float,
    avg_pdf_sec: float,
    xbrl_success_rate: float,
    pdf_fallback_rate: float,
    quarantine_rate: float = 0.0,
    xbrl_workers: int = 6,
    pdf_workers: int = 3,
    retry_factor: float = 1.0,
    db_overhead_per_filing_sec: float = 0.01,
) -> EstimateResult:
    """Phase 2 構造を反映した 3年フル外挿推定。

    計算式:
        xbrl_path = total * (1 - quarantine_rate) * avg_xbrl_sec / xbrl_workers * retry_factor
        pdf_path  = total * pdf_fallback_rate * avg_pdf_sec / pdf_workers * retry_factor
        db_overhead = total * db_overhead_per_filing_sec
        base = xbrl_path + pdf_path + db_overhead

    Ranges:
        optimistic  = base * 0.8
        pessimistic = base * 1.3
    """
    if estimated_total_filings <= 0 or sample_filings <= 0:
        return EstimateResult(
            estimated_total_filings=estimated_total_filings,
            sample_filings=sample_filings,
            avg_xbrl_sec=avg_xbrl_sec, avg_pdf_sec=avg_pdf_sec,
            xbrl_success_rate=xbrl_success_rate, pdf_fallback_rate=pdf_fallback_rate,
            quarantine_rate=quarantine_rate,
            xbrl_workers=xbrl_workers, pdf_workers=pdf_workers,
            retry_factor=retry_factor,
            base_case_sec=0.0, optimistic_sec=0.0, pessimistic_sec=0.0,
            xbrl_path_sec=0.0, pdf_path_sec=0.0, db_overhead_sec=0.0,
        )

    n = estimated_total_filings
    processable = n * (1 - quarantine_rate)  # expect some to be quarantined

    xbrl_path = processable * avg_xbrl_sec / max(xbrl_workers, 1) * retry_factor
    pdf_path = n * pdf_fallback_rate * avg_pdf_sec / max(pdf_workers, 1) * retry_factor
    db_overhead = n * db_overhead_per_filing_sec

    base = xbrl_path + pdf_path + db_overhead
    optimistic = base * 0.8
    pessimistic = base * 1.3

    return EstimateResult(
        estimated_total_filings=n,
        sample_filings=sample_filings,
        avg_xbrl_sec=avg_xbrl_sec, avg_pdf_sec=avg_pdf_sec,
        xbrl_success_rate=xbrl_success_rate, pdf_fallback_rate=pdf_fallback_rate,
        quarantine_rate=quarantine_rate,
        xbrl_workers=xbrl_workers, pdf_workers=pdf_workers,
        retry_factor=retry_factor,
        base_case_sec=base, optimistic_sec=optimistic, pessimistic_sec=pessimistic,
        xbrl_path_sec=xbrl_path, pdf_path_sec=pdf_path, db_overhead_sec=db_overhead,
    )


def compute_retry_factor(retried_count: int, completed: int) -> float:
    """retry が発生した割合 → retry_factor (1.0 〜 1.5)。"""
    if completed <= 0:
        return 1.0
    rate = retried_count / completed
    return 1.0 + min(rate * 0.5, 0.5)  # cap at 1.5
