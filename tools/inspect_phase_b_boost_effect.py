"""tools/inspect_phase_b_boost_effect.py — Phase B-boost 効果検証"""
import argparse, glob, json, os, re, sys


def _get(r, *keys, default=None):
    for k in keys:
        v = r.get(k)
        if v is not None:
            return v
    return default


def _trace_lines(r):
    """trace/debug/diagnostics を文字列リストに正規化"""
    for k in ("rule_trace", "trace", "debug", "diagnostics"):
        v = r.get(k)
        if v is None:
            continue
        if isinstance(v, list):
            return [str(x) for x in v]
        if isinstance(v, str):
            return v.split("\n")
        if isinstance(v, dict):
            return [f"{kk}: {vv}" for kk, vv in v.items()]
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--grep", default="")
    args = ap.parse_args()

    path = args.input
    if not path:
        candidates = sorted(glob.glob("logs/backfill_segments_tdnet_v2_*.jsonl"))
        if not candidates:
            print("ERROR: no v2 jsonl found"); return
        path = candidates[-1]
        print(f"Auto-selected: {path}")

    records = []
    skipped = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line.strip())
            except Exception:
                skipped += 1; continue
            if r.get("event") != "filing_result":
                continue
            if args.grep:
                g = args.grep.lower()
                haystack = " ".join(str(_get(r, "ticker", "code", "local_code", default="")),
                                    ).lower()
                haystack += " " + str(_get(r, "filing_id", "doc_id", default="")).lower()
                haystack += " " + str(_get(r, "company", "company_name", "filer_name", default="")).lower()
                if g not in haystack:
                    continue
            records.append(r)
            if args.limit and len(records) >= args.limit:
                break

    # --- Phase B-boost trace 解析 ---
    tables_seen = 0
    tables_kept = 0
    tables_rejected = 0
    boosted_tables = 0
    reject_reasons = {}
    boost_hits = {"kw_bonus_hits": 0, "header_bonus_hits": 0}
    base_scores = []
    final_scores = []
    missing_trace = 0

    # サンプル収集
    samples_boosted = []
    samples_density_reject = []
    samples_narrative_reject = []

    for r in records:
        tlines = _trace_lines(r)
        has_boost_trace = any("Phase B-boost" in l for l in tlines)

        if not has_boost_trace:
            missing_trace += 1
            # スコア情報から推定
            all_ts = r.get("score_summary", {}).get("all_table_scores", []) if isinstance(r.get("score_summary"), dict) else []
            tables_seen += len(all_ts) if all_ts else 0
            tables_kept += len(all_ts) if all_ts else 0
            for ts_info in (all_ts or []):
                s = ts_info.get("score", 0) if isinstance(ts_info, dict) else 0
                base_scores.append(s)
                final_scores.append(s)
            continue

        filing_id = _get(r, "filing_id", "doc_id", default="?")
        ticker = _get(r, "ticker", "code", "local_code", default="?")
        company = _get(r, "company", "company_name", "filer_name", default="")

        for tl in tlines:
            if "Phase B-boost: skip" in tl:
                tables_seen += 1
                tables_rejected += 1
                if "rows=" in tl and "(<3)" in tl:
                    reject_reasons["too_few_rows"] = reject_reasons.get("too_few_rows", 0) + 1
                elif "num_density=" in tl and "(<0.3)" in tl:
                    reject_reasons["low_numeric_density"] = reject_reasons.get("low_numeric_density", 0) + 1
                    if len(samples_density_reject) < 5:
                        samples_density_reject.append(
                            f"{filing_id}  ticker={ticker}  {tl.split('Phase B-boost: ')[-1][:80]}"
                        )
                elif "narrative" in tl:
                    reject_reasons["narrative_ratio_high"] = reject_reasons.get("narrative_ratio_high", 0) + 1
                    if len(samples_narrative_reject) < 5:
                        samples_narrative_reject.append(
                            f"{filing_id}  ticker={ticker}  {tl.split('Phase B-boost: ')[-1][:80]}"
                        )
                else:
                    reject_reasons["other"] = reject_reasons.get("other", 0) + 1

            elif "seg_kw_bonus" in tl:
                boost_hits["kw_bonus_hits"] += 1
                tables_seen += 1
                tables_kept += 1
                boosted_tables += 1
                if len(samples_boosted) < 5:
                    samples_boosted.append(
                        f"{filing_id}  ticker={ticker}  company={company[:20]}  {tl.split('Phase B-boost: ')[-1][:60]}"
                    )

            elif "header_label_boost" in tl:
                boost_hits["header_bonus_hits"] += 1
                tables_seen += 1
                tables_kept += 1
                boosted_tables += 1
                if len(samples_boosted) < 5:
                    samples_boosted.append(
                        f"{filing_id}  ticker={ticker}  company={company[:20]}  {tl.split('Phase B-boost: ')[-1][:60]}"
                    )

        # スコア情報
        all_ts = r.get("score_summary", {}).get("all_table_scores", []) if isinstance(r.get("score_summary"), dict) else []
        for ts_info in (all_ts or []):
            if isinstance(ts_info, dict):
                s = ts_info.get("score", 0)
                final_scores.append(s)
                base_scores.append(s)

    # --- [4] OUTCOME DIFF HINTS ---
    outcome_counts = {}
    for r in records:
        st = _get(r, "worker_status", "status", "final_status", default="unknown")
        outcome_counts[st] = outcome_counts.get(st, 0) + 1
    hint_counts = {}
    for r in records:
        h = _get(r, "quarantine_reason", "review_hint", "hint", "quarantine_hint", "reason", default="")
        if h:
            hint_counts[h] = hint_counts.get(h, 0) + 1

    # === OUTPUT ===
    def avg(vals):
        return sum(vals) / len(vals) if vals else 0.0

    print(f"\n{'='*55}")
    print("  [1] BOOST SUMMARY")
    print(f"{'='*55}")
    print(f"  total_filings:     {len(records)}")
    print(f"  skipped_lines:     {skipped}")
    print(f"  missing_trace:     {missing_trace}")
    print(f"  tables_seen:       {tables_seen}")
    print(f"  tables_kept:       {tables_kept}")
    print(f"  tables_rejected:   {tables_rejected}")
    print(f"  boosted_tables:    {boosted_tables}")
    print(f"  avg_base_score:    {avg(base_scores):.2f}")
    print(f"  avg_final_score:   {avg(final_scores):.2f}")

    print(f"\n{'='*55}")
    print("  [2] REJECT REASON COUNTS")
    print(f"{'='*55}")
    for reason, cnt in sorted(reject_reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason:35s} {cnt}")
    if not reject_reasons:
        print("  (none)")

    print(f"\n{'='*55}")
    print("  [3] BOOST HIT COUNTS")
    print(f"{'='*55}")
    for k, v in boost_hits.items():
        print(f"  {k:35s} {v}")

    print(f"\n{'='*55}")
    print("  [4] OUTCOME DIFF HINTS")
    print(f"{'='*55}")
    print("  -- status --")
    for st, cnt in sorted(outcome_counts.items(), key=lambda x: -x[1]):
        print(f"  {st:35s} {cnt}")
    print("  -- quarantine reasons --")
    target_hints = [
        "pdf_narrative_block_selected",
        "pdf_segment_like_but_invalid_structure",
        "pdf_no_sales_profit_columns",
    ]
    for h in target_hints:
        print(f"  {h:35s} {hint_counts.get(h, 0)}")
    for h, cnt in sorted(hint_counts.items(), key=lambda x: -x[1]):
        if h not in target_hints:
            print(f"  {h:35s} {cnt}")

    print(f"\n{'='*55}")
    print("  [5] SAMPLE CASES")
    print(f"{'='*55}")
    print("  -- boosted (passed) --")
    for s in samples_boosted[:5]:
        print(f"    {s}")
    if not samples_boosted:
        print("    (none)")
    print("  -- density rejected --")
    for s in samples_density_reject[:5]:
        print(f"    {s}")
    if not samples_density_reject:
        print("    (none)")
    print("  -- narrative rejected --")
    for s in samples_narrative_reject[:5]:
        print(f"    {s}")
    if not samples_narrative_reject:
        print("    (none)")
    print(f"{'='*55}")


if __name__ == "__main__":
    main()
