#!/usr/bin/env python3
"""tools/benchmark_xbrl_coverage.py — XBRL coverage 改善ベンチマーク

既存の 500 件 filing list を使い、XBRL URL 推定 + v2 worker で
coverage メトリクスを詳細に出力する。

使い方:
  .\.venv\Scripts\python.exe tools\benchmark_xbrl_coverage.py
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows cp932 対策
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.backfill.xbrl_url_inference import infer_xbrl_url_from_pdf
from lib.backfill.listing_sources.base import FilingInfo
from lib.backfill.worker_v2 import process_one_filing_v2

JST = timezone(timedelta(hours=9))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("benchmark")
logger.setLevel(logging.INFO)


# ============================================================
# メイン
# ============================================================

def main():
    sample_path = "data/precision_sample_500_20260315_172519.json"
    if not Path(sample_path).exists():
        print(f"ERROR: {sample_path} not found")
        sys.exit(1)

    raw = json.loads(Path(sample_path).read_text(encoding="utf-8"))
    total = len(raw)
    print(f"\n{'=' * 70}")
    print(f"  XBRL Coverage Benchmark — {total} filings")
    print(f"  {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S JST')}")
    print(f"{'=' * 70}\n")

    # ── Filing enrichment: XBRL URL 推定 ──
    filings = []
    xbrl_url_inferred_count = 0
    for item in raw:
        doc_url = item.get("doc_url", "")
        inferred = infer_xbrl_url_from_pdf(doc_url)
        if inferred:
            xbrl_url_inferred_count += 1

        fi = FilingInfo(
            filing_id=item["filing_id"],
            ticker=item["ticker"],
            title=item["title"],
            disclosure_date=item["disclosure_date"],
            doc_url=doc_url,
            xbrl_url=inferred,  # 推定 URL
            doc_type="financial_statement",
            company_name="",
            published_at=item.get("disclosure_date", ""),
            listing_source="benchmark",
            has_xbrl=False,  # 未確認 (ダウンロード段階で確認)
            xbrl_url_inferred=inferred is not None,
        )
        filings.append(fi)

    print(f"  xbrl_url_inferred_count: {xbrl_url_inferred_count}/{total}")
    print()

    # ── V2 worker 実行 ──
    cache_root = "data/tdnet_cache"
    results = []
    t0 = time.monotonic()

    for i, fi in enumerate(filings):
        if (i + 1) % 50 == 0 or i == 0:
            elapsed = time.monotonic() - t0
            logger.info(f"Progress: {i+1}/{total} ({elapsed:.0f}s)")

        try:
            r = process_one_filing_v2(
                fi,
                cache_root=cache_root,
                retry_download=2,
                retry_xbrl=1,
                timeout_download=20,
                timeout_xbrl=30,
                sleep_fn=lambda x: None,
            )
            results.append({
                "filing_id": fi.filing_id,
                "ticker": fi.ticker,
                "status": r.status,
                "source": r.source,
                "selected_path": r.selected_path,
                "hard_fail_reason": r.hard_fail_reason,
                "quarantine_reason": r.quarantine_reason,
                "segment_count": len(r.segment_records),
                "valid_segment_count": r.valid_segment_count,
                "confidence": r.confidence,
                "xbrl_url_inferred": fi.xbrl_url_inferred,
                "xbrl_url": fi.xbrl_url or "",
                "metrics": r.metrics,
            })
        except Exception as e:
            results.append({
                "filing_id": fi.filing_id,
                "ticker": fi.ticker,
                "status": "error",
                "source": "",
                "selected_path": "none",
                "hard_fail_reason": str(e)[:200],
                "quarantine_reason": "benchmark_error",
                "segment_count": 0,
                "valid_segment_count": 0,
                "confidence": 0.0,
                "xbrl_url_inferred": fi.xbrl_url_inferred,
                "xbrl_url": fi.xbrl_url or "",
                "metrics": {},
            })

    elapsed_total = time.monotonic() - t0

    # ── メトリクス集計 ──
    xbrl_source_available = 0
    xbrl_source_unavailable = 0
    xbrl_segment_facts_present = 0
    xbrl_segment_no_facts = 0
    xbrl_extraction_error = 0
    xbrl_segment_success = 0
    http_errors = {}

    for r in results:
        m = r.get("metrics", {})
        xbrl_resolved = m.get("xbrl_resolved", False)
        error_class = m.get("xbrl_download_error_class", "")

        if xbrl_resolved:
            xbrl_source_available += 1
        elif r["xbrl_url_inferred"]:
            xbrl_source_unavailable += 1
            if error_class:
                http_errors[error_class] = http_errors.get(error_class, 0) + 1

        # segment classification (only for xbrl_source_available)
        if xbrl_resolved:
            hfr = r["hard_fail_reason"]
            status = r["status"]
            if status in ("ok", "partial"):
                xbrl_segment_success += 1
                xbrl_segment_facts_present += 1
            elif hfr == "no_records" or "xbrl_no_segment_facts" in str(hfr):
                xbrl_segment_no_facts += 1
            elif hfr and "too_few" in str(hfr):
                xbrl_segment_facts_present += 1
                # partial facts but validation failed
            elif hfr:
                # has facts but validation issue
                xbrl_segment_facts_present += 1
                xbrl_extraction_error += 1
            else:
                xbrl_segment_no_facts += 1

    # 推定なし (URL パターン不一致) の件数
    no_inference = total - xbrl_url_inferred_count

    # ── 出力 ──
    print(f"\n{'=' * 70}")
    print(f"  XBRL Coverage Benchmark Results")
    print(f"{'=' * 70}\n")

    print(f"  [1] URL Inference")
    print(f"  {'total filings':40s}: {total}")
    print(f"  {'xbrl_url_inferred_count':40s}: {xbrl_url_inferred_count} ({xbrl_url_inferred_count/total*100:.1f}%)")
    print(f"  {'no_inference (non-1401 pattern)':40s}: {no_inference}")
    print()

    print(f"  [2] XBRL Source Availability")
    print(f"  {'xbrl_source_available_count':40s}: {xbrl_source_available} ({xbrl_source_available/total*100:.1f}%)")
    print(f"  {'xbrl_source_unavailable_count':40s}: {xbrl_source_unavailable} ({xbrl_source_unavailable/total*100:.1f}%)")
    print(f"  {'xbrl_source_available / total':40s}: {xbrl_source_available}/{total} = {xbrl_source_available/total*100:.1f}%")
    print()

    print(f"  [3] XBRL Segment Extraction")
    sfp = xbrl_segment_facts_present
    sa = xbrl_source_available or 1
    print(f"  {'xbrl_segment_facts_present_count':40s}: {xbrl_segment_facts_present}")
    print(f"  {'xbrl_segment_no_facts_count':40s}: {xbrl_segment_no_facts}")
    print(f"  {'xbrl_extraction_error_count':40s}: {xbrl_extraction_error}")
    print(f"  {'xbrl_segment_success_count':40s}: {xbrl_segment_success}")
    print(f"  {'xbrl_segment_facts / available':40s}: {xbrl_segment_facts_present}/{xbrl_source_available} = {xbrl_segment_facts_present/sa*100:.1f}%")
    ss = xbrl_segment_success
    print(f"  {'xbrl_segment_success / facts':40s}: {ss}/{sfp or 1} = {ss/(sfp or 1)*100:.1f}%")
    print(f"  {'xbrl_segment_success / total':40s}: {ss}/{total} = {ss/total*100:.1f}%")
    print()

    print(f"  [4] HTTP Error Distribution (inferred URL download failures)")
    if http_errors:
        for err, count in sorted(http_errors.items(), key=lambda x: -x[1]):
            print(f"    {err:30s}: {count}")
    else:
        print(f"    (no errors)")
    print()

    # ── hard_fail_reason 集計 ──
    print(f"  [5] Quarantine Reason Distribution")
    reason_dist = {}
    for r in results:
        if r["status"] in ("quarantined", "failed"):
            hr = r["hard_fail_reason"] or r["quarantine_reason"] or "unknown"
            reason_dist[hr] = reason_dist.get(hr, 0) + 1
    for reason, count in sorted(reason_dist.items(), key=lambda x: -x[1]):
        print(f"    {reason:40s}: {count}")
    print()

    # ── xbrl_source 分類 ──
    print(f"  [6] XBRL Source Distribution")
    source_dist = {}
    for r in results:
        m = r.get("metrics", {})
        src = m.get("xbrl_source", "unknown")
        source_dist[src] = source_dist.get(src, 0) + 1
    for src, count in sorted(source_dist.items(), key=lambda x: -x[1]):
        print(f"    {src:30s}: {count}")
    print()

    print(f"  [7] Timing")
    print(f"  {'elapsed':40s}: {elapsed_total:.1f}s")
    print(f"  {'per filing':40s}: {elapsed_total/total:.2f}s")
    print()

    # ── 結果 JSON 保存 ──
    ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    out_path = f"data/xbrl_coverage_benchmark_{ts}.json"
    summary = {
        "timestamp": ts,
        "total": total,
        "xbrl_url_inferred_count": xbrl_url_inferred_count,
        "xbrl_source_available_count": xbrl_source_available,
        "xbrl_source_unavailable_count": xbrl_source_unavailable,
        "xbrl_segment_facts_present_count": xbrl_segment_facts_present,
        "xbrl_segment_no_facts_count": xbrl_segment_no_facts,
        "xbrl_extraction_error_count": xbrl_extraction_error,
        "xbrl_segment_success_count": xbrl_segment_success,
        "http_errors": http_errors,
        "reason_distribution": reason_dist,
        "source_distribution": source_dist,
        "elapsed_sec": round(elapsed_total, 1),
        "results": results,
    }
    Path(out_path).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"  Results saved: {out_path}")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
