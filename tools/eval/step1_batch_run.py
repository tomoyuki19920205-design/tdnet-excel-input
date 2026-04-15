"""
Step 1: 全89件バッチ実行スクリプト
全 data/xbrl_archive/*.pdf に run_segment_detection_v2 をかけ
runs.jsonl と candidates.csv に結果を保存する。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import glob
import json
import csv
import traceback
from pathlib import Path
from src.analysis.segment_detection_v2 import run_segment_detection_v2

ARCHIVE_DIR = Path(__file__).parent.parent.parent / "data" / "xbrl_archive"
EVAL_DIR    = Path(__file__).parent.parent.parent / "data" / "eval"
EVAL_DIR.mkdir(parents=True, exist_ok=True)

RUNS_JSONL    = EVAL_DIR / "runs.jsonl"
CANDIDATES_CSV = EVAL_DIR / "candidates.csv"

pdfs = sorted(ARCHIVE_DIR.glob("*.pdf"))
print(f"対象: {len(pdfs)} 件\n")

CAND_COLS = [
    "pdf", "ticker", "detected_mode", "parse_quality",
    "quarantine_reason", "segment_count", "page_number", "unit_text",
]

with open(RUNS_JSONL, "w", encoding="utf-8") as jf, \
     open(CANDIDATES_CSV, "w", encoding="utf-8", newline="") as cf:

    writer = csv.DictWriter(cf, fieldnames=CAND_COLS)
    writer.writeheader()

    for i, pdf in enumerate(pdfs, 1):
        base = pdf.name
        print(f"[{i:3d}/{len(pdfs)}] {base} ... ", end="", flush=True)
        try:
            result = run_segment_detection_v2(str(pdf), doc_id=base, ticker="?")
        except Exception as e:
            print(f"ERROR: {e}")
            rec = {
                "pdf": base, "ticker": "?",
                "detected_mode": "error", "parse_quality": None,
                "quarantine_reason": str(e)[:200],
                "segment_count": 0, "page_number": None, "unit_text": None,
                "segments_json": "[]", "error": True,
            }
            jf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            writer.writerow({k: rec.get(k, "") for k in CAND_COLS})
            continue

        # detected_mode 判定
        fcol = [t for t in result.rule_trace if "F-col" in t]
        if result.success and any("SUCCESS" in t for t in fcol):
            mode = "COL_AS_SEG"
        elif result.success:
            mode = "ROW_BASED"
        else:
            mode = "quarantine"

        # page_number: provenance から取得
        page_no = None
        unit_text = None
        if result.segments:
            prov = result.segments[0].provenance or {}
            page_no = prov.get("page_no")
            unit_text = result.segments[0].unit_raw or ""

        # parse_quality: full / partial / None
        pq_full = sum(1 for s in result.segments if s.parse_quality == "full")
        pq_all  = len(result.segments)
        if pq_all == 0:
            pq = None
        elif pq_full == pq_all:
            pq = "full"
        elif pq_full > 0:
            pq = "partial"
        else:
            pq = "sales_only"

        segs_data = [
            {
                "segment_name": s.segment_name,
                "segment_name_raw": s.segment_name_raw,
                "sales": s.segment_sales,
                "profit": s.segment_profit,
                "parse_quality": s.parse_quality,
                "row_role": s.row_role,
                "is_reportable": s.is_reportable_segment,
            }
            for s in result.segments
        ]

        # Phase C トレースから unit を抽出
        unit_from_trace = ""
        for t in result.rule_trace:
            if t.startswith("Unit:"):
                unit_from_trace = t
                break

        rec = {
            "pdf": base,
            "ticker": "?",
            "detected_mode": mode,
            "parse_quality": pq,
            "quarantine_reason": result.quarantine_reason or "",
            "segment_count": len(result.segments),
            "page_number": page_no,
            "unit_text": unit_from_trace,
            "segments_json": json.dumps(segs_data, ensure_ascii=False),
            "score_summary": json.dumps(result.score_summary, ensure_ascii=False),
        }

        print(f"mode={mode} segs={len(result.segments)} qrn={result.quarantine_reason or '-'}")
        jf.write(json.dumps(rec, ensure_ascii=False) + "\n")
        writer.writerow({k: rec.get(k, "") for k in CAND_COLS})

print(f"\n完了: {RUNS_JSONL}")
print(f"完了: {CANDIDATES_CSV}")
