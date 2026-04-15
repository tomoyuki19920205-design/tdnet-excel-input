"""
Step 2-a: 詳細評価対象の可視化スクリプト

入力: data/eval/detailed_eval_targets.csv (pdf, ticker, expected_table_type)
出力: data/eval/tables_preview.txt (毎回上書き)

各PDFについて:
  - run_segment_detection_v2 を呼び出し
  - pdfplumber でセグメントページの raw_table を取得
  - 仕様フォーマットで tables_preview.txt に出力

注意: 新規関数の追加禁止。既存呼び出しのみ使用。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import csv
import re
import pdfplumber
from pathlib import Path

from src.analysis.segment_detection_v2 import (
    run_segment_detection_v2,
    _norm_text,
)

ARCHIVE_DIR  = Path(__file__).parent.parent.parent / "data" / "xbrl_archive"
EVAL_DIR     = Path(__file__).parent.parent.parent / "data" / "eval"
TARGETS_CSV  = EVAL_DIR / "detailed_eval_targets.csv"
PREVIEW_TXT  = EVAL_DIR / "tables_preview.txt"

SEP = "=" * 50

# ── raw_table の整形出力 ──────────────────────────────────────────
def format_raw_table(raw_table, max_rows=12):
    """raw_table を列幅揃えしたテキストに整形する。"""
    if not raw_table:
        return "  (なし)"
    # 正規化済みセル取得
    grid = [[(_norm_text(c) or "None") for c in row] for row in raw_table]
    if not grid:
        return "  (なし)"
    # 列幅計算
    n_cols = max(len(row) for row in grid)
    col_widths = [0] * n_cols
    for row in grid:
        for ci, cell in enumerate(row):
            if ci < n_cols:
                col_widths[ci] = max(col_widths[ci], min(len(cell), 22))
    lines = []
    for ri, row in enumerate(grid[:max_rows]):
        cells = []
        for ci in range(n_cols):
            cell = row[ci] if ci < len(row) else ""
            cell = cell[:22]  # 最大22文字
            cells.append(cell.ljust(col_widths[ci]))
        lines.append("  | " + " | ".join(cells) + " |")
    if len(raw_table) > max_rows:
        lines.append(f"  ... (+{len(raw_table) - max_rows} rows)")
    return "\n".join(lines)

# ── pdfplumber からセグメントページの raw_table を取得 ─────────────
def get_best_raw_table(pdf_path, segments, page_no_hint):
    """
    pdfplumber で pdf を開き、抽出済みセグメント名を手がかりに
    最も一致する raw_table を返す。

    1. page_no_hint ページを優先して探す
    2. 見つからなければ全ページ（最大15ページ）をスキャン
    3. セグメント名が最も多く含まれるテーブルを返す
    """
    seg_names = set()
    for seg in segments:
        name = _norm_text(seg.segment_name_raw or seg.segment_name or "")
        if name and len(name) >= 2:
            seg_names.add(name)
        # ヘッダー由来なら短い部分文字列でもヒットする
        for token in re.split(r'[\s　]+', name):
            if len(token) >= 2:
                seg_names.add(token)

    if not seg_names:
        # セグメント名なし → 最大行数テーブルを返す fallback
        try:
            with pdfplumber.open(str(pdf_path)) as pdf:
                best_tbl, best_len = None, 0
                for page in pdf.pages[:15]:
                    for tbl in (page.extract_tables() or []):
                        if len(tbl) > best_len:
                            best_len, best_tbl = len(tbl), tbl
                return best_tbl
        except Exception:
            return None

    def score_table(tbl):
        """テーブル内にセグメント名がいくつ含まれるかを返す。"""
        score = 0
        for row in tbl:
            for cell in row:
                ct = _norm_text(cell) or ""
                for name in seg_names:
                    if name in ct or ct in name:
                        score += 1
        return score

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            n_pages = len(pdf.pages)
            # page_no_hint を先頭に、残りを後ろに並べる
            if page_no_hint is not None and 0 <= page_no_hint < n_pages:
                page_order = [page_no_hint] + [p for p in range(min(n_pages, 15)) if p != page_no_hint]
            else:
                page_order = list(range(min(n_pages, 15)))

            best_tbl, best_score = None, 0
            for pg_idx in page_order:
                tbls = pdf.pages[pg_idx].extract_tables() or []
                for tbl in tbls:
                    if len(tbl) < 2:
                        continue
                    sc = score_table(tbl)
                    if sc > best_score:
                        best_score, best_tbl = sc, tbl
                # page_no_hint ページで既にスコアが高ければそこで止める
                if pg_idx == page_no_hint and best_score >= len(seg_names):
                    break
            return best_tbl
    except Exception:
        return None

# ── トレースから header / metric 候補を抽出 ───────────────────────
def extract_header_candidates(rule_trace):
    """
    F-col トレースから seg_cols と hdr 行インデックスを取得する。
    例: 'col_as_seg: DETECTED hdr=1 seg_cols=[1,2,3] ...'
    """
    for t in rule_trace:
        m = re.search(r'hdr=(\d+)\s+seg_cols=(\[[^\]]*\])', t)
        if m:
            return {"hdr_row": int(m.group(1)), "seg_cols": m.group(2)}
    return None

def extract_metric_candidates(rule_trace):
    """
    F-col トレースから sales_row / profit_row を取得する。
    例: 'col_as_seg: DETECTED ... sales_row=2 profit_row=4'
    """
    for t in rule_trace:
        ms = re.search(r'sales_row=(\d+|None)', t)
        mp = re.search(r'profit_row=(\d+|None)', t)
        if ms or mp:
            return {
                "sales_row":  ms.group(1) if ms else "None",
                "profit_row": mp.group(1) if mp else "None",
            }
    return None

# ── メイン処理 ───────────────────────────────────────────────────
def main():
    # 対象CSV読み込み
    targets = []
    with open(TARGETS_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            targets.append({
                "pdf":                row.get("pdf", "").strip(),
                "ticker":             row.get("ticker", "?").strip(),
                "expected_table_type": row.get("expected_table_type", "").strip(),
            })

    print(f"評価対象: {len(targets)} 件")

    with open(PREVIEW_TXT, "w", encoding="utf-8") as out:
        out.write(f"# tables_preview.txt — {len(targets)} 件\n\n")

        for t in targets:
            pdf_name = t["pdf"]
            pdf_path = ARCHIVE_DIR / pdf_name
            print(f"  {pdf_name} ... ", end="", flush=True)

            out.write(SEP + "\n")
            out.write(f"PDF: {pdf_name}\n")
            out.write(f"ticker: {t['ticker']}\n")
            out.write(f"expected_table_type: {t['expected_table_type']}\n")
            out.write("\n")

            # ── 検出実行 ────────────────────────────────────────────
            try:
                result = run_segment_detection_v2(
                    str(pdf_path), doc_id=pdf_name, ticker="?"
                )
            except Exception as e:
                print(f"ERROR: {e}")
                out.write(f"detected_mode: error\n")
                out.write(f"quarantine_reason: {e}\n\n")
                out.write(SEP + "\n\n")
                continue

            # mode 判定
            fcol_traces = [t2 for t2 in result.rule_trace if "F-col" in t2]
            if result.success and any("SUCCESS" in t2 for t2 in fcol_traces):
                mode = "COL_AS_SEG"
            elif result.success:
                mode = "ROW_BASED"
            else:
                mode = "quarantine"

            # page_number / unit_text
            page_no = None
            unit_text = ""
            if result.segments:
                prov = result.segments[0].provenance or {}
                page_no = prov.get("page_no")
            unit_from_trace = next(
                (t2 for t2 in result.rule_trace if t2.startswith("Unit:")), ""
            )
            if unit_from_trace:
                unit_text = unit_from_trace
            elif result.segments:
                unit_text = result.segments[0].unit_raw or ""

            # parse_quality 集計
            pq_counts = {}
            for seg in result.segments:
                pq_counts[seg.parse_quality] = pq_counts.get(seg.parse_quality, 0) + 1
            pq_summary = ", ".join(f"{k}:{v}" for k, v in pq_counts.items()) if pq_counts else "None"

            out.write(f"detected_mode: {mode}\n")
            out.write(f"parse_quality: {pq_summary}\n")
            out.write(f"quarantine_reason: {result.quarantine_reason or 'None'}\n")
            out.write("\n")
            out.write(f"page_number: {page_no if page_no is not None else 'None'}\n")
            out.write(f"unit: {unit_text or 'None'}\n")
            out.write("\n")

            # ── RAW TABLE 取得 + 表示 ───────────────────────────────
            out.write("[RAW TABLE]\n")
            raw_table = get_best_raw_table(pdf_path, result.segments, page_no)
            out.write(format_raw_table(raw_table) + "\n")
            out.write("\n")

            # ── HEADER CANDIDATES ──────────────────────────────────
            out.write("[HEADER CANDIDATES]\n")
            hdr_info = extract_header_candidates(result.rule_trace)
            if hdr_info:
                out.write(f"  hdr_row={hdr_info['hdr_row']}  seg_cols={hdr_info['seg_cols']}\n")
                # raw_table のヘッダー行を表示
                if raw_table and hdr_info['hdr_row'] < len(raw_table):
                    hdr_row_cells = [_norm_text(c) or "None" for c in raw_table[hdr_info['hdr_row']]]
                    out.write(f"  cells: {hdr_row_cells}\n")
            else:
                out.write("  (col_as_seg 非対象 または未検出)\n")
            out.write("\n")

            # ── METRIC ROW CANDIDATES ──────────────────────────────
            out.write("[METRIC ROW CANDIDATES]\n")
            met_info = extract_metric_candidates(result.rule_trace)
            if met_info:
                out.write(f"  sales_row={met_info['sales_row']}  profit_row={met_info['profit_row']}\n")
                # raw_table から該当行を表示
                if raw_table:
                    for key, label in [("sales_row", "売上行"), ("profit_row", "利益行")]:
                        ri_str = met_info.get(key, "None")
                        if ri_str != "None":
                            ri = int(ri_str)
                            if ri < len(raw_table):
                                cells = [(_norm_text(c) or "None")[:20] for c in raw_table[ri]]
                                out.write(f"  {label}[{ri}]: {cells}\n")
            else:
                out.write("  (情報なし)\n")
            out.write("\n")

            # ── EXTRACTED SEGMENTS ─────────────────────────────────
            out.write("[EXTRACTED SEGMENTS]\n")
            if result.segments:
                for seg in result.segments:
                    out.write(
                        f"  [{seg.segment_order}] {seg.segment_name}"
                        f"  sales={seg.segment_sales}"
                        f"  profit={seg.segment_profit}"
                        f"  pq={seg.parse_quality}\n"
                    )
            else:
                out.write("  (なし)\n")
            out.write("\n")
            out.write(SEP + "\n\n")

            seg_count = len(result.segments)
            print(f"mode={mode} segs={seg_count} page={page_no}")

    print(f"\n完了: {PREVIEW_TXT}")


if __name__ == "__main__":
    main()
