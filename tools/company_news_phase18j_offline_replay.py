"""Phase1.8j coverage repair and exhaustive audit using saved artifacts only."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib import news_monitor
from tools import company_news_lightweight as lightweight
from tools import company_news_strategy_v4 as strategy_v4
from tools import company_news_strategy_v5 as strategy_v5
from tools.company_news_canonical_schema import validate_instance


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _assignment(response: dict[str, Any]) -> dict[str, Any]:
    telemetry = response["strategy_telemetry"]
    return {
        "assignment_id": telemetry["assignment_id"], "ticker": telemetry["ticker"],
        "search_from": "2026-08-25", "search_to": "2026-08-31",
        "strategy_version": strategy_v5.VERSION,
    }


def _provenance_ok(response: dict[str, Any], registry: list[dict[str, Any]]) -> bool:
    queries = response["strategy_telemetry"].get("queries", [])
    if len(queries) != len(registry) or len({row.get("query_id") for row in registry}) != len(registry):
        return False
    return all(
        registry[index].get("executed") is True
        and query.get("executed") is True
        and query.get("channel") == registry[index].get("canonical_channel")
        and query.get("query") == registry[index].get("query_text")
        and query.get("exposure_index") == registry[index].get("exposure_id")
        for index, query in enumerate(queries)
    )


def exhaustive_audit(response: dict[str, Any], registry: list[dict[str, Any]],
                     discovery: dict[str, Any]) -> dict[str, Any]:
    """Evaluate independent rules without changing or short-circuiting state."""
    telemetry = response["strategy_telemetry"]
    payload = response["research_result"]
    derived = lightweight.derive_coverage_state(response, registry, discovery)
    results: list[dict[str, Any]] = []

    def check(rule: str, ok: bool, classification: str, detail: str) -> None:
        results.append({"rule": rule, "status": "PASS" if ok else "FAIL",
                        "classification": None if ok else classification,
                        "detail": detail})

    def catches(rule: str, fn: Callable[[], Any], classification: str) -> None:
        try:
            fn()
            check(rule, True, classification, "validator accepted")
        except Exception as exc:
            check(rule, False, classification, f"{type(exc).__name__}: {exc}")

    catches("canonical_schema", lambda: validate_instance(
        response, strategy_v5.canonical_output_schema()), "C")
    catches("payload_semantics", lambda: news_monitor.validate_payload(payload), "C")
    check("search_provenance", _provenance_ok(response, registry), "B",
          "SearchRecordRegistry must exactly project to canonical query telemetry")
    check("query_budget", telemetry.get("query_budget_exceeded") == derived["query_budget_exceeded"], "A",
          "query budget is derived from actual registry count")
    check("coverage_status", telemetry.get("coverage_status") == derived["coverage_status"], "A",
          f"saved={telemetry.get('coverage_status')} derived={derived['coverage_status']}")
    check("coverage_gaps", telemetry.get("coverage_gaps") == derived["coverage_gaps"], "A",
          "gaps must correspond to missing required queries or inaccessible material documents")
    saved_reviews = {row["channel"]: row["status"] for row in telemetry.get("channel_reviews", [])}
    derived_reviews = {row["channel"]: row["status"] for row in derived["channel_reviews"]}
    for channel in sorted(set(saved_reviews) | set(derived_reviews)):
        check(f"channel_review:{channel}", saved_reviews.get(channel) == derived_reviews.get(channel), "A",
              f"saved={saved_reviews.get(channel)} derived={derived_reviews.get(channel)}")
    saved_exposures = {row["exposure_index"]: row["status"]
                       for row in telemetry.get("no_news_exposure_coverage", [])}
    derived_exposures = {row["exposure_index"]: row["status"]
                         for row in derived["no_news_exposure_coverage"]}
    check("exposure_coverage", saved_exposures == derived_exposures, "A",
          f"saved={saved_exposures} derived={derived_exposures}")
    check("no_news_gate", telemetry.get("no_news_gate_pass") == derived["no_news_gate_pass"], "A",
          "gate is derived from completed required channels, gaps, and output presence")
    check("incremental_ir", discovery.get("incremental_ir_applicable") is False and all(
        row.get("scope_label") != "IN_SCOPE_INCREMENTAL_IR" for row in telemetry.get("documents", [])), "A",
        "non-applicable incremental IR must not create a gap or incremental document")
    identity = telemetry.get("company_identity", {})
    sources = {lightweight.canonicalize_url(row["url"]): row
               for row in telemetry.get("source_provenance", [])}
    identity_source = sources.get(lightweight.canonicalize_url(identity.get("source_url")))
    check("identity_evidence", bool(identity_source and identity_source.get("content_verified") is True), "B",
          "identity source must be verified provenance")
    catches("publication_precision", lambda: [news_monitor.normalize_publication_fields(
        item, allow_legacy=False) for item in payload.get("items", [])], "C")
    catches("effective_date_precision", lambda: [news_monitor.normalize_effective_fields(
        item, boundary, allow_legacy=False) for item in payload.get("items", [])
        for boundary in ("valid_from", "valid_until")], "C")
    start = date.fromisoformat("2026-08-25"); end = date.fromisoformat("2026-08-31")
    check("document_dates", all(row.get("date") is None or start <= date.fromisoformat(row["date"]) <= end
                                for row in telemetry.get("documents", [])), "C",
          "known document dates must be valid and in-window")
    documents = {row.get("url"): row for row in telemetry.get("documents", [])}
    candidates = telemetry.get("candidate_provenance", [])
    check("candidate_provenance", telemetry.get("candidate_count") == len(candidates)
          and all(row.get("candidate_index") == index and row.get("document_url") in documents
                  for index, row in enumerate(candidates)), "B",
          "candidate count, order, and document links must agree")
    selected = [row for row in candidates if row.get("selected_for_output")]
    check("materiality_and_output", telemetry.get("final_event_count") == len(selected) == len(payload.get("items", []))
          and all(documents[row["document_url"]].get("material") is True for row in selected), "E",
          "selected output must be verified material candidate provenance")
    check("tdnet_redundancy", all(row.get("scope_label") != "OUT_OF_SCOPE_TDNET" or
                                  row.get("tdnet_same_content_verified") is True
                                  for row in telemetry.get("documents", [])), "B",
          "TDNET exclusion requires verified same-content linkage")
    failures = [row for row in results if row["status"] == "FAIL"]
    return {"status": "PASS" if not failures else "FAIL", "rule_count": len(results),
            "failure_count": len(failures), "failures": failures, "rules": results,
            "derived_coverage": derived}


def replay(root: Path, output_dir: Path) -> dict[str, Any]:
    company = root / "artifacts" / "token_efficiency_phase18b_7760" / "companies" / "01-7760"
    source_path = company / "phase18i-semantic-replay" / "replayed-response.json"
    prior_path = company / "phase18i-semantic-replay" / "validation-result.json"
    discovery = _read(company / "workers" / "company-web" / "last-message.json")
    registry = _read(company / "phase18f-validation-replay" / "search-record-registry.json")["records"]
    source = _read(source_path)
    pre_audit = exhaustive_audit(source, registry, discovery)
    repaired, coverage = lightweight.apply_coverage_authority(source, registry, discovery)
    post_audit = exhaustive_audit(repaired, registry, discovery)
    assignment = _assignment(repaired)
    payload = repaired["research_result"]
    telemetry = repaired["strategy_telemetry"]

    validation: dict[str, dict[str, Any]] = {}
    checks: list[tuple[str, Callable[[], Any]]] = [
        ("canonical_schema", lambda: validate_instance(repaired, strategy_v5.canonical_output_schema())),
        ("strategy_v4", lambda: strategy_v4.validate(
            *strategy_v5.project_v4_validation(telemetry, assignment), payload)),
        ("strategy_v5", lambda: strategy_v5.validate(telemetry, assignment, payload)),
        ("payload", lambda: news_monitor.validate_payload(payload)),
    ]
    for name, fn in checks:
        try:
            fn()
            validation[name] = {"status": "PASS", "error_type": None, "error_message": None}
        except Exception as exc:
            validation[name] = {"status": "FAIL", "error_type": type(exc).__name__,
                                "error_message": str(exc)}
    coverage_consistent = (
        telemetry["coverage_status"] == coverage["coverage_status"]
        and telemetry["coverage_gaps"] == coverage["coverage_gaps"]
        and coverage["fake_gap_count"] == 0)
    semantic_keys = ("headline", "source_url", "source_type", "source_name", "category", "subcategory",
                     "direction", "importance", "earnings_relevance", "summary", "why_it_matters",
                     "evidence_excerpt", "temporal_scope", "temporal_status", "tags")
    quality_unchanged = all(
        {key: before.get(key) for key in semantic_keys} == {key: after.get(key) for key in semantic_keys}
        for before, after in zip(source["research_result"]["items"], payload["items"]))
    all_pass = (all(row["status"] == "PASS" for row in validation.values())
                and post_audit["status"] == "PASS" and _provenance_ok(repaired, registry)
                and coverage_consistent and quality_unchanged)
    prior = _read(prior_path)
    result = {
        "schema_version": "company_news_phase18j_offline_replay_v1", "ticker": "7760",
        "source_artifact": str(source_path.relative_to(company)),
        "canonical_schema_sha256": strategy_v5.canonical_schema_sha256(),
        "coverage_status_before": source["strategy_telemetry"]["coverage_status"],
        "coverage_status_after": telemetry["coverage_status"],
        "coverage_gaps_after": telemetry["coverage_gaps"],
        "coverage_matrix": coverage["coverage_matrix"],
        "fake_gap_count": coverage["fake_gap_count"],
        "pre_repair_exhaustive_audit": pre_audit,
        "post_repair_exhaustive_audit": post_audit,
        "validation": validation,
        "provenance_validation": "PASS" if _provenance_ok(repaired, registry) else "FAIL",
        "coverage_validation": "PASS" if coverage_consistent else "FAIL",
        "quality_unchanged": quality_unchanged,
        "llm_calls": 0, "web_calls": 0, "research_runs": 0,
        "phase18_total_tokens": prior["phase18_total_tokens"],
        "hard_500k": prior["hard_500k"], "preferred_400k": prior["preferred_400k"],
        "other_company_runs": 0, "production_changes": 0,
        "overall_status": ("TOKEN_EFFICIENCY_7760_PHASE18_OFFLINE_FINAL_PASS" if all_pass
                           else "FAIL_TOKEN_EFFICIENCY_7760_PHASE18J_CONTRACT"),
    }
    _write(output_dir / "replayed-response.json", repaired)
    _write(output_dir / "coverage-matrix.json", coverage["coverage_matrix"])
    _write(output_dir / "pre-repair-exhaustive-audit.json", pre_audit)
    _write(output_dir / "post-repair-exhaustive-audit.json", post_audit)
    _write(output_dir / "canonical-schema.json", strategy_v5.canonical_output_schema())
    _write(output_dir / "validation-result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = replay(args.root.resolve(), args.output_dir.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["overall_status"] == "TOKEN_EFFICIENCY_7760_PHASE18_OFFLINE_FINAL_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
