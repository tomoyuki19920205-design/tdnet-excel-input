"""
debug_single_true_fn_581606.py
真の FN 1件 140120260313581606.pdf を集中診断する。

出力:
  data/eval/debug_581606.md
  data/eval/debug_581606_candidates.csv
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import csv, re
from pathlib import Path
import pdfplumber

from src.analysis.segment_detection_v2 import run_segment_detection_v2
from src.analysis.page_scoring import score_segment_page
from src.analysis.table_scoring import score_segment_table, find_table_regions
from src.analysis.row_classifier import evaluate_candidate_guard

PROJ        = Path(__file__).parent.parent.parent
EVAL_DIR    = PROJ / "data" / "eval"
ARCHIVE_DIR = PROJ / "data" / "xbrl_archive"
PDF_NAME    = "140120260313581606.pdf"
PDF_PATH    = ARCHIVE_DIR / PDF_NAME
OUT_MD      = EVAL_DIR / "debug_581606.md"
OUT_CSV     = EVAL_DIR / "debug_581606_candidates.csv"

TOP_PAGES   = 8
MIN_SCORE   = 0.10

print(f"対象PDF: {PDF_NAME}")

# ── 1. segment_detection_v2 実行して内部値を取得 ──────────────

result = run_segment_detection_v2(str(PDF_PATH), doc_id=PDF_NAME, ticker="?")
cg     = result.score_summary.get("candidate_guard", {})

detected_mode = result.score_summary.get("detected_mode", "quarantine")
print(f"quarantine_reason: {result.quarantine_reason}")
print(f"detected_mode:     {detected_mode}")

# ── 2. Phase A: ページスコア計算 ─────────────────────────────

page_score_list = []
all_page_texts = []
with pdfplumber.open(str(PDF_PATH)) as pdf:
    for i, page in enumerate(pdf.pages[:15]):
        text = page.extract_text() or ""
        all_page_texts.append(text)
        ps = score_segment_page(text, i)
        page_score_list.append({"page": i, "score": round(ps.score, 4), "text": text})

page_score_sorted = sorted(page_score_list, key=lambda x: x["score"], reverse=True)
top_page_candidates = [p for p in page_score_sorted if p["score"] >= MIN_SCORE][:TOP_PAGES]
top_page_nos = {p["page"] for p in top_page_candidates}

print(f"\nPage Scores Top10:")
for p in page_score_sorted[:10]:
    mark = "[*]" if p["page"] in top_page_nos else "   "
    print(f"  {mark} page={p['page']}  score={p['score']}")

# ── 3. Phase B: 全ページの table candidates 全列挙 ────────────

all_candidates = []
for pg in page_score_list:
    if pg["page"] not in top_page_nos:
        continue
    lines = pg["text"].split("\n")
    regions = find_table_regions(lines)
    for rs, re_, nearby in regions:
        tlines = lines[rs:re_]
        ts = score_segment_table(tlines, nearby_text=nearby)
        if ts.score < 0.15:
            continue

        # candidate_guard を個別実行
        labels = []
        for ln in tlines:
            stripped = ln.strip()
            if not stripped:
                continue
            m = re.match(r'^([^\d△▲\-－]*)', stripped)
            label = m.group(1).strip() if m and m.group(1).strip() else stripped
            labels.append(label)

        cg_r = evaluate_candidate_guard(
            labels,
            candidate_lines=tlines,
            header_keyword_hits=int(ts.has_sales_header) + int(ts.has_profit_header),
            segment_name_like_rows=ts.segment_like_rows,
        )
        bs_cf_ratio = round(cg_r.bs_cf_like / cg_r.total_rows, 4) if cg_r.total_rows > 0 else 0.0

        all_candidates.append({
            "page":                 pg["page"],
            "raw_score":            round(ts.score, 4),
            "num_density":          round(cg_r.numeric_density, 4),
            "segment_like_rows":    ts.segment_like_rows,
            "segment_name_like_rows": cg_r.segment_name_like_rows,
            "valid_segment_like":   cg_r.valid_segment_like,
            "bs_cf_like":           cg_r.bs_cf_like,
            "bs_cf_ratio":          bs_cf_ratio,
            "header_keyword_hits":  cg_r.header_keyword_hits,
            "has_sales_header":     ts.has_sales_header,
            "has_profit_header":    ts.has_profit_header,
            "cg_accept":            cg_r.accepted,
            "cg_reject_reason":     cg_r.reject_reason,
            "lines":                tlines,
        })

# スコア降順でソート
all_candidates.sort(key=lambda x: x["raw_score"], reverse=True)
print(f"\nTable Candidates: {len(all_candidates)} 件")
for i, c in enumerate(all_candidates):
    print(f"  [{i}] page={c['page']} score={c['raw_score']} "
          f"seg={c['segment_like_rows']} b={c['bs_cf_like']} "
          f"ratio={c['bs_cf_ratio']} hdr={c['header_keyword_hits']} "
          f"accept={c['cg_accept']} rej={c['cg_reject_reason']}")

# best_table 相当（accept されたもの、なければスコア最高）
accepted = [c for c in all_candidates if c["cg_accept"]]
best = accepted[0] if accepted else (all_candidates[0] if all_candidates else None)

# ── 4. primary_failure 判定 ──────────────────────────────────

def _judge_failure(candidates, kv_result):
    if not candidates:
        return "candidate_filtered_out", "表候補が1件もない"
    top = candidates[0]
    # cg_result の最終 reject
    qrn = kv_result.quarantine_reason or ""
    if "bs_cf_guard" in qrn:
        if top["bs_cf_ratio"] >= 0.15:
            return "bs_cf_ratio_too_high", f"ratio={top['bs_cf_ratio']}"
        if top["valid_segment_like"] < 2 and top["header_keyword_hits"] == 0:
            return "header_keyword_miss", f"hdr={top['header_keyword_hits']} valid={top['valid_segment_like']}"
        return "valid_segment_like_too_low", f"valid={top['valid_segment_like']}"
    if "narrative_guard" in qrn:
        return "other", "narrative_guard が発火"
    if not candidates:
        return "page_not_selected", "ページ候補なし"
    return "other", qrn

pf, pf_detail = _judge_failure(all_candidates, result)
print(f"\nprimary_failure: {pf}  ({pf_detail})")

# ── 5. 修正案 ──────────────────────────────────────────────────

fix_candidates = []
if pf == "bs_cf_ratio_too_high":
    fix_candidates = [
        {
            "loc": "row_classifier.py evaluate_candidate_guard",
            "desc": "bs_cf_ratio_threshold を 0.15 → 0.25 に条件付き緩和（header_keyword_hits >= 1 のとき）",
            "effect": "ratio が 0.15〜0.25 の軽度汚染表を救済",
            "fp_risk": "BS/CF 表が ratio 0.15〜0.25 の場合に FP 増加リスク",
            "recommend": "★★★",
        },
        {
            "loc": "row_classifier.py _SALES_KW / _PROFIT_KW",
            "desc": "内部補完 KW に「売上」「利益」等の短縮語を追加して hdr を 1 以上にする",
            "effect": "hdr >= 1 になり b_limit=5 かつ ratio 閾値が緩和対象に",
            "fp_risk": "短縮語マッチによる過剰 hdr 検出 → FP 増加",
            "recommend": "★★☆",
        },
        {
            "loc": "row_classifier.py _bscf_light_exempt",
            "desc": "segment_name_like_rows >= 2 かつ bs_cf_ratio < 0.30 を追加免除条件にする",
            "effect": "snr が十分 (2件以上) あれば ratio が 0.25〜0.30 でも救済",
            "fp_risk": "snr が緩い場合に BS/CF 表が通過するリスク",
            "recommend": "★★☆",
        },
    ]
elif pf == "header_keyword_miss":
    fix_candidates = [
        {
            "loc": "row_classifier.py _SALES_KW",
            "desc": "KW リストに「売上」「収益」等のより短い語を追加",
            "effect": "hdr >= 1 になり b_limit=5 が有効化される",
            "fp_risk": "短縮語による誤マッチ → FP 増加",
            "recommend": "★★★",
        },
    ]
else:
    fix_candidates = [
        {
            "loc": "個別調査が必要",
            "desc": f"primary_failure={pf}。rule_trace を確認する",
            "effect": "不明",
            "fp_risk": "不明",
            "recommend": "★☆☆",
        },
    ]

# ── CSV 出力 ─────────────────────────────────────────────────

CSV_COLS = [
    "candidate_index","page","raw_score","num_density",
    "segment_like_rows","segment_name_like_rows",
    "valid_segment_like","bs_cf_like","bs_cf_ratio",
    "header_keyword_hits","has_sales_header","has_profit_header",
    "cg_accept","cg_reject_reason","is_best_table",
]
with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=CSV_COLS)
    w.writeheader()
    for i, c in enumerate(all_candidates):
        w.writerow({
            "candidate_index":        i,
            "page":                   c["page"],
            "raw_score":              c["raw_score"],
            "num_density":            c["num_density"],
            "segment_like_rows":      c["segment_like_rows"],
            "segment_name_like_rows": c["segment_name_like_rows"],
            "valid_segment_like":     c["valid_segment_like"],
            "bs_cf_like":             c["bs_cf_like"],
            "bs_cf_ratio":            c["bs_cf_ratio"],
            "header_keyword_hits":    c["header_keyword_hits"],
            "has_sales_header":       c["has_sales_header"],
            "has_profit_header":      c["has_profit_header"],
            "cg_accept":              c["cg_accept"],
            "cg_reject_reason":       c["cg_reject_reason"],
            "is_best_table":          (best is not None and c is best),
        })

# ── MD 出力 ──────────────────────────────────────────────────

md = [
    f"# 真FN診断: {PDF_NAME}\n\n",
    "## 1. 基本情報\n\n",
    f"- pdf: `{PDF_NAME}`\n",
    f"- detected_mode: `{detected_mode}`\n",
    f"- quarantine_reason: `{result.quarantine_reason}`\n",
    f"- **primary_failure**: `{pf}` ({pf_detail})\n\n",
    "---\n\n",
    "## 2. Phase A: Page Scores Top10\n\n",
    "| page | score | in_top_pages |\n|---|---|---|\n",
]
for p in page_score_sorted[:10]:
    mark = "✓" if p["page"] in top_page_nos else ""
    md.append(f"| {p['page']} | {p['score']} | {mark} |\n")

md += [
    "\n---\n\n",
    "## 3. Phase B: Table Candidates\n\n",
    "| idx | page | score | seg | snr | valid | b | ratio | hdr | accept | rej |\n",
    "|---|---|---|---|---|---|---|---|---|---|---|\n",
]
for i, c in enumerate(all_candidates):
    best_mark = " ★" if (best is not None and c is best) else ""
    md.append(
        f"| {i}{best_mark} | {c['page']} | {c['raw_score']} "
        f"| {c['segment_like_rows']} | {c['segment_name_like_rows']} "
        f"| {c['valid_segment_like']} | {c['bs_cf_like']} | {c['bs_cf_ratio']} "
        f"| {c['header_keyword_hits']} | {c['cg_accept']} | {c['cg_reject_reason'] or '-'} |\n"
    )

# best_table の中身抜粋
if best:
    tlines = best["lines"]
    md += ["\n---\n\n", "## 4. Best Table 実テキスト抜粋（先頭20行）\n\n```\n"]
    for ln in tlines[:20]:
        md.append(ln + "\n")
    md.append("```\n")

md += [
    "\n---\n\n",
    "## 5. 最小修正案\n\n",
    "| 優先 | 場所 | 内容 | FPリスク | 推奨度 |\n|---|---|---|---|---|\n",
]
for i, fc in enumerate(fix_candidates, 1):
    md.append(
        f"| {i} | {fc['loc']} | {fc['desc']} | {fc['fp_risk']} | {fc['recommend']} |\n"
    )

OUT_MD.write_text("".join(md), encoding="utf-8")
print(f"\n完了: {OUT_CSV}")
print(f"完了: {OUT_MD}")
