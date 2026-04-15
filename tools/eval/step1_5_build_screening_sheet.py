"""
Step 1.5: 簡易スクリーニングシート作成スクリプト
runs.jsonl から全89件のスクリーニングシートを生成し、
人手で has_segment_table (yes/no) を入力できる形式で出力する。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import json
import csv
from pathlib import Path

EVAL_DIR    = Path(__file__).parent.parent.parent / "data" / "eval"
RUNS_JSONL  = EVAL_DIR / "runs.jsonl"
SHEET_CSV   = EVAL_DIR / "screening_sheet.csv"

COLS = [
    "pdf", "ticker", "detected_mode", "quarantine_reason",
    "segment_count", "page_number", "unit_text",
    "segment_names_preview",      # 先頭3件のセグメント名（確認用）
    "has_segment_table",          # 人手入力: yes / no / unknown
    "expected_table_type",        # 人手入力: col_as_seg / row_based / unknown
    "notes",                      # 人手入力: 自由記述
]

rows = []
with open(RUNS_JSONL, encoding="utf-8") as f:
    for line in f:
        rec = json.loads(line.strip())
        segs = json.loads(rec.get("segments_json", "[]"))
        seg_names = [s["segment_name"] for s in segs[:3]]
        seg_preview = " / ".join(seg_names) if seg_names else ""

        rows.append({
            "pdf":                  rec["pdf"],
            "ticker":               rec.get("ticker", "?"),
            "detected_mode":        rec.get("detected_mode", ""),
            "quarantine_reason":    rec.get("quarantine_reason", ""),
            "segment_count":        rec.get("segment_count", 0),
            "page_number":          rec.get("page_number", ""),
            "unit_text":            rec.get("unit_text", ""),
            "segment_names_preview": seg_preview,
            "has_segment_table":    "",   # 人手記入
            "expected_table_type":  "",   # 人手記入
            "notes":                "",   # 人手記入
        })

# detected_mode 順でソート（COL_AS_SEG → ROW_BASED → quarantine → error）
MODE_ORDER = {"COL_AS_SEG": 0, "ROW_BASED": 1, "quarantine": 2, "error": 3}
rows.sort(key=lambda r: (MODE_ORDER.get(r["detected_mode"], 9), r["pdf"]))

with open(SHEET_CSV, "w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=COLS)
    writer.writeheader()
    writer.writerows(rows)

print(f"完了: {SHEET_CSV}  ({len(rows)} 件)")

# 簡易集計表示
from collections import Counter
mode_counts = Counter(r["detected_mode"] for r in rows)
print("\n--- mode 別件数 ---")
for mode, cnt in sorted(mode_counts.items(), key=lambda x: MODE_ORDER.get(x[0], 9)):
    print(f"  {mode:<15}: {cnt} 件")
