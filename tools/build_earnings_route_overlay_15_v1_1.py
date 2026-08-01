#!/usr/bin/env python3
"""Build the v1.1 A-score route overlay from immutable, explicit inputs.

This script intentionally does not score, recalculate predictability, or amend
timing-risk inputs.  Score-source precedence is declared below rather than
inferred from file timestamps.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
V3 = OUT / "quarterly_predictability_15_jquants_asof_v3.csv"
TIMING = OUT / "quarterly_timing_risk_15_asof_v1.csv"
INTEGRATED_SCORES = OUT / "pilot_scores_15_2026-07-15.csv"
CSV_OUT = OUT / "earnings_route_overlay_15_asof_v1_1.csv"
MD_OUT = OUT / "earnings_route_overlay_15_asof_v1_1.md"
MANIFEST_OUT = OUT / "earnings_route_score_manifest_15_asof_v1_1.csv"

# This is the explicit score-input manifest.  Do not replace this declaration
# with mtime-based selection: only these two individually regenerated scores
# supersede the integrated 15-company batch.
EXPLICIT_LATEST_INDIVIDUAL_SOURCES = {
    "1418": OUT / "pilot_score_1418_2026-07-15.json",
    "2337": OUT / "pilot_score_2337_2026-07-15.json",
}


def read_csv_by_code(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row["code"]: row for row in csv.DictReader(handle)}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_individual_score(path: Path) -> tuple[str, dict[str, int]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    values = {key: int(data["scores"][key]["score"]) for key in ("A", "B", "M", "D", "C")}
    return data["company_name"], values


def build_score_manifest(integrated: dict[str, dict[str, str]]) -> list[dict[str, str | int]]:
    manifest: list[dict[str, str | int]] = []
    for code in sorted(integrated):
        if code in EXPLICIT_LATEST_INDIVIDUAL_SOURCES:
            source = EXPLICIT_LATEST_INDIVIDUAL_SOURCES[code]
            if not source.exists():
                raise FileNotFoundError(f"explicit score source is missing: {source}")
            company_name, values = load_individual_score(source)
            source_version = "explicit_latest_individual"
            priority = "1_explicit_latest_individual"
            reason = "明示manifestで指定した個別再生成済み採点を優先。更新時刻は使用しない。"
        else:
            source = INTEGRATED_SCORES
            batch = integrated[code]
            company_name = batch["company_name"]
            values = {
                "A": int(batch["A_business_expectation"]),
                "B": int(batch["B_capital_return_expectation"]),
                "M": int(batch["M_unpriced_potential"]),
                "D": int(batch["D_downside_risk"]),
                "C": int(batch["C_information_confidence"]),
            }
            source_version = "integrated_15_batch"
            priority = "2_integrated_15_fallback"
            reason = "明示された最新個別採点がないため、15社統合採点を使用。"
        manifest.append({
            "code": code,
            "company_name": company_name,
            "score_source_path": source.relative_to(ROOT).as_posix(),
            "score_source_version": source_version,
            "score_source_sha256": sha256_file(source),
            "score_source_priority": priority,
            "A_raw": values["A"], "B_raw": values["B"], "M_raw": values["M"],
            "D_raw": values["D"], "C_raw": values["C"],
            "score_selection_reason": reason,
        })
    return manifest


def classify(v3: dict[str, str], timing: dict[str, str]) -> tuple[str, str, str, str]:
    """Apply the user-specified precedence, returning weight, evidence, status, reason."""
    risk = timing["timing_risk"]
    visibility = timing["target_quarter_visibility"]
    sales = v3["sales_pattern_v3"]
    profit = v3["operating_profit_pattern_v3"]
    gate = v3["quarterly_predictability_gate_v3"]

    # 1. High timing risk with no target-quarter visibility.
    if risk == "high" and visibility == "none":
        return "zero", "yes", "blocked", "high riskかつ対象Q可視性なし。過去Qパターンを対象QのA根拠に使わない。"
    # 2. Unknown timing risk or insufficient history/material.
    # A known medium/none timing route remains conditional even when its
    # statistical history is limited (e.g. a recent listing).  It is handled
    # by rule 4 below; treating it as insufficient here would erase that
    # explicitly observable target-quarter timing constraint.
    if risk == "unknown" or (gate == "insufficient" and risk != "medium"):
        return "unknown", "unknown", "insufficient", "計上タイミングまたは必要履歴が不足し、Aの業績ルート可否を判定できない。"
    # 3. Profit instability/structural change has precedence over a low risk label.
    if profit in {"highly_lumpy", "structural_change"}:
        return "reduced", "yes", "conditional", "営業利益パターンが高度に不規則または構造変化で、対象Q固有の裏付けを要する。"
    # 4. Medium risk without target-quarter visibility.
    if risk == "medium" and visibility == "none":
        return "reduced", "yes", "conditional", "medium riskかつ対象Q可視性なし。過去Qは補助情報に限定する。"
    # 5. Low risk plus stable sales and stable/improving-profit pattern.
    if (risk == "low" and sales in {"stable", "seasonal_but_stable"}
            and profit in {"stable", "structural_trend"}):
        return "full", "no", "usable", "低い計上タイミングリスクと安定的な売上・利益パターンが確認できる。"
    # 6. All remaining combinations.
    return "reduced", "yes", "conditional", "規定のfull利用条件を満たさず、過去Qは補助情報に限定する。"


def validate(rows: list[dict[str, str | int]], markdown: str) -> None:
    status_counts = Counter(str(r["earnings_route_status"]) for r in rows)
    weight_counts = Counter(str(r["historical_q_signal_weight"]) for r in rows)
    assert status_counts == {"usable": 2, "conditional": 6, "blocked": 2, "insufficient": 5}, status_counts
    assert weight_counts == {"full": 2, "reduced": 6, "zero": 2, "unknown": 5}, weight_counts
    for row in rows:
        status, weight, profit = (str(row[k]) for k in (
            "earnings_route_status", "historical_q_signal_weight", "operating_profit_pattern"))
        assert not (status == "blocked" and weight != "zero")
        assert not (status == "usable" and weight != "full")
        assert not (status == "insufficient" and weight != "unknown")
        assert not (profit in {"highly_lumpy", "structural_change"} and status == "usable")
        expected_line = f"- {row['code']}: timing={row['timing_risk']} / visibility={row['target_quarter_visibility']} / weight={weight} / status={status}"
        assert expected_line in markdown, expected_line
    row_2337 = next(row for row in rows if row["code"] == "2337")
    assert [row_2337[k] for k in ("A_raw", "B_raw", "M_raw", "D_raw", "C_raw")] == [47, 75, 43, 53, 77]
    assert "- 3697: timing=medium / visibility=none / weight=reduced / status=conditional" in markdown
    assert "3697: timing=low" not in markdown
    assert "3697: timing=medium / visibility=none / weight=reduced / status=usable" not in markdown


def main() -> None:
    v3 = read_csv_by_code(V3)
    timing = read_csv_by_code(TIMING)
    integrated = read_csv_by_code(INTEGRATED_SCORES)
    assert set(v3) == set(timing) == set(integrated), "input code sets differ"
    assert len(v3) == 15, "expected 15 companies"

    manifest = build_score_manifest(integrated)
    manifest_by_code = {str(row["code"]): row for row in manifest}
    rows: list[dict[str, str | int]] = []
    for code in sorted(v3):
        v, t, s = v3[code], timing[code], manifest_by_code[code]
        weight, required, status, reason = classify(v, t)
        rows.append({
            "code": code, "company_name": s["company_name"],
            "A_raw": s["A_raw"], "B_raw": s["B_raw"], "M_raw": s["M_raw"],
            "D_raw": s["D_raw"], "C_raw": s["C_raw"],
            "sales_pattern": v["sales_pattern_v3"],
            "operating_profit_pattern": v["operating_profit_pattern_v3"],
            "statistical_gate": v["quarterly_predictability_gate_v3"],
            "revenue_recognition_model": t["revenue_recognition_model"],
            "target_quarter_visibility": t["target_quarter_visibility"],
            "timing_risk": t["timing_risk"],
            "historical_q_signal_weight": weight,
            "target_specific_evidence_required": required,
            "earnings_route_status": status,
            "overlay_reason": reason,
            "missing_information": t["missing_information"],
            "score_source_path": s["score_source_path"],
            "score_source_version": s["score_source_version"],
            "score_source_sha256": s["score_source_sha256"],
            "score_source_priority": s["score_source_priority"],
        })

    status_counts = Counter(str(r["earnings_route_status"]) for r in rows)
    weight_counts = Counter(str(r["historical_q_signal_weight"]) for r in rows)
    status_lines = [
        f"- {r['code']}: timing={r['timing_risk']} / visibility={r['target_quarter_visibility']} / "
        f"weight={r['historical_q_signal_weight']} / status={r['earnings_route_status']}"
        for r in rows
    ]
    lines = [
        "# 業績期待Aの決算持ち越し根拠オーバーレイ v1.1", "",
        "- 入力は四半期予測可能性v3、計上タイミングリスクv1、明示score manifest。A/B/M/D/Cは再採点していない。",
        "- 個別再生成済みの明示入力（1418・2337）を統合採点より優先し、その他は統合採点を使用。更新時刻による選択はしていない。",
        "- 判定は業績期待Aの利用可能性だけを示す。還元期待は別ルートであり、Aを復活させない。", "",
        "## 件数", f"- earnings_route_status: {dict(status_counts)}", f"- historical_q_signal_weight: {dict(weight_counts)}", "",
        "## 完成行に基づく判定一覧", *status_lines, "",
        "## 重点確認",
        "- 2337はhigh / noneのためzero / blocked。採点値は明示個別入力の47 / 75 / 43 / 53 / 77。",
        "- 1418はstructural_changeの優先規則によりreduced / conditional。",
        "- 2484はstable / structural_trendかつlowのためfull / usable。",
        "- 3547はstable / stableかつlowのためfull / usable。",
        "- 3558はhighly_lumpy、2168はstructural_changeのため、ともにreduced / conditional。",
        "- 3697は完成行のmedium / none / reduced / conditionalに従う。",
    ]
    markdown = "\n".join(lines) + "\n"
    validate(rows, markdown)

    with MANIFEST_OUT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(manifest[0]))
        writer.writeheader(); writer.writerows(manifest)
    with CSV_OUT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    MD_OUT.write_text(markdown, encoding="utf-8")
    print({"rows": len(rows), "statuses": dict(status_counts), "weights": dict(weight_counts), "tests": "pass"})


if __name__ == "__main__":
    main()
