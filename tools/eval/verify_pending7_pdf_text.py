"""
verify_pending7_pdf_text.py
「要確認」7件の PDF 本文を直接確認し、セグメント表の有無を判定する。

対象:
  140120260313581329.pdf  段落文に事業名が混在
  140120260225568450.pdf  要確認
  140120260312580582.pdf  「メンテナンス事業は」の文章内
  140120260312581037.pdf  要確認
  140120260313581385.pdf  「ライフスタイル事業では」の文章内
  140120260304576030.pdf  要確認
  140120260306577666.pdf  要確認

出力（上書きなし）:
  data/eval/pending7_pdf_verify.csv
  data/eval/pending7_pdf_verify.md
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import csv, re
from pathlib import Path
import pdfplumber

PROJ        = Path(__file__).parent.parent.parent
EVAL_DIR    = PROJ / "data" / "eval"
ARCHIVE_DIR = PROJ / "data" / "xbrl_archive"
OUT_CSV     = EVAL_DIR / "pending7_pdf_verify.csv"
OUT_MD      = EVAL_DIR / "pending7_pdf_verify.md"

TARGETS = [
    "140120260313581329.pdf",
    "140120260225568450.pdf",
    "140120260312580582.pdf",
    "140120260312581037.pdf",
    "140120260313581385.pdf",
    "140120260304576030.pdf",
    "140120260306577666.pdf",
]

# ── 判定パターン ──────────────────────────────────────────────

# 明確なセグメント表ヘッダー（数値表の先頭行）
_HDR_TABLE = re.compile(
    r"(?:売上高|外部顧客|セグメント利益|営業利益|セグメント損失)"
    r"(?:\s|\u3000)*(?:\(|（)?(?:百万円|千円|億円)?"
    r"(?:\)|）)?"
    r".{0,40}"
    r"(?:売上高|外部顧客|セグメント利益|営業利益|セグメント損失)"
)
# セグメント名が数値と同じ行にある（表の行として存在）
_SEG_WITH_NUM = re.compile(
    r"^[\s\u3000]*"
    r"(?:[ぁ-んァ-ヶ一-龥a-zA-Zａ-ｚＡ-Ｚ０-９\w]{2,20}(?:事業|部門|セグメント|国内|海外))"
    r"[\s\u3000]+"
    r"[\d,△▲\-－]{3,}"
)
# 単一セグメント省略
_SINGLE_SEG = re.compile(
    r"単一セグメント|1つの報告セグメント|単一の報告セグメント|セグメント別.*省略|セグメント情報.*省略"
)
# 文章中の singleline 記述（「〜事業は〜」→ BS/CF 文章）
_PROSE_ONLY = re.compile(
    r"(?:事業|部門)(?:は|では|においては|において)(?:、|,).{0,60}(?:ました|ています|であり|でした)"
)

def _collect_page_texts(pdf_path, max_pages=15):
    pages = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for i, pg in enumerate(pdf.pages[:max_pages]):
                text = pg.extract_text() or ""
                pages.append((i, text))
    except Exception as e:
        print(f"  ERROR: {e}")
    return pages

def _judge(pdf_name, page_texts):
    all_lines = []
    for _, text in page_texts:
        all_lines += text.split("\n")

    single = [ln for ln in all_lines if _SINGLE_SEG.search(ln)]
    if single:
        return "keep_no", "単一セグメント省略の明示記載", single[0].strip()[:100], ""

    tbl_hdrs = [ln for ln in all_lines if _HDR_TABLE.search(ln)]
    seg_num  = [ln for ln in all_lines if _SEG_WITH_NUM.match(ln)]
    prose    = [ln for ln in all_lines if _PROSE_ONLY.search(ln)]

    if tbl_hdrs and seg_num:
        ev = tbl_hdrs[0].strip()[:100]
        return "change_to_yes", f"表ヘッダー{len(tbl_hdrs)}件+セグメント数値行{len(seg_num)}件が共存", ev, tbl_hdrs[0].strip()[:100]

    if tbl_hdrs and len(tbl_hdrs) >= 2:
        return "change_to_yes", f"セグメント表ヘッダー{len(tbl_hdrs)}件（数値行確認推奨）", tbl_hdrs[0].strip()[:100], tbl_hdrs[0].strip()[:100]

    if seg_num and len(seg_num) >= 2:
        return "change_to_yes", f"数値付き事業名行{len(seg_num)}件（表の可能性あり）", seg_num[0].strip()[:100], seg_num[0].strip()[:100]

    if prose and not tbl_hdrs:
        return "keep_no", f"事業名は文章中のみ（段落誤認）prose={len(prose)}件", prose[0].strip()[:100], ""

    return "pending", "判定根拠不足（手動確認推奨）", "", ""

# ── 実行 ─────────────────────────────────────────────────────

cands_map = {}
for r in csv.DictReader(open(EVAL_DIR / "candidates.csv", encoding="utf-8-sig")):
    cands_map[r["pdf"].strip()] = r

CSV_COLS = ["pdf","verdict","reason","evidence_excerpt","evidence_table_line","seg_count","parse_quality","recommended_action"]
results = []
for pdf_name in TARGETS:
    pdf_path  = ARCHIVE_DIR / pdf_name
    cands     = cands_map.get(pdf_name, {})
    seg_count = cands.get("segment_count", "")
    pq        = cands.get("parse_quality", "")
    print(f"  {pdf_name} ...", end="", flush=True)
    if not pdf_path.exists():
        results.append({"pdf":pdf_name,"verdict":"pending","reason":"PDF未発見","evidence_excerpt":"","evidence_table_line":"","seg_count":seg_count,"parse_quality":pq,"recommended_action":"手動確認"})
        print(" → PDF未発見")
        continue
    pages = _collect_page_texts(pdf_path)
    verdict, reason, ev, tbl_line = _judge(pdf_name, pages)
    act = {"change_to_yes":"GT修正","keep_no":"GT維持","pending":"手動確認"}[verdict]
    results.append({"pdf":pdf_name,"verdict":verdict,"reason":reason,"evidence_excerpt":ev,"evidence_table_line":tbl_line,"seg_count":seg_count,"parse_quality":pq,"recommended_action":act})
    print(f" → {verdict}")

with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=CSV_COLS)
    w.writeheader(); w.writerows(results)
print(f"\n出力: {OUT_CSV}")

# ── MD 出力 ──────────────────────────────────────────────────

from collections import Counter
cnt = Counter(r["verdict"] for r in results)

md = [
    "# 要確認7件 PDF 本文確認レポート\n\n",
    f"| 分類 | 件数 |\n|---|---|\n",
    f"| change_to_yes | **{cnt['change_to_yes']}件** |\n",
    f"| keep_no | **{cnt['keep_no']}件** |\n",
    f"| pending | **{cnt['pending']}件** |\n",
    "\n---\n\n",
    "## 全件詳細\n\n",
    "| pdf | 判定 | 根拠 |\n|---|---|---|\n",
]
for r in results:
    md.append(f"| {r['pdf']} | **{r['verdict']}** | {r['reason'][:70]} |\n")

cty = [r for r in results if r["verdict"] == "change_to_yes"]
if cty:
    md += ["\n---\n\n", "## change_to_yes 詳細\n\n"]
    for r in cty:
        md.append(f"### {r['pdf']}\n")
        md.append(f"- 理由: {r['reason']}\n")
        md.append(f"- 根拠: `{r['evidence_excerpt'][:100]}`\n\n")

pend = [r for r in results if r["verdict"] == "pending"]
if pend:
    md += ["\n---\n\n", "## pending（要手動確認）\n\n"]
    for r in pend:
        md.append(f"- **{r['pdf']}**: {r['reason']}\n")

kno = [r for r in results if r["verdict"] == "keep_no"]
if kno:
    md += ["\n---\n\n", "## keep_no 詳細\n\n"]
    for r in kno:
        md.append(f"- **{r['pdf']}**: {r['reason']}\n  - `{r['evidence_excerpt'][:80]}`\n")

n_cty = cnt["change_to_yes"]; n_kno = cnt["keep_no"]; n_pend = cnt["pending"]
md += [
    "\n---\n\n",
    "## 結論\n\n",
    f"要確認7件のうち **change_to_yes={n_cty}件**、**keep_no={n_kno}件**、**pending={n_pend}件** となった。"
    f"change_to_yes の GT 修正を行えばさらに FP を削減できる。"
    f"pending 件は引き続き手動確認が必要。\n",
]
OUT_MD.write_text("".join(md), encoding="utf-8")
print(f"出力: {OUT_MD}")
