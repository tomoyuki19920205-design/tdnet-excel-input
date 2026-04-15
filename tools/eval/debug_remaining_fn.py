"""
debug_remaining_fn.py
残り FN 件（has_segment_table=yes かつ detected_mode=quarantine）について
各PDFの検出レイヤーごとの失敗原因を特定し fn_debug_report.md を出力する。

入力:
  data/eval/screening_sheet.csv  (has_segment_table の真値)
  data/eval/candidates.csv       (最新バッチの検出結果)
  data/xbrl_archive/*.pdf        (PDF 実体)
出力:
  data/eval/fn_debug_report.md
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import csv
import re
from pathlib import Path
from collections import Counter

from src.analysis.segment_detection_v2 import run_segment_detection_v2
from src.analysis.page_scoring import score_segment_page

PROJ = Path(__file__).parent.parent.parent
EVAL_DIR    = PROJ / "data" / "eval"
ARCHIVE_DIR = PROJ / "data" / "xbrl_archive"
REPORT_MD   = EVAL_DIR / "fn_debug_report.md"

# ── 入力読み込み ──────────────────────────────────────────────

ss = {}  # pdf -> has_segment_table
for r in csv.DictReader(open(EVAL_DIR / "screening_sheet.csv", encoding="utf-8-sig")):
    ss[r["pdf"].strip()] = r.get("has_segment_table", "").strip().lower()

cands = {}  # pdf -> row
for r in csv.DictReader(open(EVAL_DIR / "candidates.csv", encoding="utf-8-sig")):
    cands[r["pdf"].strip()] = r

# 現在の FN = has_segment_table=yes かつ detected_mode=quarantine
fn_pdfs = sorted(
    p for p in cands
    if ss.get(p, "") == "yes" and cands[p]["detected_mode"] == "quarantine"
)

print(f"FN 件数: {len(fn_pdfs)} 件")

# ── 原因分類カウンタ ──────────────────────────────────────────

cause_counter = Counter()

# ── レポート生成 ──────────────────────────────────────────────

lines = [
    "# FN 詳細デバッグレポート\n",
    f"対象件数: {len(fn_pdfs)} 件\n",
    "---\n",
]

for pdf_name in fn_pdfs:
    pdf_path = ARCHIVE_DIR / pdf_name
    print(f"  処理中: {pdf_name} ...", end="", flush=True)

    if not pdf_path.exists():
        lines.append(f"\n## {pdf_name}\n\n> PDF ファイルが見つかりません\n")
        cause_counter["pdf_not_found"] += 1
        print(" NOT FOUND")
        continue

    # segment_detection_v2 を実行して全トレースを取得
    try:
        result = run_segment_detection_v2(str(pdf_path), doc_id=pdf_name, ticker="?")
    except Exception as e:
        lines.append(f"\n## {pdf_name}\n\n> 実行エラー: {e}\n")
        cause_counter["exec_error"] += 1
        print(f" ERROR: {e}")
        continue

    trace = result.rule_trace
    ss_info = result.score_summary

    # ─ page scores ─
    all_page_scores_raw = ss_info.get("page_scores", [])
    all_ts_raw = ss_info.get("all_table_scores", [])

    # Phase A のトレースから page scores を再構築
    # (score_summary に page_scores がある場合 = no_segment_page_candidate の時のみ)
    # → 通常は run_segment_detection_v2 を呼んでいるので page_scoring で直接取得する
    import pdfplumber
    page_score_list = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf_obj:
            for i, page in enumerate(pdf_obj.pages[:15]):
                text = page.extract_text() or ""
                from src.analysis.page_scoring import score_segment_page
                ps = score_segment_page(text, i)
                page_score_list.append({"page": i, "score": round(ps.score, 3)})
    except Exception:
        page_score_list = []

    page_score_list_sorted = sorted(page_score_list, key=lambda x: x["score"], reverse=True)

    # ─ top_pages に入っているか ─
    TOP_PAGES = 8
    MIN_SCORE = 0.10
    top_page_candidates = [p for p in page_score_list_sorted if p["score"] >= MIN_SCORE][:TOP_PAGES]
    top_page_nos = {p["page"] for p in top_page_candidates}

    # ─ candidate guard 情報 ─
    cg = ss_info.get("candidate_guard", {})
    valid_seg    = cg.get("valid_segment_like", "?")
    bs_cf_like   = cg.get("bs_cf_like", "?")
    seg_name_rows = cg.get("segment_name_like_rows", "?")
    rejected_by  = cg.get("reject_reason", result.quarantine_reason)

    # ─ all_table_scores ─
    all_ts = all_ts_raw  # list of dicts

    # ─ swap トレース ─
    swap_trace = [t for t in trace if "swapped" in t]

    # ─ 原因推定 ─
    cause = "unknown"
    reason_detail = ""

    if not top_page_candidates:
        cause = "page_score_insufficient"
        reason_detail = f"全ページが min_page_score={MIN_SCORE} 未満"
    elif len(all_ts) == 0:
        # top_pages に入ったが table candidates ゼロ
        cause = "table_candidate_zero"
        reason_detail = "ページは候補入りしたが table_candidates = 0"
    elif len(all_ts) == 1:
        # 候補1件 → best_table がBS表で swap 不発
        cause = "single_candidate_bs_table"
        reason_detail = (
            f"all_table_candidates=1件のみ、bs_cf_like={bs_cf_like} valid_seg={valid_seg} "
            f"→ swap 対象なくそのまま bs_cf_guard"
        )
    else:
        # 複数候補あったが swap しなかった
        if swap_trace:
            cause = "swap_done_still_quarantine"
            reason_detail = f"swap 発動したが再度 bs_cf_guard で落ちた: {swap_trace[0]}"
        else:
            cause = "multi_candidate_no_swap"
            reason_detail = (
                f"複数候補あり({len(all_ts)}件)だが swap 条件不成立: "
                f"bs_cf_like={bs_cf_like} valid_seg={valid_seg}"
            )

    cause_counter[cause] += 1
    print(f" cause={cause}")

    # ─ レポート行 ─
    lines.append(f"\n## {pdf_name}\n")
    lines.append(f"- **quarantine_reason**: `{result.quarantine_reason}`\n")
    lines.append(f"- **推定原因カテゴリ**: `{cause}`\n")
    lines.append(f"- **推定原因詳細**: {reason_detail}\n")

    # page scores top10
    lines.append("\n### Page Scores (top 10)\n")
    lines.append("| page | score | in_top_pages |\n|---|---|---|\n")
    for ps in page_score_list_sorted[:10]:
        in_top = "✓" if ps["page"] in top_page_nos else ""
        lines.append(f"| {ps['page']} | {ps['score']} | {in_top} |\n")

    # table candidates
    lines.append(f"\n### Table Candidates ({len(all_ts)} 件)\n")
    if all_ts:
        lines.append("| page | score | segment_like | reason(抜粋) |\n|---|---|---|---|\n")
        for ts in all_ts[:8]:
            lines.append(
                f"| {ts.get('page','?')} | {ts.get('score','?')} "
                f"| {ts.get('categories',{}).get('segment_row','?')} "
                f"| {ts.get('reason','')[:60]} |\n"
            )
    else:
        lines.append("> table_candidates = 0（Phase B でフィルタアウト）\n")

    # candidate_guard detail
    lines.append(f"\n### Candidate Guard 詳細\n")
    lines.append(f"- valid_segment_like: {valid_seg}\n")
    lines.append(f"- bs_cf_like: {bs_cf_like}\n")
    lines.append(f"- segment_name_like_rows: {seg_name_rows}\n")
    lines.append(f"- reject_reason: `{rejected_by}`\n")

    if swap_trace:
        lines.append(f"\n### Swap\n```\n{swap_trace[0]}\n```\n")

    lines.append("\n---\n")

# ── 集計セクション ────────────────────────────────────────────

lines.append("\n## 原因内訳（全件）\n")
lines.append("| 原因カテゴリ | 件数 |\n|---|---|\n")
for cause, cnt in cause_counter.most_common():
    lines.append(f"| {cause} | {cnt} |\n")

# ── 改善優先順位 ─────────────────────────────────────────────

prio_map = {
    "single_candidate_bs_table": (
        "1. **Phase B の num_density フィルタを column_based 限定に緩和**  \n"
        "   → BS表しか候補に残らないのは density 除外で別候補が落ちているため。\n"
        "   `num_density < 0.3` の除外を col_seg_hits >= 2 のとき免除する。"
    ),
    "page_score_insufficient": (
        "1. **Phase A の `weak_table_page_fallback` 検出条件を緩和**  \n"
        "   → segrows >= 2 + repnum >= 1 で候補化するよう閾値を下げる。"
    ),
    "table_candidate_zero": (
        "1. **Phase B の `min_table_score` を 0.15 に下げる**  \n"
        "   → ページ候補入りしたがテーブルが残らないケースに対応。"
    ),
    "multi_candidate_no_swap": (
        "1. **swap 条件 `segment_like_rows >= 2` を 1 に緩和**  \n"
        "   → OR条件（account_like_rows < segment_like_rows）の方を優先させる。"
    ),
}

top_cause = cause_counter.most_common(1)[0][0] if cause_counter else "unknown"
prio_text = prio_map.get(top_cause, "1. 個別に調査が必要")

bs_cf_guard_prio = (
    "2. **Phase B: account_like_rows が多い場合のスコアペナルティを強化**  \n"
    "   → BS/CF表が best_table に選ばれにくくする（exclusion_penalty 追加）。"
)
swap_prio = (
    "3. **Phase B-1.9 swap: `segment_like_rows >= 1` に閾値を下げる**  \n"
    "   → 条件が厳しすぎる場合の緩和（第2候補でも1行あれば救済）。"
)

lines.append("\n## 改善優先順位\n")
lines.append(f"{prio_text}\n\n")
lines.append(f"{bs_cf_guard_prio}\n\n")
lines.append(f"{swap_prio}\n")

# ── 出力 ─────────────────────────────────────────────────────

REPORT_MD.write_text("".join(lines), encoding="utf-8")
print(f"\n完了: {REPORT_MD}")
print(f"\n原因内訳: {dict(cause_counter.most_common())}")
