"""Synthetic Strategy v5 tests; no network, scheduler, or production access."""
import copy
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools import company_news_batch_benchmark as b
from tools import company_news_strategy_v5 as v
from tools.company_news_canonical_schema import SchemaContractError


def fixture():
    assignment = {"assignment_id": "synthetic-a1", "ticker": "X001", "company_name": "Example Works",
                  "search_from": "2026-08-25", "search_to": "2026-08-31", "strategy_version": v.VERSION}
    source = {"url": "https://example.test/news", "source_type": "company_news", "content_verified": True,
              "evidence_excerpt": "Existing metadata verifies identity"}
    exposures = [
        {"canonical_driver_phrase": "Baltic Dry Index", "metadata_evidence": "Freight affects delivered costs",
         "transmission_channel": "freight benchmark changes margin", "government_applicable": False,
         "government_applicability_reason": "No policy exposure", "product_context": None, "channel": "external_media"},
        {"canonical_driver_phrase": "customer vehicle production", "metadata_evidence": "Components sell to vehicle plants",
         "transmission_channel": "customer production changes order volume", "government_applicable": False,
         "government_applicability_reason": "No policy exposure", "product_context": None, "channel": "counterparty"},
    ]
    queries = []
    for index, exposure in enumerate(exposures):
        queries.append({"channel": exposure["channel"], "query": exposure["canonical_driver_phrase"] + " 2026-08-25 2026-08-31",
                        "exposure_index": index, "canonical_driver_phrase": exposure["canonical_driver_phrase"],
                        "driver_phrase_preserved": True, "executed": True, "result_count": 0,
                        "reached_sources": [], "window_relevance_checked": True,
                        "driver_phrase_preservation_reason": None})
    queries.insert(0, {"channel": "official", "query": "Example Works news press IR", "executed": True,
                       "result_count": 1, "reached_sources": [source["url"]], "window_relevance_checked": True,
                       "exposure_index": None, "canonical_driver_phrase": None,
                       "driver_phrase_preserved": None, "driver_phrase_preservation_reason": None})
    telemetry = {
        "schema_version": v.SCHEMA, "strategy_version": v.VERSION,
        "assignment_id": assignment["assignment_id"], "ticker": assignment["ticker"],
        "company_identity": {"verified_ticker": assignment["ticker"], "source_url": source["url"], "evidence": "metadata identity"},
        "economic_exposures": exposures, "queries": queries, "regulatory_themes": [], "query_budget_exceeded": False,
        "channel_reviews": [{"channel": "official", "status": "CHECKED", "evidence": "bounded window review", "exposure_index": None},
                            {"channel": "external_media", "status": "CHECKED", "evidence": "specific indicator searched", "exposure_index": 0},
                            {"channel": "counterparty", "status": "CHECKED", "evidence": "customer production searched", "exposure_index": 1}],
        "no_news_exposure_coverage": [{"exposure_index": 0, "status": "SEARCHED_NO_RESULT", "evidence": "zero window result"},
                                      {"exposure_index": 1, "status": "SEARCHED_NO_RESULT", "evidence": "zero window result"}],
        "company_web_review": {"index_checked": True, "window_items_checked": True, "material_candidates_checked": True,
                               "background_pages_opened": 0, "redundant_pages_opened": 0,
                               "old_business_pages_opened": 0, "provenance_only_pages_opened": 0},
        "documents": [], "coverage_gaps": [], "coverage_status": "COMPLETE", "source_provenance": [source],
        "candidate_provenance": [], "candidate_count": 0, "final_event_count": 0, "no_news_gate_pass": True,
        "window_review_complete": True, "stopped_after_first_event": False,
        "stage_elapsed_seconds": {"company_web": 2, "external": 5, "provenance": 1},
        "total_research_elapsed_seconds": 8,
    }
    return assignment, telemetry, {"sources_checked_count": 1, "items": []}


def canonical_response():
    assignment, telemetry, _ = fixture()
    research = {
        "schema_version": "company_news_v1", "run_id": assignment["assignment_id"],
        "task_id": assignment["assignment_id"], "ticker": assignment["ticker"],
        "collector_type": "codex_web_research", "collector_version": "company_news_lightweight_v1",
        "analysis_version": "company_news_v1", "checked_at": "2026-08-31T12:00:00+09:00",
        "sources_checked_count": 1, "items": [],
    }
    return {"research_result": research, "strategy_telemetry": telemetry}


