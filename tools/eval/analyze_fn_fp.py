"""
analyze_fn_fp.py
screening_sheet.csv を使って FN / FP を分解し原因を特定する。

FN = has_segment_table == "yes" and detected_mode == "quarantine"
FP = has_segment_table == "no"  and detected_mode in {COL_AS_SEG, ROW_BASED}

出力:
  data/eval/fn_cases.csv
  data/eval/fp_cases.csv
  data/eval/fn_fp_summary.md
"""
import csv, re
from pathlib import Path
from collections import Counter

EVAL_DIR   = Path(__file__).parent.parent.parent / "data" / "eval"
SCREEN_CSV = EVAL_DIR / "screening_sheet.csv"
FN_CSV     = EVAL_DIR / "fn_cases.csv"
FP_CSV     = EVAL_DIR / "fp_cases.csv"
SUMMARY_MD = EVAL_DIR / "fn_fp_summary.md"

SUCCESS_MODES = {"COL_AS_SEG", "ROW_BASED"}

FN_COLS = [
    "pdf", "ticker", "detected_mode", "quarantine_reason",
    "segment_count", "page_number", "unit_text",
    "segment_names_preview", "expected_table_type", "fn_reason_bucket",
]
FP_COLS = [
    "pdf", "ticker", "detected_mode", "quarantine_reason",
    "segment_count", "page_number", "unit_text",
    "segment_names_preview", "expected_table_type", "fp_reason_bucket",
]

# ── 原因バケット ────────────────────────────────────────────────

def fn_bucket(qrn: str) -> str:
    if "bs_cf_guard"                in qrn:  return "bs_cf_guard"
    if "narrative_guard"            in qrn:  return "narrative_guard"
    if "no_segment_table_candidate" in qrn:  return "no_candidate"
    if "no_valid_segment_rows"      in qrn:  return "invalid_rows"
    return "other_quarantine"

_BS_CF_WORDS = {"現金", "預金", "包括利益", "純資産", "その他の包括利益",
                "売掛金", "当期首残高", "当期末残高"}
_TOTAL_WORDS = {"報告", "合計", "計", "その他"}

def fp_bucket(row: dict) -> str:
    preview = row.get("segment_names_preview", "")
    seg_cnt = int(row.get("segment_count", 0) or 0)

    # BS/CF系ワード
    if any(w in preview for w in _BS_CF_WORDS):
        return "bs_cf_like"
    # 合計/報告系
    if any(w in preview for w in _TOTAL_WORDS):
        return "total_or_report"
    # 長文っぽい（10文字超の節が2個以上、または句読点あり）
    segments_in_preview = [s.strip() for s in re.split(r"[/／|]", preview) if s.strip()]
    long_tokens = sum(1 for s in segments_in_preview if len(s) > 10)
    if "、" in preview or "。" in preview or long_tokens >= 2:
        return "narrative_like"
    # 件数暴走
    if seg_cnt >= 10:
        return "too_many_segments"
    return "other_fp"

# ── 読み込み ──────────────────────────────────────────────────────

