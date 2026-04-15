"""
Step 5: 評価レポート生成スクリプト
metrics_summary.json / metrics_by_pdf.csv / failure_cases.csv から
evaluation_report.md を生成する。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import json, csv
from pathlib import Path

EVAL_DIR     = Path(__file__).parent.parent.parent / "data" / "eval"
METRICS_JSON = EVAL_DIR / "metrics_summary.json"
BY_PDF_CSV   = EVAL_DIR / "metrics_by_pdf.csv"
FAIL_CSV     = EVAL_DIR / "failure_cases.csv"
REPORT_MD    = EVAL_DIR / "evaluation_report.md"

with open(METRICS_JSON, encoding="utf-8") as f:
    m = json.load(f)

pdf_rows = []
with open(BY_PDF_CSV, encoding="utf-8-sig") as f:
    pdf_rows = list(csv.DictReader(f))

fail_rows = []
if FAIL_CSV.exists():
    with open(FAIL_CSV, encoding="utf-8-sig") as f:
        fail_rows = list(csv.DictReader(f))

def pct(v):
    return f"{v*100:.1f}%" if v is not None else "N/A"

def _mode_row(mode, stats):
    if not stats.get("count"):
        return f"| {mode} | 0 | - | - | - | - |"
    return (
        f"| {mode} | {stats['count']} "
        f"| {pct(stats.get('strict_success_rate'))} "
        f"| {pct(stats.get('name_success_rate'))} "
        f"| {pct(stats.get('sales_success_rate'))} "
        f"| {pct(stats.get('profit_success_rate'))} |"
    )

lines = []
A = m["A_detection"]
B = m["B_detailed_eval"]
C = m["C_segment_names"]
D = m["D_values"]
E = m["E_by_mode"]
F = m["F_failure_taxonomy"]

lines += [
    "# セグメント抽出精度 評価レポート",
    "",
    "## 1. 評価目的",
    "",
    "`segment_detection_v2.py` の実際の決算短信 PDF に対する性能を",
    "検出性能・抽出性能の2軸で定量評価する。",
    "COL_AS_SEG と ROW_BASED を分けて評価し改善優先度を明確にする。",
    "",
    "---",
    "",
    "## 2. 評価対象",
    "",
    f"| 区分 | 件数 |",
    f"|---|---|",
    f"| 全対象 PDF (xbrl_archive) | {A.get('total_pdf_count', '?')} 件 |",
    f"| セグメント表あり（has_segment_table=yes） | {A.get('has_segment_table_yes', '未記入')} 件 |",
    f"| セグメント表なし | {A.get('has_segment_table_no', '未記入')} 件 |",
    f"| 詳細評価対象 | {B.get('total_pdf_count', '?')} 件 |",
    "",
    "---",
    "",
    "## 3. A. 検出評価（全件）",
    "",
    f"| 指標 | 値 |",
    f"|---|---|",
    f"| 検出成功数 | {A.get('detected_success_count', 'N/A')} 件 |",
    f"| false negative（見逃し）| {A.get('false_negative_count', 'N/A')} 件 |",
    f"| 検出 recall | {pct(A.get('detection_recall'))} |",
    "",
    "> `has_segment_table` 列が未記入の場合は N/A。",
    "",
    "---",
    "",
    "## 4. B. 詳細抽出評価（約20件）",
    "",
    "### 全体成功率",
    "",
    f"| 指標 | 値 |",
    f"|---|---|",
    f"| 評価 PDF 数 | {B.get('total_pdf_count', '?')} 件 |",
    f"| PDF完全一致（strict success） | {B.get('strict_success_count', '?')} 件 ({pct(B.get('strict_success_rate'))}) |",
    f"| セグメント名 success率 | {pct(B.get('name_success_rate'))} |",
    f"| 売上 success率 | {pct(B.get('sales_success_rate'))} |",
    f"| 利益 success率 | {pct(B.get('profit_success_rate'))} |",
    "",
    "### mode別成功率",
    "",
    "| mode | 件数 | strict_success | name_success | sales_success | profit_success |",
    "|---|---|---|---|---|---|",
    _mode_row("COL_AS_SEG", E.get("COL_AS_SEG", {})),
    _mode_row("ROW_BASED",  E.get("ROW_BASED",  {})),
    _mode_row("quarantine", E.get("quarantine", {})),
    "",
    "---",
    "",
    "## 5. C. セグメント名精度",
    "",
    f"| 指標 | 値 |",
    f"|---|---|",
    f"| precision | {pct(C.get('precision'))} (TP={C.get('tp')} FP={C.get('fp')}) |",
    f"| recall    | {pct(C.get('recall'))} (FN={C.get('fn')}) |",
    "",
    "---",
    "",
    "## 6. D. 数値精度",
    "",
    f"| 指標 | 一致数 / 対象数 | 一致率 |",
    f"|---|---|---|",
    f"| 売上（sales）| {D.get('sales_matched', '?')} / {D.get('sales_total', '?')} | {pct(D.get('sales_match_rate'))} |",
    f"| 利益（profit）| {D.get('profit_matched', '?')} / {D.get('profit_total', '?')} | {pct(D.get('profit_match_rate'))} |",
    "",
    "> 一致条件: `|ex - gt| <= max(50, |gt| * 0.05)`（百万円単位）",
    "",
    "---",
    "",
    "## 7. 代表的失敗事例",
    "",
]

# 失敗事例（先頭5件）
if fail_rows:
    lines += ["| PDF | GT type | mode | 失敗理由 |", "|---|---|---|---|"]
    for r in fail_rows[:5]:
        lines.append(f"| {r['pdf']} | {r['gt_table_type']} | {r['detected_mode']} | {r['failure_reasons']} |")
else:
    lines.append("なし。")

lines += [
    "",
    "---",
    "",
    "## 8. Failure Taxonomy",
    "",
    "| 失敗理由 | 件数 |",
    "|---|---|",
]
for item in F:
    lines.append(f"| {item['reason']} | {item['count']} |")

# ---- 改善優先度 (自動生成) ----
priority = []
if E.get("quarantine", {}).get("count", 0) > 0:
    priority.append(("quarantine 過多", "high", "セグメント表あり PDF の quarantine を削減"))
if C.get("fp", 0) > 0:
    priority.append(("セグメント名余計な混入（precision 不足）", "high", "除外ラベルの強化"))
if C.get("fn", 0) > 0:
    priority.append(("セグメント名の漏れ（recall 不足）", "medium", "ヘッダー行検出の強化"))
sales_rate = D.get("sales_match_rate")
if sales_rate and sales_rate < 0.9:
    priority.append(("売上一致率の改善", "medium", "売上行選択ロジックの見直し"))
profit_rate = D.get("profit_match_rate")
if profit_rate and profit_rate < 0.9:
    priority.append(("利益一致率の改善", "medium", "利益行選択ロジックの見直し"))

lines += [
    "",
    "---",
    "",
    "## 9. 改善優先順位",
    "",
    "| 優先度 | 項目 | 理由 |",
    "|---|---|---|",
]
for i, (item, level, reason) in enumerate(priority, 1):
    lines.append(f"| {i} ({level}) | {item} | {reason} |")
if not priority:
    lines.append("| - | 特に優先課題なし | - |")

REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
print(f"完了: {REPORT_MD}")
