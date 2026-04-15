"""
pending7_semiauto_review.py
要確認7件について PL_summary と segment_table を区別する半自動チェック。

出力（上書きなし）:
  data/eval/pending7_semiauto_review.csv
  data/eval/pending7_semiauto_review.md
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import csv, re
from pathlib import Path
from collections import Counter
import pdfplumber

PROJ        = Path(__file__).parent.parent.parent
EVAL_DIR    = PROJ / "data" / "eval"
ARCHIVE_DIR = PROJ / "data" / "xbrl_archive"
OUT_CSV     = EVAL_DIR / "pending7_semiauto_review.csv"
OUT_MD      = EVAL_DIR / "pending7_semiauto_review.md"

TARGETS = [
    "140120260313581329.pdf",
    "140120260225568450.pdf",
    "140120260312580582.pdf",
    "140120260312581037.pdf",
    "140120260313581385.pdf",
    "140120260304576030.pdf",
    "140120260306577666.pdf",
]

# ── パターン定義 ──────────────────────────────────────────────

# 事業名候補ラベル（名詞的ラベル: 末尾が事業/部門/サービス/地域類など）
_BIZ_LABEL = re.compile(
    r"(?:^|[\s\u3000])"
    r"(?:[ぁ-んァ-ヶ一-龥A-Za-zａ-ｚＡ-Ｚ]{2,20})"
    r"(?:事業|部門|セグメント|サービス|ソリューション|システム|プロダクト"
    r"|国内|海外|アジア|北米|欧州|中国|その他地域|日本|グローバル)"
    r"(?=[\s\u3000\d,△▲\-－（(「]|$)"
, re.MULTILINE)

# 全社 PL 指標（売上高 / 営業利益 / 経常利益 / 純利益）
_PL_METRICS = re.compile(
    r"(?:売上高|売上収益|営業利益|営業損失|経常利益|経常損失|"
    r"当期純利益|当期純損失|親会社株主に帰属|四半期純利益|中間純利益)"
)
# 全社 PL を 1行に列挙するサマリ行（例: 「売上高 営業利益 経常利益」）
_PL_HEADER_ROW = re.compile(
    r"(?:売上高|売上収益).{0,10}(?:営業利益|営業損失).{0,10}(?:経常利益|当期純利益|四半期純利益)"
)
# セグメント見出し
_SEG_HEADER = re.compile(
    r"(?:セグメント情報|報告セグメント|セグメント別|事業別|所在地別セグメント"
    r"|セグメント利益|セグメント損失|セグメントの概要)"
)
# 「事業名 + 数値×2以上」の反復行（例: 「ライフスタイル事業 1,234 567」）
_REPEAT_ROW = re.compile(
    r"(?:[ぁ-んァ-ヶ一-龥A-Za-zａ-ｚＡ-Ｚ]{2,15})(?:事業|部門|セグメント)"
    r"[\s\u3000]+"
    r"[\d,△▲\-－]{2,}[\s\u3000]+"
    r"[\d,△▲\-－]{2,}"
)
# 全社 PL サマリの縦並び（4行以内に PL 指標が 3種以上集中）
def _pl_summary_flag(lines: list[str]) -> bool:
    """4行ウィンドウで PL 指標が 3 種以上集中する区間があれば True。"""
    hits = [bool(_PL_METRICS.search(ln)) for ln in lines]
    for i in range(len(hits) - 3):
        if sum(hits[i:i+4]) >= 3:
            return True
    return False

def _collect_lines(pdf_path, max_pages=15) -> list[str]:
    lines = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for pg in pdf.pages[:max_pages]:
                text = pg.extract_text() or ""
                lines += text.split("\n")
    except Exception as e:
        print(f"  ERROR: {e}")
    return lines

def _analyze(pdf_name: str) -> dict:
    pdf_path = ARCHIVE_DIR / pdf_name
    if not pdf_path.exists():
        return {k: None for k in (
            "business_label_count","metric_only_header_flag",
            "repeated_row_structure_flag","segment_header_flag",
            "pl_summary_flag","auto_decision","decision_reason",
            "recommended_manual_check"
        )} | {"auto_decision": "pending", "decision_reason": "PDF未発見",
               "recommended_manual_check": "PDFを手動で入手"}

    lines = _collect_lines(pdf_path)

    # 1. business_label_count
    biz_labels = list({m.group().strip() for ln in lines for m in [_BIZ_LABEL.search(ln)] if m})
    blc = len(biz_labels)

    # 2. metric_only_header_flag
    pl_hdr_lines = [ln for ln in lines if _PL_HEADER_ROW.search(ln)]
    metric_only_hdr = len(pl_hdr_lines) > 0

    # 3. repeated_row_structure_flag
    repeat_rows = [ln for ln in lines if _REPEAT_ROW.search(ln)]
    repeat_flag = len(repeat_rows) >= 2

    # 4. segment_header_flag
    seg_hdr_lines = [ln for ln in lines if _SEG_HEADER.search(ln)]
    seg_hdr_flag = len(seg_hdr_lines) >= 1

    # 5. pl_summary_flag
    pl_sum = _pl_summary_flag(lines)

    # ── 判定ロジック ────────────────────────────────────────
    # segment_yes: セグメント見出し or 反復行構造 があり、事業名が複数
    # keep_no:     全社PLサマリのみで事業名が少ない
    # pending:     どちらとも断定できない

    if seg_hdr_flag and blc >= 2:
        decision = "segment_yes"
        reason   = f"セグメント見出し{len(seg_hdr_lines)}件 + 事業名ラベル{blc}件"
        hint     = "抽出されたセグメント名と一致するか目視確認"
    elif repeat_flag and blc >= 2:
        decision = "segment_yes"
        reason   = f"事業名+数値の反復行{len(repeat_rows)}件 + 事業名ラベル{blc}件"
        hint     = "反復行がセグメント表か注記の明細かを目視確認"
    elif pl_sum and metric_only_hdr and blc <= 1:
        decision = "keep_no"
        reason   = f"全社PLサマリのみ（事業ラベル={blc}件、PLヘッダー行={len(pl_hdr_lines)}件）"
        hint     = "事業別リストが存在しないことを確認"
    elif pl_sum and not seg_hdr_flag and not repeat_flag:
        decision = "keep_no"
        reason   = f"全社PLサマリ検出 + セグメント構造なし（事業ラベル={blc}件）"
        hint     = "事業名が文章中のみか確認"
    else:
        decision = "pending"
        reason   = f"自動判定困難（biz={blc} seg_hdr={seg_hdr_flag} repeat={repeat_flag} pl_sum={pl_sum}）"
        hint     = "セグメント情報の有無をページ単位で確認"

    return {
        "business_label_count":      blc,
        "metric_only_header_flag":   metric_only_hdr,
        "repeated_row_structure_flag": repeat_flag,
        "segment_header_flag":       seg_hdr_flag,
        "pl_summary_flag":           pl_sum,
        "auto_decision":             decision,
        "decision_reason":           reason,
        "recommended_manual_check":  hint,
    }

# ── 実行 ─────────────────────────────────────────────────────

CSV_COLS = [
    "pdf","business_label_count","metric_only_header_flag",
    "repeated_row_structure_flag","segment_header_flag","pl_summary_flag",
    "auto_decision","decision_reason","recommended_manual_check",
]
results = []
for pdf_name in TARGETS:
    print(f"  {pdf_name} ...", end="", flush=True)
    a = _analyze(pdf_name)
    row = {"pdf": pdf_name} | a
    results.append(row)
    print(f" → {a['auto_decision']}")

with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=CSV_COLS)
    w.writeheader(); w.writerows(results)
print(f"\n出力: {OUT_CSV}")

# ── MD 出力 ──────────────────────────────────────────────────

cnt = Counter(r["auto_decision"] for r in results)
seg_yes = [r for r in results if r["auto_decision"] == "segment_yes"]
keep_no = [r for r in results if r["auto_decision"] == "keep_no"]
pending = [r for r in results if r["auto_decision"] == "pending"]

md = [
    "# 要確認7件 半自動レビュー（PL_summary vs segment_table）\n\n",
    "## 集計\n\n",
    f"| 判定 | 件数 |\n|---|---|\n",
    f"| segment_yes（GT修正候補） | **{cnt['segment_yes']}件** |\n",
    f"| keep_no（GT維持） | **{cnt['keep_no']}件** |\n",
    f"| pending（保留） | **{cnt['pending']}件** |\n",
    "\n---\n\n",
    "## 全件一覧\n\n",
    "| pdf | biz_cnt | seg_hdr | repeat | pl_sum | 判定 | 理由 |\n"
    "|---|---|---|---|---|---|---|\n",
]
for r in results:
    md.append(
        f"| {r['pdf']} | {r['business_label_count']} "
        f"| {r['segment_header_flag']} | {r['repeated_row_structure_flag']} "
        f"| {r['pl_summary_flag']} | **{r['auto_decision']}** "
        f"| {r['decision_reason'][:55]} |\n"
    )

if seg_yes:
    md += ["\n---\n\n", "## segment_yes 候補PDF一覧\n\n"]
    for r in seg_yes:
        md.append(f"- **{r['pdf']}**: {r['decision_reason']}\n  - 確認ポイント: {r['recommended_manual_check']}\n")

if keep_no:
    md += ["\n---\n\n", "## keep_no 候補PDF一覧\n\n"]
    for r in keep_no:
        md.append(f"- **{r['pdf']}**: {r['decision_reason']}\n  - 確認ポイント: {r['recommended_manual_check']}\n")

if pending:
    md += ["\n---\n\n", "## pending（保留）\n\n"]
    for r in pending:
        md.append(f"- **{r['pdf']}**: {r['decision_reason']}\n  - 確認ポイント: {r['recommended_manual_check']}\n")

# PL_summary と判定した主因まとめ
kno_reasons = [r["decision_reason"] for r in keep_no]
pl_flag_cnt = sum(1 for r in results if r["pl_summary_flag"])
seg_flag_cnt = sum(1 for r in results if r["segment_header_flag"])
md += [
    "\n---\n\n",
    "## PL_summary と判定した主因まとめ\n\n",
    f"- 全社 PL サマリ検出（pl_summary_flag=True）: {pl_flag_cnt}件\n",
    f"- セグメント見出し検出（segment_header_flag=True）: {seg_flag_cnt}件\n",
    "- keep_no 判定理由一覧:\n",
]
for r_str in kno_reasons:
    md.append(f"  - {r_str}\n")

# 最終結論
md += [
    "\n---\n\n",
    "## 最終結論\n\n",
    f"半自動チェックの結果、**segment_yes={cnt['segment_yes']}件** は GT 修正を優先して手動確認すべき候補であり、"
    f"**keep_no={cnt['keep_no']}件** は全社 PL サマリのみと推定されるため GT 維持を推奨する。"
    f"**pending={cnt['pending']}件** については自動判定が困難のため目視確認が必須である。"
    f"次のステップは segment_yes 候補の PDF を実際に開き、セグメント別表（事業名×数値の行列）が"
    f"実在するかを確認したうえで、実在すれば screening_sheet.csv を更新して precision を再計算する。\n",
]

OUT_MD.write_text("".join(md), encoding="utf-8")
print(f"出力: {OUT_MD}")

print(f"\n=== 集計 ===")
print(f"  segment_yes: {cnt['segment_yes']} 件")
print(f"  keep_no:     {cnt['keep_no']} 件")
print(f"  pending:     {cnt['pending']} 件")
if seg_yes: print("  segment_yes:", [r['pdf'] for r in seg_yes])
if keep_no:  print("  keep_no:   ", [r['pdf'] for r in keep_no])
