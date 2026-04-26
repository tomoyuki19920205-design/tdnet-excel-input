"""tools/analyze_quarantined_audit.py — quarantine 34件の優先度分類レポート

入力:  out/quarantined_segments_audit.csv
出力:  out/quarantined_segments_priority_report.csv

実行:
    python -X utf8 .\\tools\\analyze_quarantined_audit.py
"""
from __future__ import annotations

import csv
import os
import sys
from collections import Counter
from pathlib import Path

IN_CSV  = "out/quarantined_segments_audit.csv"
OUT_CSV = "out/quarantined_segments_priority_report.csv"

OUT_COLUMNS = [
    "priority",
    "ticker",
    "disclosure_date",
    "title",
    "reason",
    "selected_path",
    "fallback_reason",
    "valid_segment_count",
    "sales_non_null_count",
    "profit_non_null_count",
    "suspected_cause",
    "recommended_action",
]


def _classify(row: dict) -> tuple[str, str, str]:
    """1行を分類して (priority, suspected_cause, recommended_action) を返す。

    判定順序:
        A: valid_segment_count >= 10
        B: fallback_reason に single_segment_omitted / segment_disclosure_omitted を含む
        C: valid_segment_count 1〜5
        D: valid_segment_count = 0
    """
    try:
        valid_count = int(row.get("valid_segment_count") or 0)
    except ValueError:
        valid_count = 0

    fallback_reason = row.get("fallback_reason") or ""

    if valid_count >= 10:
        return (
            "A",
            "valid segments exist but quality gate rejected",
            "inspect quality gate / segment-name filter",
        )

    if (
        "single_segment_omitted" in fallback_reason
        or "segment_disclosure_omitted" in fallback_reason
    ):
        return (
            "B",
            "normal skip leakage into quarantine",
            "route to skipped_normal before quarantine",
        )

    if 1 <= valid_count <= 5:
        return (
            "C",
            "partial extraction / possible total-only row",
            "inspect extracted rows and labels",
        )

    # valid_count == 0 (and not B)
    return (
        "D",
        "no usable segment records",
        "likely true no_records; lower priority",
    )


def main() -> None:
    if not os.path.exists(IN_CSV):
        print(f"[error] 入力ファイルが見つかりません: {IN_CSV}", file=sys.stderr)
        sys.exit(1)

    with open(IN_CSV, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        in_rows = list(reader)

    if not in_rows:
        print("[analyze] 入力CSVが空です。終了します。")
        return

    print(f"[analyze] 入力件数: {len(in_rows)} 件")

    out_rows: list[dict] = []
    for row in in_rows:
        priority, suspected_cause, recommended_action = _classify(row)
        out_rows.append({
            "priority":             priority,
            "ticker":               row.get("ticker", ""),
            "disclosure_date":      row.get("disclosure_date", ""),
            "title":                row.get("title", ""),
            "reason":               row.get("reason", ""),
            "selected_path":        row.get("selected_path", ""),
            "fallback_reason":      row.get("fallback_reason", ""),
            "valid_segment_count":  row.get("valid_segment_count", "0"),
            "sales_non_null_count": row.get("sales_non_null_count", "0"),
            "profit_non_null_count": row.get("profit_non_null_count", "0"),
            "suspected_cause":      suspected_cause,
            "recommended_action":   recommended_action,
        })

    # priority A → D の順でソート（同一 priority 内は disclosure_date 昇順）
    out_rows.sort(key=lambda r: (r["priority"], r["disclosure_date"], r["ticker"]))

    Path(OUT_CSV).parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_COLUMNS)
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"[analyze] CSV 出力完了: {OUT_CSV}")

    # ── priority 別件数を標準出力
    priority_counts = Counter(r["priority"] for r in out_rows)
    print("\n--- priority 別件数 ---")
    for p in sorted(priority_counts):
        cnt = priority_counts[p]
        labels = {
            "A": "valid segments but quality gate rejected",
            "B": "normal skip leakage into quarantine",
            "C": "partial extraction (1〜5 segs)",
            "D": "no usable segment records",
        }
        print(f"  {p}: {cnt:>3} 件  — {labels.get(p, '')}")

    # ── reason × priority クロス集計
    print("\n--- reason × priority クロス ---")
    cross: dict[tuple[str, str], int] = Counter(
        (r["reason"], r["priority"]) for r in out_rows
    )
    reason_set = sorted({r["reason"] for r in out_rows})
    print(f"  {'reason':<40} " + "  ".join(f"P{p}" for p in "ABCD"))
    for reason in reason_set:
        counts = "  ".join(f"{cross.get((reason, p), 0):>3}" for p in "ABCD")
        print(f"  {reason:<40} {counts}")

    # ── 優先対応メモ
    print("\n--- 優先対応メモ ---")
    a_tickers = [r["ticker"] for r in out_rows if r["priority"] == "A"]
    b_tickers = [r["ticker"] for r in out_rows if r["priority"] == "B"]
    if a_tickers:
        print(f"  [A] 品質ゲート要確認: {', '.join(a_tickers)}")
    if b_tickers:
        print(f"  [B] normal_skip 漏れ: {', '.join(b_tickers)}")


if __name__ == "__main__":
    main()
