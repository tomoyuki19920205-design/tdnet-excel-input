"""
Step 2-b: Ground Truth 叩き台作成スクリプト

入力:
  data/eval/detailed_eval_targets.csv   (PDF順の基準)
  data/eval/tables_preview.txt          (step2a の出力)

出力:
  data/eval/ground_truth_review.csv     (人手確認用)
  data/eval/ground_truth.csv            (集計用 seed)

注意:
  - PDF は再実行しない。tables_preview.txt のパースのみ。
  - 数値は float 化して出力する。空欄/None は空欄。
  - 並び順: detailed_eval_targets.csv の PDF 順 → 同一 PDF 内はセグメント順
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import csv
import re
from pathlib import Path

EVAL_DIR      = Path(__file__).parent.parent.parent / "data" / "eval"
TARGETS_CSV   = EVAL_DIR / "detailed_eval_targets.csv"
PREVIEW_TXT   = EVAL_DIR / "tables_preview.txt"
REVIEW_CSV    = EVAL_DIR / "ground_truth_review.csv"
GT_CSV        = EVAL_DIR / "ground_truth.csv"

REVIEW_COLS = [
    "pdf", "ticker", "expected_table_type",
    "segment_name_raw", "segment_name_canonical",
    "sales_raw", "sales_canonical",
    "profit_raw", "profit_canonical",
    "unit_text",
    "review_status",
    "notes",
]
GT_COLS = ["pdf", "ticker", "table_type", "segment_name", "sales", "profit"]

SEP = "=" * 50


# ── ユーティリティ ────────────────────────────────────────────────

def _to_float(val: str):
    """
    文字列を float に変換する。
    - 空欄 / "None" → None
    - 0 は有効値として保持
    - 負値はそのまま保持
    """
    if val is None:
        return None
    s = val.strip()
    if s == "" or s.lower() == "none":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fmt_float(v) -> str:
    """float → CSV 出力用文字列。None → 空欄。"""
    if v is None:
        return ""
    # 整数相当なら整数表記
    if v == int(v):
        return str(int(v))
    return str(v)


# ── tables_preview.txt パーサー ───────────────────────────────────

def parse_preview_txt(preview_path: Path) -> dict:
    """
    tables_preview.txt をパースして {pdf_name: block_dict} を返す。

    block_dict のキー:
      ticker, expected_table_type, unit_text,
      quarantine_reason, segments (list of dict)

    segments の各要素:
      order, name, sales_raw, profit_raw
    """
    text = preview_path.read_text(encoding="utf-8", errors="replace")
    # SEP で分割（先頭のヘッダー行を除く）
    raw_blocks = re.split(r"={50}", text)

    blocks = {}
    for block in raw_blocks:
        block = block.strip()
        if not block:
            continue

        # PDF 名
        m_pdf = re.search(r"^PDF:\s*(.+)$", block, re.MULTILINE)
        if not m_pdf:
            continue
        pdf_name = m_pdf.group(1).strip()

        # ticker / expected_table_type / unit / quarantine_reason
        m_tk  = re.search(r"^ticker:\s*(.*)$",               block, re.MULTILINE)
        m_et  = re.search(r"^expected_table_type:\s*(.*)$",  block, re.MULTILINE)
        m_unit = re.search(r"^unit:\s*(.*)$",                block, re.MULTILINE)
        m_qrn = re.search(r"^quarantine_reason:\s*(.*)$",    block, re.MULTILINE)

        ticker   = m_tk.group(1).strip()  if m_tk  else "?"
        exp_type = m_et.group(1).strip()  if m_et  else ""
        unit_text = m_unit.group(1).strip() if m_unit else ""
        qrn      = m_qrn.group(1).strip() if m_qrn else ""

        # [EXTRACTED SEGMENTS] セクションを取得
        seg_section = ""
        m_sec = re.search(r"\[EXTRACTED SEGMENTS\](.*?)(?=\n\[|\Z)", block, re.DOTALL)
        if m_sec:
            seg_section = m_sec.group(1)

        # セグメント行をパース
        # 形式: [1] 木材  sales=1872.894  profit=110.468  pq=full
        segment_pattern = re.compile(
            r"^\s*\[(\d+)\]\s+(.+?)\s+"
            r"sales=([^\s]+)\s+"
            r"profit=([^\s]+)",
            re.MULTILINE,
        )
        segments = []
        for m in segment_pattern.finditer(seg_section):
            segments.append({
                "order":      int(m.group(1)),
                "name":       m.group(2).strip(),
                "sales_raw":  m.group(3).strip(),
                "profit_raw": m.group(4).strip(),
            })

        blocks[pdf_name] = {
            "ticker":            ticker,
            "expected_table_type": exp_type,
            "unit_text":         unit_text,
            "quarantine_reason": qrn if qrn.lower() not in ("", "none") else "",
            "segments":          segments,
        }

    return blocks


# ── メイン ───────────────────────────────────────────────────────

def main():
    # 1. detailed_eval_targets.csv から PDF 順を読み込む
    targets = []
    with open(TARGETS_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            targets.append({
                "pdf":                row.get("pdf", "").strip(),
                "ticker":             row.get("ticker", "?").strip(),
                "expected_table_type": row.get("expected_table_type", "").strip(),
            })
    print(f"評価対象: {len(targets)} 件")

    # 2. tables_preview.txt をパース
    try:
        preview_blocks = parse_preview_txt(PREVIEW_TXT)
    except FileNotFoundError:
        print(f"ERROR: {PREVIEW_TXT} が見つかりません。先に step2a を実行してください。")
        return

    # 3. 行を生成
    review_rows = []
    gt_rows     = []

    for t in targets:
        pdf = t["pdf"]
        ticker_from_targets = t["ticker"]
        exp_type_from_targets = t["expected_table_type"]

        print(f"  {pdf} ... ", end="", flush=True)

        try:
            block = preview_blocks.get(pdf)
            if block is None:
                raise KeyError(f"{pdf} は tables_preview.txt に見つかりません")

            unit_text = block["unit_text"]
            qrn       = block["quarantine_reason"]
            segments  = block["segments"]
            # ticker は targets.csv を優先、なければ preview から
            ticker = ticker_from_targets if ticker_from_targets not in ("", "?") else block["ticker"]

            if segments:
                print(f"segs={len(segments)}")
                for seg in segments:
                    sales_f  = _to_float(seg["sales_raw"])
                    profit_f = _to_float(seg["profit_raw"])
                    sales_s  = _fmt_float(sales_f)
                    profit_s = _fmt_float(profit_f)

                    review_rows.append({
                        "pdf":                    pdf,
                        "ticker":                 ticker,
                        "expected_table_type":    exp_type_from_targets,
                        "segment_name_raw":       seg["name"],
                        "segment_name_canonical": seg["name"],
                        "sales_raw":              sales_s,
                        "sales_canonical":        sales_s,
                        "profit_raw":             profit_s,
                        "profit_canonical":       profit_s,
                        "unit_text":              unit_text,
                        "review_status":          "auto_seed",
                        "notes":                  "",
                    })
                    gt_rows.append({
                        "pdf":          pdf,
                        "ticker":       ticker,
                        "table_type":   exp_type_from_targets,
                        "segment_name": seg["name"],
                        "sales":        sales_s,
                        "profit":       profit_s,
                    })
            else:
                # セグメントなし or quarantine
                note = f"quarantine:{qrn}" if qrn else "no_extracted_segments"
                print(f"needs_check ({note})")
                review_rows.append({
                    "pdf":                    pdf,
                    "ticker":                 ticker,
                    "expected_table_type":    exp_type_from_targets,
                    "segment_name_raw":       "",
                    "segment_name_canonical": "",
                    "sales_raw":              "",
                    "sales_canonical":        "",
                    "profit_raw":             "",
                    "profit_canonical":       "",
                    "unit_text":              unit_text,
                    "review_status":          "needs_check",
                    "notes":                  note,
                })
                gt_rows.append({
                    "pdf":          pdf,
                    "ticker":       ticker,
                    "table_type":   exp_type_from_targets,
                    "segment_name": "",
                    "sales":        "",
                    "profit":       "",
                })

        except Exception as e:
            print(f"ERROR: {e}")
            review_rows.append({
                "pdf":                    pdf,
                "ticker":                 ticker_from_targets,
                "expected_table_type":    exp_type_from_targets,
                "segment_name_raw":       "",
                "segment_name_canonical": "",
                "sales_raw":              "",
                "sales_canonical":        "",
                "profit_raw":             "",
                "profit_canonical":       "",
                "unit_text":              "",
                "review_status":          "needs_check",
                "notes":                  f"ERROR:{e}",
            })
            gt_rows.append({
                "pdf":          pdf,
                "ticker":       ticker_from_targets,
                "table_type":   exp_type_from_targets,
                "segment_name": "",
                "sales":        "",
                "profit":       "",
            })

    # 4. ground_truth_review.csv 出力
    with open(REVIEW_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REVIEW_COLS)
        writer.writeheader()
        writer.writerows(review_rows)
    print(f"\n完了: {REVIEW_CSV}")

    # 5. ground_truth.csv 出力
    with open(GT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=GT_COLS)
        writer.writeheader()
        writer.writerows(gt_rows)
    print(f"完了: {GT_CSV}")

    print("→ ground_truth_review.csv を人手で確認し、review_status / *_canonical を修正してください。")


if __name__ == "__main__":
    main()
