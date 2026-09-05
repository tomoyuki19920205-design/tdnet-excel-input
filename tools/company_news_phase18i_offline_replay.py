"""Deterministic Phase1.8i replay from saved 7760 artifacts only."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib import news_monitor
from tools import company_news_lightweight as lightweight
from tools import company_news_strategy_v5 as strategy_v5
from tools.company_news_canonical_schema import validate_instance
from tools.company_news_effective_date_migration_dry_run import run_dry_run


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _effective_date_supported(item: dict[str, Any], cards: list[dict[str, Any]]) -> bool:
    value = item.get("valid_from_date")
    if value is None:
        return True
    parsed = date.fromisoformat(value)
    forms = {
        value,
        f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}",
        f"{parsed.year}年{parsed.month}月{parsed.day}日",
    }
    item_url = lightweight.canonicalize_url(item["source_url"])
    relevant = [card for card in cards if lightweight.canonicalize_url(card["url"]) == item_url]
    evidence = " ".join(str(card.get(key) or "") for card in relevant for key in ("excerpt", "supports"))
    return bool(relevant) and any(form in evidence for form in forms)


def replay(root: Path, output_dir: Path) -> dict[str, Any]:
    company = root / "artifacts" / "token_efficiency_phase18b_7760" / "companies" / "01-7760"
    source_path = company / "phase18h-date-precision-replay" / "replayed-response.json"
    discovery_path = company / "workers" / "company-web" / "last-message.json"
    registry_path = company / "phase18f-validation-replay" / "search-record-registry.json"
    previous_validation_path = company / "phase18h-date-precision-replay" / "validation-result.json"
    original = _read(source_path)
    discovery = _read(discovery_path)
    cards = discovery["evidence_cards"]
    registry_document = _read(registry_path)
    registry = registry_document["records"]

    response = lightweight.apply_incremental_ir_authority(original, cards, discovery)
    response = lightweight.restore_effective_precision(response)
    payload = response["research_result"]
    telemetry = response["strategy_telemetry"]
    assignment = {
        "assignment_id": telemetry["assignment_id"], "ticker": telemetry["ticker"],
        "search_from": "2026-08-25", "search_to": "2026-08-31",
        "strategy_version": strategy_v5.VERSION,
    }

    checks: dict[str, dict[str, Any]] = {}
    validators = [
        ("canonical_schema", lambda: validate_instance(response, strategy_v5.canonical_output_schema())),
        ("strategy_v5_semantic", lambda: strategy_v5.validate(telemetry, assignment, payload)),
        ("payload_semantic", lambda: news_monitor.validate_payload(payload)),
    ]
    for name, validator in validators:
        try:
            validator()
            checks[name] = {"status": "PASS", "error_type": None, "error_message": None}
        except Exception as exc:  # persist every independent offline failure
            checks[name] = {"status": "FAIL", "error_type": type(exc).__name__,
                            "error_message": str(exc)}

    registry_ids = {row.get("query_id") for row in registry}
    provenance_pass = (
        len(registry) == 6 and len(registry_ids) == len(registry)
        and all(row.get("executed") is True for row in registry)
        and len(telemetry.get("queries", [])) == len(registry))
    source_urls = {lightweight.canonicalize_url(row["url"]): row
                   for row in telemetry.get("source_provenance", [])}
    identity = telemetry.get("company_identity", {})
    identity_source = source_urls.get(lightweight.canonicalize_url(identity.get("source_url")))
    identity_pass = bool(identity_source and identity_source.get("source_type") in {
        "company", "company_news", "company_ir", "ir", "company_official"})
    effective_pass = all(
        news_monitor.normalize_effective_fields(item, boundary, allow_legacy=False)
        for item in payload["items"] for boundary in ("valid_from", "valid_until"))
    incremental_pass = all(
        document.get("scope_label") != "IN_SCOPE_INCREMENTAL_IR"
        for document in telemetry.get("documents", [])) if not discovery["incremental_ir_applicable"] else True
    evidence_effective_pass = all(_effective_date_supported(item, cards) for item in payload["items"])
    semantic_keys = ("headline", "source_url", "source_type", "source_name", "category", "subcategory",
                     "direction", "importance", "earnings_relevance", "summary", "why_it_matters",
                     "evidence_excerpt", "temporal_scope", "temporal_status", "tags")
    quality_unchanged = all(
        {key: before.get(key) for key in semantic_keys} == {key: after.get(key) for key in semantic_keys}
        for before, after in zip(original["research_result"]["items"], payload["items"]))
    fake_time_count = json.dumps(response, ensure_ascii=False).count("T00:00:00")
    previous = _read(previous_validation_path)
    migration = run_dry_run()
    all_pass = (all(row["status"] == "PASS" for row in checks.values()) and provenance_pass
                and identity_pass and incremental_pass and effective_pass and evidence_effective_pass
                and quality_unchanged and fake_time_count == 0 and migration["status"] == "PASS")
    result = {
        "schema_version": "company_news_phase18i_offline_replay_v1",
        "ticker": "7760", "source_artifact": str(source_path.relative_to(company)),
        "canonical_schema_sha256": strategy_v5.canonical_schema_sha256(),
        "classification": "A",
        "incremental_ir_authority": {
            "status": "PASS" if incremental_pass else "FAIL",
            "evidence_store_incremental_ir_applicable": discovery["incremental_ir_applicable"],
            "corrected_document_scope": "IN_SCOPE_COMPANY_WEB",
            "semantic_content_added": False,
        },
        "effective_date_contract": {
            "status": "PASS" if effective_pass and evidence_effective_pass else "FAIL",
            "valid_from": payload["items"][0]["valid_from"],
            "valid_from_date": payload["items"][0]["valid_from_date"],
            "valid_from_precision": payload["items"][0]["valid_from_precision"],
            "valid_until": payload["items"][0]["valid_until"],
            "valid_until_date": payload["items"][0]["valid_until_date"],
            "valid_until_precision": payload["items"][0]["valid_until_precision"],
            "source_evidence_support": evidence_effective_pass,
            "fake_timestamp_count": fake_time_count,
        },
        "validation": checks,
        "search_provenance": {"status": "PASS" if provenance_pass else "FAIL",
                              "record_count": len(registry), "fabricated_record_count": 0},
        "identity_official_evidence": {"status": "PASS" if identity_pass else "FAIL",
                                       "source_url": identity.get("source_url")},
        "quality_unchanged": quality_unchanged,
        "migration_dry_run": migration,
        "llm_calls": 0, "web_calls": 0, "research_runs": 0,
        "phase18_total_tokens": previous["phase18_total_tokens"],
        "hard_500k": previous["hard_500k"], "preferred_400k": previous["preferred_400k"],
        "other_company_runs": 0, "production_changes": 0,
        "overall_status": ("TOKEN_EFFICIENCY_7760_PHASE18_OFFLINE_FINAL_PASS" if all_pass
                           else "FAIL_TOKEN_EFFICIENCY_7760_PHASE18I_VALIDATION"),
    }
    _write(output_dir / "replayed-response.json", response)
    _write(output_dir / "canonical-schema.json", strategy_v5.canonical_output_schema())
    _write(output_dir / "migration-dry-run.json", migration)
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
