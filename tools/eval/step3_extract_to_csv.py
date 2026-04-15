"""
Step 3: 詳細評価対象 PDF の抽出結果を CSV に出力するスクリプト。
入力:  data/eval/detailed_eval_targets.csv
出力:  data/eval/extracted.csv
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import csv
from pathlib import Path
from src.analysis.segment_detection_v2 import run_segment_detection_v2

ARCHIVE_DIR  = Path(__file__).parent.parent.parent / "data" / "xbrl_archive"
EVAL_DIR     = Path(__file__).parent.parent.parent / "data" / "eval"
TARGETS_CSV  = EVAL_DIR / "detailed_eval_targets.csv"
EXTRACTED_CSV = EVAL_DIR / "extracted.csv"

COLS = [
    "pdf", "ticker", "detected_mode",
    "segment_name_raw", "segment_name_norm",
    "sales", "profit",
    "parse_quality", "quarantine_reason",
    "page_number", "unit_text",
]

targets = []
with open(TARGETS_CSV, encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        targets.append({
            "pdf": row["pdf"].strip(),
            "ticker": row.get("ticker", "?").strip(),
        })

print(f"評価対象: {len(targets)} 件")
all_rows = []

for t in targets:
    pdf_path = ARCHIVE_DIR / t["pdf"]
    print(f"  {t['pdf']} ... ", end="", flush=True)

    try:
        result = run_segment_detection_v2(str(pdf_path), doc_id=t["pdf"], ticker="?")
    except Exception as e:
        print(f"ERROR: {e}")
        all_rows.append({
            "pdf": t["pdf"], "ticker": t["ticker"],
            "detected_mode": "error",
            "segment_name_raw": "", "segment_name_norm": "",
            "sales": "", "profit": "",
            "parse_quality": "", "quarantine_reason": str(e)[:200],
            "page_number": "", "unit_text": "",
        })
        continue

    fcol = [t2 for t2 in result.rule_trace if "F-col" in t2]
    mode = "COL_AS_SEG" if (result.success and any("SUCCESS" in t2 for t2 in fcol)) \
           else ("ROW_BASED" if result.success else "quarantine")

    unit_text = next((t2 for t2 in result.rule_trace if t2.startswith("Unit:")), "")

    if result.success and result.segments:
        print(f"mode={mode} segs={len(result.segments)}")
        for seg in result.segments:
            prov = seg.provenance or {}
            all_rows.append({
                "pdf": t["pdf"],
                "ticker": t["ticker"],
                "detected_mode": mode,
                "segment_name_raw": seg.segment_name_raw or seg.segment_name,
                "segment_name_norm": seg.segment_name,
                "sales": "" if seg.segment_sales is None else seg.segment_sales,
                "profit": "" if seg.segment_profit is None else seg.segment_profit,
                "parse_quality": seg.parse_quality,
                "quarantine_reason": "",
                "page_number": prov.get("page_no", ""),
                "unit_text": unit_text,
            })
    else:
        print(f"quarantine:{result.quarantine_reason}")
        all_rows.append({
            "pdf": t["pdf"], "ticker": t["ticker"],
            "detected_mode": "quarantine",
            "segment_name_raw": "", "segment_name_norm": "",
            "sales": "", "profit": "",
            "parse_quality": "",
            "quarantine_reason": result.quarantine_reason or "",
            "page_number": "", "unit_text": unit_text,
        })

with open(EXTRACTED_CSV, "w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=COLS)
    writer.writeheader()
    writer.writerows(all_rows)

print(f"\n完了: {EXTRACTED_CSV}  ({len(all_rows)} 行)")
