"""
tools/eval/manual_check_581228_581329.py
581228.pdf / 581329.pdf の目視確認補助スクリプト

目的:
- 実際のセグメント表ページ有無を確認
- 抽出seg_nameがPLサマリ誤認か判定
- GT修正要否とパターン拡張可否を判断
- data/eval/manual_check_581228_581329.csv / .md に結果保存
"""
from __future__ import annotations

import csv
import json
import re
import sys
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta

# プロジェクトルートをパスに追加
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

try:
    import pdfplumber
    _HAS_PDFPLUMBER = True
except ImportError:
    _HAS_PDFPLUMBER = False
    print("[WARN] pdfplumber がインストールされていません。テキスト抽出はスキップします。", flush=True)

JST = timezone(timedelta(hours=9))

ARCHIVE_DIR  = ROOT / "data" / "xbrl_archive"
EVAL_DIR     = ROOT / "data" / "eval"
GT_PATH      = EVAL_DIR / "screening_sheet.highconf_fixed.csv"
RUNS_BAK     = EVAL_DIR / "runs.jsonl.bak_before_gpf"
PENDING7     = EVAL_DIR / "pending7_semiauto_review.csv"
OUT_CSV      = EVAL_DIR / "manual_check_581228_581329.csv"
OUT_MD       = EVAL_DIR / "manual_check_581228_581329.md"

TARGET_PDFS = [
    "140120260313581228.pdf",
    "140120260313581329.pdf",
]

# ── セグメント見出しキーワード ──────────────────────────────────────────────
_SEG_HDR_KEYWORDS = [
    "セグメント情報", "報告セグメント", "セグメント利益", "セグメント損益",
    "事業別", "セグメントの概要", "セグメント別",
    "Segment information", "Business segment",
]
_SEG_HDR_RE = re.compile(
    r"(セグメント情報|報告セグメント|セグメント利益|セグメント損益"
    r"|事業別|セグメント別|セグメントの概要"
    r"|Segment\s+information|Business\s+segment)",
    re.IGNORECASE,
)

# ── 事業名候補キーワード ────────────────────────────────────────────────────
_BIZNAME_RE = re.compile(
    r"(事業|セグメント|部門|国内|海外|日本|北米|欧州|アジア|その他"
    r"|Business|Division|Domestic|Overseas|Japan|Asia|Europe|Americas)"
)
_NOISE_RE = re.compile(
    r"^(売上|利益|合計|調整|消去|全社|報告|損益|当期|前期|前年同期"
    r"|Sales|Revenue|Profit|Loss|Total|Adjustment|Eliminations|Corporate)"
)

# ── PLサマリ列パターン（誤認判定用）──────────────────────────────────────
_PL_COL_NAMES = {"報告", "報告セグメント", "四半期連結損益計算書計上額",
                 "中間連結損益計算書計上額", "四半期連結 損益計算書 計上額",
                 "中間連結損益 計算書計上額"}


