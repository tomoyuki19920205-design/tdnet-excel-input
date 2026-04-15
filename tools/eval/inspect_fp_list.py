"""
tools/eval/inspect_fp_list.py
FP一覧を安全に確認するスクリプト。

ターミナル出力:
  - 総件数、先頭5件のみ
  - preview は改行除去・空白圧縮・60文字切り捨て済み

全件詳細は以下に保存:
  data/eval/fp_list_clean.csv
  data/eval/fp_list_clean.md

使い方:
  python tools/eval/inspect_fp_list.py [--gt screening_sheet.highconf_fixed.csv]
"""
import sys, os, re, csv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import argparse
from pathlib import Path

# ── パス定義 ────────────────────────────────────────────────────────
PROJ     = Path(__file__).parent.parent.parent
EVAL_DIR = PROJ / "data" / "eval"
OUT_CSV  = EVAL_DIR / "fp_list_clean.csv"
OUT_MD   = EVAL_DIR / "fp_list_clean.md"

DEFAULT_GT = "screening_sheet.highconf_fixed.csv"
DETECT_MODES = {"COL_AS_SEG", "ROW_BASED"}
PREVIEW_MAX  = 60
OUT_COLS     = ["pdf", "detected_mode", "segment_count", "page_number", "preview_clean", "quarantine_reason"]

# ── ユーティリティ ──────────────────────────────────────────────────
def clean_preview(raw: str, maxlen: int = PREVIEW_MAX) -> str:
    """改行→空白、連続空白圧縮、maxlen 文字で打ち切る。"""
    s = str(raw or "")
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    s = re.sub(r"[ \t\u3000]+", " ", s).strip()
    if len(s) > maxlen:
        s = s[:maxlen - 1] + "…"
    return s

# ── 引数 ────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="FP一覧確認スクリプト")
    p.add_argument("--gt", default=DEFAULT_GT,
                   help=f"GTスクリーニングシートCSV (default: {DEFAULT_GT})")
    return p.parse_args()

def main():
    args = parse_args()
    gt_path = EVAL_DIR / args.gt
    if not gt_path.exists():
        print(f"[ERROR] GTファイルが見つかりません: {gt_path}", file=sys.stderr)
        sys.exit(1)

    # ── GT 読み込み ──────────────────────────────────────────────────
    with gt_path.open(encoding="utf-8-sig", newline="") as f:
        all_rows = list(csv.DictReader(f))

    # ── FP 抽出 ──────────────────────────────────────────────────────
    fp_rows = []
    for r in all_rows:
        hs   = r.get("has_segment_table", "").strip().lower()
        mode = r.get("detected_mode", "").strip()
        if hs == "no" and mode in DETECT_MODES:
            fp_rows.append({
                "pdf":             r.get("pdf", "").strip(),
                "detected_mode":   mode,
                "segment_count":   r.get("segment_count", "").strip(),
                "page_number":     r.get("page_number", "").strip(),
                "preview_clean":   clean_preview(r.get("segment_names_preview", "")),
                "quarantine_reason": r.get("quarantine_reason", "").strip(),
            })

    total = len(fp_rows)

    # ── CSV 書き出し ─────────────────────────────────────────────────
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS)
        w.writeheader()
        w.writerows(fp_rows)

    # ── MD 書き出し ──────────────────────────────────────────────────
    md_lines = [
        f"# FP 一覧（{args.gt}）\n\n",
        f"**FP合計: {total}件**\n\n",
        "| # | pdf | mode | seg | pg | preview | qrn |\n",
        "|---|-----|------|-----|----|---------|----|  \n",
    ]
    for i, r in enumerate(fp_rows, 1):
        pdf  = r["pdf"]
        mode = r["detected_mode"]
        seg  = r["segment_count"]
        pg   = r["page_number"]
        prev = r["preview_clean"].replace("|", "｜")
        qrn  = r["quarantine_reason"].replace("|", "｜")
        md_lines.append(f"| {i} | {pdf} | {mode} | {seg} | {pg} | {prev} | {qrn} |\n")

    OUT_MD.write_text("".join(md_lines), encoding="utf-8-sig")

    # ── ターミナル出力（最小限） ─────────────────────────────────────
    print(f"GT: {gt_path.name}  FP合計: {total}件")
    print(f"--- 先頭{min(5, total)}件 ---")
    for i, r in enumerate(fp_rows[:5], 1):
        print(f"  {i:2d}. {r['pdf']}  mode={r['detected_mode']}  seg={r['segment_count']}  pg={r['page_number']}")
        print(f"      [{r['preview_clean']}]")
    if total > 5:
        print(f"  ... 残り {total - 5} 件は出力ファイルを参照")
    print(f"\n出力:")
    print(f"  {OUT_CSV}")
    print(f"  {OUT_MD}")

if __name__ == "__main__":
    main()
