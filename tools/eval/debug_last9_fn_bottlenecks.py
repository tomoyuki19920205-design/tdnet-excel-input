"""
debug_last9_fn_bottlenecks.py
残り FN（最新 candidates.csv × screening_sheet.csv）について
bs_cf_ratio / segment_name_like_rows どちらがボトルネックかを切り分ける。

出力:
  data/eval/fn_last9_bottlenecks.csv
  data/eval/fn_last9_bottlenecks.md
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import csv
from pathlib import Path
from collections import Counter

from src.analysis.segment_detection_v2 import run_segment_detection_v2

PROJ        = Path(__file__).parent.parent.parent
EVAL_DIR    = PROJ / "data" / "eval"
ARCHIVE_DIR = PROJ / "data" / "xbrl_archive"
OUT_CSV     = EVAL_DIR / "fn_last9_bottlenecks.csv"
OUT_MD      = EVAL_DIR / "fn_last9_bottlenecks.md"

BS_CF_RATIO_THRESHOLD_CURRENT = 0.15  # 現行値

# ── 入力 ─────────────────────────────────────────────────────

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
print(f"FN 件数: {len(fn_pdfs)} 件")

# ── 各PDF を再実行して candidate_guard 内部値を取得 ──────────

CSV_COLS = [
    "pdf", "ticker", "quarantine_reason",
    "b", "total", "bs_cf_ratio",
    "segment_name_like_rows", "valid_segment_like", "header_keyword_hits",
    "ratio_would_pass_if_020", "ratio_would_pass_if_025",
    "name_rows_would_pass_if_ge2",
    "primary_bottleneck", "notes",
]

rows_out = []

for pdf_name in fn_pdfs:
    pdf_path = ARCHIVE_DIR / pdf_name
    print(f"  {pdf_name} ...", end="", flush=True)

    if not pdf_path.exists():
        rows_out.append({
            "pdf": pdf_name, "ticker": "?",
            "quarantine_reason": "pdf_not_found",
            "b": "", "total": "", "bs_cf_ratio": "",
            "segment_name_like_rows": "", "valid_segment_like": "",
            "header_keyword_hits": "",
            "ratio_would_pass_if_020": "", "ratio_would_pass_if_025": "",
            "name_rows_would_pass_if_ge2": "",
            "primary_bottleneck": "other", "notes": "PDF not found",
        })
        print(" NOT FOUND")
        continue

    try:
        result = run_segment_detection_v2(str(pdf_path), doc_id=pdf_name, ticker="?")
    except Exception as e:
        rows_out.append({
            "pdf": pdf_name, "ticker": "?",
            "quarantine_reason": f"exec_error:{e}",
            "b": "", "total": "", "bs_cf_ratio": "",
            "segment_name_like_rows": "", "valid_segment_like": "",
            "header_keyword_hits": "",
            "ratio_would_pass_if_020": "", "ratio_would_pass_if_025": "",
            "name_rows_would_pass_if_ge2": "",
            "primary_bottleneck": "other", "notes": str(e)[:80],
        })
        print(f" ERROR")
        continue

    cg = result.score_summary.get("candidate_guard", {})
    b     = int(cg.get("bs_cf_like", 0) or 0)
    v     = int(cg.get("valid_segment_like", 0) or 0)
    snr   = int(cg.get("segment_name_like_rows", 0) or 0)
    hdr   = int(cg.get("header_keyword_hits", 0) or 0)
    qrn   = result.quarantine_reason or ""
    # total_rows = 全カテゴリ合計（score_summary に直接ないため算出）
    total = sum(
        int(cg.get(k, 0) or 0)
        for k in ("valid_segment_like", "narrative_like", "bs_cf_like",
                  "detail_breakdown_like", "total_or_metric_like",
                  "garbage_fragment_like", "pl_account_like", "unknown")
    )

    bs_cf_ratio = round(b / total, 4) if total > 0 else 0.0

    # 仮想判定: threshold を緩めたら通るか
    # 注: ratio チェック は b / total >= threshold のときトリガー
    #     かつ _bscf_triggered が True のまま _has_table_signal がなければ reject
    # ここでは ratio チェック起因かどうかを推定する
    ratio_trigger_current = (total > 0 and bs_cf_ratio >= BS_CF_RATIO_THRESHOLD_CURRENT)
    ratio_trigger_020     = (total > 0 and bs_cf_ratio >= 0.20)
    ratio_trigger_025     = (total > 0 and bs_cf_ratio >= 0.25)

    ratio_would_pass_020 = ratio_trigger_current and not ratio_trigger_020
    ratio_would_pass_025 = ratio_trigger_current and not ratio_trigger_025

    # name_rows 起因: 現行では snr<3 だが >=2 なら免除条件を満たすか
    # 現行免除: b <= b_limit and snr >= 3
    b_limit = 5 if hdr >= 1 else 3
    exempt_current = (b <= b_limit and snr >= 3)
    exempt_if_ge2  = (b <= b_limit and snr >= 2)
    name_rows_would_pass = (not exempt_current) and exempt_if_ge2

    # primary_bottleneck 判定
    ratio_is_cause   = ratio_trigger_current  # ratio チェックが発火している
    name_rows_is_cause = (snr < 3)             # name_rows 不足

    if ratio_is_cause and name_rows_is_cause:
        bottleneck = "both"
    elif ratio_is_cause:
        bottleneck = "ratio"
    elif name_rows_is_cause:
        bottleneck = "name_rows"
    else:
        bottleneck = "other"

    notes = (
        f"b={b} total={total} ratio={bs_cf_ratio:.3f} "
        f"snr={snr} v={v} hdr={hdr} b_limit={b_limit}"
    )
    print(f" bottleneck={bottleneck} {notes}")

    rows_out.append({
        "pdf": pdf_name, "ticker": "?",
        "quarantine_reason": qrn,
        "b": b, "total": total, "bs_cf_ratio": bs_cf_ratio,
        "segment_name_like_rows": snr,
        "valid_segment_like": v,
        "header_keyword_hits": hdr,
        "ratio_would_pass_if_020": "yes" if ratio_would_pass_020 else "no",
        "ratio_would_pass_if_025": "yes" if ratio_would_pass_025 else "no",
        "name_rows_would_pass_if_ge2": "yes" if name_rows_would_pass else "no",
        "primary_bottleneck": bottleneck,
        "notes": notes,
    })

# ── CSV 出力 ─────────────────────────────────────────────────

with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=CSV_COLS)
    w.writeheader()
    w.writerows(rows_out)

# ── MD レポート ──────────────────────────────────────────────

bc = Counter(r["primary_bottleneck"] for r in rows_out)
ratio_cnt    = bc.get("ratio", 0)
name_cnt     = bc.get("name_rows", 0)
both_cnt     = bc.get("both", 0)
other_cnt    = bc.get("other", 0)

# 優先順位マップ
PRIO_MAP = {
    "ratio": (
        "**bs_cf_ratio_threshold を 0.20〜0.25 に緩和**  \n"
        "   `b / total >= threshold` の閾値を条件付き（hdr >= 1 のとき限定）で引き上げる。"
    ),
    "name_rows": (
        "**_bscf_light_exempt の segment_name_like_rows 基準を 3→2 に緩和**  \n"
        "   `snr >= 2` でも免除できるよう1段下げる（ratio 条件は維持）。"
    ),
    "both": (
        "**ratio と name_rows を同時緩和（条件付き）**  \n"
        "   hdr >= 1 かつ snr >= 2 かつ ratio < 0.25 を免除要件とする複合条件を追加。"
    ),
    "other": (
        "**個別 PDF 調査**  \n"
        "   上記いずれにも分類されないケースは trace ログを手動確認する。"
    ),
}
top3 = [c for c, _ in bc.most_common()] + ["other", "other", "other"]
prio1 = PRIO_MAP.get(top3[0], "個別調査")
prio2 = PRIO_MAP.get(top3[1] if top3[1] != top3[0] else "other", "個別調査")
prio3 = "**Phase A/B の候補ページ・テーブル選択を調査**  \n   上記で解消しない FN はページスコアリング層の問題。"

md_lines = [
    "# FN 残り9件 ボトルネック分析\n\n",
    f"対象件数: {len(rows_out)} 件\n\n",
    "---\n\n",
    "## 1. 対象一覧\n\n",
    "| pdf | b | total | ratio | snr | hdr | bottleneck |\n",
    "|---|---|---|---|---|---|---|\n",
]
for r in rows_out:
    md_lines.append(
        f"| {r['pdf']} | {r['b']} | {r['total']} | {r['bs_cf_ratio']} "
        f"| {r['segment_name_like_rows']} | {r['header_keyword_hits']} "
        f"| **{r['primary_bottleneck']}** |\n"
    )

md_lines += [
    "\n---\n\n",
    "## 2. primary_bottleneck 別件数\n\n",
    "| bottleneck | 件数 |\n|---|---|\n",
    f"| ratio     | {ratio_cnt} |\n",
    f"| name_rows | {name_cnt} |\n",
    f"| both      | {both_cnt} |\n",
    f"| other     | {other_cnt} |\n",
    "\n---\n\n",
    "## 3. ratio_would_pass 内訳\n\n",
    "| 条件 | 通過できる件数 |\n|---|---|\n",
    f"| threshold=0.20 に緩和 | {sum(1 for r in rows_out if r['ratio_would_pass_if_020']=='yes')} |\n",
    f"| threshold=0.25 に緩和 | {sum(1 for r in rows_out if r['ratio_would_pass_if_025']=='yes')} |\n",
    f"| name_rows>=2 に緩和   | {sum(1 for r in rows_out if r['name_rows_would_pass_if_ge2']=='yes')} |\n",
    "\n---\n\n",
    "## 4. 次の修正優先順位\n\n",
    f"1. {prio1}\n\n",
    f"2. {prio2}\n\n",
    f"3. {prio3}\n",
]

OUT_MD.write_text("".join(md_lines), encoding="utf-8")
print(f"\n完了: {OUT_CSV}")
print(f"完了: {OUT_MD}")
print(f"\nbottleneck 内訳: ratio={ratio_cnt} name_rows={name_cnt} both={both_cnt} other={other_cnt}")
