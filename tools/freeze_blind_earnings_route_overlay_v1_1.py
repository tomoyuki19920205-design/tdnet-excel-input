#!/usr/bin/env python3
"""Create a pre-label integrity manifest for the blind earnings overlay v1.1."""
from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
FREEZE_OUT = OUT / "earnings_model_blind_freeze_2026-07-15_v1_1.json"
OVERLAY_SCRIPT = ROOT / "tools" / "build_earnings_route_overlay_15_v1_1.py"

MODEL_CODE = {
    "tools/build_quarterly_predictability_15_jquants_v2.py": "model_code_quarterly_observation_and_single_quarter_conversion",
    "tools/audit_quarterly_predictability_v3.py": "model_code_predictability_threshold_audit",
    "tools/classify_quarterly_timing_risk_15.py": "model_code_revenue_timing_risk_classification",
    "tools/build_earnings_route_overlay_15_v1_1.py": "model_code_A_route_overlay",
}
DIRECT_INPUTS = {
    "output/quarterly_history_15_jquants_asof_v2.csv": "input_single_quarter_history_for_v3",
    "output/quarterly_predictability_15_jquants_asof_v2.csv": "input_v2_classification_for_v3_change_audit",
    "output/pilot_score_1418_2026-07-15.json": "input_explicit_latest_individual_score_1418",
    "output/pilot_score_2337_2026-07-15.json": "input_explicit_latest_individual_score_2337",
    "output/pilot_scores_15_2026-07-15.csv": "input_integrated_15_score_fallback",
}
PRIMARY_OUTPUTS = {
    "output/quarterly_predictability_15_jquants_asof_v3.csv": "output_predictability_v3",
    "output/quarterly_predictability_15_jquants_asof_v3.md": "output_predictability_v3_report",
    "output/quarterly_timing_risk_15_asof_v1.csv": "output_timing_risk_v1",
    "output/quarterly_timing_risk_15_asof_v1.md": "output_timing_risk_v1_report",
    "output/earnings_route_overlay_15_asof_v1_1.csv": "output_A_route_overlay_v1_1",
    "output/earnings_route_overlay_15_asof_v1_1.md": "output_A_route_overlay_v1_1_report",
    "output/earnings_route_score_manifest_15_asof_v1_1.csv": "output_score_source_manifest_v1_1",
}
REGENERATED_OUTPUTS = (
    "output/earnings_route_overlay_15_asof_v1_1.csv",
    "output/earnings_route_overlay_15_asof_v1_1.md",
    "output/earnings_route_score_manifest_15_asof_v1_1.csv",
)
TARGET_CODES = ["1418", "205A", "3547", "3558", "3697", "2164", "2168", "2449", "4197", "9238", "198A", "2337", "244A", "2484", "280A"]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8").strip()


def git_status(relative_path: str) -> tuple[bool, str]:
    tracked = bool(run_git("ls-files", "--", relative_path))
    porcelain = run_git("status", "--short", "--", relative_path)
    return tracked, porcelain or "clean"


def file_record(relative_path: str, role: str, kind: str, generated_by: str) -> dict[str, object]:
    path = ROOT / relative_path
    if not path.is_file():
        raise FileNotFoundError(f"required freeze target is missing: {relative_path}")
    tracked, status = git_status(relative_path)
    return {
        "relative_path": relative_path,
        "role": role,
        "sha256": sha256(path),
        "file_size": path.stat().st_size,
        "git_tracked": tracked,
        "git_status": status,
        "generated_by": generated_by,
        "input_or_output": kind,
    }


def assert_score_sources() -> None:
    manifest_path = OUT / "earnings_route_score_manifest_15_asof_v1_1.csv"
    with manifest_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 15, "score manifest must contain 15 companies"
    source_paths = {row["score_source_path"] for row in rows}
    expected = {
        "output/pilot_score_1418_2026-07-15.json",
        "output/pilot_score_2337_2026-07-15.json",
        "output/pilot_scores_15_2026-07-15.csv",
    }
    assert source_paths == expected, (source_paths, expected)
    for row in rows:
        assert sha256(ROOT / row["score_source_path"]) == row["score_source_sha256"], row["code"]
    row_1418 = next(row for row in rows if row["code"] == "1418")
    row_2337 = next(row for row in rows if row["code"] == "2337")
    assert row_1418["score_source_priority"] == "1_explicit_latest_individual"
    assert row_2337["score_source_priority"] == "1_explicit_latest_individual"


def main() -> None:
    assert_score_sources()
    before = {relative: sha256(ROOT / relative) for relative in REGENERATED_OUTPUTS}
    subprocess.run([sys.executable, str(OVERLAY_SCRIPT)], cwd=ROOT, check=True)
    after = {relative: sha256(ROOT / relative) for relative in REGENERATED_OUTPUTS}
    equality = {relative: {"before_sha256": before[relative], "after_sha256": after[relative], "matches": before[relative] == after[relative]} for relative in REGENERATED_OUTPUTS}
    if not all(item["matches"] for item in equality.values()):
        raise RuntimeError(f"reproduction mismatch: {equality}")
    assert_score_sources()

    files: list[dict[str, object]] = []
    for relative, role in MODEL_CODE.items():
        files.append(file_record(relative, role, "code", "manual_model_freeze"))
    for relative, role in DIRECT_INPUTS.items():
        files.append(file_record(relative, role, "input", "preexisting_blind_model_input"))
    for relative, role in PRIMARY_OUTPUTS.items():
        files.append(file_record(relative, role, "output", "blind_model_pipeline"))

    jst = timezone(timedelta(hours=9))
    payload = {
        "freeze_schema_version": "1.1",
        "freeze_purpose": "pre-label blind-model integrity seal",
        "analysis_as_of_jst": "2026-07-15T15:20:00+09:00",
        "freeze_created_at_jst": datetime.now(jst).isoformat(timespec="seconds"),
        "git_head_before_commit": run_git("rev-parse", "HEAD"),
        "data_source": {
            "database": "data/jquants.db",
            "table": "jquants_financials_normalized",
            "disclosed_date_condition": "disclosed_date < '2026-07-15'",
            "cutoff_rationale": "J-Quants disclosed_date has no time, so all 2026-07-15 disclosures are excluded.",
        },
        "target_codes": TARGET_CODES,
        "score_source_priority": [
            "1. Explicitly declared latest individual score (1418 and 2337).",
            "2. Integrated 15-company score fallback when no explicit individual score is declared.",
            "3. missing when neither source exists (not used in this freeze).",
        ],
        "overlay_rule_summary": [
            "high timing risk plus target-quarter visibility none: zero / blocked.",
            "unknown timing risk, or insufficient information outside a known medium/none route: unknown / insufficient.",
            "highly_lumpy or structural_change operating-profit pattern: reduced / conditional.",
            "medium timing risk plus visibility none: reduced / conditional.",
            "low timing risk plus stable/seasonal sales and stable/structural_trend profit: full / usable.",
            "all remaining combinations: reduced / conditional.",
        ],
        "blindness_assertions": {
            "outcome_labels_not_referenced": True,
            "post_earnings_information_not_referenced": True,
            "market_reaction_not_referenced": True,
            "label_comparison_performed": False,
        },
        "reproduction_check": {
            "command": "python tools/build_earnings_route_overlay_15_v1_1.py",
            "outputs": equality,
            "all_match": True,
        },
        "files": files,
    }
    FREEZE_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"freeze_manifest": str(FREEZE_OUT.relative_to(ROOT)), "sha256": sha256(FREEZE_OUT), "reproduction": "pass", "file_count": len(files)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
