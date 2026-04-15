"""
apply_gt_audit_and_rerun_metrics.py
last9_gt_audit.csv の監査結果を screening_sheet.csv に反映し、
修正後の GT で metrics を再計算する。

出力（既存ファイルは上書きしない）:
  data/eval/screening_sheet.after_gt_audit.csv
  data/eval/metrics_summary.after_gt_audit.json
  data/eval/metrics_by_pdf.after_gt_audit.csv
  data/eval/failure_cases.after_gt_audit.csv
  data/eval/gt_audit_apply_summary.md
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import csv, json, re
from pathlib import Path
from collections import defaultdict

PROJ     = Path(__file__).parent.parent.parent
EVAL_DIR = PROJ / "data" / "eval"

AUDIT_CSV      = EVAL_DIR / "last9_gt_audit.csv"
SCREEN_CSV     = EVAL_DIR / "screening_sheet.csv"
GT_CSV         = EVAL_DIR / "ground_truth.csv"
EXTRACTED_CSV  = EVAL_DIR / "extracted.csv"

OUT_SCREEN     = EVAL_DIR / "screening_sheet.after_gt_audit.csv"
OUT_METRICS    = EVAL_DIR / "metrics_summary.after_gt_audit.json"
OUT_BY_PDF     = EVAL_DIR / "metrics_by_pdf.after_gt_audit.csv"
OUT_FAIL       = EVAL_DIR / "failure_cases.after_gt_audit.csv"
OUT_SUMMARY_MD = EVAL_DIR / "gt_audit_apply_summary.md"

# ── 監査結果読み込み ──────────────────────────────────────────

audit_updates = {}  # pdf -> audited_has_segment_table
for r in csv.DictReader(open(AUDIT_CSV, encoding="utf-8-sig")):
    cur = r["current_has_segment_table"].strip().lower()
    aud = r["audited_has_segment_table"].strip().lower()
    if cur != aud:
        audit_updates[r["pdf"].strip()] = aud

print(f"監査更新対象: {len(audit_updates)} 件")
for pdf, new_val in audit_updates.items():
    print(f"  {pdf}: yes → {new_val}")

# ── screening_sheet.csv コピー＋反映 ──────────────────────────

all_screen_rows = []
screen_fieldnames = None
with open(SCREEN_CSV, encoding="utf-8-sig", newline="") as f:
    reader = csv.DictReader(f)
    screen_fieldnames = reader.fieldnames
    for row in reader:
        pdf = row["pdf"].strip()
        if pdf in audit_updates:
            row = dict(row)
            row["has_segment_table"] = audit_updates[pdf]
        all_screen_rows.append(row)

with open(OUT_SCREEN, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=screen_fieldnames)
    w.writeheader()
    w.writerows(all_screen_rows)
print(f"出力: {OUT_SCREEN}")

# ── 検出評価ロジック（step4_metrics.py から流用） ────────────

def _safe_rate(num, denom):
    return round(num / denom, 4) if denom > 0 else None

def _compute_detection_metrics(screen_rows_list):
    """screening_sheet 行リストから検出評価指標を集計。"""
    all_pdfs    = [r["pdf"].strip() for r in screen_rows_list]
    has_yes     = {r["pdf"].strip() for r in screen_rows_list if r.get("has_segment_table","").strip().lower() == "yes"}
    has_no      = {r["pdf"].strip() for r in screen_rows_list if r.get("has_segment_table","").strip().lower() == "no"}
    mode_map    = {r["pdf"].strip(): r.get("detected_mode","").strip() for r in screen_rows_list}

    total       = len(all_pdfs)
    quarantine  = sum(1 for p in all_pdfs if mode_map.get(p) == "quarantine")
    detected    = sum(1 for p in all_pdfs if mode_map.get(p) in {"COL_AS_SEG","ROW_BASED"})
    fn_count    = sum(1 for p in has_yes if mode_map.get(p) == "quarantine")
    fp_count    = sum(1 for p in has_no  if mode_map.get(p) in {"COL_AS_SEG","ROW_BASED"})
    tp_count    = sum(1 for p in has_yes if mode_map.get(p) in {"COL_AS_SEG","ROW_BASED"})

    recall      = _safe_rate(tp_count, len(has_yes))
    precision   = _safe_rate(tp_count, tp_count + fp_count)
    qrate       = _safe_rate(quarantine, total)

    return {
        "total_pdf_count":        total,
        "has_segment_yes_count":  len(has_yes),
        "has_segment_no_count":   len(has_no),
        "quarantine_count":       quarantine,
        "detected_success_count": detected,
        "true_positive_count":    tp_count,
        "false_negative_count":   fn_count,
        "false_positive_count":   fp_count,
        "detection_recall":       recall,
        "detection_precision":    precision,
        "quarantine_rate":        qrate,
    }

# 修正前（元ファイル）
before_rows = all_screen_rows.copy()
# before_rows は audit_updates 反映済みなので、元の値を再読み込み
before_rows_orig = []
with open(SCREEN_CSV, encoding="utf-8-sig", newline="") as f:
    before_rows_orig = list(csv.DictReader(f))

before_metrics = _compute_detection_metrics(before_rows_orig)
after_metrics  = _compute_detection_metrics(all_screen_rows)

# ── metrics_summary JSON ─────────────────────────────────────

metrics_combined = {
    "detection_before_gt_audit": before_metrics,
    "detection_after_gt_audit":  after_metrics,
    "gt_audit_updates":          len(audit_updates),
    "updated_pdfs":              list(audit_updates.keys()),
}

with open(OUT_METRICS, "w", encoding="utf-8") as f:
    json.dump(metrics_combined, f, ensure_ascii=False, indent=2)
print(f"出力: {OUT_METRICS}")

# ── metrics_by_pdf.csv ────────────────────────────────────────

BY_PDF_COLS = ["pdf","has_segment_table_before","has_segment_table_after","detected_mode","changed"]
by_pdf_rows = []
screen_before_map = {r["pdf"].strip(): r for r in before_rows_orig}

for r in all_screen_rows:
    pdf = r["pdf"].strip()
    before_val = screen_before_map.get(pdf, {}).get("has_segment_table","?")
    after_val  = r.get("has_segment_table","?")
    changed    = "yes" if before_val != after_val else "no"
    by_pdf_rows.append({
        "pdf": pdf,
        "has_segment_table_before": before_val,
        "has_segment_table_after":  after_val,
        "detected_mode": r.get("detected_mode",""),
        "changed": changed,
    })

with open(OUT_BY_PDF, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=BY_PDF_COLS)
    w.writeheader()
    w.writerows(by_pdf_rows)
print(f"出力: {OUT_BY_PDF}")

# ── failure_cases.csv ─────────────────────────────────────────

FAIL_COLS = ["pdf","has_segment_table","detected_mode","case_type"]
fail_rows = []
for r in all_screen_rows:
    pdf  = r["pdf"].strip()
    hst  = r.get("has_segment_table","").strip().lower()
    mode = r.get("detected_mode","").strip()
    if hst == "yes" and mode == "quarantine":
        fail_rows.append({"pdf":pdf,"has_segment_table":hst,"detected_mode":mode,"case_type":"FN"})
    elif hst == "no" and mode in ("COL_AS_SEG","ROW_BASED"):
        fail_rows.append({"pdf":pdf,"has_segment_table":hst,"detected_mode":mode,"case_type":"FP"})

with open(OUT_FAIL, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=FAIL_COLS)
    w.writeheader()
    w.writerows(fail_rows)
print(f"出力: {OUT_FAIL}")

# ── summary MD ───────────────────────────────────────────────

b = before_metrics
a = after_metrics

md = [
    "# GT 監査適用 + メトリクス再計算サマリ\n\n",
    f"更新件数: **{len(audit_updates)} 件**\n\n",
    "## 更新対象 PDF\n\n",
    "| pdf | 変更内容 |\n|---|---|\n",
]
for pdf, new_val in audit_updates.items():
    md.append(f"| {pdf} | has_segment_table: yes → **{new_val}** |\n")

md += [
    "\n---\n\n",
    "## 修正前後の指標比較\n\n",
    "| 指標 | 修正前 | 修正後 | 変化 |\n|---|---|---|---|\n",
]

def _diff(a_val, b_val):
    if a_val is None or b_val is None:
        return ""
    diff = a_val - b_val
    sign = "+" if diff > 0 else ""
    return f"{sign}{diff}" if isinstance(diff, int) else f"{sign}{diff:.4f}"

metrics_to_compare = [
    ("false_negative_count",   "FN（取りこぼし）件数"),
    ("false_positive_count",   "FP（誤検出）件数"),
    ("true_positive_count",    "TP（正検出）件数"),
    ("detection_recall",       "recall（再現率）"),
    ("detection_precision",    "precision（適合率）"),
    ("quarantine_count",       "quarantine 件数"),
    ("quarantine_rate",        "quarantine 率"),
    ("has_segment_yes_count",  "has_segment_yes 件数"),
]
for key, label in metrics_to_compare:
    bv = b.get(key, "-")
    av = a.get(key, "-")
    dv = _diff(av, bv) if isinstance(bv, (int, float)) and isinstance(av, (int, float)) else ""
    md.append(f"| {label} | {bv} | **{av}** | {dv} |\n")

OUT_SUMMARY_MD.write_text("".join(md), encoding="utf-8")
print(f"出力: {OUT_SUMMARY_MD}")

print(f"\n=== 修正前後サマリ ===")
print(f"  FN: {b['false_negative_count']} → {a['false_negative_count']}")
print(f"  FP: {b['false_positive_count']} → {a['false_positive_count']}")
print(f"  recall: {b['detection_recall']} → {a['detection_recall']}")
print(f"  precision: {b['detection_precision']} → {a['detection_precision']}")
