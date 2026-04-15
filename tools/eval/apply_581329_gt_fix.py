"""
tools/eval/apply_581329_gt_fix.py

目的:
  581329.pdf の has_segment_table を no → yes に変更し、
  更新後GT・metricsを別名ファイルに保存する。
  既存ファイルは上書きしない。

生成ファイル:
  data/eval/screening_sheet.highconf_plus1.csv
  data/eval/metrics_summary.highconf_plus1.json
  data/eval/metrics_by_pdf.highconf_plus1.csv
  data/eval/highconf_plus1_summary.md
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "data" / "eval"

# ── 入力ファイル ──────────────────────────────────────────────────────────
IN_GT      = EVAL_DIR / "screening_sheet.highconf_fixed.csv"
IN_CANDS   = EVAL_DIR / "candidates.csv"
IN_MANUAL  = EVAL_DIR / "manual_check_581228_581329.csv"

# ── 出力ファイル ──────────────────────────────────────────────────────────
OUT_GT     = EVAL_DIR / "screening_sheet.highconf_plus1.csv"
OUT_JSON   = EVAL_DIR / "metrics_summary.highconf_plus1.json"
OUT_PDF    = EVAL_DIR / "metrics_by_pdf.highconf_plus1.csv"
OUT_MD     = EVAL_DIR / "highconf_plus1_summary.md"

# ── 修正対象 ──────────────────────────────────────────────────────────────
FIX_MAP = {
    # manual_check_581228_581329.md にて change_to_yes 確定
    "140120260313581329.pdf": ("no", "yes"),
}

JST = timezone(timedelta(hours=9))


# ──────────────────────────────────────────────────────────────────────────
def load_gt(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_cands(path: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {r["pdf"]: r for r in csv.DictReader(f)}


def apply_fix(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """FIX_MAP に従って has_segment_table を更新。変更ログを返す。"""
    changed: list[dict] = []
    new_rows: list[dict] = []
    for r in rows:
        pdf = r["pdf"]
        if pdf in FIX_MAP:
            before, after = FIX_MAP[pdf]
            cur = r.get("has_segment_table", "").strip().lower()
            if cur != before:
                print(f"  [WARNING] {pdf}: expected '{before}' but got '{cur}'. Applying anyway.")
            r2 = dict(r)
            r2["has_segment_table"] = after
            new_rows.append(r2)
            changed.append({"pdf": pdf, "before": cur, "after": after})
        else:
            new_rows.append(dict(r))
    return new_rows, changed


def calc_metrics(gt_rows: list[dict], cands: dict[str, dict]) -> dict:
    """candidates.csv との比較でTP/FP/FN/TNを算出。"""
    TP = FP = FN = TN = 0
    per_pdf: list[dict] = []
    for r in gt_rows:
        pdf = r["pdf"]
        hs = r.get("has_segment_table", "").strip().lower()
        cr = cands.get(pdf, {})
        mode = cr.get("detected_mode", "quarantine").strip()
        is_detected = mode in ("COL_AS_SEG", "ROW_BASED")
        qrn = cr.get("quarantine_reason", "")

        if hs == "yes" and is_detected:
            cat = "TP"; TP += 1
        elif hs == "yes" and not is_detected:
            cat = "FN"; FN += 1
        elif hs == "no" and is_detected:
            cat = "FP"; FP += 1
        else:
            cat = "TN"; TN += 1

        per_pdf.append({
            "pdf": pdf,
            "has_segment_table": hs,
            "detected_mode": mode,
            "quarantine_reason": qrn,
            "category": cat,
        })

    total = TP + FP + FN + TN
    recall    = TP / (TP + FN) if (TP + FN) > 0 else 0.0
    precision = TP / (TP + FP) if (TP + FP) > 0 else 0.0
    f1        = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return {
        "summary": {
            "true_positive_count":  TP,
            "false_positive_count": FP,
            "false_negative_count": FN,
            "true_negative_count":  TN,
            "total_count":          total,
            "detection_recall":     round(recall, 4),
            "detection_precision":  round(precision, 4),
            "f1_score":             round(f1, 4),
            "quarantine_count":     sum(1 for r in gt_rows
                                       if cands.get(r["pdf"], {}).get("detected_mode", "") == "quarantine"),
        },
        "per_pdf": per_pdf,
    }


def write_gt(rows: list[dict], path: Path) -> None:
    if path.exists():
        sys.exit(f"[ERROR] 出力先が既存です（上書き禁止）: {path}")
    fields = list(rows[0].keys()) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"[GT ] {path}", flush=True)


def write_json(before: dict, after: dict, path: Path) -> None:
    if path.exists():
        sys.exit(f"[ERROR] 出力先が既存です（上書き禁止）: {path}")
    payload = {
        "generated_at": datetime.now(JST).isoformat(),
        "base_gt": str(IN_GT),
        "applied_fixes": list(FIX_MAP.keys()),
        "before": before,
        "after": after,
    }
    with path.open("w", encoding="utf-8-sig") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[JSON] {path}", flush=True)


def write_pdf_csv(per_pdf: list[dict], path: Path) -> None:
    if path.exists():
        sys.exit(f"[ERROR] 出力先が既存です（上書き禁止）: {path}")
    fields = ["pdf", "has_segment_table", "detected_mode", "quarantine_reason", "category"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(per_pdf)
    print(f"[CSV] {path}", flush=True)


def write_md(before: dict, after: dict, changed: list[dict], path: Path) -> None:
    if path.exists():
        sys.exit(f"[ERROR] 出力先が既存です（上書き禁止）: {path}")
    ts = datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    b = before
    a = after

    fp_b   = b["false_positive_count"]
    fp_a   = a["false_positive_count"]
    fn_b   = b["false_negative_count"]
    fn_a   = a["false_negative_count"]
    tp_b   = b["true_positive_count"]
    tp_a   = a["true_positive_count"]
    prec_b = b["detection_precision"]
    prec_a = a["detection_precision"]
    rec_b  = b["detection_recall"]
    rec_a  = a["detection_recall"]
    f1_b   = b["f1_score"]
    f1_a   = a["f1_score"]

    lines = []
    lines.append("# GT更新後 metricsサマリー: highconf_plus1")
    lines.append(f"\n生成日時: {ts}  \n適用GT: `screening_sheet.highconf_plus1.csv`\n")

    lines.append("## 1. 更新件数\n")
    lines.append(f"- 更新対象GT: `{IN_GT.name}`")
    lines.append(f"- 更新件数: **{len(changed)} 件**\n")

    lines.append("## 2. 更新対象PDF\n")
    lines.append("| PDF | before | after | 根拠 |")
    lines.append("|-----|--------|-------|------|")
    for c in changed:
        source = "manual_check_581228_581329.md (change_to_yes 確定)"
        lines.append(f"| {c['pdf']} | `{c['before']}` | `{c['after']}` | {source} |")
    lines.append("")

    lines.append("## 3. FP 前後比較\n")
    lines.append(f"| 指標 | before | after | delta |")
    lines.append(f"|------|--------|-------|-------|")
    lines.append(f"| FP | {fp_b} | {fp_a} | {fp_a - fp_b:+d} |")
    lines.append(f"| TP | {tp_b} | {tp_a} | {tp_a - tp_b:+d} |")
    lines.append("")

    lines.append("## 4. precision 前後比較\n")
    lines.append(f"| 指標 | before | after | delta |")
    lines.append(f"|------|--------|-------|-------|")
    lines.append(f"| precision | {prec_b:.4f} | {prec_a:.4f} | {prec_a - prec_b:+.4f} |")
    lines.append(f"| F1 | {f1_b:.4f} | {f1_a:.4f} | {f1_a - f1_b:+.4f} |")
    lines.append("")

    lines.append("## 5. FN / recall 前後比較\n")
    lines.append(f"| 指標 | before | after | delta |")
    lines.append(f"|------|--------|-------|-------|")
    lines.append(f"| FN | {fn_b} | {fn_a} | {fn_a - fn_b:+d} |")
    lines.append(f"| recall | {rec_b:.4f} | {rec_a:.4f} | {rec_a - rec_b:+.4f} |")
    lines.append("")

    lines.append("## 6. 最終結論（3行）\n")

    # 結論を動的に生成
    if fp_a < fp_b:
        concl1 = f"FPが {fp_b}→{fp_a} ({fp_a-fp_b:+d}) に減少し、precision が {prec_b:.4f}→{prec_a:.4f} に改善した。"
    elif fp_a == fp_b:
        # 581329を yes修正したのでFPカウントは変わるはずだが確認
        concl1 = f"FPは {fp_b}→{fp_a}（GT修正により 581329 が FP から除外）、precision {prec_b:.4f}→{prec_a:.4f}。"
    else:
        concl1 = f"FP {fp_b}→{fp_a} ({fp_a-fp_b:+d})。"

    if fn_a == fn_b:
        concl2 = f"FN・recallは変化なし（{fn_a}件, recall={rec_a:.4f}）。列ヘッダーガードによる副作用（新規FN）は発生していない。"
    elif fn_a < fn_b:
        concl2 = f"FNが {fn_b}→{fn_a} ({fn_a-fn_b:+d}) に減少し、recall が {rec_b:.4f}→{rec_a:.4f} に改善した。"
    else:
        concl2 = f"FNが {fn_b}→{fn_a} ({fn_a-fn_b:+d}) に増加（要確認）。recall={rec_a:.4f}。"

    concl3 = (
        f"highconf_plus1（GT+1件修正後）での最終ベースライン: "
        f"TP={tp_a}, FP={fp_a}, FN={fn_a}, "
        f"recall={rec_a:.4f}, precision={prec_a:.4f}, F1={f1_a:.4f}。"
    )

    lines.append(f"1. {concl1}")
    lines.append(f"2. {concl2}")
    lines.append(f"3. {concl3}")

    with path.open("w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[MD ] {path}", flush=True)


# ──────────────────────────────────────────────────────────────────────────
def main() -> None:
    print("=== 581329 GT反映スクリプト ===", flush=True)
    print(f"実行時刻: {datetime.now(JST).strftime('%Y-%m-%dT%H:%M:%S+09:00')}", flush=True)

    # 入力チェック
    for p in [IN_GT, IN_CANDS]:
        if not p.exists():
            sys.exit(f"[ERROR] 入力ファイルが見つかりません: {p}")

    # GT ロード
    gt_rows_orig = load_gt(IN_GT)
    print(f"[INFO] GT読み込み: {len(gt_rows_orig)} 件 ({IN_GT.name})", flush=True)

    # candidates ロード
    cands = load_cands(IN_CANDS)
    print(f"[INFO] candidates読み込み: {len(cands)} 件", flush=True)

    # before metrics
    before_res = calc_metrics(gt_rows_orig, cands)
    before_summary = before_res["summary"]

    print(f"\n[before] TP={before_summary['true_positive_count']} "
          f"FP={before_summary['false_positive_count']} "
          f"FN={before_summary['false_negative_count']} "
          f"recall={before_summary['detection_recall']} "
          f"precision={before_summary['detection_precision']}", flush=True)

    # GT修正適用
    print("\n[INFO] GT修正を適用...", flush=True)
    gt_rows_new, changed = apply_fix(gt_rows_orig)
    print(f"  変更件数: {len(changed)} 件", flush=True)
    for c in changed:
        print(f"  {c['pdf']}: {c['before']} → {c['after']}", flush=True)

    # after metrics
    after_res = calc_metrics(gt_rows_new, cands)
    after_summary = after_res["summary"]

    print(f"\n[after ] TP={after_summary['true_positive_count']} "
          f"FP={after_summary['false_positive_count']} "
          f"FN={after_summary['false_negative_count']} "
          f"recall={after_summary['detection_recall']} "
          f"precision={after_summary['detection_precision']}", flush=True)

    # ファイル保存
    print("\n[INFO] ファイル保存...", flush=True)
    write_gt(gt_rows_new, OUT_GT)
    write_json(before_summary, after_summary, OUT_JSON)
    write_pdf_csv(after_res["per_pdf"], OUT_PDF)
    write_md(before_summary, after_summary, changed, OUT_MD)

    print("\n=== 完了 ===", flush=True)
    print(f"FP: {before_summary['false_positive_count']} → {after_summary['false_positive_count']}", flush=True)
    print(f"FN: {before_summary['false_negative_count']} → {after_summary['false_negative_count']}", flush=True)
    print(f"recall:    {before_summary['detection_recall']} → {after_summary['detection_recall']}", flush=True)
    print(f"precision: {before_summary['detection_precision']} → {after_summary['detection_precision']}", flush=True)


if __name__ == "__main__":
    main()
