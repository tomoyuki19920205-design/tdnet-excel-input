"""lib/backfill/reporting.py — ベンチマークレポート生成

JSON / Markdown / コンソール出力とボトルネック自動コメント。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("backfill.report")
JST = timezone(timedelta(hours=9))


# ================================================================
# ボトルネック自動コメント (ルールベース)
# ================================================================

def generate_notes(metrics: dict, estimate: dict | None = None) -> list[str]:
    """実測値からボトルネック所見を自動生成する。"""
    notes: list[str] = []

    elapsed = metrics.get("elapsed_sec", 0)
    pdf_stage = metrics.get("pdf_stage_sec", 0)
    xbrl_stage = metrics.get("xbrl_stage_sec", 0)

    # PDF stage dominance
    if elapsed > 0 and pdf_stage / elapsed > 0.6:
        pct = pdf_stage / elapsed * 100
        notes.append(f"PDF stage is dominant bottleneck ({pct:.0f}% of total time)")
    elif elapsed > 0 and xbrl_stage / elapsed > 0.7:
        pct = xbrl_stage / elapsed * 100
        notes.append(f"XBRL stage is dominant ({pct:.0f}% of total time)")

    # PDF fallback rate
    fb_str = metrics.get("pdf_fallback_rate", "0%")
    fb_val = _parse_pct(fb_str)
    if fb_val > 35:
        notes.append(f"High PDF fallback rate ({fb_str}) — consider improving XBRL extraction")
    elif fb_val > 0 and fb_val <= 15:
        notes.append(f"Low PDF fallback rate ({fb_str}) — XBRL extraction is effective")

    # Cache effectiveness
    cache_pdf = metrics.get("cache_hit_pdf", 0)
    cache_xbrl = metrics.get("cache_hit_xbrl", 0)
    completed = metrics.get("filing_completed", metrics.get("completed", 0))
    if completed > 0:
        total_cache = cache_pdf + cache_xbrl
        if total_cache / completed > 0.3:
            notes.append(f"Cache reuse is effective ({total_cache}/{completed} cache hits)")

    # Quarantine rate
    q = metrics.get("filing_quarantined", metrics.get("quarantined", 0))
    if completed > 0 and q / completed > 0.1:
        notes.append(f"High quarantine rate ({q}/{completed} = {q/completed:.0%}) — extraction quality needs review")

    # Batch utilization — summary の avg_batch_size を直接参照
    avg_batch = metrics.get("avg_batch_size", 0)
    if isinstance(avg_batch, str):
        try:
            avg_batch = float(avg_batch)
        except ValueError:
            avg_batch = 0
    upserted = metrics.get("upserted", 0)
    if avg_batch > 0 and avg_batch < 20 and upserted > 0:
        notes.append(f"DB batching may be underutilized (avg batch size={avg_batch:.0f})")

    # Retry / timeout
    retried = metrics.get("retried", 0)
    timeouts = metrics.get("timeouts", 0)
    if retried > 5:
        notes.append(f"Significant retry activity ({retried} filings retried)")
    if timeouts > 3:
        notes.append(f"Multiple timeouts detected ({timeouts} filings)")

    # Avg sec per filing
    avg_sec = metrics.get("avg_sec_per_filing", 0)
    if avg_sec > 10:
        notes.append(f"Slow average processing ({avg_sec:.1f}s per filing)")
    elif avg_sec > 0 and avg_sec < 1:
        notes.append(f"Fast processing ({avg_sec:.2f}s per filing) — may indicate cache dominance")

    # Estimate notes
    if estimate:
        hours = estimate.get("base_case_hours", 0)
        if hours > 24:
            notes.append(f"Estimated 3Y backfill is long ({hours:.1f}h) — consider increasing parallelism")
        elif hours > 0 and hours < 4:
            notes.append(f"Estimated 3Y backfill is fast ({hours:.1f}h)")

    return notes


def _parse_pct(s: str | float | int) -> float:
    """'35.2%' → 35.2 or numeric passthrough."""
    if isinstance(s, (int, float)):
        return float(s) * 100 if s <= 1 else float(s)
    try:
        return float(str(s).rstrip("%"))
    except (ValueError, TypeError):
        return 0.0


# ================================================================
# per-filing 統計 (p50/p90/slowest)
# ================================================================

def compute_percentiles(durations_ms: list[int | float]) -> dict:
    """p50 / p90 / max / min / avg を計算する。"""
    if not durations_ms:
        return {"p50_ms": 0, "p90_ms": 0, "max_ms": 0, "min_ms": 0, "avg_ms": 0, "count": 0}
    s = sorted(durations_ms)
    n = len(s)
    return {
        "p50_ms": int(s[n // 2]),
        "p90_ms": int(s[int(n * 0.9)]) if n > 1 else int(s[0]),
        "max_ms": int(s[-1]),
        "min_ms": int(s[0]),
        "avg_ms": int(sum(s) / n),
        "count": n,
    }


def extract_slowest(results: list, n: int = 10) -> list[dict]:
    """FilingResult のリストから最も遅い n 件を返す。"""
    items = []
    for r in results:
        ms = (r.metrics or {}).get("total_ms", 0)
        items.append({
            "filing_id": r.filing_id,
            "status": r.status,
            "via": r.via,
            "total_ms": ms,
            "segments": len(r.segment_records) if r.segment_records else 0,
        })
    items.sort(key=lambda x: x["total_ms"], reverse=True)
    return items[:n]


# ================================================================
# JSON Report
# ================================================================

def build_report(
    *,
    benchmark_name: str,
    phase2: bool,
    xbrl_workers: int,
    pdf_workers: int,
    workers: int,
    metrics: dict,
    estimate: dict | None = None,
    notes: list[str] | None = None,
    percentiles: dict | None = None,
    slowest: list[dict] | None = None,
    run_id: str = "",
    date_range: str = "",
) -> dict:
    """ベンチマーク JSON レポートを構築する。"""
    return {
        "run_id": run_id,
        "benchmark_name": benchmark_name,
        "timestamp": datetime.now(JST).isoformat(),
        "phase2": phase2,
        "workers": {
            "xbrl": xbrl_workers if phase2 else workers,
            "pdf": pdf_workers if phase2 else 0,
            "phase1": workers if not phase2 else 0,
        },
        "date_range": date_range,
        "metrics": metrics,
        "percentiles": percentiles or {},
        "slowest_filings": slowest or [],
        "estimate_3y": estimate or {},
        "notes": notes or [],
    }


def save_json_report(report: dict, path: str) -> str:
    """JSON レポートを保存する。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    logger.info(f"[report] JSON saved: {p}")
    return str(p)


