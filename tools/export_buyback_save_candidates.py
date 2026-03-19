#!/usr/bin/env python3
"""export_buyback_save_candidates.py — buyback 保存候補 / 人手 review キュー 切り出しツール

review_buyback_results.csv を入力にして、以下を出力する。
1. review_save_candidates.csv — DB 保存候補一覧
2. review_manual_review_queue.csv — 人手レビュー対象一覧
3. review_operation_summary.md — 運用サマリ

Usage:
  cd "C:\\Users\\takuy\\OneDrive\\tdnet-excel-input"
  .\\.venv\\Scripts\\python.exe tools/export_buyback_save_candidates.py \\
    --review artifacts/buyback_review_candidates_tuned/review_buyback_results.csv \\
    --output-dir artifacts/buyback_review_operation
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows cp932 対策
if sys.stdout and hasattr(sys.stdout, "encoding"):
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

JST = timezone(timedelta(hours=9))
logger = logging.getLogger("export_save_candidates")

# ============================================================
# 定数: 保存候補の列一覧
# ============================================================
_SAVE_CANDIDATE_COLUMNS = [
    "file_path", "file_name", "ticker", "disclosure_date", "title",
    "manifest_candidate_score", "manifest_review_priority",
    "matched_keywords", "event_type", "confidence_final",
    "review_bucket", "extracted_fields_count",
    "missing_key_fields",
    "shares_limit", "shares_acquired", "shares_cancelled",
    "amount_limit_million_yen", "amount_acquired_million_yen",
    "start_date", "end_date", "cancel_date",
    "acquisition_method", "save_reason",
]

_MANUAL_REVIEW_COLUMNS = [
    "file_path", "file_name", "ticker", "title",
    "manifest_candidate_score", "manifest_review_priority",
    "matched_keywords", "review_bucket", "confidence_final",
    "extracted_fields_count", "missing_key_fields",
    "event_type", "review_reason",
]

# ============================================================
# 判定ロジック
# ============================================================

def _safe_float(val: Any) -> float:
    """安全に float 変換。None/空/非数値 → 0.0"""
    if val is None or val == "":
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _safe_int(val: Any) -> int:
    """安全に int 変換。None/空/非数値 → 0"""
    if val is None or val == "":
        return 0
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 0


def _safe_bool(val: Any) -> bool:
    """安全に bool 変換。'True'/'true'/True → True"""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() == "true"
    return bool(val)


def is_save_candidate(
    row: dict,
    *,
    min_confidence: float = 0.60,
    min_extracted_fields: int = 1,
) -> tuple[bool, str]:
    """保存候補か判定する。

    条件:
    - review_bucket == 'high_confidence_extracted'
    - is_buyback_related == true
    - confidence_final >= min_confidence
    - extracted_fields_count >= min_extracted_fields

    Returns:
        (is_candidate, reason)
    """
    bucket = (row.get("review_bucket") or "").strip()
    buyback = _safe_bool(row.get("is_buyback_related", False))
    conf = _safe_float(row.get("confidence_final"))
    fields = _safe_int(row.get("extracted_fields_count"))

    if bucket != "high_confidence_extracted":
        return False, ""
    if not buyback:
        return False, ""
    if conf < min_confidence:
        return False, ""
    if fields < min_extracted_fields:
        return False, ""

    reason = "high_confidence_extracted"
    if fields >= 3:
        reason = "high_confidence_extracted_with_core_fields"

    return True, reason


def is_manual_review_candidate(
    row: dict,
    *,
    include_priorities: set[str] | None = None,
) -> tuple[bool, str]:
    """人手レビュー対象か判定する。

    条件:
    - manifest_review_priority が include_priorities に含まれる
    - かつ以下のいずれか:
      - review_bucket in {classifier_only, low_confidence, extraction_failed}
      - treasury_cancel で fields 不足
      - buyback_related だが save 候補に届かない

    Returns:
        (is_candidate, reason)
    """
    priority = (row.get("manifest_review_priority") or "").strip().lower()
    bucket = (row.get("review_bucket") or "").strip()
    buyback = _safe_bool(row.get("is_buyback_related", False))
    event_type = (row.get("event_type") or "").strip()
    fields = _safe_int(row.get("extracted_fields_count"))

    # priority フィルタ ("all" は全件許可)
    allowed = include_priorities or {"medium", "high"}
    if "all" not in allowed and priority not in allowed:
        # 空の priority は scanner 連携がない場合なので all 扱い
        if priority != "" or "all" not in allowed:
            if priority not in allowed and priority != "":
                return False, ""

    # non_buyback / excluded / text_extract_failed はどちらにも入らない
    if bucket in ("non_buyback", "excluded", "text_extract_failed"):
        return False, ""
    if not buyback:
        return False, ""

    # save candidate ならここには来ない（呼び出し側で先に判定する前提）

    # classifier_only
    if bucket == "classifier_only":
        return True, "classifier_only"

    # low_confidence
    if bucket == "low_confidence":
        return True, "low_confidence"

    # extraction_failed
    if bucket == "extraction_failed":
        return True, "extraction_failed"

    # cancel 系で fields 不足
    if event_type == "treasury_cancel" and fields == 0:
        return True, "cancel_missing_fields"

    # その他 buyback_related だが save に届かない
    if buyback and bucket not in ("high_confidence_extracted",):
        return True, f"bucket_{bucket}"

    return False, ""


# ============================================================
# CSV 読み込み
# ============================================================

def load_review_results(path: str) -> list[dict]:
    """review_buyback_results.csv を読み込む"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Review results not found: {path}")
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


