"""
Step 4: 指標集計スクリプト

入力:
  data/eval/screening_sheet.csv   (検出評価 89件)
  data/eval/ground_truth.csv      (詳細評価 GT)
  data/eval/extracted.csv         (step3 の出力)

出力:
  data/eval/metrics_summary.json
  data/eval/metrics_by_pdf.csv
  data/eval/failure_cases.csv
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import csv, json, re
from pathlib import Path
from collections import defaultdict, Counter

EVAL_DIR      = Path(__file__).parent.parent.parent / "data" / "eval"
TARGETS_CSV   = EVAL_DIR / "detailed_eval_targets.csv"
GT_CSV        = EVAL_DIR / "ground_truth.csv"
EXTRACTED_CSV = EVAL_DIR / "extracted.csv"
SCREEN_CSV    = EVAL_DIR / "screening_sheet.csv"
METRICS_JSON  = EVAL_DIR / "metrics_summary.json"
BY_PDF_CSV    = EVAL_DIR / "metrics_by_pdf.csv"
FAIL_CSV      = EVAL_DIR / "failure_cases.csv"

# ── 数値一致条件 ──────────────────────────────────────────────────
ABS_TOL = 50      # ±50百万円
REL_TOL = 0.05    # ±5%

# 集計除外する正規化済みセグメント名
_EXCLUDE_NAMES = {"", "報告", "合計", "計", "その他"}

# ── ユーティリティ ────────────────────────────────────────────────

def normalize_segment_name(s: str) -> str:
    """セグメント名を比較用に正規化する。"""
    if not s:
        return ""
    s = str(s).strip()
    # 全角英数字・スペースを半角に
    s = s.translate(str.maketrans(
        "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
        "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
        "０１２３４５６７８９　",
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789 "
    ))
    # 括弧注記除去
    s = re.sub(r'[（(][^）)]*[）)]', '', s)
    # 語尾の「事業」「部門」「セグメント」を除去（比較用）
    s = re.sub(r'(事業|部門|セグメント)+$', '', s)
    # 「セグメント」を途中から除去
    s = re.sub(r'セグメント', '', s)
    # 連続空白圧縮・前後除去
    s = re.sub(r'\s+', ' ', s).strip()
    return s.lower()


def to_float(v) -> float | None:
    """空欄/"None" → None、それ以外 → float。"""
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() == "none":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def num_match(ex: float | None, gt: float | None) -> bool:
    """数値一致判定。"""
    if gt is None and ex is None:
        return True
    if gt is None or ex is None:
        return False
    return abs(ex - gt) <= max(ABS_TOL, abs(gt) * REL_TOL)


def _safe_rate(num: int, denom: int):
    return round(num / denom, 4) if denom > 0 else None


# ── 入力ファイル読み込み ──────────────────────────────────────────

# detailed_eval_targets.csv から PDF 順序を確定
target_order = []
target_ticker = {}
if TARGETS_CSV.exists():
    with open(TARGETS_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            pdf = row["pdf"].strip()
            target_order.append(pdf)
            target_ticker[pdf] = row.get("ticker", "?").strip()

# ground_truth.csv
gt_by_pdf: dict[str, list] = defaultdict(list)
gt_ticker_by_pdf: dict[str, str] = {}
if GT_CSV.exists():
    with open(GT_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            pdf = row["pdf"].strip()
            gt_ticker_by_pdf[pdf] = row.get("ticker", "?").strip()
            name_raw = row.get("segment_name", "").strip()
            name_norm = normalize_segment_name(name_raw)
            if name_norm in _EXCLUDE_NAMES:
                continue
            gt_by_pdf[pdf].append({
                "segment_name":      name_raw,
                "segment_name_norm": name_norm,
                "sales":             to_float(row.get("sales")),
                "profit":            to_float(row.get("profit")),
                "table_type":        row.get("table_type", "").strip(),
            })

# extracted.csv
ex_by_pdf: dict[str, list] = defaultdict(list)
ex_mode_by_pdf:  dict[str, str] = {}
ex_qrn_by_pdf:   dict[str, str] = {}
if EXTRACTED_CSV.exists():
    with open(EXTRACTED_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            pdf  = row["pdf"].strip()
            mode = row.get("detected_mode", "").strip()
            ex_mode_by_pdf[pdf] = mode
            qrn = row.get("quarantine_reason", "").strip()
            if qrn:
                ex_qrn_by_pdf[pdf] = qrn
            name_raw  = row.get("segment_name_raw",  row.get("segment_name_norm", "")).strip()
            name_norm = normalize_segment_name(row.get("segment_name_norm", row.get("segment_name_raw", "")))
            if name_norm in _EXCLUDE_NAMES:
                continue
            if mode != "quarantine" and name_norm:
                ex_by_pdf[pdf].append({
                    "segment_name":      name_raw,
                    "segment_name_norm": name_norm,
                    "sales":             to_float(row.get("sales")),
                    "profit":            to_float(row.get("profit")),
                })
else:
    print(f"WARNING: {EXTRACTED_CSV} が見つかりません。extracted列は空になります。")

# screening_sheet.csv
screen_rows: dict[str, dict] = {}
has_seg_yes   = set()
has_seg_no    = set()
has_seg_unkn  = set()
all_screen_pdfs = []
if SCREEN_CSV.exists():
    with open(SCREEN_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            pdf = row["pdf"].strip()
            all_screen_pdfs.append(pdf)
            screen_rows[pdf] = row
            val = row.get("has_segment_table", "").strip().lower()
            if val == "yes":
                has_seg_yes.add(pdf)
            elif val == "no":
                has_seg_no.add(pdf)
            else:
                has_seg_unkn.add(pdf)

# ================================================================
# ■ 1. 検出評価（screening_sheet.csv ベース 全89件）
# ================================================================
total_pdf_count         = len(all_screen_pdfs)
has_seg_yes_count       = len(has_seg_yes)
has_seg_no_count        = len(has_seg_no)
has_seg_unkn_count      = len(has_seg_unkn)

quarantine_count        = sum(
    1 for pdf in all_screen_pdfs
    if screen_rows[pdf].get("detected_mode", "").strip() == "quarantine"
)
detected_success_count  = sum(
    1 for pdf in all_screen_pdfs
    if screen_rows[pdf].get("detected_mode", "").strip() in {"COL_AS_SEG", "ROW_BASED"}
)

# FN: has_segment_table=yes かつ quarantine
false_negative_count = sum(
    1 for pdf in has_seg_yes
    if screen_rows.get(pdf, {}).get("detected_mode", "").strip() == "quarantine"
)
# FP: has_segment_table=no かつ 検出成功
false_positive_count = sum(
    1 for pdf in has_seg_no
    if screen_rows.get(pdf, {}).get("detected_mode", "").strip() in {"COL_AS_SEG", "ROW_BASED"}
)

detected_success_on_yes = sum(
    1 for pdf in has_seg_yes
    if screen_rows.get(pdf, {}).get("detected_mode", "").strip() in {"COL_AS_SEG", "ROW_BASED"}
)
detection_recall = _safe_rate(detected_success_on_yes, has_seg_yes_count)
quarantine_rate  = _safe_rate(quarantine_count, total_pdf_count)

# ================================================================
# ■ 2. 詳細評価（ground_truth.csv vs extracted.csv）
# ================================================================
BY_PDF_COLS = [
    "pdf", "ticker", "gt_table_type", "detected_mode",
    "gt_segment_count", "ex_segment_count",
    "missing_segment_count", "extra_segment_count",
    "exact_segment_set_match",
    "sales_match_rate", "profit_match_rate", "both_match_rate",
    "pdf_name_success", "pdf_value_success", "pdf_strict_success",
]

FAIL_COLS = [
    "pdf", "ticker",
    "primary_failure", "secondary_failures",
    "gt_table_type", "detected_mode",
    "missing_segments", "extra_segments",
    "notes",
]

# PDF 順を target_order → 残りはソート
all_detail_pdfs_set = set(gt_by_pdf) | set(ex_by_pdf)
ordered_pdfs = [p for p in target_order if p in all_detail_pdfs_set]
remaining    = sorted(all_detail_pdfs_set - set(ordered_pdfs))
all_detail_pdfs = ordered_pdfs + remaining

pdf_results  = []
fail_rows    = []

for pdf in all_detail_pdfs:
    gt_segs  = gt_by_pdf.get(pdf, [])
    ex_segs  = ex_by_pdf.get(pdf, [])
    mode     = ex_mode_by_pdf.get(pdf, "quarantine")
    gt_type  = gt_segs[0]["table_type"] if gt_segs else ""
    ticker   = target_ticker.get(pdf) or gt_ticker_by_pdf.get(pdf, "?")

    # 重複名は最初の1件採用
    gt_seen: dict[str, dict] = {}
    for g in gt_segs:
        if g["segment_name_norm"] not in gt_seen:
            gt_seen[g["segment_name_norm"]] = g
    ex_seen: dict[str, dict] = {}
    for e in ex_segs:
        if e["segment_name_norm"] not in ex_seen:
            ex_seen[e["segment_name_norm"]] = e

    gt_names = set(gt_seen)
    ex_names = set(ex_seen)

    missing = gt_names - ex_names
    extra   = ex_names - gt_names
    exact_match = (len(missing) == 0 and len(extra) == 0)

    # 共通セグメントの値一致
    common = gt_names & ex_names
    sales_ok = profit_ok = both_ok = 0
    for nm in common:
        s_match = num_match(ex_seen[nm]["sales"],  gt_seen[nm]["sales"])
        p_match = num_match(ex_seen[nm]["profit"], gt_seen[nm]["profit"])
        if s_match:
            sales_ok  += 1
        if p_match:
            profit_ok += 1
        if s_match and p_match:
            both_ok += 1

    n_common = len(common)
    sales_match_rate  = _safe_rate(sales_ok,  n_common)
    profit_match_rate = _safe_rate(profit_ok, n_common)
    both_match_rate   = _safe_rate(both_ok,   n_common)

    # GT 全件の値一致（GT→EX 方向で missing を False 扱い）
    all_sales_match  = True
    all_profit_match = True
    for nm, g in gt_seen.items():
        e = ex_seen.get(nm)
        if e is None:
            all_sales_match  = False
            all_profit_match = False
        else:
            if not num_match(e["sales"],  g["sales"]):
                all_sales_match  = False
            if not num_match(e["profit"], g["profit"]):
                all_profit_match = False

    pdf_name_success  = exact_match
    pdf_value_success = (n_common > 0 and all_sales_match and all_profit_match and len(missing) == 0)
    pdf_strict_success = pdf_name_success and pdf_value_success and len(extra) == 0

    pdf_results.append({
        "pdf":                    pdf,
        "ticker":                 ticker,
        "gt_table_type":          gt_type,
        "detected_mode":          mode,
        "gt_segment_count":       len(gt_seen),
        "ex_segment_count":       len(ex_seen),
        "missing_segment_count":  len(missing),
        "extra_segment_count":    len(extra),
        "exact_segment_set_match": int(exact_match),
        "sales_match_rate":       sales_match_rate if sales_match_rate is not None else "",
        "profit_match_rate":      profit_match_rate if profit_match_rate is not None else "",
        "both_match_rate":        both_match_rate if both_match_rate is not None else "",
        "pdf_name_success":       int(pdf_name_success),
        "pdf_value_success":      int(pdf_value_success),
        "pdf_strict_success":     int(pdf_strict_success),
    })

    # ── failure taxonomy ──────────────────────────────────────────
    if not pdf_strict_success:
        failures = []
        # detected_mode と gt_table_type のモード不一致
        mode_expect = gt_type.upper().replace("-", "_") if gt_type else ""
        if mode_expect and mode != "quarantine" and mode_expect not in mode:
            failures.append("mode_mismatch")
        if mode == "quarantine":
            failures.append("quarantine")
        if not ex_segs:
            failures.append("no_extracted_rows")
        # total_column / suspicious_label
        suspicious = [nm for nm in extra if nm in {"報告", "合計", "計", "その他"}]
        if suspicious:
            failures.append("total_column_included")
        remaining_extra = [nm for nm in extra if nm not in {"報告", "合計", "計", "その他"}]
        if remaining_extra:
            failures.append("segment_extra")
        if missing:
            failures.append("segment_missing")
        if not all_sales_match and n_common > 0:
            failures.append("sales_mismatch")
        if not all_profit_match and n_common > 0:
            failures.append("profit_mismatch")

        primary   = failures[0] if failures else "unknown"
        secondary = "|".join(failures[1:]) if len(failures) > 1 else ""

        fail_rows.append({
            "pdf":               pdf,
            "ticker":            ticker,
            "primary_failure":   primary,
            "secondary_failures": secondary,
            "gt_table_type":     gt_type,
            "detected_mode":     mode,
            "missing_segments":  "|".join(sorted(missing)[:5]),
            "extra_segments":    "|".join(sorted(extra)[:5]),
            "notes":             ex_qrn_by_pdf.get(pdf, ""),
        })

# ── セグメント名 precision / recall (全詳細対象) ─────────────────
tp_n = fp_n = fn_n = 0
for r in pdf_results:
    pdf    = r["pdf"]
    gt_set = set(g["segment_name_norm"] for g in gt_by_pdf.get(pdf, []))
    ex_set = set(e["segment_name_norm"] for e in ex_by_pdf.get(pdf, []))
    # exclude
    gt_set -= _EXCLUDE_NAMES
    ex_set -= _EXCLUDE_NAMES
    tp_n += len(gt_set & ex_set)
    fp_n += len(ex_set - gt_set)
    fn_n += len(gt_set - ex_set)

seg_precision = _safe_rate(tp_n, tp_n + fp_n)
seg_recall    = _safe_rate(tp_n, tp_n + fn_n)

# ── セグメントレベル sales/profit 一致率 ─────────────────────────
s_ok = s_tot = p_ok = p_tot = b_ok = 0
for pdf in all_detail_pdfs:
    gt_segs = gt_by_pdf.get(pdf, [])
    ex_segs = ex_by_pdf.get(pdf, [])
    ex_map  = {e["segment_name_norm"]: e for e in ex_segs}
    for g in gt_segs:
        e = ex_map.get(g["segment_name_norm"])
        if g["sales"] is not None:
            s_tot += 1
            sm = e is not None and num_match(e["sales"], g["sales"])
            if sm:
                s_ok += 1
        if g["profit"] is not None:
            p_tot += 1
            pm = e is not None and num_match(e["profit"], g["profit"])
            if pm:
                p_ok += 1
        if g["sales"] is not None and g["profit"] is not None:
            sm = e is not None and num_match(e["sales"],  g["sales"])
            pm = e is not None and num_match(e["profit"], g["profit"])
            if sm and pm:
                b_ok += 1

seg_sales_rate  = _safe_rate(s_ok, s_tot)
seg_profit_rate = _safe_rate(p_ok, p_tot)
seg_both_rate   = _safe_rate(b_ok, s_tot) if s_tot > 0 else None  # 分母は sales_total

n_detail = len(pdf_results)
name_ok_count   = sum(r["pdf_name_success"]   for r in pdf_results)
value_ok_count  = sum(r["pdf_value_success"]  for r in pdf_results)
strict_ok_count = sum(r["pdf_strict_success"] for r in pdf_results)

# ── mode / gt_table_type 別集計 ───────────────────────────────────
def _by_group(key: str) -> dict:
    groups: dict[str, list] = defaultdict(list)
    for r in pdf_results:
        groups[r[key]].append(r)
    result = {}
    for grp, items in sorted(groups.items()):
        n = len(items)
        result[grp] = {
            "count":                  n,
            "pdf_name_success_rate":   _safe_rate(sum(r["pdf_name_success"]   for r in items), n),
            "pdf_value_success_rate":  _safe_rate(sum(r["pdf_value_success"]  for r in items), n),
            "pdf_strict_success_rate": _safe_rate(sum(r["pdf_strict_success"] for r in items), n),
        }
    return result

# ── failure top ───────────────────────────────────────────────────
fail_counter: Counter = Counter()
for row in fail_rows:
    fail_counter[row["primary_failure"]] += 1
    for sf in row["secondary_failures"].split("|"):
        sf = sf.strip()
        if sf:
            fail_counter[sf] += 1

# ================================================================
# ■ 3. metrics_summary.json
# ================================================================
summary = {
    "screening": {
        "total_pdf_count":              total_pdf_count,
        "has_segment_table_yes_count":  has_seg_yes_count,
        "has_segment_table_no_count":   has_seg_no_count,
        "has_segment_table_unknown_count": has_seg_unkn_count,
        "quarantine_count":             quarantine_count,
        "detected_success_count":       detected_success_count,
        "false_negative_count":         false_negative_count,
        "false_positive_count":         false_positive_count,
        "detection_recall":             detection_recall,
        "quarantine_rate":              quarantine_rate,
    },
    "detailed": {
        "detailed_pdf_count":           n_detail,
        "pdf_name_success_count":       name_ok_count,
        "pdf_name_success_rate":        _safe_rate(name_ok_count,   n_detail),
        "pdf_value_success_count":      value_ok_count,
        "pdf_value_success_rate":       _safe_rate(value_ok_count,  n_detail),
        "pdf_strict_success_count":     strict_ok_count,
        "pdf_strict_success_rate":      _safe_rate(strict_ok_count, n_detail),
        "segment_precision":            seg_precision,
        "segment_recall":               seg_recall,
        "segment_tp":                   tp_n,
        "segment_fp":                   fp_n,
        "segment_fn":                   fn_n,
        "sales_match_rate":             seg_sales_rate,
        "profit_match_rate":            seg_profit_rate,
        "both_match_rate":              seg_both_rate,
    },
    "by_gt_table_type":  _by_group("gt_table_type"),
    "by_detected_mode":  _by_group("detected_mode"),
    "failure_top":       dict(fail_counter.most_common(10)),
}

with open(METRICS_JSON, "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)

with open(BY_PDF_CSV, "w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=BY_PDF_COLS)
    writer.writeheader()
    writer.writerows(pdf_results)

with open(FAIL_CSV, "w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=FAIL_COLS)
    writer.writeheader()
    writer.writerows(fail_rows)

print(f"完了: {METRICS_JSON}")
print(f"完了: {BY_PDF_CSV}  ({len(pdf_results)} 行)")
print(f"完了: {FAIL_CSV}  ({len(fail_rows)} 行)")
print(f"\n--- 検出評価 ({total_pdf_count} 件) ---")
print(f"  has_segment_table yes={has_seg_yes_count} no={has_seg_no_count} unknown={has_seg_unkn_count}")
print(f"  detected_success={detected_success_count}  quarantine={quarantine_count}  quarantine_rate={quarantine_rate}")
print(f"  false_negative={false_negative_count}  false_positive={false_positive_count}")
print(f"  detection_recall={detection_recall}")
print(f"\n--- 詳細評価 ({n_detail} 件) ---")
print(f"  pdf_name_success:   {name_ok_count}/{n_detail}  ({_safe_rate(name_ok_count,   n_detail) or 0:.1%})")
print(f"  pdf_value_success:  {value_ok_count}/{n_detail}  ({_safe_rate(value_ok_count,  n_detail) or 0:.1%})")
print(f"  pdf_strict_success: {strict_ok_count}/{n_detail}  ({_safe_rate(strict_ok_count, n_detail) or 0:.1%})")
print(f"  segment precision={seg_precision}  recall={seg_recall}")
print(f"  sales_match_rate={seg_sales_rate}  profit_match_rate={seg_profit_rate}")
print(f"\n--- failure top ---")
for reason, cnt in fail_counter.most_common(5):
    print(f"  {reason}: {cnt}")