# ================================================================
# Markdown Report
# ================================================================

def build_markdown_report(report: dict) -> str:
    """Markdown 形式のレポートを生成する。"""
    lines: list[str] = []

    lines.append(f"# Backfill Benchmark Report: {report.get('benchmark_name', 'unnamed')}")
    lines.append("")
    lines.append(f"**Run ID:** {report.get('run_id', 'N/A')}")
    lines.append(f"**Date:** {report.get('timestamp', 'N/A')}")
    lines.append(f"**Phase 2:** {'Yes' if report.get('phase2') else 'No'}")
    w = report.get("workers", {})
    if report.get("phase2"):
        lines.append(f"**Workers:** XBRL={w.get('xbrl', '?')} PDF={w.get('pdf', '?')}")
    else:
        lines.append(f"**Workers:** {w.get('phase1', '?')}")
    if report.get("date_range"):
        lines.append(f"**Date Range:** {report['date_range']}")
    lines.append("")

    # Metrics table
    lines.append("## Metrics")
    lines.append("")
    m = report.get("metrics", {})
    lines.append("| Metric | Value |")
    lines.append("|:---|---:|")
    for k, v in m.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")

    # Percentiles
    pct = report.get("percentiles", {})
    if pct:
        lines.append("## Duration Percentiles")
        lines.append("")
        lines.append("| Stat | ms |")
        lines.append("|:---|---:|")
        for k, v in pct.items():
            lines.append(f"| {k} | {v} |")
        lines.append("")

    # Slowest
    slow = report.get("slowest_filings", [])
    if slow:
        lines.append("## Slowest Filings")
        lines.append("")
        lines.append("| filing_id | status | via | total_ms | segments |")
        lines.append("|:---|:---|:---|---:|---:|")
        for s in slow[:10]:
            lines.append(f"| {s['filing_id'][:16]}... | {s['status']} | {s.get('via', '-')} | {s['total_ms']} | {s.get('segments', 0)} |")
        lines.append("")

    # Estimate
    est = report.get("estimate_3y", {})
    if est:
        lines.append("## 3-Year Full Backfill Estimate")
        lines.append("")
        lines.append("| Parameter | Value |")
        lines.append("|:---|---:|")
        for k, v in est.items():
            lines.append(f"| {k} | {v} |")
        lines.append("")

    # Notes
    notes = report.get("notes", [])
    if notes:
        lines.append("## Observations")
        lines.append("")
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")

    lines.append("---")
    lines.append("*Note: Estimates are sample-based extrapolations. Actual times may vary.*")
    lines.append("")

    return "\n".join(lines)


def save_markdown_report(report: dict, path: str) -> str:
    """Markdown レポートを保存する。"""
    md = build_markdown_report(report)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(md)
    logger.info(f"[report] Markdown saved: {p}")
    return str(p)


# ================================================================
# compare-workers 比較テーブル
# ================================================================

def build_comparison_table(runs: list[dict]) -> str:
    """複数ワーカー設定の比較テーブル (Markdown)。"""
    if not runs:
        return ""
    headers = ["workers", "elapsed_sec", "avg_sec", "ok_xbrl", "ok_pdf", "needs_pdf",
               "xbrl_rate", "pdf_fb_rate", "quarantined"]
    lines = ["## Worker Comparison", "", "| " + " | ".join(headers) + " |",
             "|" + "|".join(["---:"] * len(headers)) + "|"]
    for r in runs:
        m = r.get("metrics", {})
        w = r.get("workers", {})
        wk = f"{w.get('xbrl', w.get('phase1', '?'))}/{w.get('pdf', '-')}"
        lines.append(
            f"| {wk} | {m.get('elapsed_sec', '-')} | {m.get('avg_sec_per_filing', '-')} | "
            f"{m.get('filing_ok_xbrl', m.get('ok_xbrl', '-'))} | {m.get('filing_ok_pdf', m.get('ok_pdf', '-'))} | {m.get('filing_needs_pdf', m.get('needs_pdf', '-'))} | "
            f"{m.get('xbrl_success_rate', '-')} | {m.get('pdf_fallback_rate', '-')} | "
            f"{m.get('filing_quarantined', m.get('quarantined', '-'))} |"
        )
    lines.append("")
    return "\n".join(lines)
