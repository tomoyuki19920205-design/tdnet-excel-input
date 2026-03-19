"""tools/compare_ab_runs.py — 固定母集団 A/B 比較 (V2 対応版)

使い方:
  python tools/compare_ab_runs.py --before <v1_jsonl> --after <v2_jsonl>

改善/悪化の定義:
  改善(strict):  quarantined/failed → ok
  改善(broad):   quarantined/failed → ok/partial
  悪化(strict):  ok → quarantined/failed
  悪化(broad):   ok/partial → quarantined/failed

V2 固有キー (worker_version=v2):
  selected_path, fallback_used, fallback_reason,
  hard_fail_reason, quarantine_reason,
  valid_segment_count, sales_non_null_count, profit_non_null_count,
  confidence, candidate_summary
"""
import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path


# ── 改善/悪化の判定 ──

_GOOD = {"ok", "partial"}
_BAD  = {"quarantined", "failed"}

def _classify_change(before_status: str, after_status: str) -> str:
    """改善/悪化/据え置きを分類。"""
    if before_status in _BAD and after_status == "ok":
        return "improved_strict"
    if before_status in _BAD and after_status in _GOOD:
        return "improved_broad"
    if before_status == "ok" and after_status in _BAD:
        return "regressed_strict"
    if before_status in _GOOD and after_status in _BAD:
        return "regressed_broad"
    if before_status == after_status:
        return "same"
    return "changed"


def _load_jsonl(path: str) -> dict[str, dict]:
    """JSONL を filing_id → event dict にマップ。"""
    filings = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line.strip())
            if d.get("event") != "filing_result":
                continue
            fid = d.get("filing_id", "")
            if fid:
                filings[fid] = d
    return filings


def _safe_get(d: dict, key: str, default=""):
    """missing key を安全に取得。v1/v2 互換。"""
    return d.get(key, default) or default


def _format_example(e: dict) -> list[str]:
    """1 filing の詳細を複数行で返す。"""
    lines = []
    lines.append(f"  {e['filing_id']} ticker={e['ticker']}")
    lines.append(f"    status:  {e['before_status']:12s} → {e['after_status']}")

    # source / path (v2 only, v1 fallback)
    bv = _safe_get(e, "before_via")
    av = _safe_get(e, "after_via")
    bp = _safe_get(e, "before_selected_path", bv)
    ap = _safe_get(e, "after_selected_path", av)
    if bp or ap:
        lines.append(f"    path:    {bp:12s} → {ap}")
    if bv != bp or av != ap:
        lines.append(f"    via:     {bv:12s} → {av}")

    # reason
    br = _safe_get(e, "before_quarantine_reason") or _safe_get(e, "before_hint")
    ar = _safe_get(e, "after_quarantine_reason") or _safe_get(e, "after_hint")
    if br or ar:
        lines.append(f"    reason:  {br:12s} → {ar}")

    # fallback
    bf = _safe_get(e, "before_fallback_reason")
    af = _safe_get(e, "after_fallback_reason")
    if bf or af:
        lines.append(f"    fallback:{bf:12s} → {af}")

    # counts
    for key_name, label in [
        ("rows", "rows"), ("valid_segment_count", "valid_seg"),
        ("sales_non_null_count", "sales_nn"), ("profit_non_null_count", "profit_nn"),
    ]:
        bk = f"before_{key_name}"
        ak = f"after_{key_name}"
        bval = e.get(bk, 0) or 0
        aval = e.get(ak, 0) or 0
        diff = aval - bval
        if diff != 0:
            lines.append(f"    {label:12s} {bval} → {aval}  ({diff:+d})")

    return lines


