"""
audit_fp24_gt_labels.py
FP（誤検出）24件のGTラベルを再監査し、keep_no/change_to_yes/pendingに分類する。

出力:
  data/eval/gt_reaudit_fp24.csv
  data/eval/gt_reaudit_fp24_summary.md
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
OUT_CSV     = EVAL_DIR / "gt_reaudit_fp24.csv"
OUT_MD      = EVAL_DIR / "gt_reaudit_fp24_summary.md"

# ── 入力ファイル読み込み ──────────────────────────────────────

fail_rows = list(csv.DictReader(open(EVAL_DIR / "failure_cases.final.csv", encoding="utf-8-sig")))
fp24 = [r for r in fail_rows if r["case_type"].strip() == "FP"]
print(f"FP 件数: {len(fp24)} 件")

cands_map = {}
for r in csv.DictReader(open(EVAL_DIR / "candidates.csv", encoding="utf-8-sig")):
    cands_map[r["pdf"].strip()] = r

# ── 監査ルール ────────────────────────────────────────────────

# 単一セグメント省略パターン
_SINGLE_SEG_RE = re.compile(
    r"単一セグメント|единый|1つの報告セグメント|セグメント別.*省略|セグメント情報.*省略|"
    r"当社グループは.*単一|1個の報告セグメント"
)
# セグメント表らしいパターン（複数事業列あり）
_SEG_TABLE_RE = re.compile(
    r"(売上高|外部顧客|営業利益|セグメント利益|セグメント損失).{0,50}(売上高|外部顧客|営業利益|セグメント利益)"
)
# 事業別/地域別セグメント行
_SEG_ROW_RE = re.compile(
    r"(?:事業|部門|セグメント|国内|海外|北米|欧州|アジア|アジア太平洋|中国|コンシューマ|エンタープライズ"
    r"|ヘルスケア|フード|ケミカル|エネルギー|建設|金融|保険|情報|通信|物流|製造|販売).{0,15}"
    r"[\d,△▲]"
)
# BS/CF 文言
_BSCF_RE = re.compile(
    r"貸借対照表|資産の部|負債の部|純資産|キャッシュ・フロー|現金及び預金|流動資産|固定資産|"
    r"売掛金|受取手形|繰延税金|包括利益|当期首残高|当期末残高|資本金"
)
# 地域別・製品別売上のみ書類
_REGION_ONLY_RE = re.compile(
    r"(?:地域別|製品別|品目別|国内外別|所在地別).*売上"
)
# 前期比較表パターン（前期/当期の2列）
_PERIOD_RE = re.compile(r"(?:前期|前連結|前中間|前年|2024|2025|2026).{0,20}(?:当期|当連結|当中間|当年)")

# ── 監査ロジック ──────────────────────────────────────────────

def _audit(pdf_name: str, cands: dict) -> dict:
    pdf_path = ARCHIVE_DIR / pdf_name
    mode = cands.get("detected_mode", "")
    seg_count = cands.get("segment_count", "0")
    pq = cands.get("parse_quality", "")
    page_no = cands.get("page_number", "")

    if not pdf_path.exists():
        return {
            "re_audit_result": "pending",
            "decision_reason": "PDF未発見",
            "evidence_page": "",
            "evidence_excerpt": "",
        }

    all_text = ""
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for pg in pdf.pages[:15]:
                all_text += (pg.extract_text() or "") + "\n"
    except Exception as e:
        return {
            "re_audit_result": "pending",
            "decision_reason": f"PDF読み取りエラー: {str(e)[:40]}",
            "evidence_page": "",
            "evidence_excerpt": "",
        }

    lines = all_text.split("\n")

    # --- ルール1: 単一セグメント省略明記 ---
    for ln in lines:
        if _SINGLE_SEG_RE.search(ln):
            return {
                "re_audit_result": "keep_no",
                "decision_reason": "単一セグメント省略の明示記載",
                "evidence_page": "",
                "evidence_excerpt": ln.strip()[:100],
            }

    # --- ルール2: BS/CF 語が支配的かみる ---
    bscf_lines = [ln for ln in lines if _BSCF_RE.search(ln)]
    seg_rows   = [ln for ln in lines if _SEG_ROW_RE.search(ln)]

    # --- ルール3: 前期/当期の2列比較表のみ ---
    period_lines = [ln for ln in lines if _PERIOD_RE.search(ln)]

    # --- ルール4: セグメント表らしいパターンが複数あるか ---
    seg_table_hits = [ln for ln in lines if _SEG_TABLE_RE.search(ln)]
    region_only    = any(_REGION_ONLY_RE.search(ln) for ln in lines)

    # --- 判定 ---
    seg_count_int = int(seg_count) if str(seg_count).isdigit() else 0

    if seg_rows and seg_table_hits and seg_count_int >= 3:
        # 事業別行 + ヘッダー行 + 3件以上 → セグメント表の可能性
        return {
            "re_audit_result": "change_to_yes",
            "decision_reason": f"事業別行{len(seg_rows)}件・ヘッダー行{len(seg_table_hits)}件・seg_count={seg_count_int} → セグメント表の可能性",
            "evidence_page": page_no,
            "evidence_excerpt": seg_rows[0].strip()[:80],
        }

    if region_only:
        return {
            "re_audit_result": "pending",
            "decision_reason": "地域別/製品別売上のみ（セグメント定義確認要）",
            "evidence_page": page_no,
            "evidence_excerpt": next((ln.strip()[:80] for ln in lines if _REGION_ONLY_RE.search(ln)), ""),
        }

    if seg_rows and seg_count_int >= 2:
        return {
            "re_audit_result": "pending",
            "decision_reason": f"事業別行{len(seg_rows)}件あるが表形式が確認できず（要手動確認）",
            "evidence_page": page_no,
            "evidence_excerpt": seg_rows[0].strip()[:80],
        }

    if pq in ("sales_only", "partial_sales_only"):
        return {
            "re_audit_result": "pending",
            "decision_reason": "売上のみ（利益なし）→ セグメント定義確認要",
            "evidence_page": page_no,
            "evidence_excerpt": "",
        }

    if len(bscf_lines) > len(seg_rows) * 2:
        return {
            "re_audit_result": "keep_no",
            "decision_reason": f"BS/CF語が支配的（bscf={len(bscf_lines)} vs seg={len(seg_rows)}）",
            "evidence_page": "",
            "evidence_excerpt": bscf_lines[0].strip()[:80] if bscf_lines else "",
        }

    if period_lines and seg_count_int <= 2:
        return {
            "re_audit_result": "keep_no",
            "decision_reason": f"前期/当期の2列比較表と推定（seg_count={seg_count_int}）",
            "evidence_page": page_no,
            "evidence_excerpt": period_lines[0].strip()[:80] if period_lines else "",
        }

    return {
        "re_audit_result": "keep_no",
        "decision_reason": "セグメント表パターン未検出・段落誤認の可能性",
        "evidence_page": page_no,
        "evidence_excerpt": "",
    }

# ── 監査実行 ─────────────────────────────────────────────────

CSV_COLS = [
    "pdf","current_gt","re_audit_result","decision_reason",
    "evidence_page","evidence_excerpt","candidate_mode",
    "segment_count","parse_quality","recommended_action",
]
results = []
for fp in fp24:
    pdf_name = fp["pdf"].strip()
    cands    = cands_map.get(pdf_name, {})
    print(f"  {pdf_name} ...", end="", flush=True)
    audit = _audit(pdf_name, cands)
    action_map = {
        "keep_no":        "GT維持",
        "change_to_yes":  "GT修正",
        "pending":        "定義確認",
    }
    row = {
        "pdf":               pdf_name,
        "current_gt":        "no",
        "re_audit_result":   audit["re_audit_result"],
        "decision_reason":   audit["decision_reason"],
        "evidence_page":     audit["evidence_page"],
        "evidence_excerpt":  audit["evidence_excerpt"],
        "candidate_mode":    cands.get("detected_mode", ""),
        "segment_count":     cands.get("segment_count", ""),
        "parse_quality":     cands.get("parse_quality", ""),
        "recommended_action": action_map.get(audit["re_audit_result"], "定義確認"),
    }
    results.append(row)
    print(f" → {audit['re_audit_result']}")

with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=CSV_COLS)
    w.writeheader()
    w.writerows(results)
print(f"\n出力: {OUT_CSV}")

# ── MD 出力 ──────────────────────────────────────────────────

cnt = Counter(r["re_audit_result"] for r in results)
keep_no_rows      = [r for r in results if r["re_audit_result"] == "keep_no"]
change_yes_rows   = [r for r in results if r["re_audit_result"] == "change_to_yes"]
pending_rows      = [r for r in results if r["re_audit_result"] == "pending"]

# keep_no 主因分類
def _keep_reason_cat(reason: str) -> str:
    if "単一セグメント" in reason: return "単一セグメント省略"
    if "BS/CF" in reason:          return "BS/CF"
    if "比較表" in reason:         return "比較表"
    if "段落誤認" in reason or "パターン未検出" in reason: return "注記文/段落誤認"
    return "その他"

keep_cats = Counter(_keep_reason_cat(r["decision_reason"]) for r in keep_no_rows)

md = [
    "# FP24件 GT再監査レポート\n\n",
    f"総件数: **{len(results)} 件**\n\n",
    "## 集計\n\n",
    f"| 分類 | 件数 |\n|---|---|\n",
    f"| keep_no（GT維持） | **{cnt['keep_no']}件** |\n",
    f"| change_to_yes（GT修正候補） | **{cnt['change_to_yes']}件** |\n",
    f"| pending（保留） | **{cnt['pending']}件** |\n",
    "\n---\n\n",
    "## 対象一覧\n\n",
    "| pdf | 分類 | 理由 |\n|---|---|---|\n",
]
for r in results:
    md.append(f"| {r['pdf']} | **{r['re_audit_result']}** | {r['decision_reason'][:60]} |\n")

if change_yes_rows:
    md += ["\n---\n\n", "## change_to_yes のPDF一覧\n\n"]
    for r in change_yes_rows:
        md.append(f"- **{r['pdf']}**: {r['decision_reason']}\n  - 根拠: {r['evidence_excerpt'][:80]}\n")

if pending_rows:
    md += ["\n---\n\n", "## pending のPDF一覧\n\n"]
    for r in pending_rows:
        md.append(f"- **{r['pdf']}**: {r['decision_reason']}\n  - 根拠: {r['evidence_excerpt'][:80]}\n")

md += ["\n---\n\n", "## keep_no 主因内訳\n\n", "| 主因 | 件数 |\n|---|---|\n"]
for cat, n in keep_cats.most_common():
    md.append(f"| {cat} | {n}件 |\n")

# 最終結論
n_yes = cnt["change_to_yes"]; n_pend = cnt["pending"]; n_keep = cnt["keep_no"]
conclusion = (
    f"GT修正候補は **{n_yes}件**、保留は **{n_pend}件** である。"
    f"まず change_to_yes {n_yes}件の GT を yes に修正してから precision を再計算すべきである。"
    f"次に pending {n_pend}件を手動確認してセグメント定義を整理したうえで、"
    f"残る keep_no {n_keep}件（主に{keep_cats.most_common(1)[0][0]}）への narrative_guard 強化に進むことを推奨する。"
)
md += ["\n---\n\n", "## 最終結論\n\n", conclusion + "\n"]

OUT_MD.write_text("".join(md), encoding="utf-8")
print(f"出力: {OUT_MD}")
print(f"\n=== 集計 ===")
print(f"  keep_no:     {cnt['keep_no']} 件")
print(f"  change_yes:  {cnt['change_to_yes']} 件")
print(f"  pending:     {cnt['pending']} 件")
if change_yes_rows:
    print("  change_yes PDF:", [r['pdf'] for r in change_yes_rows])
if pending_rows:
    print("  pending PDF:   ", [r['pdf'] for r in pending_rows])
