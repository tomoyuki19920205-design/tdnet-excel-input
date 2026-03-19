#!/usr/bin/env python3
"""tune_buyback_scanner_score.py — Scanner score 閾値 / keyword 重み tuning ツール

既存の buyback_candidates.csv と review_buyback_results.csv を使い、
現行ルール vs 新ルールで再スコアリングして before/after 比較を出力する。

Usage:
  python tools/tune_buyback_scanner_score.py \
    --candidates artifacts/buyback_candidates/buyback_candidates.csv \
    --review     artifacts/buyback_review_candidates/review_buyback_results.csv \
    --output-dir artifacts/buyback_tuning

  python tools/tune_buyback_scanner_score.py \
    --candidates artifacts/buyback_candidates/buyback_candidates.csv \
    --review     artifacts/buyback_review_candidates/review_buyback_results.csv \
    --rules      configs/buyback_scanner_rules.json \
    --output-dir artifacts/buyback_tuning --verbose
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.find_buyback_candidate_docs import (
    KeywordHit,
    build_default_rules,
    load_scoring_rules,
    score_candidate_with_details,
    classify_review_priority,
    find_keyword_hits,
    STRONG_KEYWORDS,
    WEAK_KEYWORDS,
    EXCLUDE_HINTS,
)
from tools.analyze_buyback_scanner_vs_review import (
    normalize_join_path,
    load_csv,
    write_csv as _write_csv,
    _safe_int,
    _safe_float,
    _safe_bool,
)

logger = logging.getLogger("buyback_tuning")
JST = timezone(timedelta(hours=9))


# ============================================================
# 再スコアリング
# ============================================================

def rescore_candidate(
    matched_keywords: str,
    metadata: dict,
    rules: dict,
) -> tuple[int, list[str]]:
    """matched_keywords 文字列から KeywordHit を再構築し、rules で再スコアリング。

    Returns:
        (new_score, new_contributions)
    """
    strong_map = rules.get("strong_keywords", {})
    weak_map = rules.get("weak_keywords", {})
    penalty_map = rules.get("penalty_keywords", {})

    # keyword を展開
    kws = []
    for sep in ["|", ",", ";"]:
        if sep in matched_keywords:
            kws = [k.strip() for k in matched_keywords.split(sep) if k.strip()]
            break
    if not kws:
        kws = [matched_keywords.strip()] if matched_keywords.strip() else []

    # KeywordHit を再構築
    hits: list[KeywordHit] = []
    for kw in kws:
        if kw in strong_map:
            hits.append(KeywordHit(keyword=kw, position=0, strength="strong"))
        elif kw in weak_map:
            hits.append(KeywordHit(keyword=kw, position=0, strength="weak"))
        elif kw in penalty_map:
            hits.append(KeywordHit(keyword=kw, position=0, strength="exclude"))
        else:
            # 元の strength を推定
            if kw in STRONG_KEYWORDS:
                hits.append(KeywordHit(keyword=kw, position=0, strength="strong"))
            elif kw in WEAK_KEYWORDS:
                hits.append(KeywordHit(keyword=kw, position=0, strength="weak"))
            elif kw in EXCLUDE_HINTS:
                hits.append(KeywordHit(keyword=kw, position=0, strength="exclude"))
            else:
                # unknown → weak fallback
                hits.append(KeywordHit(keyword=kw, position=0, strength="weak"))

    return score_candidate_with_details(hits, metadata, rules)


# ============================================================
# Join candidates & review
# ============================================================

def join_candidates_and_review(
    candidates: list[dict],
    review_rows: list[dict],
) -> tuple[list[dict], list[dict]]:
    """candidates と review を basename join する。"""
    review_by_key: dict[str, dict] = {}
    review_used: set[str] = set()

    for r in review_rows:
        key = normalize_join_path(r.get("file_path", ""))
        if key:
            review_by_key[key] = r

    joined: list[dict] = []
    failures: list[dict] = []

    for c in candidates:
        path = c.get("file_path", "")
        key = normalize_join_path(path)
        if not key:
            failures.append({"source_side": "candidates", "raw_path": path, "reason": "empty"})
            continue

        r = review_by_key.get(key)
        if r is None:
            failures.append({"source_side": "candidates", "raw_path": path, "reason": "no_review"})
            continue

        review_used.add(key)
        joined.append({
            "join_key": key,
            "file_path": path,
            "file_name": c.get("file_name", ""),
            "matched_keywords": c.get("matched_keywords", ""),
            "old_candidate_score": _safe_int(c.get("candidate_score", "")),
            "old_priority": c.get("review_priority", "").lower(),
            "derived_ticker": c.get("derived_ticker", ""),
            "derived_title": c.get("derived_title", ""),
            # review 側
            "review_bucket": r.get("review_bucket", ""),
            "is_buyback_related": _safe_bool(r.get("is_buyback_related", "")),
            "confidence_final": _safe_float(r.get("confidence_final", "")),
            "extracted_fields_count": _safe_int(r.get("extracted_fields_count", "")),
            "missing_key_fields": r.get("missing_key_fields", ""),
            "event_type_candidate": r.get("event_type_candidate", ""),
            "event_type": r.get("event_type", ""),
        })

    for r in review_rows:
        key = normalize_join_path(r.get("file_path", ""))
        if key and key not in review_used:
            failures.append({"source_side": "review", "raw_path": r.get("file_path", ""), "reason": "no_candidate"})

    return joined, failures


# ============================================================
# Tuning label
# ============================================================

def assign_tuning_label(old_p: str, new_p: str) -> str:
    if old_p == new_p:
        return f"unchanged_{old_p}"
    priority_order = {"low": 0, "medium": 1, "high": 2}
    old_rank = priority_order.get(old_p, 0)
    new_rank = priority_order.get(new_p, 0)
    if new_rank > old_rank:
        return f"promoted_to_{new_p}"
    return f"demoted_to_{new_p}"


# ============================================================
# Keyword adjustment suggestion
# ============================================================

def suggest_keyword_adjustment(
    keyword: str,
    stats: dict,
    rules: dict,
) -> tuple[str, str]:
    """keyword の stats から調整方向と理由を返す。"""
    total = stats.get("total", 0)
    if total == 0:
        return "keep", "no_data"

    hce = stats.get("high_confidence_extracted_count", 0)
    non_bb = stats.get("non_buyback_count", 0)
    cls_only = stats.get("classifier_only_count", 0)

    hce_rate = hce / total
    non_bb_rate = non_bb / total
    cls_rate = cls_only / total

    # 判定 heuristic
    strong_map = rules.get("strong_keywords", {})
    weak_map = rules.get("weak_keywords", {})
    penalty_map = rules.get("penalty_keywords", {})

    current_weight = (
        strong_map.get(keyword)
        or weak_map.get(keyword)
        or penalty_map.get(keyword)
        or 0
    )

    if non_bb_rate >= 0.5:
        if keyword in strong_map:
            return "decrease", f"false positive rate {non_bb_rate:.0%}"
        elif keyword in weak_map:
            return "move_to_penalty", f"false positive rate {non_bb_rate:.0%}"
        else:
            return "keep", f"already penalty or unknown"

    if hce_rate >= 0.5 and keyword in weak_map:
        return "move_to_strong", f"strong positive ({hce_rate:.0%} high_confidence)"

    if hce_rate >= 0.7:
        return "increase", f"strong positive ({hce_rate:.0%} high_confidence)"

    if cls_rate >= 0.6 and hce_rate < 0.2:
        return "decrease", f"mostly classifier_only ({cls_rate:.0%})"

    return "keep", "balanced"


# ============================================================
# 集計関数
# ============================================================

def build_priority_comparison(joined: list[dict]) -> list[dict]:
    old_counter = Counter(j["old_priority"] for j in joined)
    new_counter = Counter(j["new_priority"] for j in joined)
    rows = []
    for p in ["high", "medium", "low"]:
        old_c = old_counter.get(p, 0)
        new_c = new_counter.get(p, 0)
        rows.append({
            "priority_label": p,
            "old_count": old_c,
            "new_count": new_c,
            "delta": new_c - old_c,
        })
    return rows


def build_priority_bucket_cross(joined: list[dict], priority_key: str) -> list[dict]:
    counts: Counter = Counter()
    for j in joined:
        p = j[priority_key] or "unknown"
        b = j["review_bucket"] or "unknown"
        counts[(p, b)] += 1

    priority_totals: Counter = Counter()
    for (p, b), c in counts.items():
        priority_totals[p] += c

    rows = []
    for (p, b), c in sorted(counts.items()):
        rows.append({
            "priority": p,
            "review_bucket": b,
            "count": c,
            "row_pct": round(c / (priority_totals[p] or 1) * 100, 1),
        })
    return rows


def build_keyword_adjustment_candidates(
    joined: list[dict],
    rules: dict,
) -> list[dict]:
    """keyword 別の集計と調整候補。"""
    kw_stats: dict[str, dict] = {}
    for j in joined:
        kws_raw = j.get("matched_keywords", "")
        if not kws_raw:
            continue
        for sep in ["|", ",", ";"]:
            if sep in kws_raw:
                parts = [k.strip() for k in kws_raw.split(sep) if k.strip()]
                break
        else:
            parts = [kws_raw.strip()] if kws_raw.strip() else []

        for kw in parts:
            if kw not in kw_stats:
                kw_stats[kw] = {
                    "keyword": kw, "total": 0,
                    "high_confidence_extracted_count": 0,
                    "non_buyback_count": 0,
                    "classifier_only_count": 0,
                    "low_confidence_count": 0,
                }
            d = kw_stats[kw]
            d["total"] += 1
            bucket = j.get("review_bucket", "")
            if bucket == "high_confidence_extracted":
                d["high_confidence_extracted_count"] += 1
            elif bucket == "non_buyback":
                d["non_buyback_count"] += 1
            elif bucket == "classifier_only":
                d["classifier_only_count"] += 1
            elif bucket == "low_confidence":
                d["low_confidence_count"] += 1

    rows = []
    strong_map = rules.get("strong_keywords", {})
    weak_map = rules.get("weak_keywords", {})
    penalty_map = rules.get("penalty_keywords", {})
    for kw in sorted(kw_stats, key=lambda k: -kw_stats[k]["total"]):
        d = kw_stats[kw]
        cw = strong_map.get(kw) or weak_map.get(kw) or penalty_map.get(kw) or 0
        direction, reason = suggest_keyword_adjustment(kw, d, rules)
        d["current_weight"] = cw
        d["suggested_direction"] = direction
        d["suggested_reason"] = reason
        rows.append(d)
    return rows


def build_tuning_mismatch_focus(joined: list[dict]) -> list[dict]:
    cases = []
    for j in joined:
        reasons = []
        old_p = j["old_priority"]
        new_p = j["new_priority"]
        b = j["review_bucket"]

        if old_p == "high" and b == "non_buyback":
            reasons.append("old_high_non_buyback")
        if old_p == "high" and b == "classifier_only":
            reasons.append("old_high_classifier_only")
        if old_p == "low" and b == "high_confidence_extracted":
            reasons.append("old_low_high_confidence")
        if new_p != old_p:
            label = j["tuning_label"]
            if "promoted" in label and b in ("high_confidence_extracted", "low_confidence"):
                reasons.append(f"improvement:{label}")
            elif "demoted" in label and b == "high_confidence_extracted":
                reasons.append(f"degradation:{label}")
            elif "promoted" in label and b == "non_buyback":
                reasons.append(f"wrong_promotion:{label}")

        if reasons:
            cases.append({
                "file_path": j["file_path"],
                "old_priority": old_p,
                "new_priority": new_p,
                "old_candidate_score": j["old_candidate_score"],
                "new_candidate_score": j["new_candidate_score"],
                "review_bucket": b,
                "matched_keywords": j["matched_keywords"],
                "tuning_reason": "; ".join(reasons),
            })
    return cases


# ============================================================
# Summary MD
# ============================================================

def write_summary_md(
    path: str,
    *,
    candidates_path: str,
    review_path: str,
    rules_path: str,
    candidate_count: int,
    join_ok: int,
    join_fail: int,
    joined: list[dict],
    comparison: list[dict],
    before_matrix: list[dict],
    after_matrix: list[dict],
    kw_adj: list[dict],
    mismatch: list[dict],
    old_rules: dict,
    new_rules: dict,
) -> None:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    # precision 計算
    def _precision(pkey: str):
        total = sum(1 for j in joined if j[pkey] == "high")
        hce = sum(1 for j in joined if j[pkey] == "high" and j["review_bucket"] == "high_confidence_extracted")
        return (round(hce / total * 100, 1), hce, total) if total else (0, 0, 0)

    def _med_rate(pkey: str):
        total = sum(1 for j in joined if j[pkey] == "medium")
        bb = sum(1 for j in joined if j[pkey] == "medium" and j["is_buyback_related"])
        return (round(bb / total * 100, 1), bb, total) if total else (0, 0, 0)

    old_prec, old_hce, old_ht = _precision("old_priority")
    new_prec, new_hce, new_ht = _precision("new_priority")
    old_mr, old_mb, old_mt = _med_rate("old_priority")
    new_mr, new_mb, new_mt = _med_rate("new_priority")

    promoted_hce = sum(1 for j in joined if "promoted" in j["tuning_label"]
                       and j["review_bucket"] == "high_confidence_extracted")
    demoted_fp = sum(1 for j in joined if "demoted" in j["tuning_label"]
                     and j["review_bucket"] in ("non_buyback", "excluded"))

    with open(path, "w", encoding="utf-8") as f:
        f.write("# Buyback Scanner Score Tuning — Summary\n\n")
        f.write(f"- **実行時刻**: {now}\n")
        f.write(f"- **candidates**: `{candidates_path}`\n")
        f.write(f"- **review**: `{review_path}`\n")
        f.write(f"- **rules**: `{rules_path or 'default'}`\n\n")

        f.write("## 基本統計\n\n")
        f.write("| 項目 | 件数 |\n|:---|---:|\n")
        f.write(f"| candidate 行数 | {candidate_count:,} |\n")
        f.write(f"| join 成功 | {join_ok:,} |\n")
        f.write(f"| join 失敗 | {join_fail:,} |\n\n")

        f.write("## before/after priority 分布\n\n")
        f.write("| priority | old | new | delta |\n|:---|---:|---:|---:|\n")
        for c in comparison:
            f.write(f"| {c['priority_label']} | {c['old_count']} | {c['new_count']} | {c['delta']:+d} |\n")

        f.write("\n## before/after precision 指標\n\n")
        f.write("| 指標 | old | new |\n|:---|---:|---:|\n")
        f.write(f"| high → HCE 率 | {old_prec}% ({old_hce}/{old_ht}) | {new_prec}% ({new_hce}/{new_ht}) |\n")
        f.write(f"| medium → buyback 率 | {old_mr}% ({old_mb}/{old_mt}) | {new_mr}% ({new_mb}/{new_mt}) |\n")
        f.write(f"| promoted → HCE | — | {promoted_hce} |\n")
        f.write(f"| demoted → FP | — | {demoted_fp} |\n")

        # thresholds
        old_th = old_rules.get("priority_thresholds", {})
        new_th = new_rules.get("priority_thresholds", {})
        f.write("\n## 閾値比較\n\n")
        f.write("| 閾値 | old | new |\n|:---|---:|---:|\n")
        f.write(f"| high | {old_th.get('high', '?')} | {new_th.get('high', '?')} |\n")
        f.write(f"| medium | {old_th.get('medium', '?')} | {new_th.get('medium', '?')} |\n")

        # keyword adj top
        f.write("\n## keyword 調整候補 (上位20)\n\n")
        f.write("| keyword | total | HCE | non_bb | weight | suggest | reason |\n")
        f.write("|:---|---:|---:|---:|---:|:---|:---|\n")
        for k in kw_adj[:20]:
            f.write(
                f"| {k['keyword']} | {k['total']} "
                f"| {k['high_confidence_extracted_count']} "
                f"| {k['non_buyback_count']} "
                f"| {k['current_weight']} "
                f"| {k['suggested_direction']} "
                f"| {k['suggested_reason']} |\n"
            )

        f.write(f"\n## mismatch focus: {len(mismatch)} 件\n\n")

        f.write("\n## 所見\n\n")
        if new_prec > old_prec:
            f.write(f"- high precision 改善: {old_prec}% → {new_prec}%\n")
        elif new_prec < old_prec:
            f.write(f"- ⚠️ high precision 低下: {old_prec}% → {new_prec}%\n")
        else:
            f.write(f"- high precision 変化なし: {old_prec}%\n")

        if promoted_hce > 0:
            f.write(f"- 新ルールで {promoted_hce} 件の真候補が上位に昇格\n")
        if demoted_fp > 0:
            f.write(f"- 新ルールで {demoted_fp} 件の false positive が降格\n")

        adj_kws = [k for k in kw_adj if k["suggested_direction"] not in ("keep",)]
        if adj_kws:
            f.write(f"- 調整推奨キーワード: {', '.join(k['keyword'] for k in adj_kws[:5])}\n")


# ============================================================
# 出力列定義
# ============================================================

_RESCORED_COLUMNS = [
    "file_path", "file_name", "matched_keywords",
    "old_candidate_score", "old_priority",
    "new_candidate_score", "new_priority",
    "score_delta", "score_contributions_old", "score_contributions_new",
    "review_bucket", "confidence_final", "extracted_fields_count",
    "missing_key_fields", "tuning_label",
]

_COMPARISON_COLUMNS = ["priority_label", "old_count", "new_count", "delta"]

_CROSS_COLUMNS = ["priority", "review_bucket", "count", "row_pct"]

_KW_ADJ_COLUMNS = [
    "keyword", "total",
    "high_confidence_extracted_count", "non_buyback_count",
    "classifier_only_count", "low_confidence_count",
    "current_weight", "suggested_direction", "suggested_reason",
]

_MISMATCH_COLUMNS = [
    "file_path", "old_priority", "new_priority",
    "old_candidate_score", "new_candidate_score",
    "review_bucket", "matched_keywords", "tuning_reason",
]


# ============================================================
# CLI
# ============================================================

def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scanner score 閾値 / keyword 重み tuning ツール",
    )
    parser.add_argument("--candidates", required=True,
                        help="buyback_candidates.csv")
    parser.add_argument("--review", required=True,
                        help="review_buyback_results.csv")
    parser.add_argument("--rules", default=None,
                        help="新ルール JSON (省略時はデフォルト)")
    parser.add_argument("--output-dir", default="artifacts/buyback_tuning")
    parser.add_argument("--high-threshold", type=int, default=None,
                        help="新 high 閾値 (rules JSON 上書き)")
    parser.add_argument("--medium-threshold", type=int, default=None,
                        help="新 medium 閾値 (rules JSON 上書き)")
    parser.add_argument("--verbose", action="store_true")
    opts = parser.parse_args(args)

    level = logging.DEBUG if opts.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 旧ルール = デフォルト
    old_rules = build_default_rules()
    # 新ルール = ファイル or デフォルト
    new_rules = load_scoring_rules(opts.rules)

    # CLI 閾値上書き
    if opts.high_threshold is not None:
        new_rules["priority_thresholds"]["high"] = opts.high_threshold
    if opts.medium_threshold is not None:
        new_rules["priority_thresholds"]["medium"] = opts.medium_threshold

    logger.info(f"Old thresholds: {old_rules['priority_thresholds']}")
    logger.info(f"New thresholds: {new_rules['priority_thresholds']}")

    # 1. CSV 読み込み
    candidates = load_csv(opts.candidates)
    review_rows = load_csv(opts.review)
    logger.info(f"candidates: {len(candidates)}行, review: {len(review_rows)}行")

    if not candidates or not review_rows:
        logger.error("入力 CSV が空または存在しません")
        return 1

    # 2. Join
    joined, failures = join_candidates_and_review(candidates, review_rows)
    logger.info(f"join: {len(joined)}件成功, {len(failures)}件失敗")

    # 3. 再スコアリング
    for j in joined:
        metadata = {
            "derived_ticker": j.get("derived_ticker", ""),
            "derived_title": j.get("derived_title", ""),
        }
        # old contributions
        _, old_contribs = rescore_candidate(
            j["matched_keywords"], metadata, old_rules
        )
        j["score_contributions_old"] = "|".join(old_contribs)

        # new score
        new_score, new_contribs = rescore_candidate(
            j["matched_keywords"], metadata, new_rules
        )
        new_thresholds = new_rules.get("priority_thresholds")
        new_priority = classify_review_priority(new_score, new_thresholds)

        j["new_candidate_score"] = new_score
        j["new_priority"] = new_priority
        j["score_delta"] = new_score - j["old_candidate_score"]
        j["score_contributions_new"] = "|".join(new_contribs)
        j["tuning_label"] = assign_tuning_label(j["old_priority"], new_priority)

    # 4. 集計
    comparison = build_priority_comparison(joined)
    before_matrix = build_priority_bucket_cross(joined, "old_priority")
    after_matrix = build_priority_bucket_cross(joined, "new_priority")
    kw_adj = build_keyword_adjustment_candidates(joined, new_rules)
    mismatch = build_tuning_mismatch_focus(joined)

    # 5. 出力
    out = opts.output_dir
    _write_csv(os.path.join(out, "tuning_re_scored_candidates.csv"), joined, _RESCORED_COLUMNS)
    _write_csv(os.path.join(out, "tuning_priority_comparison.csv"), comparison, _COMPARISON_COLUMNS)
    _write_csv(os.path.join(out, "tuning_priority_bucket_before.csv"), before_matrix, _CROSS_COLUMNS)
    _write_csv(os.path.join(out, "tuning_priority_bucket_after.csv"), after_matrix, _CROSS_COLUMNS)
    _write_csv(os.path.join(out, "tuning_keyword_adjustment_candidates.csv"), kw_adj, _KW_ADJ_COLUMNS)
    _write_csv(os.path.join(out, "tuning_mismatch_focus.csv"), mismatch, _MISMATCH_COLUMNS)

    write_summary_md(
        os.path.join(out, "tuning_summary.md"),
        candidates_path=opts.candidates,
        review_path=opts.review,
        rules_path=opts.rules or "",
        candidate_count=len(candidates),
        join_ok=len(joined),
        join_fail=len(failures),
        joined=joined,
        comparison=comparison,
        before_matrix=before_matrix,
        after_matrix=after_matrix,
        kw_adj=kw_adj,
        mismatch=mismatch,
        old_rules=old_rules,
        new_rules=new_rules,
    )

    logger.info(f"出力: {out}/")
    logger.info(f"完了: joined={len(joined)} mismatch={len(mismatch)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
