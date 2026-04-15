"""
apply_highconf_gt_fix.py
信頼度「高」4件の has_segment_table を no → yes に修正し metrics を再計算する。

対象PDF（no → yes）:
  140120260313581337.pdf  「売上高（百万円） セグメント利益又は損失」直接ヒット
  140120260313581228.pdf  セグメント表ヘッダー行あり
  140120260313581377.pdf  事業名が列として並んでいる
  140120260312580646.pdf  インターネット通販事業 売上高・営業利益の定量あり

出力（上書きなし）:
  data/eval/screening_sheet.highconf_fixed.csv
  data/eval/metrics_summary.highconf_fixed.json
  data/eval/highconf_fix_summary.md
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import csv, json
from pathlib import Path

PROJ     = Path(__file__).parent.parent.parent
EVAL_DIR = PROJ / "data" / "eval"

SCREEN_CSV  = EVAL_DIR / "screening_sheet.csv"
CANDS_CSV   = EVAL_DIR / "candidates.csv"
OUT_SCREEN  = EVAL_DIR / "screening_sheet.highconf_fixed.csv"
OUT_METRICS = EVAL_DIR / "metrics_summary.highconf_fixed.json"
OUT_SUMMARY = EVAL_DIR / "highconf_fix_summary.md"

# 修正対象
FIXES = {
    "140120260313581337.pdf": "yes",
    "140120260313581228.pdf": "yes",
    "140120260313581377.pdf": "yes",
    "140120260312580646.pdf": "yes",
}

# candidates.csv から最新 detected_mode を取得
cands_mode = {}
with open(CANDS_CSV, encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
        cands_mode[r["pdf"].strip()] = r.get("detected_mode", "").strip()

# screening_sheet 読み込み＋修正
before_rows, after_rows = [], []
screen_fieldnames = None
with open(SCREEN_CSV, encoding="utf-8-sig", newline="") as f:
    reader = csv.DictReader(f)
    screen_fieldnames = reader.fieldnames
    for row in reader:
        br = dict(row)
        # candidates から detected_mode を補完
        pdf = br["pdf"].strip()
        if pdf in cands_mode:
            br["detected_mode"] = cands_mode[pdf]
        before_rows.append(br)
        ar = dict(br)
        if pdf in FIXES:
            ar["has_segment_table"] = FIXES[pdf]
        after_rows.append(ar)

with open(OUT_SCREEN, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=screen_fieldnames)
    w.writeheader()
    w.writerows(after_rows)
print(f"出力: {OUT_SCREEN}")

# 指標計算
def _safe_rate(n, d): return round(n / d, 4) if d > 0 else None

def _compute(rows):
    has_yes = {r["pdf"].strip() for r in rows if r.get("has_segment_table","").strip().lower() == "yes"}
    has_no  = {r["pdf"].strip() for r in rows if r.get("has_segment_table","").strip().lower() == "no"}
    mode    = {r["pdf"].strip(): r.get("detected_mode","").strip() for r in rows}
    total   = len(rows)
    qtn     = sum(1 for r in rows if mode.get(r["pdf"].strip()) == "quarantine")
    tp      = sum(1 for p in has_yes if mode.get(p) in {"COL_AS_SEG","ROW_BASED"})
    fn      = sum(1 for p in has_yes if mode.get(p) == "quarantine")
    fp      = sum(1 for p in has_no  if mode.get(p) in {"COL_AS_SEG","ROW_BASED"})
    return {
        "total_pdf_count":        total,
        "has_segment_yes_count":  len(has_yes),
        "has_segment_no_count":   len(has_no),
        "quarantine_count":       qtn,
        "true_positive_count":    tp,
        "false_negative_count":   fn,
        "false_positive_count":   fp,
        "detection_recall":       _safe_rate(tp, len(has_yes)),
        "detection_precision":    _safe_rate(tp, tp + fp),
        "quarantine_rate":        _safe_rate(qtn, total),
    }

bm = _compute(before_rows)
am = _compute(after_rows)

with open(OUT_METRICS, "w", encoding="utf-8") as f:
    json.dump({"before": bm, "after": am, "fixes": len(FIXES)}, f, ensure_ascii=False, indent=2)
print(f"出力: {OUT_METRICS}")

# MD サマリ
def _d(av, bv):
    if isinstance(av,(int,float)) and isinstance(bv,(int,float)):
        d = av-bv
        return (f"+{d}" if d>0 else str(d)) if isinstance(d,int) else (f"+{d:.4f}" if d>0 else f"{d:.4f}")
    return ""

COMPARE = [
    ("has_segment_yes_count", "has_seg_yes 件数"),
    ("has_segment_no_count",  "has_seg_no 件数"),
    ("false_negative_count",  "**FN 件数**"),
    ("false_positive_count",  "**FP 件数**"),
    ("true_positive_count",   "TP 件数"),
    ("detection_recall",      "**recall（再現率）**"),
    ("detection_precision",   "**precision（適合率）**"),
    ("quarantine_count",      "quarantine 件数"),
    ("quarantine_rate",       "quarantine 率"),
]

md = [
    "# 信頼度「高」4件 GT 修正 + 指標再計算\n\n",
    f"修正件数: **{len(FIXES)} 件**（has_segment_table: no → yes）\n\n",
    "## 修正対象 PDF\n\n",
    "| pdf | 根拠 |\n|---|---|\n",
    "| 140120260313581337.pdf | 「売上高（百万円） セグメント利益又は損失」ヘッダー直接ヒット |\n",
    "| 140120260313581228.pdf | セグメント表ヘッダー行あり |\n",
    "| 140120260313581377.pdf | 事業名が列として並んでいる |\n",
    "| 140120260312580646.pdf | インターネット通販事業の売上高・営業利益の定量あり |\n",
    "\n---\n\n",
    "## 修正前後の指標比較\n\n",
    "| 指標 | 修正前 | 修正後 | 変化 |\n|---|---|---|---|\n",
]
for key, label in COMPARE:
    bv = bm.get(key,"-"); av = am.get(key,"-")
    md.append(f"| {label} | {bv} | **{av}** | {_d(av,bv)} |\n")

new_fn = am["false_negative_count"]
new_fp = am["false_positive_count"]
new_rec = am["detection_recall"]
new_pre = am["detection_precision"]
md += [
    "\n---\n\n",
    "## 結論\n\n",
    f"GT 修正後、**FP は {bm['false_positive_count']} → {new_fp} 件**に削減され、"
    f"**precision は {bm['detection_precision']} → {new_pre}** に改善した。"
    f"recall は {bm['detection_recall']} → {new_rec} となった（FN が増加した場合は GT 修正で yes になった件が quarantine のままのケース）。"
    f"FP 残 {new_fp} 件のうち次は「要確認7件」の手動 PDF 確認へ進む。\n",
]
OUT_SUMMARY.write_text("".join(md), encoding="utf-8")
print(f"出力: {OUT_SUMMARY}")

print(f"\n=== 修正前後サマリ ===")
print(f"  FN: {bm['false_negative_count']} → {am['false_negative_count']}")
print(f"  FP: {bm['false_positive_count']} → {am['false_positive_count']}")
print(f"  recall:    {bm['detection_recall']} → {am['detection_recall']}")
print(f"  precision: {bm['detection_precision']} → {am['detection_precision']}")
print(f"  has_seg_yes: {bm['has_segment_yes_count']} → {am['has_segment_yes_count']}")