def test_v2_baseline_and_v4_inheritance_are_explicit():
    c = v.contract()
    assert c["baseline_strategy_version"] == b.STRATEGY_V2_VERSION
    assert c["exposure_query_rewrite_inherited_from_v4"] is False
    assert c["inherited_from_v4"] == ["non_tdnet_scope", "bounded_company_web_window_scan", "incremental_ir", "tdnet_exclusion", "trivial_exclusion"]


def test_specific_indicator_customer_driver_and_company_name_optional():
    _, telemetry, _ = fixture()
    plan = v.query_plan("Example Works", telemetry["economic_exposures"], search_from="2026-08-25", search_to="2026-08-31")
    external = [q for q in plan if q.get("exposure_index") is not None]
    assert any("Baltic Dry Index" in q["query"] for q in external)
    assert any("customer vehicle production" in q["query"] for q in external)
    assert all(q["driver_phrase_preserved"] for q in external)
    assert all("Example Works" not in q["query"] for q in external)


def test_complete_semantic_no_news_telemetry_and_bounded_web():
    a, telemetry, payload = fixture()
    assert v.validate(telemetry, a, payload) is telemetry
    assert telemetry["company_web_review"]["redundant_pages_opened"] == 0


def test_canonical_generation_schema_accepts_full_valid_sample():
    response = canonical_response()
    assert v.validate_generated_response(response) is response
    assert len(v.canonical_schema_sha256()) == 64


@pytest.mark.parametrize("mutation,pattern", [
    (lambda r: r["strategy_telemetry"]["queries"][0].update(channel="company_web"), "channel"),
    (lambda r: r["strategy_telemetry"]["queries"][0].pop("executed"), "executed"),
    (lambda r: r["strategy_telemetry"].update(regulatory_themes=[{"theme": str(i), "exposure_index": 0} for i in range(3)]), "item count"),
    (lambda r: r["strategy_telemetry"].pop("no_news_exposure_coverage"), "no_news_exposure_coverage"),
])
def test_canonical_schema_rejects_aliases_and_missing_contract(mutation, pattern):
    response = canonical_response(); mutation(response)
    with pytest.raises(SchemaContractError, match=pattern):
        v.validate_generated_response(response)


def test_canonical_schema_rejects_long_earnings_relevance():
    response = canonical_response()
    response["research_result"]["items"] = [{
        "headline": "Price revision", "source_url": "https://example.test/news", "published_at": "2026-08-31T00:00:00+09:00",
        "valid_from": None, "valid_until": None, "source_type": "company", "source_name": "Example Works",
        "category": "pricing", "subcategory": None, "direction": "mixed", "importance": "medium",
        "earnings_relevance": "a detailed explanation that is deliberately longer than the canonical enum field permits",
        "summary": "Price revised", "why_it_matters": "Margin transmission", "evidence_excerpt": "New prices apply",
        "temporal_scope": "ongoing", "temporal_status": "ongoing", "tags": ["NON_TDNET"],
    }]
    with pytest.raises(SchemaContractError, match="earnings_relevance"):
        v.validate_generated_response(response)


@pytest.mark.parametrize("mutation,message", [
    (lambda t: t["queries"].pop(1), "specific query"),
    (lambda t: t["queries"][1].update(driver_phrase_preserved=False, driver_phrase_preservation_reason="translated"), "not preserved"),
    (lambda t: t["no_news_exposure_coverage"].pop(), "semantic exposure"),
    (lambda t: t["queries"][1].pop("result_count"), "query result telemetry"),
    (lambda t: t["company_web_review"].pop("index_checked"), "Company Web"),
    (lambda t: t["stage_elapsed_seconds"].pop("provenance"), "stage timing"),
])
def test_telemetry_fails_closed(mutation, message):
    a, telemetry, payload = fixture(); mutation(telemetry)
    with pytest.raises(b.BenchmarkError, match=message):
        v.validate(telemetry, a, payload)


def test_government_applicability_is_consistent():
    a, telemetry, payload = fixture()
    exposure = telemetry["economic_exposures"][0]
    exposure.update(government_applicable=True, government_applicability_reason="freight subsidy exposure")
    with pytest.raises(b.BenchmarkError, match="government applicability"):
        v.validate(telemetry, a, payload)
    telemetry["queries"].append({"channel": "government", "query": "Baltic Dry Index subsidy 2026-08-25",
                                 "exposure_index": 0, "canonical_driver_phrase": "Baltic Dry Index",
                                 "driver_phrase_preserved": True, "executed": True, "result_count": 0,
                                 "reached_sources": [], "window_relevance_checked": True,
                                 "driver_phrase_preservation_reason": None})
    telemetry["regulatory_themes"] = [{"theme": "freight subsidy", "exposure_index": 0}]
    assert v.validate(telemetry, a, payload)


