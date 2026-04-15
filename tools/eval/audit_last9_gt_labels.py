"""
audit_last9_gt_labels.py
残り FN 9件の has_segment_table ラベルを再監査し、
GT エラーかロジックエラーかを切り分ける。

出力:
  data/eval/last9_gt_audit.csv
  data/eval/last9_gt_audit.md
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import csv
import re
from pathlib import Path

import pdfplumber

PROJ        = Path(__file__).parent.parent.parent
EVAL_DIR    = PROJ / "data" / "eval"
ARCHIVE_DIR = PROJ / "data" / "xbrl_archive"
OUT_CSV     = EVAL_DIR / "last9_gt_audit.csv"
OUT_MD      = EVAL_DIR / "last9_gt_audit.md"

# ── 単一セグメント省略パターン ────────────────────────────────

_SINGLE_SEG_PATTERNS = [
    r"単一セグメント(?:で|である|のため).*省略",
    r"セグメント(?:情報|別|開示).*省略",
    r"単一の報告セグメント",
    r"1(?:つ|個)の報告セグメント",
]
_SINGLE_SEG_RE = re.compile("|".join(_SINGLE_SEG_PATTERNS))

# セグメント表らしい行パターン（複数列の売上/利益）
_TABLE_HEADER_RE = re.compile(
    r"(売上高|外部顧客|営業利益|セグメント利益|セグメント損失|売上収益|営業収益)"
)
_SEGMENT_ROW_RE = re.compile(
    r".*(事業|部門|セグメント|国内|海外|北米|欧州|アジア).{0,10}\s+[\d,△▲]"
)

# ── FN 9件を動的取得 ─────────────────────────────────────────

ss = {}
for r in csv.DictReader(open(EVAL_DIR / "screening_sheet.csv", encoding="utf-8-sig")):
    ss[r["pdf"].strip()] = r.get("has_segment_table", "").strip().lower()

cands = {}
for r in csv.DictReader(open(EVAL_DIR / "candidates.csv", encoding="utf-8-sig")):
    cands[r["pdf"].strip()] = r

fn_pdfs = sorted(
    p for p in cands
    if ss.get(p, "") == "yes" and cands[p]["detected_mode"] == "quarantine"
)
print(f"FN 件数: {len(fn_pdfs)} 件\n")

# ── 監査 ──────────────────────────────────────────────────────

CSV_COLS = [
    "pdf", "ticker", "current_has_segment_table",
    "audited_has_segment_table", "audit_reason",
    "evidence_text", "recommended_action", "notes",
]

rows_out = []

for pdf_name in fn_pdfs:
    pdf_path = ARCHIVE_DIR / pdf_name
    print(f"  {pdf_name} ...", end="", flush=True)

    if not pdf_path.exists():
        rows_out.append({
            "pdf": pdf_name, "ticker": "?",
            "current_has_segment_table": ss.get(pdf_name, "?"),
            "audited_has_segment_table": "unknown",
            "audit_reason": "pdf_not_found",
            "evidence_text": "PDF が存在しない",
            "recommended_action": "manual_review",
            "notes": "",
        })
        print(" NOT FOUND")
        continue

    # 全ページのテキストを結合して監査
    all_text = ""
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages[:15]:
                t = page.extract_text() or ""
                all_text += t + "\n"
    except Exception as e:
        rows_out.append({
            "pdf": pdf_name, "ticker": "?",
            "current_has_segment_table": ss.get(pdf_name, "?"),
            "audited_has_segment_table": "unknown",
            "audit_reason": "read_error",
            "evidence_text": str(e)[:80],
            "recommended_action": "manual_review",
            "notes": "",
        })
        print(f" READ ERROR")
        continue

    lines = all_text.split("\n")

    # --- 監査ルール 1: 単一セグメント省略明記 ---
    single_seg_evidence = ""
    for ln in lines:
        m = _SINGLE_SEG_RE.search(ln)
        if m:
            single_seg_evidence = ln.strip()[:120]
            break

    if single_seg_evidence:
        audited = "no"
        reason  = "explicit_single_segment_omission"
        evidence = single_seg_evidence
        action  = "change_yes_to_no"
        notes   = "単一セグメント省略の明示記載あり"
        print(f" → {reason}")
        rows_out.append({
            "pdf": pdf_name, "ticker": "?",
            "current_has_segment_table": ss.get(pdf_name, "yes"),
            "audited_has_segment_table": audited,
            "audit_reason": reason,
            "evidence_text": evidence,
            "recommended_action": action,
            "notes": notes,
        })
        continue

    # --- 監査ルール 2 / 3: セグメント表の有無を判定 ---
    # a) ヘッダー行（売上高/営業利益等）があるか
    hdr_hits = [ln.strip() for ln in lines if _TABLE_HEADER_RE.search(ln)]
    # b) セグメント名行（事業名/地域名 + 数値）があるか
    seg_rows = [ln.strip() for ln in lines if _SEGMENT_ROW_RE.search(ln)]

    if hdr_hits and seg_rows:
        # セグメント表が実際にありそう
        audited = "yes"
        reason  = "actual_segment_table_found"
        evidence = (hdr_hits[0][:60] + " / " + seg_rows[0][:60])
        action  = "keep_yes"
        notes   = f"ヘッダー行{len(hdr_hits)}件 セグメント名行{len(seg_rows)}件"
        print(f" → {reason}")
    elif hdr_hits and not seg_rows:
        # 売上/利益行はあるが事業別行なし → 注記のみ
        audited = "no"
        reason  = "segment_note_only"
        evidence = hdr_hits[0][:80]
        action  = "change_yes_to_no"
        notes   = "売上/利益語はあるが事業別セグメント行なし"
        print(f" → {reason}")
    elif not hdr_hits:
        # ヘッダー行もない
        audited = "no"
        reason  = "no_segment_table_found"
        evidence = "(ヘッダーキーワードなし)"
        action  = "change_yes_to_no"
        notes   = "売上/利益系キーワードなし"
        print(f" → {reason}")
    else:
        audited = "unknown"
        reason  = "ambiguous"
        evidence = seg_rows[0][:80] if seg_rows else "(不明)"
        action  = "manual_review"
        notes   = "判定困難"
        print(f" → {reason}")

    rows_out.append({
        "pdf": pdf_name, "ticker": "?",
        "current_has_segment_table": ss.get(pdf_name, "yes"),
        "audited_has_segment_table": audited,
        "audit_reason": reason,
        "evidence_text": evidence,
        "recommended_action": action,
        "notes": notes,
    })

# ── CSV 出力 ─────────────────────────────────────────────────

with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=CSV_COLS)
    w.writeheader()
    w.writerows(rows_out)

# ── MD レポート ──────────────────────────────────────────────

yes_to_no  = [r for r in rows_out if r["recommended_action"] == "change_yes_to_no"]
keep_yes   = [r for r in rows_out if r["recommended_action"] == "keep_yes"]
unknowns   = [r for r in rows_out if r["audited_has_segment_table"] == "unknown"]

md = [
    "# GT ラベル再監査レポート（残り FN 9件）\n\n",
    f"対象件数: {len(rows_out)} 件\n\n",
    "---\n\n",
    "## 1. 対象一覧\n\n",
    "| pdf | current | audited | reason | action |\n",
    "|---|---|---|---|---|\n",
]
for r in rows_out:
    md.append(
        f"| {r['pdf']} | {r['current_has_segment_table']} "
        f"| **{r['audited_has_segment_table']}** "
        f"| {r['audit_reason']} | {r['recommended_action']} |\n"
    )

md += [
    "\n---\n\n",
    "## 2. 集計\n\n",
    f"- **yes → no 修正候補**: {len(yes_to_no)} 件\n",
    f"- **yes 維持**: {len(keep_yes)} 件\n",
    f"- **unknown**: {len(unknowns)} 件\n",
    "\n---\n\n",
    "## 3. 各PDF 根拠サマリ\n\n",
]
for r in rows_out:
    md.append(
        f"### {r['pdf']}\n"
        f"- 現在: `{r['current_has_segment_table']}` → 監査後: `{r['audited_has_segment_table']}`\n"
        f"- 理由: `{r['audit_reason']}`\n"
        f"- 根拠: {r['evidence_text'][:100]}\n"
        f"- 推奨: `{r['recommended_action']}`\n\n"
    )

if yes_to_no:
    md += [
        "---\n\n",
        "## 4. screening_sheet.csv 反映すべき修正一覧\n\n",
        "| pdf | 変更内容 |\n|---|---|\n",
    ]
    for r in yes_to_no:
        md.append(f"| {r['pdf']} | has_segment_table: yes → **no** ({r['audit_reason']}) |\n")

OUT_MD.write_text("".join(md), encoding="utf-8")

print(f"\n完了: {OUT_CSV}")
print(f"完了: {OUT_MD}")
print(f"\nyes→no: {len(yes_to_no)}件  keep_yes: {len(keep_yes)}件  unknown: {len(unknowns)}件")
if yes_to_no:
    print("\n=== 修正候補 PDF ===")
    for r in yes_to_no:
        print(f"  {r['pdf']}  ({r['audit_reason']})")