# ============================================================
# メイン分離ロジック
# ============================================================

def split_candidates(
    rows: list[dict],
    *,
    min_confidence: float = 0.60,
    min_extracted_fields: int = 1,
    include_priorities: set[str] | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """review 結果を save_candidates / manual_review / skipped に分離する。

    Returns:
        (save_candidates, manual_review, skipped)
    """
    allowed = include_priorities or {"medium", "high"}
    save_candidates: list[dict] = []
    manual_review: list[dict] = []
    skipped: list[dict] = []

    for row in rows:
        priority = (row.get("manifest_review_priority") or "").strip().lower()

        # priority フィルタ ("all" は全件許可)
        if "all" not in allowed and priority not in allowed and priority != "":
            skipped.append(row)
            continue

        # save 候補判定
        is_save, save_reason = is_save_candidate(
            row,
            min_confidence=min_confidence,
            min_extracted_fields=min_extracted_fields,
        )
        if is_save:
            row["save_reason"] = save_reason
            save_candidates.append(row)
            continue

        # manual review 判定
        is_review, review_reason = is_manual_review_candidate(
            row, include_priorities=allowed,
        )
        if is_review:
            row["review_reason"] = review_reason
            manual_review.append(row)
            continue

        skipped.append(row)

    return save_candidates, manual_review, skipped


# ============================================================
# CSV 出力
# ============================================================

def _write_csv(
    rows: list[dict],
    path: str,
    columns: list[str],
) -> None:
    """指定列のみの CSV を書き出す。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    logger.info(f"CSV 出力: {path} ({len(rows)} rows)")


# ============================================================
# Summary 出力
# ============================================================

def generate_summary(
    *,
    review_path: str,
    total_rows: int,
    save_candidates: list[dict],
    manual_review: list[dict],
    skipped: list[dict],
    min_confidence: float,
    min_extracted_fields: int,
    include_priorities: set[str],
) -> str:
    """review_operation_summary.md を生成する。"""
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

    # 統計
    all_rows = save_candidates + manual_review + skipped
    if "all" in include_priorities:
        medium_high = all_rows
    else:
        medium_high = [r for r in all_rows
                       if (r.get("manifest_review_priority") or "").strip().lower()
                       in include_priorities]
    non_buyback = [r for r in all_rows
                   if not _safe_bool(r.get("is_buyback_related", False))]
    excluded = [r for r in all_rows
                if (r.get("review_bucket") or "").strip() in ("excluded", "non_buyback")]

    # event_type 分布
    et_counts: dict[str, int] = {}
    for r in save_candidates:
        et = r.get("event_type") or "unknown"
        et_counts[et] = et_counts.get(et, 0) + 1

    # review reason 分布
    rr_counts: dict[str, int] = {}
    for r in manual_review:
        rr = r.get("review_reason") or "unknown"
        rr_counts[rr] = rr_counts.get(rr, 0) + 1

    save_rate = (len(save_candidates) / len(medium_high) * 100
                 if medium_high else 0)
    review_rate = (len(manual_review) / len(medium_high) * 100
                   if medium_high else 0)

    lines = [
        "# Buyback Review Operation — Summary",
        "",
        f"- **実行時刻**: {now}",
        f"- **review results**: `{review_path}`",
        "",
        "## パラメータ",
        "",
        f"| パラメータ | 値 |",
        f"|:---|:---|",
        f"| min_confidence | {min_confidence} |",
        f"| min_extracted_fields | {min_extracted_fields} |",
        f"| include_priorities | {', '.join(sorted(include_priorities))} |",
        "",
        "## 集計",
        "",
        "| 項目 | 件数 |",
        "|:---|---:|",
        f"| 入力 review 行数 | {total_rows} |",
        f"| medium/high 対象 | {len(medium_high)} |",
        f"| **save candidates** | **{len(save_candidates)}** |",
        f"| **manual review queue** | **{len(manual_review)}** |",
        f"| skipped (low/non-target) | {len(skipped)} |",
        f"| non_buyback | {len(non_buyback)} |",
        f"| excluded | {len(excluded)} |",
        f"| save candidate rate | {save_rate:.1f}% |",
        f"| manual review rate | {review_rate:.1f}% |",
        "",
    ]

    if et_counts:
        lines.append("## save candidate event_type 分布")
        lines.append("")
        lines.append("| event_type | 件数 |")
        lines.append("|:---|---:|")
        for et, cnt in sorted(et_counts.items(), key=lambda x: -x[1]):
            lines.append(f"| {et} | {cnt} |")
        lines.append("")

    if rr_counts:
        lines.append("## manual review 主因")
        lines.append("")
        lines.append("| review_reason | 件数 |")
        lines.append("|:---|---:|")
        for rr, cnt in sorted(rr_counts.items(), key=lambda x: -x[1]):
            lines.append(f"| {rr} | {cnt} |")
        lines.append("")

    # 所見
    lines.append("## 所見")
    lines.append("")
    if save_candidates:
        lines.append(
            f"- medium/high 帯 {len(medium_high)} 件のうち "
            f"**{len(save_candidates)} 件** が保存候補 "
            f"({save_rate:.1f}%)"
        )
    else:
        lines.append("- 保存候補は 0 件")
    if manual_review:
        main_reason = max(rr_counts, key=rr_counts.get) if rr_counts else "N/A"
        lines.append(
            f"- manual review queue: {len(manual_review)} 件 "
            f"(主因: {main_reason})"
        )
    if any("cancel" in (r.get("event_type") or "") for r in manual_review):
        lines.append("- cancel 系は review を維持すべき (recall 未検証)")
    lines.append(
        "- 現段階では auto-save ではなく human-in-the-loop が妥当"
    )
    lines.append("")

    return "\n".join(lines)


# ============================================================
# メイン
# ============================================================

def main(args: list[str] | None = None) -> int:
    """メインエントリポイント。"""
    parser = argparse.ArgumentParser(
        description="buyback 保存候補 / 人手 review キュー 切り出しツール",
    )
    parser.add_argument(
        "--review", required=True,
        help="review_buyback_results.csv のパス",
    )
    parser.add_argument(
        "--output-dir", default="artifacts/buyback_review_operation",
        help="出力ディレクトリ",
    )
    parser.add_argument(
        "--min-confidence", type=float, default=0.60,
        help="保存候補の最低 confidence (default: 0.60)",
    )
    parser.add_argument(
        "--min-core-fields", type=int, default=1,
        help="保存候補の最低 extracted_fields_count (default: 1)",
    )
    parser.add_argument(
        "--include-priority", default="all",
        help="review 対象 priority (カンマ区切り or 'all', default: all)",
    )
    parser.add_argument("--verbose", action="store_true")

    opts = parser.parse_args(args)

    level = logging.DEBUG if opts.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    include_priorities = {
        p.strip().lower() for p in opts.include_priority.split(",")
    }

    # 読み込み
    rows = load_review_results(opts.review)
    logger.info(f"review rows: {len(rows)}")

    # 分離
    save_candidates, manual_review, skipped = split_candidates(
        rows,
        min_confidence=opts.min_confidence,
        min_extracted_fields=opts.min_core_fields,
        include_priorities=include_priorities,
    )

    logger.info(
        f"save candidates: {len(save_candidates)}, "
        f"manual review: {len(manual_review)}, "
        f"skipped: {len(skipped)}"
    )

    # 出力
    out_dir = opts.output_dir
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    _write_csv(
        save_candidates,
        os.path.join(out_dir, "review_save_candidates.csv"),
        _SAVE_CANDIDATE_COLUMNS,
    )
    _write_csv(
        manual_review,
        os.path.join(out_dir, "review_manual_review_queue.csv"),
        _MANUAL_REVIEW_COLUMNS,
    )

    summary = generate_summary(
        review_path=opts.review,
        total_rows=len(rows),
        save_candidates=save_candidates,
        manual_review=manual_review,
        skipped=skipped,
        min_confidence=opts.min_confidence,
        min_extracted_fields=opts.min_core_fields,
        include_priorities=include_priorities,
    )
    summary_path = os.path.join(out_dir, "review_operation_summary.md")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary)
    logger.info(f"summary: {summary_path}")

    # コンソール出力
    print()
    print(f"保存候補: {len(save_candidates)} 件")
    print(f"人手 review: {len(manual_review)} 件")
    print(f"skipped: {len(skipped)} 件")
    print(f"出力先: {out_dir}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
