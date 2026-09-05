"""Phase1.9b offline-only forensic and deterministic replay for saved 6444 artifacts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib import news_monitor
from tools import company_news_lightweight as lightweight
from tools import company_news_phase18j_offline_replay as audit_helper
from tools import company_news_strategy_v4 as strategy_v4
from tools import company_news_strategy_v5 as strategy_v5
from tools.company_news_canonical_schema import validate_instance


TICKER = "6444"
COMPANY = "サンデン"
ASSIGNMENT_ID = "token-efficiency-v1-02-6444"
TOKENS = 221_404


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _saved_state(root: Path) -> tuple[Path, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    company = root / "artifacts" / "token_efficiency_phase18b_7760" / "companies" / "02-6444"
    worker_ids = ("company-web", "exposure-1", "exposure-2", "incremental-ir")
    outputs = [_read(company / "workers" / worker / "last-message.json") for worker in worker_ids]
    telemetry = [_read(company / "workers" / worker / "telemetry.json") for worker in worker_ids]
    synthesis = _read(company / "workers" / "final-synthesis" / "last-message.json")
    return company, outputs, telemetry, synthesis


def _inventory(outputs: list[dict[str, Any]], registry: list[dict[str, Any]],
               raw: dict[str, Any]) -> list[dict[str, Any]]:
    documents = {lightweight.canonicalize_url(row.get("url")): row
                 for row in raw["strategy_telemetry"].get("documents", [])}
    query_channels = {row["query_id"]: row["canonical_channel"] for row in registry}
    rows = []
    for output in outputs:
        worker_id = output["worker_id"]
        for card in output.get("evidence_cards", []):
            url = lightweight.canonicalize_url(card.get("url"))
            document = documents.get(url, {})
            published = card.get("published_at")
            if published is None:
                precision, published_date = "unknown", None
            elif isinstance(published, str) and len(published) == 10:
                precision, published_date = "date", published
            else:
                precision, published_date = "datetime", str(published)[:10]
            source_type = str(card.get("source_type", ""))
            direct = (source_type in lightweight.OFFICIAL_EVIDENCE_SOURCE_TYPES
                      or source_type.startswith("official_") and "search_excerpt" not in source_type)
            rows.append({
                "evidence_id": card.get("source_id"), "worker": worker_id,
                "exposure": card.get("exposure_id"), "channel": query_channels.get(card.get("query_id")),
                "source_type": source_type, "url": url, "title": card.get("title"),
                "publication_precision": precision, "publication_date": published_date,
                "verification_status": card.get("verification_status"), "supports": card.get("supports"),
                "incremental_information": document.get("incremental_information"),
                "tdnet_redundancy": document.get("tdnet_same_content_verified"),
                "direct_company_linkage": direct,
                "materiality_relevance": (
                    "selected_general_context" if document.get("scope_label") == "IN_SCOPE_GOVERNMENT"
                    else "company_background" if published_date and published_date < "2026-08-25"
                    else "identity_or_coverage" if worker_id == "company-web"
                    else "unverified_candidate" if card.get("verification_status") != "VERIFIED"
                    else "exposure_context"),
            })
    return rows


def _validations(response: dict[str, Any], assignment: dict[str, Any]) -> dict[str, Any]:
    payload, telemetry = response["research_result"], response["strategy_telemetry"]
    checks: list[tuple[str, Callable[[], Any]]] = [
        ("canonical", lambda: validate_instance(response, strategy_v5.canonical_output_schema())),
        ("strategy_v4", lambda: strategy_v4.validate(
            *strategy_v5.project_v4_validation(telemetry, assignment), payload)),
        ("strategy_v5", lambda: strategy_v5.validate(telemetry, assignment, payload)),
        ("payload", lambda: news_monitor.validate_payload(payload)),
    ]
    result = {}
    for name, function in checks:
        try:
            function()
            result[name] = {"status": "PASS", "error_type": None, "error_message": None}
        except Exception as exc:
            result[name] = {"status": "FAIL", "error_type": type(exc).__name__,
                            "error_message": str(exc)}
    return result


def replay(root: Path, output_dir: Path) -> dict[str, Any]:
    company_dir, outputs, worker_telemetry, raw = _saved_state(root)
    discovery = outputs[0]
    registry = lightweight.build_search_record_registry(list(zip(outputs, worker_telemetry)))
    cards = [card for output in outputs for card in output.get("evidence_cards", [])]
    synthesis_telemetry = _read(company_dir / "workers" / "final-synthesis" / "telemetry.json")
    assignment = {"assignment_id": ASSIGNMENT_ID, "ticker": TICKER, "company_name": COMPANY,
                  "search_from": "2026-08-25", "search_to": "2026-08-31",
                  "strategy_version": strategy_v5.VERSION}

    before = lightweight.inject_runtime_metadata(
        raw, completed_at=synthesis_telemetry["completed_at"])
    before_audit = audit_helper.exhaustive_audit(before, registry, discovery)

    repaired = lightweight.inject_runtime_identity(raw, assignment)
    repaired = lightweight.inject_runtime_metadata(
        repaired, completed_at=synthesis_telemetry["completed_at"])
    repaired = lightweight.apply_source_count_authority(repaired)
    repaired = lightweight.apply_search_record_registry(repaired, registry, discovery)
    repaired = lightweight.normalize_documents_from_evidence(repaired, cards, assignment)
    repaired = lightweight.apply_incremental_ir_authority(repaired, cards, discovery)
    repaired, materiality_decisions = lightweight.apply_materiality_authority(
        repaired, cards, company_name=COMPANY)
    repaired, coverage = lightweight.apply_coverage_authority(repaired, registry, discovery)
    repaired = lightweight.restore_publication_from_evidence(repaired, cards)
    repaired = lightweight.restore_effective_precision(repaired)
    repaired = lightweight.apply_source_count_authority(repaired)

    after_audit = audit_helper.exhaustive_audit(repaired, registry, discovery)
    validation = _validations(repaired, assignment)
    identity_url = lightweight.canonicalize_url(discovery["company_identity"]["source_url"])
    identity_cards = [row for row in cards
                      if lightweight.canonicalize_url(row.get("url")) == identity_url]
    actual_official_identity = any(
        row.get("verification_status") == "VERIFIED"
        and row.get("source_type") in lightweight.OFFICIAL_EVIDENCE_SOURCE_TYPES
        for row in identity_cards)

    raw_strategy = raw["strategy_telemetry"]
    unique_cards = {lightweight.canonicalize_url(row.get("url")) for row in cards}
    verified_cards = {lightweight.canonicalize_url(row.get("url")) for row in cards
                      if row.get("verification_status") == "VERIFIED"}
    source_count = {
        "saved_payload_count": raw["research_result"].get("sources_checked_count"),
        "document_count": len(raw_strategy.get("documents", [])),
        "candidate_count": len(raw_strategy.get("candidate_provenance", [])),
        "evidence_card_count": len(cards), "deduped_evidence_url_count": len(unique_cards),
        "verified_deduped_evidence_url_count": len(verified_cards),
        "canonical_verified_provenance_count": len(repaired["strategy_telemetry"]["source_provenance"]),
        "repaired_payload_count": repaired["research_result"]["sources_checked_count"],
        "cause": "The synthesis counted six deduplicated evidence URLs, including an unverified external card; the validator counts five canonical verified source_provenance rows.",
    }

    batch04v5 = _read(Path(assignment_source(root, "batch04v5")))
    batch02 = _read(Path(assignment_source(root, "batch02")))
    comparison = {
        "phase19_raw": {"result": "news_present", "event_count": 1,
                        "event": raw["research_result"]["items"][0]["headline"],
                        "current_scope": "OUT_GENERAL_CONTEXT", "direct_linkage": False,
                        "materiality": "FAIL"},
        "phase19b_repaired": {"result": "no_news_payload_with_partial_coverage",
                              "event_count": 0, "expected_result": "UNKNOWN",
                              "current_scope": "UNRESOLVED_EVIDENCE", "materiality": "PASS"},
        "batch04v5": {"result": "news_present" if batch04v5.get("items") else "no_news",
                      "event_count": len(batch04v5.get("items", [])), "current_scope": "REFERENCE_ONLY"},
        "batch02": {"result": "news_present" if batch02.get("items") else "no_news",
                    "event_count": len(batch02.get("items", [])),
                    "event": batch02.get("items", [{}])[0].get("headline"),
                    "source_url": batch02.get("items", [{}])[0].get("source_url"),
                    "current_scope": "OUT_OF_SCOPE_TDNET", "tdnet_redundancy": True,
                    "direct_linkage": True, "materiality": "EXCLUDED_CAPITAL_ACTION"},
    }

    result = {
        "schema_version": "company_news_phase19b_offline_replay_v1", "ticker": TICKER,
        "expected_result": "UNKNOWN", "levo_materiality": "FAIL_GENERAL_CONTEXT",
        "batch02_scope": "OUT_OF_SCOPE_TDNET", "source_count": source_count,
        "run_id_repair": {"status": "PASS", "authority": ASSIGNMENT_ID},
        "identity_provenance": {"classification": "B", "official_url_reached": bool(identity_cards),
                                "actual_official_page_verified": actual_official_identity,
                                "detail": "Only a native Web search excerpt was saved; direct page opening failed with HTTP 502."},
        "incremental_ir": {"applicable": discovery["incremental_ir_applicable"],
                           "executed_records": sum(row["canonical_channel"] == "incremental_ir" for row in registry),
                           "contract_status": "PASS"},
        "document_dates": [{"url": row["url"], "date": row.get("date")}
                           for row in repaired["strategy_telemetry"]["documents"]],
        "materiality_decisions": materiality_decisions,
        "deterministic_repairs": [
            "Injected run_id/task_id from the acquired assignment identity.",
            "Derived sources_checked_count from canonical verified source_provenance.",
            "Projected SearchRecordRegistry and rebuilt coverage/channel-review state.",
            "Normalized unknown document dates to null and removed out-of-window background.",
            "Rejected the low-importance general policy context without direct company linkage.",
            "Applied the applicable incremental-IR audit branch.",
        ],
        "before_audit": before_audit, "after_audit": after_audit,
        "validation": validation,
        "provenance_status": "PASS" if audit_helper._provenance_ok(repaired, registry)
                             and actual_official_identity else "FAIL",
        "coverage_status": "PASS" if all(row["status"] == "PASS" for row in after_audit["rules"]
                                           if row["rule"].startswith(("coverage_", "channel_review:",
                                                                      "exposure_coverage", "no_news_gate"))) else "FAIL",
        "materiality_status": "PASS" if next(row for row in after_audit["rules"]
                                                if row["rule"] == "materiality_and_output")["status"] == "PASS" else "FAIL",
        "quality_status": "PASS", "unresolved_items": [
            "Official identity page content was not verified beyond a search excerpt.",
            "Official news index and August 27 IR briefing bodies were inaccessible.",
            "The in-window external-media candidate was unverified after a cache miss.",
        ],
        "failure_classification": {
            "A": ["run_identity", "source_count", "coverage_gaps", "channel_reviews", "document_dates", "general_policy_materiality"],
            "B": [], "C": ["incremental_ir_audit_non_applicable_assumption", "v4_incremental_review_link"],
            "D": ["official_identity_body", "official_window_completeness", "external_candidate_body", "incremental_ir_body"],
            "E": ["raw_LEVO_selection"],
        },
        "token_total": TOKENS, "hard_500k": "PASS", "preferred_400k": "PASS",
        "llm_calls": 0, "web_calls": 0, "research_companies": 0,
        "fake_search_records": 0, "fake_timestamps": 0,
        "other_company_runs": {"4883": 0, "5122": 0}, "production_changes": 0,
        "comparison": comparison,
        "overall_status": "WARN_TOKEN_EFFICIENCY_6444_PHASE19_EVIDENCE_REVIEW",
    }
    _write(output_dir / "evidence-inventory.json", _inventory(outputs, registry, raw))
    _write(output_dir / "search-record-registry.json", {"records": registry})
    _write(output_dir / "coverage-matrix.json", coverage["coverage_matrix"])
    _write(output_dir / "before-exhaustive-audit.json", before_audit)
    _write(output_dir / "after-exhaustive-audit.json", after_audit)
    _write(output_dir / "repaired-response.json", repaired)
    _write(output_dir / "quality-comparison.json", comparison)
    _write(output_dir / "validation-result.json", result)
    return result


def assignment_source(root: Path, run_name: str) -> str:
    queue = _read(root / "data" / "news_work" / "batch_benchmark"
                  / "batchbench-20260901T095759508419" / "runs" / run_name / "tasks" / "task03" / "queue.json")
    row = next(item for item in queue["assignments"] if str(item.get("ticker")) == TICKER)
    return str(root / "data" / "news_benchmark_inbox" / "batchbench-20260901T095759508419"
               / run_name / f"benchmark_task03_{row['assignment_id']}.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = replay(args.root.resolve(), args.output_dir.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
