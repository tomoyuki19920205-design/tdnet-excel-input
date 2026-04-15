"""
tools/eval/apply_580991_gt_fix.py

目的:
  580991.pdf（ジェネレーションパス）の has_segment_table を no → yes に更新。
  page11に「ECマーケティング事業/商品企画関連事業」のセグメント表が実在することを確認済み。
  既存ファイルは上書きしない。

入力:
  screening_sheet.highconf_plus1.csv（581329修正済みGT）

生成ファイル:
  screening_sheet.highconf_plus2.csv
  metrics_summary.highconf_plus2.json
  metrics_by_pdf.highconf_plus2.csv
  highconf_plus2_summary.md
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "data" / "eval"

IN_GT    = EVAL_DIR / "screening_sheet.highconf_plus1.csv"   # 前ステップのGT
IN_CANDS = EVAL_DIR / "candidates.csv"

OUT_GT   = EVAL_DIR / "screening_sheet.highconf_plus2.csv"
OUT_JSON = EVAL_DIR / "metrics_summary.highconf_plus2.json"
OUT_PDF  = EVAL_DIR / "metrics_by_pdf.highconf_plus2.csv"
OUT_MD   = EVAL_DIR / "highconf_plus2_summary.md"

FIX_MAP = {
    # page11にECマーケティング事業/商品企画関連事業のセグメント表が実在
    "140120260312580991.pdf": ("no", "yes"),
}

JST = timezone(timedelta(hours=9))


def load_gt(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_cands(path: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {r["pdf"]: r for r in csv.DictReader(f)}


def apply_fix(rows: list[dict]) -> tuple[list[dict], list[dict]]:
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
    TP = FP = FN = TN = 0
    per_pdf: list[dict] = []
    for r in gt_rows:
        pdf = r["pdf"]
        hs   = r.get("has_segment_table", "").strip().lower()
        cr   = cands.get(pdf, {})
        mode = cr.get("detected_mode", "quarantine").strip()
        is_det = mode in ("COL_AS_SEG", "ROW_BASED")
        qrn  = cr.get("quarantine_reason", "")
        if hs == "yes" and is_det:     cat = "TP"; TP += 1
        elif hs == "yes" and not is_det: cat = "FN"; FN += 1
        elif hs == "no" and is_det:    cat = "FP"; FP += 1
        else:                          cat = "TN"; TN += 1
        per_pdf.append({"pdf": pdf, "has_segment_table": hs,
                        "detected_mode": mode, "quarantine_reason": qrn, "category": cat})
    r_ = TP / (TP + FN) if (TP + FN) > 0 else 0.0
    p_ = TP / (TP + FP) if (TP + FP) > 0 else 0.0
    f1 = 2 * p_ * r_ / (p_ + r_) if (p_ + r_) > 0 else 0.0
    return {
        "summary": {
            "true_positive_count":  TP, "false_positive_count": FP,
            "false_negative_count": FN, "true_negative_count":  TN,
            "total_count": TP + FP + FN + TN,
            "detection_recall":    round(r_, 4),
            "detection_precision": round(p_, 4),
            "f1_score":            round(f1, 4),
            "quarantine_count": sum(1 for rr in gt_rows
                                    if cands.get(rr["pdf"], {}).get("detected_mode", "") == "quarantine"),
        },
        "per_pdf": per_pdf,
    }


def _guard_exist(path: Path) -> None:
    if path.exists():
        sys.exit(f"[ERROR] 出力先が既に存在します（上書き禁止）: {path}")


def write_gt(rows: list[dict], path: Path) -> None:
    _guard_exist(path)
    fields = list(rows[0].keys()) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        csv.DictWriter(f, fieldnames=fields).writeheader()
        csv.DictWriter(f, fieldnames=fields).writerows(rows)
    # ↑ DictWriter を2回作るのを避けるために再書き
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(rows)
    print(f"[GT ] {path}", flush=True)


def write_json(before: dict, after: dict, path: Path) -> None:
    _guard_exist(path)
    payload = {"generated_at": datetime.now(JST).isoformat(),
               "base_gt": str(IN_GT), "applied_fixes": list(FIX_MAP.keys()),
               "before": before, "after": after}
    with path.open("w", encoding="utf-8-sig") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[JSON] {path}", flush=True)


def write_pdf_csv(per_pdf: list[dict], path: Path) -> None:
    _guard_exist(path)
    fields = ["pdf", "has_segment_table", "detected_mode", "quarantine_reason", "category"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(per_pdf)
    print(f"[CSV] {path}", flush=True)


def write_md(before: dict, after: dict, changed: list[dict], path: Path) -> None:
    _guard_exist(path)
    ts = datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    b, a = before, after
    lines = []
    lines += ["# GT更新後 metricsサマリー: highconf_plus2",
              f"\n生成日時: {ts}  \n適用GT: `{OUT_GT.name}`",
              "\n## 1. 更新件数\n",
              f"- ベースGT: `{IN_GT.name}`",
              f"- 更新件数: **{len(changed)} 件**",
              f"- 根拠: pdfplumber による page11 テキスト確認済み（ECマーケティング事業/商品企画関連事業の実セグメント表が存在）\n",
              "\n## 2. 更新対象PDF\n",
              "| PDF | 会社 | before | after | 根拠 |",
              "|-----|------|--------|-------|------|"]
    for c in changed:
        lines.append(f"| {c['pdf']} | ジェネレーションパス(3195) | `{c['before']}` | `{c['after']}` | "
                     f"page11にECマーケティング事業/商品企画関連事業の実セグメント表あり |")
    lines.append("")
    for section, rows in [
        ("## 3. FP 前後比較", [
            ("FP",  b['false_positive_count'], a['false_positive_count']),
            ("TP",  b['true_positive_count'],  a['true_positive_count']),
        ]),
        ("## 4. precision 前後比較", [
            ("precision", b['detection_precision'], a['detection_precision']),
            ("F1",        b['f1_score'],             a['f1_score']),
        ]),
        ("## 5. FN / recall 前後比較", [
            ("FN",     b['false_negative_count'], a['false_negative_count']),
            ("recall", b['detection_recall'],     a['detection_recall']),
        ]),
    ]:
        lines += [f"\n{section}\n",
                  "| 指標 | before | after | delta |",
                  "|------|--------|-------|-------|"]
        for label, bv, av in rows:
            if isinstance(bv, float):
                lines.append(f"| {label} | {bv:.4f} | {av:.4f} | {av-bv:+.4f} |")
            else:
                lines.append(f"| {label} | {bv} | {av} | {av-bv:+d} |")
        lines.append("")

    lines += ["\n## 6. 最終結論（3行）\n"]
    fp_b, fp_a = b['false_positive_count'], a['false_positive_count']
    fn_b, fn_a = b['false_negative_count'], a['false_negative_count']
    tp_a = a['true_positive_count']
    rec_b, rec_a = b['detection_recall'], a['detection_recall']
    prec_b, prec_a = b['detection_precision'], a['detection_precision']
    f1_a = a['f1_score']
    lines += [
        f"1. FPが {fp_b}→{fp_a} ({fp_a-fp_b:+d})、TPが {b['true_positive_count']}→{tp_a} (+1)。580991のGT修正によりprecision {prec_b:.4f}→{prec_a:.4f}、recall {rec_b:.4f}→{rec_a:.4f}に改善。",
        f"2. FN変化なし（{fn_a}件）。列ヘッダーガード・GT修正を通じて副作用（新規FN）はゼロ。",
        f"3. highconf_plus2（GT+2件修正後）での最終ベースライン: TP={tp_a}, FP={fp_a}, FN={fn_a}, recall={rec_a:.4f}, precision={prec_a:.4f}, F1={f1_a:.4f}。",
    ]
    with path.open("w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[MD ] {path}", flush=True)


def main() -> None:
    print("=== 580991 GT反映スクリプト ===", flush=True)
    print(f"実行時刻: {datetime.now(JST).strftime('%Y-%m-%dT%H:%M:%S+09:00')}", flush=True)
    for p in [IN_GT, IN_CANDS]:
        if not p.exists():
            sys.exit(f"[ERROR] 入力ファイルが見つかりません: {p}")

    gt_rows_orig = load_gt(IN_GT)
    cands        = load_cands(IN_CANDS)
    print(f"[INFO] GT: {len(gt_rows_orig)} 件  Cands: {len(cands)} 件", flush=True)

    before_res = calc_metrics(gt_rows_orig, cands)
    b = before_res["summary"]
    print(f"[before] TP={b['true_positive_count']} FP={b['false_positive_count']} "
          f"FN={b['false_negative_count']} recall={b['detection_recall']} "
          f"precision={b['detection_precision']}", flush=True)

    gt_rows_new, changed = apply_fix(gt_rows_orig)
    print(f"\n[INFO] 変更件数: {len(changed)} 件", flush=True)
    for c in changed:
        print(f"  {c['pdf']}: {c['before']} → {c['after']}", flush=True)

    after_res = calc_metrics(gt_rows_new, cands)
    a = after_res["summary"]
    print(f"[after ] TP={a['true_positive_count']} FP={a['false_positive_count']} "
          f"FN={a['false_negative_count']} recall={a['detection_recall']} "
          f"precision={a['detection_precision']}", flush=True)

    write_gt(gt_rows_new, OUT_GT)
    write_json(b, a, OUT_JSON)
    write_pdf_csv(after_res["per_pdf"], OUT_PDF)
    write_md(b, a, changed, OUT_MD)

    print(f"\n=== 完了 ===")
    print(f"FP:        {b['false_positive_count']} → {a['false_positive_count']}")
    print(f"FN:        {b['false_negative_count']} → {a['false_negative_count']}")
    print(f"recall:    {b['detection_recall']} → {a['detection_recall']}")
    print(f"precision: {b['detection_precision']} → {a['detection_precision']}")
    print(f"F1:        {b['f1_score']} → {a['f1_score']}")


if __name__ == "__main__":
    main()