def test_duplicate_query_is_rejected_before_research_state_is_accepted():
    a, telemetry, payload = fixture()
    telemetry["queries"].append(copy.deepcopy(telemetry["queries"][1]))
    telemetry["query_budget_exceeded"] = False
    with pytest.raises(b.BenchmarkError, match="duplicate query"):
        v.validate(telemetry, a, payload)


def test_group_source_incremental_ir_tdnet_and_trivial_rules():
    a, telemetry, payload = fixture()
    doc = {"date": "2026-08-26", "title": "Group customer capacity agreement", "url": "https://example.test/group",
           "final_url": "https://example.test/group", "scope_label": "IN_SCOPE_COUNTERPARTY", "scope_reason": "group source",
           "material": True, "body_status": "VERIFIED", "attempt_evidence": None,
           "tdnet_same_content_verified": None, "tdnet_url": None, "tdnet_identity_evidence": None,
           "incremental_information": None, "incremental_information_evidence": None, "index_evidence": None}
    telemetry["documents"] = [doc]
    telemetry["source_provenance"].append({**telemetry["source_provenance"][0], "url": doc["url"]})
    telemetry["candidate_provenance"] = [{"candidate_index": 0, "document_url": doc["url"], "selected_for_output": False,
                                           "selection_reason": "group source supported but below material threshold",
                                           "source_url": None, "headline": None, "economic_transmission": None, "event_key": None}]
    telemetry["candidate_count"] = 1
    payload["sources_checked_count"] = 2
    assert v.validate(telemetry, a, payload)
    doc.update(scope_label="IN_SCOPE_INCREMENTAL_IR", incremental_information=True,
               incremental_information_evidence="Q&A adds customer capacity constraint")
    assert v.validate(telemetry, a, payload)
    doc.update(scope_label="OUT_OF_SCOPE_TDNET", title="決算短信", material=False, body_status="SKIPPED",
               tdnet_same_content_verified=True, tdnet_url="https://example.test/tdnet", tdnet_identity_evidence="same filing",
               incremental_information=False)
    assert v.validate(telemetry, a, payload)
    doc.update(scope_label="OUT_OF_SCOPE_TRIVIAL", title="Holiday notice", index_evidence="official index", tdnet_same_content_verified=False)
    assert v.validate(telemetry, a, payload)


def test_leakage_and_identity_hardcodes_are_zero():
    root = Path(b.__file__).parents[1]
    scan = v.leakage_scan(v.prompt_text(root), {}, [])
    assert scan["status"] == "PASS"
    assert scan["company_hardcode_count"] == scan["ticker_hardcode_count"] == 0
    assert scan["gold_url_leakage_count"] == scan["gold_title_leakage_count"] == 0


def test_prepare_isolated_same_order_and_inert(tmp_path):
    companies = [{"ticker": f"X{i:03d}", "company_name": f"Synthetic Works {i}", "sector": "materials"} for i in range(192)]
    cohort = {"schema_version": b.BENCHMARK_SCHEMA, "benchmark_mode": True, "benchmark_id": "batchbench-v5-synthetic",
              "search_from": "2026-08-25", "search_to": "2026-08-31", "reference_tickers": [], "companies": companies}
    b.atomic_write_json(b._benchmark_dir(tmp_path, cohort["benchmark_id"]) / "cohort.json", cohort)
    b._prepare_named_run(tmp_path, cohort["benchmark_id"], cohort, companies, 4, "batch04v2", reference=False,
                         now=datetime(2026, 9, 5, tzinfo=timezone.utc))
    b.atomic_write_text(tmp_path / "config" / v.PROMPT_NAME, v.prompt_text(Path(b.__file__).parents[1]))
    manifest = b.prepare_run(tmp_path, cohort["benchmark_id"], 4, strategy=v.VERSION)
    expected = hashlib.sha256("\n".join(c["ticker"] for c in companies).encode()).hexdigest()
    assert manifest["status"] == "prepared" and manifest["company_order_sha256"] == expected
    assert manifest["baseline_run_name"] == "batch04v2" and not b._paths(tmp_path)["active"].exists()
    assert not (tmp_path / "data" / "news_inbox").exists()