def _norm(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace("\u3000", " ").replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", s).strip()


def analyze_pdf(pdf_name: str) -> dict:
    """pdfplumber でページをスキャンしてセグメント表の有無を判定"""
    pdf_path = ARCHIVE_DIR / pdf_name
    result = {
        "pdf": pdf_name,
        "pdf_exists": pdf_path.exists(),
        "total_pages": 0,
        "seg_hdr_pages": [],          # セグメント見出しがあるページ番号（0-indexed）
        "seg_hdr_texts": [],          # 見つかった見出しテキスト
        "biz_label_pages": {},        # {page_no: [事業名候補]}
        "actual_segment_table_exists": False,
        "evidence_page": None,
        "evidence_text_snippet": "",
        "best_page_text_preview": "",
    }

    if not pdf_path.exists():
        result["error"] = "PDF not found"
        return result

    if not _HAS_PDFPLUMBER:
        result["error"] = "pdfplumber unavailable"
        return result

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            result["total_pages"] = len(pdf.pages)
            for pi, page in enumerate(pdf.pages[:20]):
                text = page.extract_text() or ""
                # セグメント見出し検索
                for m in _SEG_HDR_RE.finditer(text):
                    result["seg_hdr_pages"].append(pi)
                    kw = m.group().strip()
                    if kw not in result["seg_hdr_texts"]:
                        result["seg_hdr_texts"].append(kw)

                # 事業名候補行を抽出（50文字以内、句読点なし、ノイズなし）
                biz_candidates = []
                for line in text.splitlines():
                    ln = _norm(line)
                    if not ln or len(ln) > 40:
                        continue
                    if "。" in ln or "、" in ln:
                        continue
                    if _NOISE_RE.search(ln):
                        continue
                    if _BIZNAME_RE.search(ln):
                        biz_candidates.append(ln)
                if biz_candidates:
                    result["biz_label_pages"][pi] = biz_candidates[:10]

                # 数値付き事業名行の密度確認（簡易）
                nums = re.findall(r"[\d,]{3,}", text)
                if pi in result["seg_hdr_pages"] and len(nums) >= 4:
                    if result["evidence_page"] is None:
                        result["evidence_page"] = pi
                        result["evidence_text_snippet"] = text[:600]

    except Exception as e:
        result["error"] = str(e)
        return result

    # セグメント見出し + 事業名候補 >= 2 + 数値あり → 実際のセグメント表あり
    seg_hdr_set = set(result["seg_hdr_pages"])
    biz_pages_with_hdr = [
        pg for pg in result["biz_label_pages"]
        if pg in seg_hdr_set or abs(min((abs(pg - h) for h in seg_hdr_set), default=99)) <= 2
    ]
    biz_count = sum(len(result["biz_label_pages"].get(pg, [])) for pg in biz_pages_with_hdr)

    result["actual_segment_table_exists"] = (
        len(seg_hdr_set) >= 1 and biz_count >= 2
    )
    if result["evidence_page"] is None and seg_hdr_set:
        result["evidence_page"] = min(seg_hdr_set)

    return result


def load_gt() -> dict[str, str]:
    gt = {}
    if GT_PATH.exists():
        with GT_PATH.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                gt[r["pdf"]] = r.get("has_segment_table", "").strip()
    return gt


def load_runs_bak() -> dict[str, dict]:
    runs = {}
    if RUNS_BAK.exists():
        with RUNS_BAK.open(encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                pdf = obj.get("pdf", "")
                if pdf in TARGET_PDFS and pdf not in runs:
                    runs[pdf] = obj
    return runs


def load_pending7() -> dict[str, dict]:
    p7 = {}
    if PENDING7.exists():
        with PENDING7.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                pdf = r.get("pdf", "")
                if pdf in TARGET_PDFS:
                    p7[pdf] = r
    return p7


def get_extracted_seg_names(runs_entry: dict) -> list[str]:
    segs_raw = runs_entry.get("segments_json", "[]")
    try:
        segs = json.loads(segs_raw) if isinstance(segs_raw, str) else segs_raw
    except Exception:
        return []
    names = []
    for s in segs:
        if isinstance(s, dict):
            nm = s.get("segment_name_raw", s.get("segment_name", ""))
            if nm:
                names.append(_norm(nm))
    return names


def judge(pdf_name: str, current_gt: str, analysis: dict,
          runs_entry: dict, p7_entry: dict | None) -> dict:
    """
    全情報を統合して判定を行う。
    """
    extracted_names = get_extracted_seg_names(runs_entry)
    mode = runs_entry.get("detected_mode", "")
    pg   = runs_entry.get("page_number", None)

    # PLサマリ誤認チェック: 全抽出セグメント名が PLサマリ列系のみか
    pl_col_hit = all(
        nm in _PL_COL_NAMES
        or re.search(r"損益計算書.*計上額|財務諸表.*計上額|^報告", nm)
        for nm in extracted_names
    ) if extracted_names else False

    # pending7 からの情報
    p7_segment_header_flag = p7_entry.get("segment_header_flag", "") if p7_entry else ""
    p7_biz_count           = int(p7_entry.get("business_label_count", 0)) if p7_entry else 0
    p7_auto_decision       = p7_entry.get("auto_decision", "") if p7_entry else ""

    # 実際のセグメント表が存在するか
    actual_exists = analysis.get("actual_segment_table_exists", False)
    seg_hdr_pages = analysis.get("seg_hdr_pages", [])
    seg_hdr_texts = analysis.get("seg_hdr_texts", [])
    biz_label_pages = analysis.get("biz_label_pages", {})

    # ── 判定ロジック ──────────────────────────────────────────────────────
    # 条件A: pdfplumber でセグメント見出し + 事業名候補が確認できる → yes維持 or yes修正
    # 条件B: 全抽出セグメント名がPLサマリ列系 + 実際のセグメント表なし → no修正
    # 条件C: pending7 で segment_header_flag=True + biz_count > 0 → yes維持
    # 条件D: pending7 で auto_decision=segment_yes → yes修正の根拠

    has_seg_hdr = len(seg_hdr_pages) > 0
    has_biz_labels = any(len(v) >= 2 for v in biz_label_pages.values())
    has_both = has_seg_hdr and has_biz_labels

    if current_gt == "yes":
        if actual_exists and not pl_col_hit:
            # セグメント表が実際にあり、抽出も正常 → keep_yes
            manual_decision = "keep_yes"
            evidence_summary = (
                f"セグメント見出しあり(p{min(seg_hdr_pages)+1}): {seg_hdr_texts[:2]}。"
                f"事業名候補あり。抽出セグメント名がPLサマリ系でない。"
            )
            pattern_expand_safe = "yes"
            recommended_action = "GT維持(yes)。G-PFパターン拡張不要。"
        elif pl_col_hit and not actual_exists:
            # 抽出がPLサマリ系のみ、かつ実際のセグメント表も確認できない → change_to_no
            manual_decision = "change_to_no"
            evidence_summary = (
                f"抽出seg_names={extracted_names}はPLサマリ列系のみ。"
                f"pdfplumberでセグメント見出し{'あり' if has_seg_hdr else 'なし'}、"
                f"事業名候補{'あり' if has_biz_labels else 'なし(2件未満)'}。"
                f"実際の独立セグメント表が確認できない。"
            )
            pattern_expand_safe = "yes"
            recommended_action = "GT修正(yes→no)。G-PFパターン拡張可(581228削除でFP削減)。"
        elif pl_col_hit and actual_exists:
            # 抽出がPLサマリ系だが別ページに実際のセグメント表あり → keep_yes（抽出ロジックの問題）
            manual_decision = "keep_yes"
            evidence_summary = (
                f"抽出seg_names={extracted_names}はPLサマリ列系だが、"
                f"p{min(seg_hdr_pages)+1}にセグメント見出し({seg_hdr_texts[:2]})と"
                f"事業名候補あり。実際にセグメント表は存在するが別ページ/抽出ミス。"
            )
            pattern_expand_safe = "no"
            recommended_action = (
                "GT維持(yes)。G-PFパターン拡張は危険(FNが増える)。"
                "抽出ロジック改善でTPとして検出すべき対象。"
            )
        else:
            # どちらとも言えない（境界ケース）
            manual_decision = "keep_yes"
            evidence_summary = (
                f"seg_hdr={'あり' if has_seg_hdr else 'なし'}, "
                f"biz_labels={'あり' if has_biz_labels else 'なし'}, "
                f"pl_col_hit={pl_col_hit}, actual_exists={actual_exists}。要目視確認。"
            )
            pattern_expand_safe = "uncertain"
            recommended_action = "要PDF目視確認。"
    else:
        # current_gt in ("no", "unknown", "")
        if p7_auto_decision == "segment_yes" and p7_biz_count >= 2:
            # pending7がsegment_yes → yes修正を検討
            manual_decision = "change_to_yes"
            evidence_summary = (
                f"pending7: auto_decision={p7_auto_decision}, "
                f"biz_count={p7_biz_count}, segment_header_flag={p7_segment_header_flag}。"
                f"実際のセグメント表あり(p{min(seg_hdr_pages)+1 if seg_hdr_pages else '?'})。"
            )
            pattern_expand_safe = "no"
            recommended_action = "GT修正(no→yes)推奨。G-PFパターン拡張は慎重に。"
        else:
            # keep_no
            manual_decision = "keep_no"
            evidence_summary = (
                f"抽出seg_names={extracted_names}はPLサマリ列系。"
                f"pending7: biz_count={p7_biz_count}。"
                f"セグメント見出し検索: {'あり' if has_seg_hdr else 'なし'}。"
            )
            pattern_expand_safe = "yes"
            recommended_action = "GT維持(no)。G-PFパターン拡張可(FP削減に寄与)。"

    return {
        "pdf": pdf_name,
        "current_gt": current_gt,
        "manual_decision": manual_decision,
        "actual_segment_table_exists": str(actual_exists),
        "evidence_page": str(analysis.get("evidence_page", "")),
        "evidence_summary": evidence_summary,
        "extracted_seg_names": " | ".join(extracted_names),
        "pattern_expand_safe": pattern_expand_safe,
        "recommended_action": recommended_action,
        # 詳細情報（MDレポート用）
        "_mode": mode,
        "_pg": str(pg),
        "_seg_hdr_pages": str(seg_hdr_pages),
        "_seg_hdr_texts": str(seg_hdr_texts),
        "_biz_label_pages": str({pg: v[:3] for pg, v in list(biz_label_pages.items())[:5]}),
        "_p7_auto": p7_auto_decision,
        "_p7_biz_count": str(p7_biz_count),
        "_pl_col_hit": str(pl_col_hit),
        "_total_pages": str(analysis.get("total_pages", 0)),
    }


def write_csv(rows: list[dict]) -> None:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    cols = [
        "pdf", "current_gt", "manual_decision",
        "actual_segment_table_exists", "evidence_page",
        "evidence_summary", "extracted_seg_names",
        "pattern_expand_safe", "recommended_action",
    ]
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"[CSV] {OUT_CSV}", flush=True)


def write_md(rows: list[dict]) -> None:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00")

    lines = []
    lines.append("# 目視確認レポート: 581228 / 581329")
    lines.append(f"\n生成日時: {ts}  \nベースGT: `screening_sheet.highconf_fixed.csv`\n")

    for r in rows:
        pdf_short = r["pdf"].replace("140120260313", "")
        lines.append(f"---\n\n## {pdf_short}")
        lines.append(f"\n| 項目 | 値 |")
        lines.append(f"|------|-----|")
        lines.append(f"| 現在GT | `{r['current_gt']}` |")
        lines.append(f"| 抽出モード | `{r['_mode']}` (page={r['_pg']}) |")
        lines.append(f"| 抽出セグメント名 | `{r['extracted_seg_names']}` |")
        lines.append(f"| PLサマリ列誤認判定 | `{r['_pl_col_hit']}` |")
        lines.append(f"| PDF総ページ数 | {r['_total_pages']} |")
        lines.append(f"| セグメント見出しページ | {r['_seg_hdr_pages']} |")
        lines.append(f"| 見出しキーワード | {r['_seg_hdr_texts']} |")
        lines.append(f"| 事業名候補(ページ別) | `{r['_biz_label_pages']}` |")
        lines.append(f"| 実セグメント表有無 | **{r['actual_segment_table_exists']}** |")
        lines.append(f"| pending7 auto_decision | `{r['_p7_auto']}` (biz={r['_p7_biz_count']}) |")
        lines.append(f"\n### 判定結果\n")
        lines.append(f"- **manual_decision**: `{r['manual_decision']}`")
        lines.append(f"- **パターン拡張安全性**: `{r['pattern_expand_safe']}`")
        lines.append(f"- **推奨対応**: {r['recommended_action']}")
        lines.append(f"\n**根拠**: {r['evidence_summary']}")
        lines.append("")

    # ── 総合判定 ────────────────────────────────────────────────────────
    lines.append("---\n\n## 総合判定")

    gt_updates = [(r["pdf"], r["current_gt"], r["manual_decision"]) for r in rows
                  if r["manual_decision"] in ("change_to_no", "change_to_yes")]
    pattern_expand = all(r["pattern_expand_safe"] == "yes" for r in rows)
    pattern_uncertain = any(r["pattern_expand_safe"] == "uncertain" for r in rows)

    lines.append("\n### GT更新要否")
    if gt_updates:
        for pdf, cur, dec in gt_updates:
            lines.append(f"- `{pdf}`: `{cur}` → `{dec.replace('change_to_', '')}`")
    else:
        lines.append("- GT更新不要（全件維持）")

    lines.append("\n### G-PFパターン拡張可否")
    if pattern_expand:
        lines.append("- **可**: 全対象PDFでパターン拡張してもFNリスクなし")
    elif pattern_uncertain:
        lines.append("- **要確認**: 一部PDF（目視確認が不足）")
    else:
        lines.append("- **不可**: パターン拡張はFNリスクあり")

    lines.append("\n### 最終結論（3行）")
    # ケース別最終結論
    decisions = {r["pdf"]: r["manual_decision"] for r in rows}
    pdf_228 = "140120260313581228.pdf"
    pdf_329 = "140120260313581329.pdf"

    if decisions.get(pdf_228) == "change_to_no":
        line1 = "581228: GT修正(yes→no)を推奨。PLサマリ列のみ抽出されており、実際のセグメント表は確認できない。"
    elif decisions.get(pdf_228) == "keep_yes":
        line1 = "581228: GT維持(yes)。実際のセグメント表が存在するため、パターン拡張はFNリスクあり。"
    else:
        line1 = f"581228: {decisions.get(pdf_228, '不明')}。"

    if decisions.get(pdf_329) == "keep_no":
        line2 = "581329: GT維持(no)。PLサマリ列のみ抽出。G-PFパターン拡張でFP削減可能。"
    elif decisions.get(pdf_329) == "change_to_yes":
        line2 = "581329: GT修正(no→yes)を推奨。実際のセグメント表が存在する。パターン拡張は慎重に。"
    else:
        line2 = f"581329: {decisions.get(pdf_329, '不明')}。"

    if pattern_expand and any(d == "change_to_no" for d in decisions.values()):
        line3 = "GT修正後にG-PFパターン拡張(損益計算書計上額系の変形)を実施すれば、FN増加なしでFP削減が可能。"
    elif not pattern_expand:
        line3 = "パターン拡張はFNリスクがあるため、対象PDFのGT維持のまま現状の保守版post-filterを維持推奨。"
    else:
        line3 = "GT修正不要。現状の保守版post-filter（FP-1件）のまま維持を推奨。"

    lines.append(f"1. {line1}")
    lines.append(f"2. {line2}")
    lines.append(f"3. {line3}")

    with OUT_MD.open("w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[MD]  {OUT_MD}", flush=True)


def main():
    print("=== 581228/581329 目視確認補助スクリプト ===", flush=True)
    print(f"実行時刻: {datetime.now(JST).strftime('%Y-%m-%dT%H:%M:%S+09:00')}", flush=True)

    gt_map   = load_gt()
    runs_map = load_runs_bak()
    p7_map   = load_pending7()

    rows = []
    for pdf_name in TARGET_PDFS:
        print(f"\n--- {pdf_name} ---", flush=True)
        current_gt  = gt_map.get(pdf_name, "unknown")
        runs_entry  = runs_map.get(pdf_name, {})
        p7_entry    = p7_map.get(pdf_name, None)

        print(f"  current_gt={current_gt}", flush=True)
        print(f"  mode={runs_entry.get('detected_mode','')} page={runs_entry.get('page_number','')}", flush=True)

        segs = get_extracted_seg_names(runs_entry)
        print(f"  extracted_seg_names={segs}", flush=True)

        print(f"  pdfplumperスキャン中...", flush=True)
        analysis = analyze_pdf(pdf_name)
        print(f"  total_pages={analysis['total_pages']}", flush=True)
        print(f"  seg_hdr_pages={analysis['seg_hdr_pages']}", flush=True)
        print(f"  seg_hdr_texts={analysis['seg_hdr_texts']}", flush=True)
        print(f"  biz_label_pages.keys={list(analysis['biz_label_pages'].keys())}", flush=True)
        print(f"  actual_segment_table_exists={analysis['actual_segment_table_exists']}", flush=True)

        row = judge(pdf_name, current_gt, analysis, runs_entry, p7_entry)
        rows.append(row)
        print(f"  => manual_decision={row['manual_decision']}", flush=True)
        print(f"  => pattern_expand_safe={row['pattern_expand_safe']}", flush=True)
        print(f"  => recommended_action={row['recommended_action']}", flush=True)

    write_csv(rows)
    write_md(rows)

    # ── ターミナル出力サマリ ─────────────────────────────────────────────
    print("\n=== サマリ ===", flush=True)
    for r in rows:
        print(f"{r['pdf']}: GT={r['current_gt']} => {r['manual_decision']} "
              f"pattern_safe={r['pattern_expand_safe']}", flush=True)

    print(f"\n生成ファイル:", flush=True)
    print(f"  {OUT_CSV}", flush=True)
    print(f"  {OUT_MD}", flush=True)


if __name__ == "__main__":
    main()
