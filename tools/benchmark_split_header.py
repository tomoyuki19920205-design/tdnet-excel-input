#!/usr/bin/env python3
"""
benchmark_split_header.py — 分割ヘッダー復元ベンチマーク

quarantine 済み案件に対して ON/OFF 比較を行い、
rescued / regressed / ambiguous 件数を計測する。

使い方:
  python tools/benchmark_split_header.py --limit 20
  python tools/benchmark_split_header.py --review-hint pdf_no_sales_profit_columns
  python tools/benchmark_split_header.py --compare-success --limit 50
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path


def _find_quarantine_review_files(data_dir: str) -> list[Path]:
    """quarantine review JSONL を探す."""
    results = []
    for root, dirs, files in os.walk(data_dir):
        for f in files:
            if f.startswith("quarantine_review") and f.endswith(".jsonl"):
                results.append(Path(root) / f)
    return results


def _load_quarantine_entries(
    review_files: list[Path],
    only_hint: str = "",
    limit: int = 0,
) -> list[dict]:
    entries = []
    for fp in review_files:
        with open(fp, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if only_hint and entry.get("review_hint", "") != only_hint:
                    continue
                entries.append(entry)
                if limit and len(entries) >= limit:
                    return entries
    return entries


def _test_reconstruction_on_headers(header_lines: list[str], enable: bool) -> dict:
    """ヘッダー行に対してreconstruction ON/OFF でスコアリング."""
    from src.analysis.header_reconstruction import reconstruct_from_lines, score_metric_header

    result = reconstruct_from_lines(header_lines, enable_reconstruction=enable)
    headers = result.reconstructed_headers

    best_sales = 0.0
    best_profit = 0.0
    best_sales_col = -1
    best_profit_col = -1
    details = []

    for i, h in enumerate(headers):
        scores = score_metric_header(h)
        s_score = scores["sales"].total_score
        p_score = scores["profit"].total_score
        details.append({
            "col": i,
            "header": h,
            "sales_score": s_score,
            "profit_score": p_score,
            "sales_match": scores["sales"].matched_terms[:2],
            "profit_match": scores["profit"].matched_terms[:2],
        })
        if s_score > best_sales:
            best_sales = s_score
            best_sales_col = i
        if p_score > best_profit:
            best_profit = p_score
            best_profit_col = i

    return {
        "headers": headers,
        "best_sales_score": best_sales,
        "best_profit_score": best_profit,
        "best_sales_col": best_sales_col,
        "best_profit_col": best_profit_col,
        "has_sales": best_sales >= 50,
        "has_profit": best_profit >= 50,
        "details": details,
        "steps": result.steps,
        "reconstruction_enabled": enable,
    }


def main():
    parser = argparse.ArgumentParser(description="分割ヘッダー復元ベンチマーク")
    parser.add_argument("--data-dir", default="data/quarantine_review", help="quarantine review dir")
    parser.add_argument("--review-hint", default="pdf_no_sales_profit_columns")
    parser.add_argument("--limit", type=int, default=0, help="0=all")
    parser.add_argument("--compare-success", action="store_true", help="成功済サンプルも比較 (regression check)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    review_files = _find_quarantine_review_files(args.data_dir)
    if not review_files:
        print(f"[WARN] No quarantine review files found in {args.data_dir}")
        return

    entries = _load_quarantine_entries(review_files, only_hint=args.review_hint, limit=args.limit)
    print(f"\n=== Benchmark: Split Header Reconstruction ===")
    print(f"  Review files: {len(review_files)}")
    print(f"  Entries loaded: {len(entries)} (hint={args.review_hint})")
    print()

    rescued = []
    unresolved = []
    ambiguous = []
    regressed = []

    for entry in entries:
        header_snapshot = entry.get("header_snapshot", [])
        if not header_snapshot:
            continue

        ticker = entry.get("ticker", "?")
        doc_id = entry.get("doc_id", "?")

        # OFF
        off_result = _test_reconstruction_on_headers(header_snapshot, enable=False)
        # ON
        on_result = _test_reconstruction_on_headers(header_snapshot, enable=True)

        had_before = off_result["has_sales"] or off_result["has_profit"]
        has_after = on_result["has_sales"] or on_result["has_profit"]

        status = "unchanged"
        if not had_before and has_after:
            status = "rescued"
            rescued.append({"ticker": ticker, "doc_id": doc_id, "on": on_result})
        elif had_before and not has_after:
            status = "regressed"
            regressed.append({"ticker": ticker, "doc_id": doc_id, "off": off_result, "on": on_result})
        elif has_after and (on_result["best_sales_score"] > off_result["best_sales_score"] + 10 or
                          on_result["best_profit_score"] > off_result["best_profit_score"] + 10):
            status = "improved"
            rescued.append({"ticker": ticker, "doc_id": doc_id, "on": on_result})
        elif not had_before and not has_after:
            status = "unresolved"
            unresolved.append({"ticker": ticker, "doc_id": doc_id, "on": on_result})
        else:
            status = "unchanged"

        if args.verbose or status in ("rescued", "regressed"):
            print(f"  [{status}] ticker={ticker} doc_id={doc_id}")
            if on_result["steps"]:
                for step in on_result["steps"][:3]:
                    print(f"    merge: {step.get('parts',[])} → {step.get('result','?')} (score={step.get('score','?')})")
            if status == "rescued":
                print(f"    sales={on_result['best_sales_score']:.0f} profit={on_result['best_profit_score']:.0f}")

    print(f"\n=== Results ===")
    print(f"  Total entries:    {len(entries)}")
    print(f"  Rescued:          {len(rescued)}")
    print(f"  Unresolved:       {len(unresolved)}")
    print(f"  Regressed:        {len(regressed)}")
    print(f"  Ambiguous:        {len(ambiguous)}")

    if regressed:
        print(f"\n  ⚠️  REGRESSIONS DETECTED!")
        for r in regressed:
            print(f"    {r['ticker']} / {r['doc_id']}")

    if rescued:
        print(f"\n  ✅ Rescued list:")
        for r in rescued[:20]:
            print(f"    {r['ticker']} / {r['doc_id']}")


if __name__ == "__main__":
    main()