def main():
    parser = argparse.ArgumentParser(description="Fixed-population A/B comparison (V2 compatible)")
    parser.add_argument("--before", required=True, help="Before JSONL path (v1 or v2)")
    parser.add_argument("--after", required=True, help="After JSONL path (v1 or v2)")
    parser.add_argument("--output", default=None, help="Output file path (default: stdout)")
    args = parser.parse_args()

    before = _load_jsonl(args.before)
    after = _load_jsonl(args.after)

    # Detect worker versions
    before_wv = "v1"
    after_wv = "v1"
    for d in before.values():
        if d.get("worker_version"):
            before_wv = d["worker_version"]
            break
    for d in after.values():
        if d.get("worker_version"):
            after_wv = d["worker_version"]
            break

    common_ids = sorted(set(before.keys()) & set(after.keys()))
    only_before = sorted(set(before.keys()) - set(after.keys()))
    only_after = sorted(set(after.keys()) - set(before.keys()))

    out_lines: list[str] = []
    def p(s=""):
        out_lines.append(s)

    p(f"{'=' * 70}")
    p(f"Fixed-Population A/B Comparison")
    p(f"{'=' * 70}")
    p(f"Before: {os.path.basename(args.before)}  (worker={before_wv}, total={len(before)})")
    p(f"After:  {os.path.basename(args.after)}  (worker={after_wv}, total={len(after)})")
    p(f"Common filing IDs: {len(common_ids)}")
    p(f"Only in before:    {len(only_before)}")
    p(f"Only in after:     {len(only_after)}")
    p()

    # ── Transition matrix (4x4) ──
    all_statuses = {"ok", "partial", "quarantined", "failed"}
    transitions = Counter()
    entries = []

    for fid in common_ids:
        b = before[fid]
        a = after[fid]
        bs = b.get("status", "?")
        as_ = a.get("status", "?")
        key = f"{bs} → {as_}"
        transitions[key] += 1

        entry = {
            "filing_id": fid,
            "ticker": b.get("ticker", a.get("ticker", "")),
            "before_status": bs,
            "after_status": as_,
            "classification": _classify_change(bs, as_),
            # v1 compatible keys
            "before_hint": _safe_get(b, "review_hint"),
            "after_hint": _safe_get(a, "review_hint"),
            "before_rows": b.get("rows", 0) or 0,
            "after_rows": a.get("rows", 0) or 0,
            "before_via": _safe_get(b, "via"),
            "after_via": _safe_get(a, "via"),
            "before_sales_non_null_count": b.get("sales_non_null_count", 0) or 0,
            "after_sales_non_null_count": a.get("sales_non_null_count", 0) or 0,
            "before_profit_non_null_count": b.get("profit_non_null_count", 0) or 0,
            "after_profit_non_null_count": a.get("profit_non_null_count", 0) or 0,
            # v2 keys (safe fallback for v1)
            "before_selected_path": _safe_get(b, "selected_path", _safe_get(b, "via")),
            "after_selected_path": _safe_get(a, "selected_path", _safe_get(a, "via")),
            "before_fallback_used": b.get("fallback_used", False),
            "after_fallback_used": a.get("fallback_used", False),
            "before_fallback_reason": _safe_get(b, "fallback_reason"),
            "after_fallback_reason": _safe_get(a, "fallback_reason"),
            "before_quarantine_reason": _safe_get(b, "quarantine_reason"),
            "after_quarantine_reason": _safe_get(a, "quarantine_reason"),
            "before_hard_fail_reason": _safe_get(b, "hard_fail_reason"),
            "after_hard_fail_reason": _safe_get(a, "hard_fail_reason"),
            "before_valid_segment_count": b.get("valid_segment_count", 0) or 0,
            "after_valid_segment_count": a.get("valid_segment_count", 0) or 0,
            "before_confidence": b.get("confidence", 0) or 0,
            "after_confidence": a.get("confidence", 0) or 0,
            "before_candidate_summary": _safe_get(b, "candidate_summary"),
            "after_candidate_summary": _safe_get(a, "candidate_summary"),
        }
        entries.append(entry)

    # ── 1. Transition Matrix ──
    p(f"--- Transition Matrix (fixed population: {len(common_ids)}) ---")
    for key, count in sorted(transitions.items()):
        p(f"  {key}: {count}")
    p()

    # ── 2. 改善/悪化サマリ ──
    improved_strict = [e for e in entries if e["classification"] == "improved_strict"]
    improved_broad  = [e for e in entries if e["classification"] in ("improved_strict", "improved_broad")]
    regressed_strict = [e for e in entries if e["classification"] == "regressed_strict"]
    regressed_broad  = [e for e in entries if e["classification"] in ("regressed_strict", "regressed_broad")]
    same = [e for e in entries if e["classification"] == "same"]

    p(f"--- Improvement / Regression Summary ---")
    p(f"  Improved (strict: bad→ok):        {len(improved_strict)}")
    p(f"  Improved (broad:  bad→ok/partial): {len(improved_broad)}")
    p(f"  Regressed (strict: ok→bad):        {len(regressed_strict)}")
    p(f"  Regressed (broad:  ok/partial→bad): {len(regressed_broad)}")
    p(f"  Same status:                        {len(same)}")
    p()

    # ── 3. selected_path change ──
    path_changes = Counter()
    for e in entries:
        bp = e["before_selected_path"] or "?"
        ap = e["after_selected_path"] or "?"
        if bp != ap:
            path_changes[f"{bp} → {ap}"] += 1
    if path_changes:
        p(f"--- selected_path changes ---")
        for k, v in sorted(path_changes.items(), key=lambda x: -x[1]):
            p(f"  {k}: {v}")
        p()

    # ── 4. fallback_reason breakdown ──
    before_fb = Counter(_safe_get(e, "before_fallback_reason") for e in entries if e.get("before_fallback_used"))
    after_fb  = Counter(_safe_get(e, "after_fallback_reason") for e in entries if e.get("after_fallback_used"))
    before_fb.pop("", None)
    after_fb.pop("", None)
    if before_fb or after_fb:
        p(f"--- fallback_reason breakdown ---")
        all_fb_keys = sorted(set(before_fb.keys()) | set(after_fb.keys()))
        for k in all_fb_keys:
            p(f"  {k:30s} before={before_fb.get(k, 0):3d}  after={after_fb.get(k, 0):3d}")
        p()

    # ── 5. quarantine_reason breakdown ──
    before_qr = Counter()
    after_qr = Counter()
    for e in entries:
        if e["before_status"] in _BAD:
            r = e["before_quarantine_reason"] or e["before_hint"] or "unknown"
            before_qr[r] += 1
        if e["after_status"] in _BAD:
            r = e["after_quarantine_reason"] or e["after_hint"] or "unknown"
            after_qr[r] += 1
    if before_qr or after_qr:
        p(f"--- quarantine/fail reason breakdown ---")
        all_qr_keys = sorted(set(before_qr.keys()) | set(after_qr.keys()))
        for k in all_qr_keys:
            p(f"  {k:35s} before={before_qr.get(k, 0):3d}  after={after_qr.get(k, 0):3d}")
        p()

    # ── 6. Quality metrics ──
    quality_keys = [
        ("rows", "rows"),
        ("valid_segment_count", "valid_seg_count"),
        ("sales_non_null_count", "sales_nn"),
        ("profit_non_null_count", "profit_nn"),
    ]
    ok_both = [e for e in entries if e["before_status"] in _GOOD and e["after_status"] in _GOOD]
    if ok_both:
        p(f"--- Quality Comparison (both ok/partial: {len(ok_both)} filings) ---")
        for key, label in quality_keys:
            bsum = sum(e.get(f"before_{key}", 0) for e in ok_both)
            asum = sum(e.get(f"after_{key}", 0) for e in ok_both)
            diff = asum - bsum
            p(f"  {label:20s} {bsum:6d} → {asum:6d}  (diff={diff:+d})")
        p()

    # ── 7. Representative examples ──
    def _pick(lst, n=3):
        return lst[:n]

    p(f"--- Representative Examples: Improved ({len(improved_broad)}) ---")
    for e in _pick(improved_broad):
        for line in _format_example(e):
            p(line)
    if not improved_broad:
        p("  (none)")
    p()

    p(f"--- Representative Examples: Regressed ({len(regressed_broad)}) ---")
    for e in _pick(regressed_broad):
        for line in _format_example(e):
            p(line)
    if not regressed_broad:
        p("  (none)")
    p()

    p(f"--- Representative Examples: Same Status ({len(same)}) ---")
    for e in _pick(same):
        for line in _format_example(e):
            p(line)
    if not same:
        p("  (none)")
    p()

    # ── 8. EDINET metadata (v1 compat) ──
    has_edinet = any(after[fid].get("edinet_resolve_attempted") for fid in common_ids)
    if has_edinet:
        p(f"--- EDINET Metadata (all {len(common_ids)} common filings) ---")
        edinet_keys = [
            "edinet_api_key_present", "edinet_resolve_attempted",
            "edinet_resolve_succeeded", "edinet_cache_hit",
            "xbrl_fallback_attempted", "xbrl_fallback_succeeded",
        ]
        for k in edinet_keys:
            count = sum(1 for fid in common_ids if after[fid].get(k))
            p(f"  {k}: {count}")
        p()

    # ── 9. Overall summary ──
    total_before_ok = sum(1 for e in entries if e["before_status"] == "ok")
    total_after_ok  = sum(1 for e in entries if e["after_status"] == "ok")
    total_before_partial = sum(1 for e in entries if e["before_status"] == "partial")
    total_after_partial  = sum(1 for e in entries if e["after_status"] == "partial")
    total_before_q = sum(1 for e in entries if e["before_status"] == "quarantined")
    total_after_q  = sum(1 for e in entries if e["after_status"] == "quarantined")
    total_before_f = sum(1 for e in entries if e["before_status"] == "failed")
    total_after_f  = sum(1 for e in entries if e["after_status"] == "failed")

    p(f"{'=' * 70}")
    p(f"SUMMARY (fixed population: {len(common_ids)})")
    p(f"{'=' * 70}")
    p(f"  Worker versions: {before_wv} → {after_wv}")
    p(f"  ok:          {total_before_ok:4d} → {total_after_ok:4d}  (diff={total_after_ok - total_before_ok:+d})")
    p(f"  partial:     {total_before_partial:4d} → {total_after_partial:4d}  (diff={total_after_partial - total_before_partial:+d})")
    p(f"  quarantined: {total_before_q:4d} → {total_after_q:4d}  (diff={total_after_q - total_before_q:+d})")
    p(f"  failed:      {total_before_f:4d} → {total_after_f:4d}  (diff={total_after_f - total_before_f:+d})")
    p()
    p(f"  Improved (strict): {len(improved_strict)}")
    p(f"  Improved (broad):  {len(improved_broad)}")
    p(f"  Regressed (strict): {len(regressed_strict)}")
    p(f"  Regressed (broad):  {len(regressed_broad)}")

    # Write output
    result = "\n".join(out_lines)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(result, encoding="utf-8")
        print(f"Written to {args.output}")
    else:
        print(result)


if __name__ == "__main__":
    main()
