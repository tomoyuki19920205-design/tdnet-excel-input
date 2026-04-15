#!/usr/bin/env python3
"""
compare_segment_ab.py
2段構え導入前後のセグメント抽出結果を A/B 比較するツール。

使用例:
  python tools/compare_segment_ab.py --before logs/before.jsonl --after logs/after.jsonl

複数ファイルを結合して比較する場合:
  python tools/compare_segment_ab.py \
      --before logs/b1.jsonl logs/b2.jsonl \
      --after  logs/a1.jsonl logs/a2.jsonl
"""

import argparse
import io
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# Windows コンソールでの日本語文字化けを防ぐ
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# JSONL 読み込み
# ---------------------------------------------------------------------------

def load_jsonl(paths: list[str]) -> list[dict]:
    """複数 jsonl を結合して filing_result イベントのみ返す。"""
    records: list[dict] = []
    skipped = 0
    for path in paths:
        p = Path(path)
        if not p.exists():
            print(f"[WARN] ファイルが見つかりません: {path}", file=sys.stderr)
            continue
        with open(p, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    print(f"[WARN] {path}:{lineno} JSON parse error: {e}", file=sys.stderr)
                    skipped += 1
                    continue
                if obj.get("event") == "filing_result":
                    records.append(obj)
    if skipped:
        print(f"[INFO] skipped {skipped} lines (parse error)", file=sys.stderr)
    return records


# ---------------------------------------------------------------------------
# フィールド抽出ヘルパ
# ---------------------------------------------------------------------------

def get(rec: dict, *keys, default=None):
    """キーゆれに対応: 複数候補を順番に試す。"""
    for k in keys:
        if k in rec and rec[k] is not None:
            return rec[k]
    return default


def norm_status(rec: dict) -> str:
    """status の正規化。failed はそのまま返す。"""
    s = get(rec, "status", "worker_status", "selected_status", default="unknown")
    if s is None:
        return "unknown"
    return str(s).lower()


def quarantine_reason(rec: dict) -> str:
    """クォランティン理由を返す (複数ソースを統合)。"""
    # 直接フィールド
    q = get(rec, "quarantine_reason", default=None)
    if q:
        return str(q)
    # fallback_reason
    fb = get(rec, "fallback_reason", "hard_fail_reason", default=None)
    if fb:
        return str(fb)
    # rule_trace から理由を推定
    trace = get(rec, "rule_trace", default=[])
    if isinstance(trace, list):
        for t in trace:
            # Phase D-rescue: sales_margin_insufficient ...
            m = re.search(r"Phase D-rescue: (\S+)", t)
            if m:
                return m.group(1)
            # rescue が不要な失敗理由
            for keyword in [
                "no_segment_table_candidate",
                "pdf_no_sales_profit_columns",
                "sales_margin_insufficient",
                "too_few_valid_segments",
                "high_invalid_ratio",
            ]:
                if keyword in t:
                    return keyword
    return ""


def extract_orientation(rec: dict) -> str:
    """
    rule_trace から orientation を抽出。
    Phase B-orient: page=X orientation=XXX ... を探す。
    最も多い orientation を採用 (unknown 以外を優先)。
    """
    trace = get(rec, "rule_trace", default=[])
    if not isinstance(trace, list):
        return "unknown"
    counts: dict[str, int] = defaultdict(int)
    for t in trace:
        m = re.search(r"orientation=(\S+)", t)
        if m:
            counts[m.group(1)] += 1
    if not counts:
        return "unknown"
    # row_based / column_based を優先
    for preferred in ("column_based", "row_based"):
        if preferred in counts:
            return preferred
    return max(counts, key=lambda k: counts[k])


def has_column_based_bonus(rec: dict) -> bool:
    trace = get(rec, "rule_trace", default=[])
    if not isinstance(trace, list):
        return False
    return any("column_based_bonus" in t for t in trace)


def rescued_by_column(rec: dict) -> bool:
    """rescued_by=column_based_signal を含む行があるか。"""
    trace = get(rec, "rule_trace", default=[])
    if not isinstance(trace, list):
        return False
    # score_summary.candidate_guard.rescued_by も確認
    ss = rec.get("score_summary", {})
    cg = ss.get("candidate_guard", {}) if isinstance(ss, dict) else {}
    if isinstance(cg, dict) and "column_based" in str(cg.get("rescued_by", "")):
        return True
    return any("rescued_by=column_based" in t for t in trace)


def header_boost_suppressed(rec: dict) -> bool:
    """header_boost_suppressed_by_bs_cf を含む行があるか。"""
    trace = get(rec, "rule_trace", default=[])
    if not isinstance(trace, list):
        return False
    return any("header_boost_suppressed_by_bs_cf" in t for t in trace)


def match_key(rec: dict) -> str:
    """突合キー: ticker + filing_id"""
    ticker = get(rec, "ticker", default="")
    filing_id = get(rec, "filing_id", default="")
    return f"{ticker}||{filing_id}"


def company_name(rec: dict) -> str:
    return get(rec, "company", "company_name", default="")


# ---------------------------------------------------------------------------
# キー集計ヘルパ
# ---------------------------------------------------------------------------

REASON_KEYS = [
    "no_segment_table_candidate",
    "pdf_no_sales_profit_columns",
    "sales_margin_insufficient",
    "too_few_valid_segments",
    "high_invalid_ratio",
]


def count_reasons(records: list[dict]) -> dict[str, int]:
    cnt: dict[str, int] = defaultdict(int)
    for rec in records:
        r = quarantine_reason(rec)
        if r:
            cnt[r] += 1
    return cnt


# ---------------------------------------------------------------------------
# メインロジック
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="2段構え導入前後のセグメント抽出結果 A/B 比較"
    )
    parser.add_argument("--before", nargs="+", required=True, metavar="JSONL",
                        help="2段構え導入前の jsonl (複数可)")
    parser.add_argument("--after", nargs="+", required=True, metavar="JSONL",
                        help="2段構え導入後の jsonl (複数可)")
    args = parser.parse_args()

    before_recs = load_jsonl(args.before)
    after_recs = load_jsonl(args.after)

    before_map: dict[str, dict] = {}
    for rec in before_recs:
        k = match_key(rec)
        before_map[k] = rec  # 同一キーは後勝ち

    after_map: dict[str, dict] = {}
    for rec in after_recs:
        k = match_key(rec)
        after_map[k] = rec

    all_keys = set(before_map) | set(after_map)
    matched_keys = set(before_map) & set(after_map)
    unmatched_before_keys = set(before_map) - set(after_map)
    unmatched_after_keys = set(after_map) - set(before_map)

    matched_before = [before_map[k] for k in matched_keys]
    matched_after = [after_map[k] for k in matched_keys]

    # ------------------------------------------------------------------
    # [1] MATCH SUMMARY
    # ------------------------------------------------------------------
    sep = "=" * 60
    print(sep)
    print("[1] MATCH SUMMARY")
    print(sep)
    print(f"  before total      : {len(before_recs)}")
    print(f"  after  total      : {len(after_recs)}")
    print(f"  matched           : {len(matched_keys)}")
    print(f"  unmatched_before  : {len(unmatched_before_keys)}")
    print(f"  unmatched_after   : {len(unmatched_after_keys)}")
    print()

    # ------------------------------------------------------------------
    # [2] STATUS DIFF (matched のみ)
    # ------------------------------------------------------------------
    status_diff: dict[str, int] = defaultdict(int)
    for k in matched_keys:
        bs = norm_status(before_map[k])
        as_ = norm_status(after_map[k])
        status_diff[f"{bs} -> {as_}"] += 1

    print(sep)
    print("[2] STATUS DIFF (matched pairs)")
    print(sep)
    STATUS_ORDER = ["ok", "partial", "quarantined", "failed", "unknown"]
    pairs_shown = set()
    for bs in STATUS_ORDER:
        for as_ in STATUS_ORDER:
            key = f"{bs} -> {as_}"
            if key in status_diff:
                pairs_shown.add(key)
                print(f"  {key:<30}: {status_diff[key]}")
    # 上記以外
    for k, v in sorted(status_diff.items()):
        if k not in pairs_shown:
            print(f"  {k:<30}: {v}")
    print()

    # ------------------------------------------------------------------
    # [3] REASON DIFF
    # ------------------------------------------------------------------
    def count_reason_key(records: list[dict]) -> dict[str, int]:
        cnt: dict[str, int] = defaultdict(int)
        for rec in records:
            r = quarantine_reason(rec)
            if r:
                cnt[r] += 1
        return cnt

    before_reasons = count_reason_key(matched_before)
    after_reasons = count_reason_key(matched_after)

    # 全レコードでも集計
    before_reasons_all = count_reason_key(before_recs)
    after_reasons_all = count_reason_key(after_recs)

    print(sep)
    print("[3] REASON DIFF  (matched / all ファイル内)")
    print(sep)
    all_reason_keys = sorted(
        set(before_reasons_all) | set(after_reasons_all)
    )
    # 指定の重要キーを先頭に
    priority = REASON_KEYS
    other_keys = [k for k in all_reason_keys if k not in priority]
    display_keys = [k for k in priority if k in set(before_reasons_all) | set(after_reasons_all)] + other_keys
    print(f"  {'reason':<42} {'before':>7} {'after':>7} {'diff':>7}")
    print("  " + "-" * 62)
    for r in display_keys:
        bv = before_reasons_all.get(r, 0)
        av = after_reasons_all.get(r, 0)
        diff = av - bv
        diff_str = f"{diff:+d}" if diff != 0 else "   0"
        print(f"  {r:<42} {bv:>7} {av:>7} {diff_str:>7}")
    print()

    # ------------------------------------------------------------------
    # [4] ORIENTATION DIFF (after only, matched)
    # ------------------------------------------------------------------
    orient_cnt: dict[str, int] = defaultdict(int)
    col_bonus_count = 0
    rescued_by_col_count = 0
    header_suppressed_count = 0

    for rec in after_recs:
        orient = extract_orientation(rec)
        orient_cnt[orient] += 1
        if has_column_based_bonus(rec):
            col_bonus_count += 1
        if rescued_by_column(rec):
            rescued_by_col_count += 1
        if header_boost_suppressed(rec):
            header_suppressed_count += 1

    print(sep)
    print("[4] ORIENTATION DIFF (after 全件)")
    print(sep)
    for o in ("row_based", "column_based", "unknown"):
        print(f"  {o:<25}: {orient_cnt.get(o, 0)}")
    for o in sorted(orient_cnt):
        if o not in ("row_based", "column_based", "unknown"):
            print(f"  {o:<25}: {orient_cnt[o]}")
    print(f"  {'column_based_bonus 発火':<25}: {col_bonus_count}")
    print(f"  {'rescued_by=column_based':<25}: {rescued_by_col_count}")
    print(f"  {'header_boost_suppressed':<25}: {header_suppressed_count}")
    print()

    # ------------------------------------------------------------------
    # [5] IMPROVEMENT CASES (before=quarantined → after=ok/partial)
    # ------------------------------------------------------------------
    print(sep)
    print("[5] IMPROVEMENT CASES (before=quarantined → after=ok/partial, 最大10件)")
    print(sep)
    improvements = []
    for k in matched_keys:
        bs = norm_status(before_map[k])
        as_ = norm_status(after_map[k])
        if bs == "quarantined" and as_ in ("ok", "partial"):
            improvements.append(k)

    if not improvements:
        print("  (なし)")
    else:
        header = f"  {'ticker':<8} {'company':<20} {'before_reason':<36} {'after_status':<12} {'orient':<14} {'col_bonus':<10} {'rescued'}"
        print(header)
        print("  " + "-" * 120)
        for k in improvements[:10]:
            br = quarantine_reason(before_map[k]) or "-"
            ar = after_map[k]
            orient = extract_orientation(ar)
            col_b = "YES" if has_column_based_bonus(ar) else "-"
            resc = "YES" if rescued_by_column(ar) else "-"
            ticker = get(before_map[k], "ticker", default="-")
            comp = company_name(before_map[k]) or company_name(after_map[k]) or "-"
            comp = comp[:19]
            print(f"  {ticker:<8} {comp:<20} {br:<36} {norm_status(ar):<12} {orient:<14} {col_b:<10} {resc}")
    print()

    # ------------------------------------------------------------------
    # [6] REGRESSION CASES (before=ok/partial → after=quarantined)
    # ------------------------------------------------------------------
    print(sep)
    print("[6] REGRESSION CASES (before=ok/partial → after=quarantined, 最大10件)")
    print(sep)
    regressions = []
    for k in matched_keys:
        bs = norm_status(before_map[k])
        as_ = norm_status(after_map[k])
        if bs in ("ok", "partial") and as_ == "quarantined":
            regressions.append(k)

    if not regressions:
        print("  (なし)")
    else:
        header = f"  {'ticker':<8} {'company':<20} {'before_status':<14} {'after_reason':<36} {'orient':<14} {'hdr_suppressed'}"
        print(header)
        print("  " + "-" * 108)
        for k in regressions[:10]:
            ar_rec = after_map[k]
            ar = quarantine_reason(ar_rec) or "-"
            orient = extract_orientation(ar_rec)
            hdr_sup = "YES" if header_boost_suppressed(ar_rec) else "-"
            ticker = get(before_map[k], "ticker", default="-")
            comp = company_name(before_map[k]) or company_name(ar_rec) or "-"
            comp = comp[:19]
            bs = norm_status(before_map[k])
            print(f"  {ticker:<8} {comp:<20} {bs:<14} {ar:<36} {orient:<14} {hdr_sup}")
    print()

    # ------------------------------------------------------------------
    # [7] CONCLUSION
    # ------------------------------------------------------------------
    n_improve = len(improvements)
    n_regress = len(regressions)
    n_matched = len(matched_keys)
    col_bonus_in_improve = sum(1 for k in improvements if has_column_based_bonus(after_map[k]))

    print(sep)
    print("[7] CONCLUSION")
    print(sep)
    pct_i = 100 * n_improve / n_matched if n_matched else 0
    pct_r = 100 * n_regress / n_matched if n_matched else 0
    print(f"  matched {n_matched} 件中、改善(quarantined→ok/partial): {n_improve} 件 ({pct_i:.1f}%)、"
          f"悪化(ok/partial→quarantined): {n_regress} 件 ({pct_r:.1f}%)")
    if n_improve > 0:
        pct_cb = 100 * col_bonus_in_improve / n_improve
        print(f"  改善例のうち column_based_bonus が寄与したケース: {col_bonus_in_improve}/{n_improve} 件 ({pct_cb:.1f}%)")
    if col_bonus_count > 0 or rescued_by_col_count > 0:
        print(f"  after 全件で column_based_bonus 発火: {col_bonus_count} 件、rescued_by=column_based: {rescued_by_col_count} 件")
    if n_regress == 0:
        print("  悪化ゼロ: 2段構えによる誤検知増加は確認されていない。")
    else:
        print(f"  悪化 {n_regress} 件あり: [6] を参照し header_boost_suppressed の影響を精査すること。")
    print("  未検証: XBRL 直接成功ルートへの影響、PDF以外のパスの精度変化は本比較の対象外。")
    print(sep)


if __name__ == "__main__":
    main()
