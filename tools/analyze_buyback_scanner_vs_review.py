#!/usr/bin/env python3
"""analyze_buyback_scanner_vs_review.py — Scanner vs Review 乖離分析ツール

candidate scanner (find_buyback_candidate_docs.py) の出力と
review (review_buyback_extraction.py) の出力を突き合わせ、
priority × bucket のズレを定量分析する。

Usage:
  python tools/analyze_buyback_scanner_vs_review.py \
    --manifest artifacts/buyback_candidates/candidate_manifest.csv \
    --review   artifacts/buyback_review_candidates/review_buyback_results.csv \
    --output-dir artifacts/buyback_alignment

  python tools/analyze_buyback_scanner_vs_review.py \
    --manifest artifacts/buyback_candidates/buyback_candidates.csv \
    --review   artifacts/buyback_review_candidates/review_buyback_results.csv \
    --output-dir artifacts/buyback_alignment --verbose
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import PurePosixPath

logger = logging.getLogger("buyback_alignment")
JST = timezone(timedelta(hours=9))

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ============================================================
# パス正規化
# ============================================================

def normalize_join_path(raw: str) -> str:
    """パスを正規化して join key にする。

    - バックスラッシュ → スラッシュ統一
    - 小文字化 (Windows ファイルシステム差吸収)
    - 末尾スラッシュ除去
    - 絶対パスなら basename にフォールバック
    """
    if not raw:
        return ""
    p = raw.strip().replace("\\", "/").rstrip("/")
    # 絶対パス → basename に縮退 (異なる prefix でも合致させる)
    basename = p.rsplit("/", 1)[-1] if "/" in p else p
    return basename.lower()


# ============================================================
# CSV 読み込み (列名ゆらぎ吸収)
# ============================================================

# manifest / candidates CSV で使われ得る列名のマッピング
_MANIFEST_COLUMN_ALIASES = {
    "path": ["path", "file_path"],
    "candidate_score": ["candidate_score", "manifest_candidate_score"],
    "review_priority": ["review_priority", "manifest_review_priority"],
    "matched_keywords": ["matched_keywords", "manifest_matched_keywords"],
    "matched_keyword_count": ["matched_keyword_count", "manifest_matched_keyword_count"],
    "ticker": ["ticker", "derived_ticker", "manifest_ticker"],
    "title": ["title", "derived_title", "manifest_title"],
    "disclosure_date": ["disclosure_date", "derived_disclosure_date", "manifest_disclosure_date"],
}

_REVIEW_COLUMN_ALIASES = {
    "path": ["file_path"],
    "file_name": ["file_name"],
    "is_buyback_related": ["is_buyback_related"],
    "event_type_candidate": ["event_type_candidate"],
    "event_type": ["event_type"],
    "classification_confidence": ["classification_confidence"],
    "extraction_confidence": ["extraction_confidence"],
    "confidence_final": ["confidence_final"],
    "review_bucket": ["review_bucket"],
    "extracted_fields_count": ["extracted_fields_count"],
    "missing_key_fields": ["missing_key_fields"],
    # manifest 埋め込み列 (review が --manifest 付きで走った場合)
    "manifest_candidate_score": ["manifest_candidate_score"],
    "manifest_review_priority": ["manifest_review_priority"],
    "manifest_matched_keywords": ["manifest_matched_keywords"],
    "manifest_matched_keyword_count": ["manifest_matched_keyword_count"],
}


def _resolve_column(row: dict, canonical: str, aliases: dict[str, list[str]]) -> str:
    """aliases から canonical 列の値を取得する。"""
    for alias in aliases.get(canonical, [canonical]):
        v = row.get(alias, "")
        if v:
            return str(v)
    return ""


def load_csv(path: str) -> list[dict]:
    """CSV を読み込む。"""
    if not path or not os.path.isfile(path):
        logger.warning(f"CSV not found: {path}")
        return []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader)


def _safe_int(v: str, default: int = 0) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


def _safe_float(v: str, default: float = 0.0) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _safe_bool(v: str) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


# ============================================================
# Join
# ============================================================

def join_manifest_and_review(
    manifest_rows: list[dict],
    review_rows: list[dict],
) -> tuple[list[dict], list[dict]]:
    """manifest と review を basename join する。

    Returns:
        (joined_rows, join_failure_rows)
    """
    # review 側インデックスを構築
    review_by_key: dict[str, dict] = {}
    review_used: set[str] = set()
    for r in review_rows:
        key = normalize_join_path(_resolve_column(r, "path", _REVIEW_COLUMN_ALIASES))
        if key:
            review_by_key[key] = r

    joined: list[dict] = []
    failures: list[dict] = []

    for m in manifest_rows:
        m_path = _resolve_column(m, "path", _MANIFEST_COLUMN_ALIASES)
        key = normalize_join_path(m_path)
        if not key:
            failures.append({
                "source_side": "manifest",
                "raw_path": m_path,
                "normalized_join_key": key,
                "reason": "empty_path",
            })
            continue

        r = review_by_key.get(key)
        if r is None:
            failures.append({
                "source_side": "manifest",
                "raw_path": m_path,
                "normalized_join_key": key,
                "reason": "no_review_match",
            })
            continue

        review_used.add(key)
        joined.append(_merge_row(m, r, key))

    # review にあって manifest にない行
    for r in review_rows:
        key = normalize_join_path(_resolve_column(r, "path", _REVIEW_COLUMN_ALIASES))
        if key and key not in review_used:
            failures.append({
                "source_side": "review",
                "raw_path": _resolve_column(r, "path", _REVIEW_COLUMN_ALIASES),
                "normalized_join_key": key,
                "reason": "no_manifest_match",
            })

    return joined, failures


def _merge_row(m: dict, r: dict, join_key: str) -> dict:
    """manifest 行と review 行をマージする。"""
    score = _safe_int(
        _resolve_column(m, "candidate_score", _MANIFEST_COLUMN_ALIASES)
        or _resolve_column(r, "manifest_candidate_score", _REVIEW_COLUMN_ALIASES)
    )
    priority = (
        _resolve_column(m, "review_priority", _MANIFEST_COLUMN_ALIASES)
        or _resolve_column(r, "manifest_review_priority", _REVIEW_COLUMN_ALIASES)
        or ""
    ).lower()
    keywords = (
        _resolve_column(m, "matched_keywords", _MANIFEST_COLUMN_ALIASES)
        or _resolve_column(r, "manifest_matched_keywords", _REVIEW_COLUMN_ALIASES)
    )
    kw_count = _safe_int(
        _resolve_column(m, "matched_keyword_count", _MANIFEST_COLUMN_ALIASES)
        or _resolve_column(r, "manifest_matched_keyword_count", _REVIEW_COLUMN_ALIASES)
    )

    bucket = r.get("review_bucket", "")
    is_buyback = _safe_bool(r.get("is_buyback_related", ""))
    conf_final = _safe_float(r.get("confidence_final", ""))
    ext_count = _safe_int(r.get("extracted_fields_count", ""))
    missing = r.get("missing_key_fields", "")

    return {
        "join_key": join_key,
        # manifest 側
        "manifest_path": _resolve_column(m, "path", _MANIFEST_COLUMN_ALIASES),
        "manifest_candidate_score": score,
        "manifest_review_priority": priority,
        "manifest_matched_keywords": keywords,
        "manifest_matched_keyword_count": kw_count,
        "manifest_ticker": _resolve_column(m, "ticker", _MANIFEST_COLUMN_ALIASES),
        "manifest_title": _resolve_column(m, "title", _MANIFEST_COLUMN_ALIASES),
        "manifest_disclosure_date": _resolve_column(m, "disclosure_date", _MANIFEST_COLUMN_ALIASES),
        # review 側
        "file_path": r.get("file_path", ""),
        "file_name": r.get("file_name", ""),
        "is_buyback_related": is_buyback,
        "event_type_candidate": r.get("event_type_candidate", ""),
        "event_type": r.get("event_type", ""),
        "classification_confidence": _safe_float(r.get("classification_confidence", "")),
        "extraction_confidence": _safe_float(r.get("extraction_confidence", "")),
        "confidence_final": conf_final,
        "review_bucket": bucket,
        "extracted_fields_count": ext_count,
        "missing_key_fields": missing,
        # 派生列 (後で埋める)
        "score_band": "",
        "alignment_label": "",
        "likely_true_positive": False,
        "likely_false_positive": False,
        "likely_missed_candidate": False,
        "likely_needs_rule_improvement": False,
    }


# ============================================================
# Score band / Alignment flags
# ============================================================

DEFAULT_SCORE_BINS = [0, 3, 6, 10, 100]


def parse_score_bins(bins_str: str) -> list[int]:
    """'0,3,6,10,100' → [0, 3, 6, 10, 100]"""
    try:
        bins = sorted(set(int(x.strip()) for x in bins_str.split(",")))
        return bins if bins else DEFAULT_SCORE_BINS
    except (ValueError, TypeError):
        return DEFAULT_SCORE_BINS


def assign_score_band(score: int, bins: list[int]) -> str:
    """score → '0-2', '3-5', '6-9', '10+' のようなバンドラベル。"""
    for i in range(len(bins) - 1):
        lo, hi = bins[i], bins[i + 1]
        if lo <= score < hi:
            return f"{lo}-{hi - 1}" if hi < 100 else f"{lo}+"
    # 最後の bin 超え
    return f"{bins[-1]}+"


def derive_alignment_flags(row: dict, bins: list[int]) -> None:
    """スコアバンド / alignment label / proxy flag を付与する (in-place)。"""
    score = row["manifest_candidate_score"]
    priority = row["manifest_review_priority"]
    bucket = row["review_bucket"]
    is_buyback = row["is_buyback_related"]
    ext_count = row["extracted_fields_count"]

    row["score_band"] = assign_score_band(score, bins)
    row["alignment_label"] = f"scanner_{priority}__{bucket}" if priority and bucket else ""

    # -- proxy 指標 --
    # likely_true_positive: review が高信頼で抽出成功
    row["likely_true_positive"] = (bucket == "high_confidence_extracted")

    # likely_false_positive: scanner が high/medium で review が non_buyback/excluded
    row["likely_false_positive"] = (
        priority in ("high", "medium")
        and bucket in ("non_buyback", "excluded")
    ) or (
        priority == "high"
        and bucket == "classifier_only"
        and ext_count == 0
    )

    # likely_missed_candidate: scanner low なのに review 高信頼
    row["likely_missed_candidate"] = (
        priority == "low"
        and bucket == "high_confidence_extracted"
    )

    # likely_needs_rule_improvement
    row["likely_needs_rule_improvement"] = bucket in (
        "classifier_only", "low_confidence", "extraction_failed",
    ) and is_buyback


# ============================================================
# Keyword 展開
# ============================================================

def expand_keywords(joined: list[dict]) -> list[dict]:
    """matched_keywords を展開して keyword × row の行を返す。"""
    rows = []
    for j in joined:
        kws_raw = j.get("manifest_matched_keywords", "")
        if not kws_raw:
            continue
        for sep in ["|", ",", ";"]:
            if sep in kws_raw:
                parts = [k.strip() for k in kws_raw.split(sep) if k.strip()]
                break
        else:
            parts = [kws_raw.strip()] if kws_raw.strip() else []
        for kw in parts:
            rows.append({**j, "_keyword": kw})
    return rows


# ============================================================
# 集計関数
# ============================================================

def build_priority_bucket_matrix(joined: list[dict]) -> list[dict]:
    """priority × bucket のクロス集計。"""
    counts: Counter = Counter()
    for j in joined:
        p = j["manifest_review_priority"] or "unknown"
        b = j["review_bucket"] or "unknown"
        counts[(p, b)] += 1

    total = len(joined) or 1
    priority_totals: Counter = Counter()
    bucket_totals: Counter = Counter()
    for (p, b), c in counts.items():
        priority_totals[p] += c
        bucket_totals[b] += c

    rows = []
    for (p, b), c in sorted(counts.items()):
        rows.append({
            "manifest_review_priority": p,
            "review_bucket": b,
            "count": c,
            "row_pct": round(c / (priority_totals[p] or 1) * 100, 1),
            "col_pct": round(c / (bucket_totals[b] or 1) * 100, 1),
        })
    return rows


def build_score_band_summary(joined: list[dict]) -> list[dict]:
    """score_band ごとの集計。"""
    bands: dict[str, dict] = {}
    for j in joined:
        sb = j["score_band"]
        if sb not in bands:
            bands[sb] = {
                "score_band": sb, "total": 0,
                "buyback_related_count": 0,
                "high_confidence_extracted_count": 0,
                "classifier_only_count": 0,
                "low_confidence_count": 0,
                "non_buyback_count": 0,
            }
        d = bands[sb]
        d["total"] += 1
        if j["is_buyback_related"]:
            d["buyback_related_count"] += 1
        bucket = j["review_bucket"]
        if bucket == "high_confidence_extracted":
            d["high_confidence_extracted_count"] += 1
        elif bucket == "classifier_only":
            d["classifier_only_count"] += 1
        elif bucket == "low_confidence":
            d["low_confidence_count"] += 1
        elif bucket == "non_buyback":
            d["non_buyback_count"] += 1

    rows = []
    for sb in sorted(bands.keys()):
        d = bands[sb]
        t = d["total"] or 1
        d["buyback_related_rate"] = round(d["buyback_related_count"] / t * 100, 1)
        d["high_confidence_extracted_rate"] = round(d["high_confidence_extracted_count"] / t * 100, 1)
        rows.append(d)
    return rows


def build_keyword_summary(expanded: list[dict]) -> list[dict]:
    """キーワード別集計。"""
    kw_stats: dict[str, dict] = {}
    for e in expanded:
        kw = e["_keyword"]
        if kw not in kw_stats:
            kw_stats[kw] = {
                "keyword": kw, "total": 0,
                "buyback_related_count": 0,
                "high_confidence_extracted_count": 0,
                "non_buyback_count": 0,
                "classifier_only_count": 0,
                "low_confidence_count": 0,
                "score_sum": 0,
            }
        d = kw_stats[kw]
        d["total"] += 1
        d["score_sum"] += e.get("manifest_candidate_score", 0)
        if e.get("is_buyback_related"):
            d["buyback_related_count"] += 1
        bucket = e.get("review_bucket", "")
        if bucket == "high_confidence_extracted":
            d["high_confidence_extracted_count"] += 1
        elif bucket == "non_buyback":
            d["non_buyback_count"] += 1
        elif bucket == "classifier_only":
            d["classifier_only_count"] += 1
        elif bucket == "low_confidence":
            d["low_confidence_count"] += 1

    rows = []
    for kw in sorted(kw_stats, key=lambda k: -kw_stats[k]["total"]):
        d = kw_stats[kw]
        t = d["total"] or 1
        d["avg_candidate_score"] = round(d.pop("score_sum") / t, 1)
        d["false_positive_rate"] = round(d["non_buyback_count"] / t * 100, 1)
        d["high_confidence_rate"] = round(d["high_confidence_extracted_count"] / t * 100, 1)
        rows.append(d)
    return rows


def build_mismatch_cases(joined: list[dict]) -> list[dict]:
    """レビュー優先の乖離ケースを抽出する。"""
    cases = []
    for j in joined:
        reasons = []
        p = j["manifest_review_priority"]
        b = j["review_bucket"]

        if p in ("high", "medium") and b == "non_buyback":
            reasons.append("scanner_high_medium_but_non_buyback")
        if p == "high" and b == "classifier_only":
            reasons.append("scanner_high_but_classifier_only")
        if p == "low" and b == "high_confidence_extracted":
            reasons.append("scanner_low_but_high_confidence")
        if b == "high_confidence_extracted" and j["extracted_fields_count"] <= 2:
            reasons.append("high_confidence_but_few_fields")
        if j.get("event_type_candidate", "").startswith("treasury_cancel") and b == "classifier_only":
            reasons.append("cancel_classifier_only")
        if j["likely_false_positive"]:
            reasons.append("likely_false_positive")
        if j["likely_missed_candidate"]:
            reasons.append("likely_missed_candidate")

        if reasons:
            cases.append({
                "manifest_path": j["manifest_path"],
                "manifest_review_priority": p,
                "manifest_candidate_score": j["manifest_candidate_score"],
                "manifest_matched_keywords": j["manifest_matched_keywords"],
                "review_bucket": b,
                "confidence_final": j["confidence_final"],
                "extracted_fields_count": j["extracted_fields_count"],
                "missing_key_fields": j["missing_key_fields"],
                "file_name": j["file_name"],
                "manifest_title": j["manifest_title"],
                "event_type_candidate": j["event_type_candidate"],
                "event_type": j["event_type"],
                "mismatch_reason": "; ".join(reasons),
            })
    return cases


# ============================================================
# 出力
# ============================================================

_JOINED_COLUMNS = [
    "join_key",
    "manifest_path", "manifest_candidate_score", "manifest_review_priority",
    "manifest_matched_keywords", "manifest_matched_keyword_count",
    "manifest_ticker", "manifest_title", "manifest_disclosure_date",
    "file_path", "file_name",
    "is_buyback_related", "event_type_candidate", "event_type",
    "classification_confidence", "extraction_confidence", "confidence_final",
    "review_bucket", "extracted_fields_count", "missing_key_fields",
    "score_band", "alignment_label",
    "likely_true_positive", "likely_false_positive",
    "likely_missed_candidate", "likely_needs_rule_improvement",
]

_MATRIX_COLUMNS = [
    "manifest_review_priority", "review_bucket", "count", "row_pct", "col_pct",
]

_SCORE_BAND_COLUMNS = [
    "score_band", "total",
    "buyback_related_count", "buyback_related_rate",
    "high_confidence_extracted_count", "high_confidence_extracted_rate",
    "classifier_only_count", "low_confidence_count", "non_buyback_count",
]

_KEYWORD_COLUMNS = [
    "keyword", "total",
    "buyback_related_count", "high_confidence_extracted_count",
    "non_buyback_count", "classifier_only_count", "low_confidence_count",
    "avg_candidate_score", "false_positive_rate", "high_confidence_rate",
]

_MISMATCH_COLUMNS = [
    "manifest_path", "manifest_review_priority", "manifest_candidate_score",
    "manifest_matched_keywords", "review_bucket", "confidence_final",
    "extracted_fields_count", "missing_key_fields",
    "file_name", "manifest_title", "event_type_candidate", "event_type",
    "mismatch_reason",
]

_JOIN_FAILURE_COLUMNS = [
    "source_side", "raw_path", "normalized_join_key", "reason",
]


def write_csv(path: str, rows: list[dict], columns: list[str]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def write_summary_md(
    path: str,
    *,
    manifest_path: str,
    review_path: str,
    manifest_count: int,
    review_count: int,
    joined: list[dict],
    failures: list[dict],
    matrix: list[dict],
    score_bands: list[dict],
    keyword_summary: list[dict],
    mismatch_cases: list[dict],
    bins: list[int],
) -> None:
    """Markdown サマリを出力する。"""
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    join_ok = len(joined)
    join_fail = len(failures)

    # priority 分布
    p_counts = Counter(j["manifest_review_priority"] for j in joined)
    # bucket 分布
    b_counts = Counter(j["review_bucket"] for j in joined)

    # precision 的指標
    high_total = sum(1 for j in joined if j["manifest_review_priority"] == "high")
    high_hce = sum(1 for j in joined if j["manifest_review_priority"] == "high"
                   and j["review_bucket"] == "high_confidence_extracted")
    high_precision = round(high_hce / high_total * 100, 1) if high_total else 0

    med_total = sum(1 for j in joined if j["manifest_review_priority"] == "medium")
    med_buyback = sum(1 for j in joined if j["manifest_review_priority"] == "medium"
                      and j["is_buyback_related"])
    med_rate = round(med_buyback / med_total * 100, 1) if med_total else 0

    low_total = sum(1 for j in joined if j["manifest_review_priority"] == "low")
    low_hce = sum(1 for j in joined if j["manifest_review_priority"] == "low"
                  and j["review_bucket"] == "high_confidence_extracted")

    fp_count = sum(1 for j in joined if j["likely_false_positive"])
    missed_count = sum(1 for j in joined if j["likely_missed_candidate"])

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Buyback Scanner vs Review — Alignment Summary\n\n")
        f.write(f"- **実行時刻**: {now}\n")
        f.write(f"- **manifest**: `{manifest_path}`\n")
        f.write(f"- **review**: `{review_path}`\n")
        f.write(f"- **score bins**: `{bins}`\n\n")

        f.write("## 基本統計\n\n")
        f.write("| 項目 | 件数 |\n|:---|---:|\n")
        f.write(f"| manifest 行数 | {manifest_count:,} |\n")
        f.write(f"| review 行数 | {review_count:,} |\n")
        f.write(f"| join 成功 | {join_ok:,} |\n")
        f.write(f"| join 失敗 | {join_fail:,} |\n\n")

        f.write("## manifest_review_priority 分布\n\n")
        f.write("| priority | 件数 |\n|:---|---:|\n")
        for p in ["high", "medium", "low", "unknown"]:
            c = p_counts.get(p, 0)
            if c:
                f.write(f"| {p} | {c:,} |\n")

        f.write("\n## review_bucket 分布\n\n")
        f.write("| bucket | 件数 |\n|:---|---:|\n")
        for b, c in b_counts.most_common():
            f.write(f"| {b} | {c:,} |\n")

        # priority × bucket matrix (top rows)
        f.write("\n## priority × bucket クロス集計\n\n")
        f.write("| priority | bucket | count | row% | col% |\n")
        f.write("|:---|:---|---:|---:|---:|\n")
        for m in sorted(matrix, key=lambda x: (-x["count"],)):
            f.write(
                f"| {m['manifest_review_priority']} | {m['review_bucket']} "
                f"| {m['count']} | {m['row_pct']}% | {m['col_pct']}% |\n"
            )

        # score band
        f.write("\n## score 帯別集計\n\n")
        f.write("| band | total | buyback% | high_conf% | cls_only | low_conf | non_bb |\n")
        f.write("|:---|---:|---:|---:|---:|---:|---:|\n")
        for sb in score_bands:
            f.write(
                f"| {sb['score_band']} | {sb['total']} "
                f"| {sb['buyback_related_rate']}% "
                f"| {sb['high_confidence_extracted_rate']}% "
                f"| {sb['classifier_only_count']} "
                f"| {sb['low_confidence_count']} "
                f"| {sb['non_buyback_count']} |\n"
            )

        # keyword top
        f.write("\n## キーワード別傾向 (上位20)\n\n")
        f.write("| keyword | total | high_conf | non_bb | FP% | HC% | avg_score |\n")
        f.write("|:---|---:|---:|---:|---:|---:|---:|\n")
        for kw in keyword_summary[:20]:
            f.write(
                f"| {kw['keyword']} | {kw['total']} "
                f"| {kw['high_confidence_extracted_count']} "
                f"| {kw['non_buyback_count']} "
                f"| {kw['false_positive_rate']}% "
                f"| {kw['high_confidence_rate']}% "
                f"| {kw['avg_candidate_score']} |\n"
            )

        # precision 指標
        f.write("\n## Scanner 精度指標\n\n")
        f.write("| 項目 | 値 |\n|:---|---:|\n")
        f.write(f"| scanner high → high_confidence_extracted 率 | {high_precision}% ({high_hce}/{high_total}) |\n")
        f.write(f"| scanner medium → buyback_related 率 | {med_rate}% ({med_buyback}/{med_total}) |\n")
        f.write(f"| scanner low に潜む high_confidence 件数 | {low_hce} / {low_total} |\n")
        f.write(f"| likely_false_positive | {fp_count} |\n")
        f.write(f"| likely_missed_candidate | {missed_count} |\n")
        f.write(f"| mismatch_cases | {len(mismatch_cases)} |\n")

        # 所見
        f.write("\n## 所見\n\n")
        if high_precision >= 70:
            f.write(f"- scanner high は高精度 ({high_precision}%)、auto-review 候補として有効\n")
        elif high_precision >= 40:
            f.write(f"- scanner high の precision は中程度 ({high_precision}%)。閾値見直しの余地あり\n")
        else:
            f.write(f"- scanner high の precision が低い ({high_precision}%)。score 計算ルールの見直し推奨\n")

        if med_rate >= 30:
            f.write(f"- medium にも真の buyback が多く ({med_rate}%)、review 対象として有効\n")
        else:
            f.write(f"- medium の buyback 率は低い ({med_rate}%)。medium は優先度下げ可能\n")

        if low_hce > 0:
            f.write(f"- low に high_confidence が {low_hce} 件あり。scanner キーワードの拡充を検討\n")

        if fp_count > 0:
            # false positive keyword 特定
            fp_kws = Counter()
            for j in joined:
                if j["likely_false_positive"]:
                    kws = j["manifest_matched_keywords"]
                    for sep in ["|", ",", ";"]:
                        if sep in kws:
                            for k in kws.split(sep):
                                k = k.strip()
                                if k:
                                    fp_kws[k] += 1
                            break
                    else:
                        if kws.strip():
                            fp_kws[kws.strip()] += 1
            if fp_kws:
                top_fp = fp_kws.most_common(5)
                f.write(f"- false positive の上位キーワード: {', '.join(f'{k}({c})' for k, c in top_fp)}\n")


# ============================================================
# CLI
# ============================================================

def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scanner vs Review 乖離分析ツール",
    )
    parser.add_argument("--manifest", required=True,
                        help="candidate_manifest.csv or buyback_candidates.csv")
    parser.add_argument("--review", required=True,
                        help="review_buyback_results.csv")
    parser.add_argument("--output-dir", default="artifacts/buyback_alignment",
                        help="出力ディレクトリ")
    parser.add_argument("--score-bins", default="0,3,6,10,100",
                        help="score 帯の境界 (カンマ区切り)")
    parser.add_argument("--verbose", action="store_true")
    opts = parser.parse_args(args)

    level = logging.DEBUG if opts.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    bins = parse_score_bins(opts.score_bins)
    logger.info(f"score bins: {bins}")

    # 1. CSV 読み込み
    manifest_rows = load_csv(opts.manifest)
    review_rows = load_csv(opts.review)
    logger.info(f"manifest: {len(manifest_rows)}行, review: {len(review_rows)}行")

    if not manifest_rows:
        logger.error("manifest CSV が空または見つかりません")
        return 1
    if not review_rows:
        logger.error("review CSV が空または見つかりません")
        return 1

    # 2. Join
    joined, failures = join_manifest_and_review(manifest_rows, review_rows)
    logger.info(f"join: {len(joined)}件成功, {len(failures)}件失敗")

    # 3. 派生列
    for j in joined:
        derive_alignment_flags(j, bins)

    # 4. 集計
    matrix = build_priority_bucket_matrix(joined)
    score_bands = build_score_band_summary(joined)
    expanded = expand_keywords(joined)
    keyword_summary = build_keyword_summary(expanded)
    mismatch_cases = build_mismatch_cases(joined)

    # 5. 出力
    out = opts.output_dir
    write_csv(os.path.join(out, "alignment_joined.csv"), joined, _JOINED_COLUMNS)
    write_csv(os.path.join(out, "alignment_priority_bucket_matrix.csv"), matrix, _MATRIX_COLUMNS)
    write_csv(os.path.join(out, "alignment_score_band_summary.csv"), score_bands, _SCORE_BAND_COLUMNS)
    write_csv(os.path.join(out, "alignment_keyword_summary.csv"), keyword_summary, _KEYWORD_COLUMNS)
    write_csv(os.path.join(out, "alignment_mismatch_cases.csv"), mismatch_cases, _MISMATCH_COLUMNS)
    write_csv(os.path.join(out, "alignment_join_failures.csv"), failures, _JOIN_FAILURE_COLUMNS)

    write_summary_md(
        os.path.join(out, "alignment_summary.md"),
        manifest_path=opts.manifest,
        review_path=opts.review,
        manifest_count=len(manifest_rows),
        review_count=len(review_rows),
        joined=joined,
        failures=failures,
        matrix=matrix,
        score_bands=score_bands,
        keyword_summary=keyword_summary,
        mismatch_cases=mismatch_cases,
        bins=bins,
    )

    logger.info(f"出力: {out}/")
    logger.info(
        f"完了: joined={len(joined)} failures={len(failures)} "
        f"mismatch={len(mismatch_cases)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
