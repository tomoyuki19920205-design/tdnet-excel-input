#!/usr/bin/env python3
"""tools/investigate_quarantine.py — quarantine ケースの詳細調査

too_few_valid_segments と no_records の個別ケースを深掘りする。
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("investigate")
logger.setLevel(logging.INFO)


def load_benchmark_results() -> dict:
    """最新のベンチマーク結果を読み込む。"""
    data_dir = Path("data")
    candidates = sorted(data_dir.glob("xbrl_coverage_benchmark_*.json"), reverse=True)
    if not candidates:
        print("ERROR: No benchmark results found")
        sys.exit(1)
    path = candidates[0]
    print(f"Loading: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def investigate_too_few(results: list[dict]) -> list[dict]:
    """too_few_valid_segments / too_few_sales ケースを深掘り。"""
    from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip
    from lib.backfill.cache import ensure_cache_layout, has_xbrl

    cases = [r for r in results
             if r["hard_fail_reason"] in ("too_few_valid_segments", "too_few_sales")]

    analysis = []
    for r in cases:
        fid = r["filing_id"]
        ticker = r["ticker"]
        cache_root = "data/tdnet_cache"
        paths = ensure_cache_layout(cache_root, fid)

        info = {
            "filing_id": fid,
            "ticker": ticker,
            "hard_fail_reason": r["hard_fail_reason"],
            "segment_count": r["segment_count"],
            "valid_segment_count": r["valid_segment_count"],
            "confidence": r["confidence"],
        }

        # メタデータ読み取り
        if paths.metadata_json.exists():
            meta = json.loads(paths.metadata_json.read_text(encoding="utf-8"))
            info["title"] = meta.get("title", "")
            info["disclosure_date"] = meta.get("disclosure_date", "")
        else:
            info["title"] = ""
            info["disclosure_date"] = ""

        # XBRL ZIP から直接 segment 行を再抽出
        if has_xbrl(paths):
            try:
                rows = extract_segments_from_xbrl_zip(str(paths.xbrl_zip))
                info["raw_row_count"] = len(rows) if rows else 0
                if rows:
                    info["raw_rows"] = []
                    for row in rows:
                        info["raw_rows"].append({
                            "segment_name": row.raw_segment_name,
                            "normalized_name": row.normalized_segment_name,
                            "sales": row.sales,
                            "profit": row.profit,
                            "period": row.period,
                            "quarter": row.quarter,
                            "member_local": getattr(row, "member_local_name", ""),
                            "axis": getattr(row, "axis_name", ""),
                            "context": getattr(row, "context_id", ""),
                        })
            except Exception as e:
                info["raw_row_count"] = -1
                info["extraction_error"] = str(e)[:200]
        else:
            info["raw_row_count"] = -1
            info["extraction_error"] = "no_xbrl_zip"

        # validation の詳細
        # extract_segments_result.json があれば読む
        seg_result_path = paths.extract_segments_result_json
        if seg_result_path.exists():
            seg_data = json.loads(seg_result_path.read_text(encoding="utf-8"))
            info["saved_segments"] = seg_data
        else:
            info["saved_segments"] = []

        # quarantine.json があれば読む
        if paths.quarantine_json.exists():
            q_data = json.loads(paths.quarantine_json.read_text(encoding="utf-8"))
            info["quarantine_detail"] = q_data
        else:
            info["quarantine_detail"] = {}

        analysis.append(info)

    return analysis


def investigate_no_records_sample(results: list[dict], sample_size: int = 80) -> list[dict]:
    """no_records ケースの層化サンプル調査。"""
    from lib.backfill.cache import ensure_cache_layout, has_xbrl
    import zipfile

    cases = [r for r in results if r["hard_fail_reason"] == "no_records"]

    # 層化: disclosure_date の月別に均等サンプリング
    by_month: dict[str, list] = {}
    for c in cases:
        m = c.get("metrics", {})
        # disclosure_date がない場合があるのでメタデータから取る
        cache_root = "data/tdnet_cache"
        fid = c["filing_id"]
        paths = ensure_cache_layout(cache_root, fid)
        if paths.metadata_json.exists():
            meta = json.loads(paths.metadata_json.read_text(encoding="utf-8"))
            dd = meta.get("disclosure_date", "")
        else:
            dd = ""
        month = dd[:7] if dd else "unknown"
        by_month.setdefault(month, []).append(c)

    # 各月から均等にサンプリング
    sample = []
    months = sorted(by_month.keys())
    per_month = max(1, sample_size // len(months)) if months else sample_size
    for m in months:
        sample.extend(by_month[m][:per_month])
    sample = sample[:sample_size]

    analysis = []
    for r in sample:
        fid = r["filing_id"]
        ticker = r["ticker"]
        paths = ensure_cache_layout("data/tdnet_cache", fid)

        info = {
            "filing_id": fid,
            "ticker": ticker,
        }

        # メタデータ
        if paths.metadata_json.exists():
            meta = json.loads(paths.metadata_json.read_text(encoding="utf-8"))
            info["title"] = meta.get("title", "")
            info["disclosure_date"] = meta.get("disclosure_date", "")
        else:
            info["title"] = ""
            info["disclosure_date"] = ""

        # XBRL ZIP の中身を調査
        info["classification"] = "unknown"
        info["xbrl_zip_exists"] = has_xbrl(paths)

        if has_xbrl(paths):
            try:
                with zipfile.ZipFile(str(paths.xbrl_zip), "r") as zf:
                    names = zf.namelist()
                    info["zip_file_count"] = len(names)
                    # xbrl / ixbrl ファイルを探す
                    xbrl_files = [n for n in names if n.endswith((".xbrl", ".xml", ".htm", ".html"))]
                    info["xbrl_files"] = xbrl_files[:10]

                    # セグメント関連の namespace/element を探す
                    segment_hints = []
                    member_hints = []
                    axis_hints = []
                    for xf in xbrl_files[:5]:  # 最大5ファイルをスキャン
                        try:
                            content = zf.read(xf).decode("utf-8", errors="replace")
                            # segment axis を探す
                            if "OperatingSegments" in content or "BusinessSegment" in content:
                                segment_hints.append(xf)
                            if "Segment" in content and "Member" in content:
                                member_hints.append(xf)
                            if "Axis" in content:
                                axis_hints.append(xf)
                            # member/axis パターンの抽出
                            import re
                            members = re.findall(r'(\w+Member)\b', content)
                            axes = re.findall(r'(\w+Axis)\b', content)
                            if members:
                                info.setdefault("found_members", []).extend(
                                    list(set(members))[:20]
                                )
                            if axes:
                                info.setdefault("found_axes", []).extend(
                                    list(set(axes))[:10]
                                )
                        except Exception:
                            pass

                    info["has_segment_content"] = bool(segment_hints)
                    info["has_member_content"] = bool(member_hints)
                    info["has_axis_content"] = bool(axis_hints)

                    # 分類
                    if not segment_hints and not member_hints:
                        info["classification"] = "A_no_segment_facts"
                    elif segment_hints and not member_hints:
                        info["classification"] = "C_member_pattern_miss"
                    elif member_hints and segment_hints:
                        info["classification"] = "B_extractor_miss"
                    else:
                        info["classification"] = "D_context_filter_miss"

            except Exception as e:
                info["classification"] = "error"
                info["error"] = str(e)[:200]
        else:
            info["classification"] = "no_xbrl_zip"

        # found_members の dedup
        if "found_members" in info:
            info["found_members"] = list(set(info["found_members"]))[:20]
        if "found_axes" in info:
            info["found_axes"] = list(set(info["found_axes"]))[:10]

        analysis.append(info)

    return analysis


def main():
    bench = load_benchmark_results()
    results = bench["results"]

    print(f"\n{'=' * 70}")
    print(f"  Quarantine Investigation")
    print(f"{'=' * 70}\n")

    # ── too_few_valid_segments ──
    print(f"  [1] too_few_valid_segments / too_few_sales — 個別調査")
    print(f"  {'─' * 60}\n")

    too_few = investigate_too_few(results)
    for i, case in enumerate(too_few):
        print(f"  Case {i+1}: {case['ticker']} ({case['disclosure_date']}) — {case['hard_fail_reason']}")
        print(f"    title: {case.get('title', '')[:60]}")
        print(f"    raw_row_count: {case['raw_row_count']}")
        print(f"    valid_segment_count: {case['valid_segment_count']}")
        print(f"    confidence: {case['confidence']}")

        if case.get("raw_rows"):
            print(f"    raw_rows:")
            for row in case["raw_rows"]:
                name = row["segment_name"]
                norm = row["normalized_name"]
                sales = row["sales"]
                profit = row["profit"]
                member = row.get("member_local", "")
                print(f"      {name:30s} | sales={sales} | profit={profit} | member={member}")
        if case.get("extraction_error"):
            print(f"    extraction_error: {case['extraction_error']}")
        print()

    # ── summary ──
    print(f"\n  too_few summary:")
    reasons = Counter(c["hard_fail_reason"] for c in too_few)
    for reason, count in reasons.most_common():
        print(f"    {reason}: {count}")
    row_counts = [c["raw_row_count"] for c in too_few if c["raw_row_count"] >= 0]
    if row_counts:
        print(f"    avg raw_row_count: {sum(row_counts)/len(row_counts):.1f}")
        print(f"    raw_row_count distribution: {sorted(row_counts)}")

    # ── no_records ──
    print(f"\n\n  [2] no_records — 層化サンプル調査 (80件)")
    print(f"  {'─' * 60}\n")

    no_records = investigate_no_records_sample(results, sample_size=80)
    classification_counts = Counter(c["classification"] for c in no_records)

    print(f"  Classification distribution:")
    for cls, count in classification_counts.most_common():
        print(f"    {cls:35s}: {count}")
    print()

    # member パターン集計
    all_members = []
    all_axes = []
    for c in no_records:
        all_members.extend(c.get("found_members", []))
        all_axes.extend(c.get("found_axes", []))

    member_counts = Counter(all_members)
    axis_counts = Counter(all_axes)

    if member_counts:
        print(f"  Found Member patterns (top 20):")
        for m, count in member_counts.most_common(20):
            print(f"    {m:40s}: {count}")
    print()

    if axis_counts:
        print(f"  Found Axis patterns (top 10):")
        for a, count in axis_counts.most_common(10):
            print(f"    {a:40s}: {count}")
    print()

    # B/C/D 分類のケースを列挙
    for cls in ["B_extractor_miss", "C_member_pattern_miss", "D_context_filter_miss"]:
        cases_cls = [c for c in no_records if c["classification"] == cls]
        if cases_cls:
            print(f"\n  [{cls}] — {len(cases_cls)} cases:")
            for c in cases_cls[:5]:
                print(f"    {c['ticker']} ({c.get('disclosure_date','')}) {c.get('title','')[:50]}")
                if c.get("found_members"):
                    print(f"      members: {c['found_members'][:5]}")

    # ── 結果 JSON 保存 ──
    ts = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d_%H%M%S")
    out_path = f"data/quarantine_investigation_{ts}.json"
    summary = {
        "too_few_cases": too_few,
        "no_records_sample": no_records,
        "no_records_classification": dict(classification_counts),
        "member_patterns": dict(member_counts.most_common(30)),
        "axis_patterns": dict(axis_counts.most_common(10)),
    }
    Path(out_path).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\n  Results saved: {out_path}")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