all_rows = []
with open(SCREEN_CSV, encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        all_rows.append(row)

total      = len(all_rows)
yes_count  = sum(1 for r in all_rows if r.get("has_segment_table","").strip().lower() == "yes")
no_count   = sum(1 for r in all_rows if r.get("has_segment_table","").strip().lower() == "no")
unkn_count = total - yes_count - no_count

fn_rows = []
fp_rows = []

for row in all_rows:
    has_seg = row.get("has_segment_table","").strip().lower()
    mode    = row.get("detected_mode","").strip()
    qrn     = row.get("quarantine_reason","").strip()

    if has_seg == "yes" and mode == "quarantine":
        fn_rows.append({**{c: row.get(c,"") for c in FN_COLS[:-1]},
                        "fn_reason_bucket": fn_bucket(qrn)})

    elif has_seg == "no" and mode in SUCCESS_MODES:
        fp_rows.append({**{c: row.get(c,"") for c in FP_COLS[:-1]},
                        "fp_reason_bucket": fp_bucket(row)})

fn_counter = Counter(r["fn_reason_bucket"] for r in fn_rows)
fp_counter = Counter(r["fp_reason_bucket"] for r in fp_rows)

# ── CSV 出力 ─────────────────────────────────────────────────────

with open(FN_CSV, "w", encoding="utf-8-sig", newline="") as f:
    csv.DictWriter(f, fieldnames=FN_COLS).writeheader()
    csv.DictWriter(f, fieldnames=FN_COLS).writerows(fn_rows)

with open(FP_CSV, "w", encoding="utf-8-sig", newline="") as f:
    csv.DictWriter(f, fieldnames=FP_COLS).writeheader()
    csv.DictWriter(f, fieldnames=FP_COLS).writerows(fp_rows)

# ── 改善優先順位 ─────────────────────────────────────────────────

def _priorities(fn_cnt: Counter, fp_cnt: Counter) -> list[str]:
    prios = []
    # FN が多い guard を最優先
    top_fn = fn_cnt.most_common(1)
    if top_fn:
        bucket = top_fn[0][0]
        label_map = {
            "bs_cf_guard":      "bs_cf_guard の緩和条件見直し（セグメント表と BS/CF を区別できる追加スコアを導入）",
            "narrative_guard":  "narrative_guard のしきい値緩和（テキスト密度スコアを段階的に調整）",
            "no_candidate":     "セグメント表候補なし → Phase-B ブースト条件の拡充",
            "invalid_rows":     "no_valid_segment_rows → 行バリデーション基準の緩和または補完抽出の追加",
            "other_quarantine": "その他 quarantine 原因の詳細調査",
        }
        prios.append(label_map.get(bucket, bucket))
    # FP が多いパターン
    top_fp = fp_cnt.most_common(1)
    if top_fp:
        bucket = top_fp[0][0]
        label_map2 = {
            "bs_cf_like":      "BS/CF/包括利益ワードの誤検出遮断（ガード句の拡充）",
            "total_or_report": "「報告」「合計」列のみセグメントとして誤採用 → 除外フィルタ追加",
            "narrative_like":  "本文テキストのテーブル誤認識 → narrative_guard を強化",
            "too_many_segments":"ROW_BASED の多件数暴走抑制（最大セグメント数上限の設定）",
            "other_fp":        "その他 FP の詳細調査",
        }
        prios.append(label_map2.get(bucket, bucket))
    # segment_count 暴走系
    if fp_cnt.get("too_many_segments", 0) > 0 and "too_many_segments" not in top_fp[0][0]:
        prios.append("ROW_BASED の多件数暴走抑制（segment_count >= 10 を警告 or 除外）")
    elif len(prios) < 3:
        prios.append("詳細評価 GT を89件全体に拡充して precision/recall を再計測")
    return prios[:3]

priorities = _priorities(fn_counter, fp_counter)

# ── Markdown レポート ────────────────────────────────────────────

def _table(counter: Counter, label: str) -> str:
    lines = [f"| {label} | 件数 |", "|---|---|"]
    for k, v in counter.most_common():
        lines.append(f"| {k} | {v} |")
    return "\n".join(lines)

def _case_table(rows: list, id_col: str, bucket_col: str, extra_col: str) -> str:
    lines = [f"| pdf | {bucket_col} | {extra_col} |", "|---|---|---|"]
    for r in rows[:10]:
        lines.append(f"| {r['pdf']} | {r[bucket_col]} | {r.get(extra_col, '')} |")
    return "\n".join(lines)

md = f"""# FN / FP 分析レポート

## 全体サマリー

| 項目 | 件数 |
|---|---|
| 総 PDF 数 | {total} |
| has_segment_table = yes | {yes_count} |
| has_segment_table = no  | {no_count} |
| has_segment_table = unknown | {unkn_count} |
| **FN 件数** | **{len(fn_rows)}** |
| **FP 件数** | **{len(fp_rows)}** |

---

## FN バケット別件数

{_table(fn_counter, "fn_reason_bucket")}

## FP バケット別件数

{_table(fp_counter, "fp_reason_bucket")}

---

## FN 上位10件

{_case_table(fn_rows, "pdf", "fn_reason_bucket", "quarantine_reason")}

## FP 上位10件

{_case_table(fp_rows, "pdf", "fp_reason_bucket", "segment_names_preview")}

---

## 改善優先順位

1. {priorities[0] if len(priorities) > 0 else "(なし)"}
2. {priorities[1] if len(priorities) > 1 else "(なし)"}
3. {priorities[2] if len(priorities) > 2 else "(なし)"}
"""

SUMMARY_MD.write_text(md, encoding="utf-8")

# ── コンソール出力 ────────────────────────────────────────────────

print(f"完了: {FN_CSV}  ({len(fn_rows)} 件)")
print(f"完了: {FP_CSV}  ({len(fp_rows)} 件)")
print(f"完了: {SUMMARY_MD}")
print(f"\nFN バケット: {dict(fn_counter.most_common())}")
print(f"FP バケット: {dict(fp_counter.most_common())}")
print(f"\n改善優先順位:")
for i, p in enumerate(priorities, 1):
    print(f"  {i}. {p}")
