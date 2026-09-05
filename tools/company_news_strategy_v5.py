"""V2 query-fidelity benchmark contract with bounded v4 scope additions.

This module is pure validation and planning code.  It performs no search,
activation, routing, queue, production, or network operation.
"""
from __future__ import annotations

import inspect
import math
import re
from pathlib import Path

from lib import news_monitor
from tools import company_news_batch_benchmark as b
from tools import company_news_strategy_v4 as v4
from tools.company_news_canonical_schema import (
    canonical_output_schema as build_canonical_output_schema,
    schema_sha256,
    validate_instance,
)

VERSION = "company_news_research_strategy_v5"
RUN_NAME = "batch04v5"
SCHEMA = "company_news_benchmark_strategy_telemetry_v5"
PROMPT_NAME = "company_news_research_strategy_v5_prompt.md"
EXPECTED_ORDER_SHA256 = "3191f7fd27034417eefd44b2f536699ef700cd3eb6d6b80c7cd014ae817d992e"
IN_SCOPE = v4.IN_SCOPE
OUT_SCOPE = v4.OUT_SCOPE
SCOPE_LABELS = v4.SCOPE_LABELS
CHANNELS = v4.CHANNELS
EXPOSURE_STATUSES = {"SEARCHED_FOUND", "SEARCHED_NO_RESULT", "NOT_APPLICABLE"}
GOVERNMENT_SIGNALS = {"regulation", "financing", "rate", "tariff", "subsidy", "safety", "procurement"}


def canonical_output_schema():
    """Return the generator and validator's shared nested output contract."""
    return build_canonical_output_schema(
        telemetry_schema=SCHEMA,
        strategy_version=VERSION,
        channels=CHANNELS,
        scope_labels=SCOPE_LABELS,
        exposure_statuses=EXPOSURE_STATUSES,
    )


def canonical_schema_sha256():
    return schema_sha256(canonical_output_schema())


def validate_generated_response(value):
    validate_instance(value, canonical_output_schema())
    for index, item in enumerate(value["research_result"]["items"]):
        news_monitor.normalize_publication_fields(item, f"items[{index}]", allow_legacy=False)
        news_monitor.normalize_effective_fields(item, "valid_from", allow_legacy=False)
        news_monitor.normalize_effective_fields(item, "valid_until", allow_legacy=False)
    return value


def require(ok, message):
    if not ok:
        raise b.BenchmarkError("strategy v5: " + message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def contract():
    return {
        "strategy_version": VERSION,
        "baseline_strategy_version": b.STRATEGY_V2_VERSION,
        "baseline_behavior": "v2_official_first_specific_driver_query_fidelity",
        "inherited_from_v4": ["non_tdnet_scope", "bounded_company_web_window_scan", "incremental_ir", "tdnet_exclusion", "trivial_exclusion"],
        "exposure_query_rewrite_inherited_from_v4": False,
        "scope_schema": v4.SCOPE_SCHEMA,
        "production_write_forbidden": True,
        "economic_exposure_minimum": 2,
        "economic_exposure_maximum": 3,
        "specific_driver_query_minimum_per_exposure": 1,
        "company_name_required_in_external_query": False,
        "company_web": {"index_families": ["news", "press", "ir"], "deep_profile_default": False, "redundant_background_default": False},
        "incremental_ir_allowed_information": ["pricing", "volume", "order", "capacity", "customer", "margin_assumption", "supply_constraint", "capex"],
        "query_target_average_max": 5.90,
        "query_budget_is_failure_gate": False,
        "time_budget_is_failure_gate": False,
        "token_budget_is_failure_gate": False,
        "partial_is_no_news": False,
        "candidate_only_event_policy": "NOT_A_FALSE_POSITIVE_PENDING_BLIND_ADJUDICATION",
        "precision_status_after_run": "PROVISIONAL_PENDING_SCOPE_AND_BLIND_ADJUDICATION",
    }


def evaluation_policy():
    return {
        "primary_metric": "non_tdnet_material_recall_v1",
        "denominator": 27,
        "material_found_minimum": 16,
        "material_found_preferred": 18,
        "scope_minimum": {"COMPANY_WEB": [6, 7], "EXTERNAL_MEDIA": [4, 8], "GOVERNMENT": [2, 4], "COUNTERPARTY": [3, 7], "INCREMENTAL_IR": [1, 1]},
        "scheduled_total_tokens_hard_max": 170_698_094,
        "scheduled_total_tokens_preferred_max": 150_000_000,
        "average_queries_per_company_max": 5.90,
        "average_research_seconds_hard_max": 90,
        "average_research_seconds_preferred_max": 60,
        "budgets_are_diagnostic": True,
        "failure_count_target": 0,
        "same_cohort_pass_authorizes_production": False,
    }


def prompt_text(root: Path) -> str:
    return (root / "config" / PROMPT_NAME).read_text(encoding="utf-8").replace("{{REPO}}", str(root.resolve()))


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def query_plan(company_name, exposures, *, search_from, search_to):
    """Build v2-style specific queries without replacing canonical drivers."""
    require(isinstance(exposures, list) and 2 <= len(exposures) <= 3, "derive 2..3 economic exposures")
    require(all(isinstance(x, dict) and all(text(x.get(k)) for k in
                ("canonical_driver_phrase", "metadata_evidence", "transmission_channel")) for x in exposures),
            "economic exposure metadata/transmission missing")
    require(text(company_name), "company name missing")
    require(text(search_from) and text(search_to), "window missing")
    plan = [{"channel": "official", "query": f"{company_name} news press IR {search_from} {search_to}"}]
    for index, exposure in enumerate(exposures):
        driver = exposure["canonical_driver_phrase"]
        require(text(driver), "canonical driver phrase missing")
        context = exposure.get("product_context", "")
        query = " ".join(x for x in (driver, context, search_from, search_to) if x)
        plan.append({"channel": exposure.get("channel", "external_media"), "query": query,
                     "exposure_index": index, "canonical_driver_phrase": driver,
                     "driver_phrase_preserved": _normalized(driver) in _normalized(query)})
        if exposure.get("government_applicable") is True:
            plan.append({"channel": "government", "query": f"{driver} government {search_from} {search_to}",
                         "exposure_index": index, "canonical_driver_phrase": driver,
                         "driver_phrase_preserved": True})
    return plan


def validate(value, assignment, payload):
    require(isinstance(value, dict), "telemetry must be object")
    require(value.get("schema_version") == SCHEMA and value.get("strategy_version") == VERSION
            and assignment.get("strategy_version") == VERSION, "version mismatch")
    exposures = value.get("economic_exposures")
    require(isinstance(exposures, list) and 2 <= len(exposures) <= 3, "derive 2..3 economic exposures")
    for exposure in exposures:
        require(all(text(exposure.get(k)) for k in ("canonical_driver_phrase", "metadata_evidence", "transmission_channel")), "exposure telemetry incomplete")
        require(type(exposure.get("government_applicable")) is bool and text(exposure.get("government_applicability_reason")), "government applicability missing")
    queries = value.get("queries")
    require(isinstance(queries, list) and queries, "queries missing")
    by_exposure = {i: [] for i in range(len(exposures))}
    seen_queries = set()
    for query in queries:
        require(query.get("channel") in CHANNELS and text(query.get("query")) and query.get("executed") is True, "query execution record missing")
        query_key = _normalized(query["query"])
        require(query_key not in seen_queries, "duplicate query execution")
        seen_queries.add(query_key)
        require(type(query.get("result_count")) is int and query["result_count"] >= 0
                and isinstance(query.get("reached_sources"), list), "query result telemetry missing")
        require(query.get("window_relevance_checked") is True, "window relevance not checked")
        if query.get("exposure_index") is not None:
            index = query["exposure_index"]
            require(type(index) is int and index in by_exposure, "query exposure link invalid")
            require(text(query.get("canonical_driver_phrase")) and type(query.get("driver_phrase_preserved")) is bool, "driver preservation telemetry missing")
            if not query["driver_phrase_preserved"]:
                require(text(query.get("driver_phrase_preservation_reason")), "unpreserved driver needs reason")
            by_exposure[index].append(query)
    for index, exposure in enumerate(exposures):
        driver = _normalized(exposure["canonical_driver_phrase"])
        specific = [q for q in by_exposure[index] if q["channel"] != "government"]
        require(specific, "specific query not generated for major exposure")
        require(any(q["driver_phrase_preserved"] and driver in _normalized(q["query"]) for q in specific), "canonical driver phrase not preserved in a query")
        government = [q for q in by_exposure[index] if q["channel"] == "government"]
        require((len(government) == 1) if exposure["government_applicable"] else not government, "government applicability/query inconsistency")
    coverage = value.get("no_news_exposure_coverage")
    require(isinstance(coverage, list) and len(coverage) == len(exposures), "semantic exposure coverage incomplete")
    require({c.get("exposure_index") for c in coverage} == set(by_exposure), "semantic exposure coverage links invalid")
    if not payload.get("items"):
        for item in coverage:
            require(item.get("status") in EXPOSURE_STATUSES and text(item.get("evidence")), "no-news exposure coverage invalid")
            if item["status"].startswith("SEARCHED_"):
                require(by_exposure[item["exposure_index"]], "searched coverage has no query")
    web = value.get("company_web_review")
    require(isinstance(web, dict) and web.get("index_checked") is True and web.get("window_items_checked") is True
            and web.get("material_candidates_checked") is True, "bounded Company Web review incomplete")
    for key in ("background_pages_opened", "redundant_pages_opened", "old_business_pages_opened", "provenance_only_pages_opened"):
        require(type(web.get(key)) is int and web[key] >= 0, "Company Web traversal telemetry missing")
    times = value.get("stage_elapsed_seconds")
    require(isinstance(times, dict) and {"company_web", "external", "provenance"} <= set(times), "stage timing incomplete")
    for elapsed in [*times.values(), value.get("total_research_elapsed_seconds")]:
        require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0, "invalid elapsed time")
    require(value["total_research_elapsed_seconds"] >= sum(times.values()), "total shorter than stage times")
    require(value.get("candidate_count") == len(value.get("candidate_provenance", []))
            and value.get("final_event_count") == len(payload.get("items", [])), "candidate/final counts mismatch")

    # Reuse the frozen v4 scope/candidate safety checks without inheriting its
    # query planning.  The copied object is validation-only and never persisted.
    common = dict(value)
    common.update(schema_version=v4.SCHEMA, strategy_version=v4.VERSION)
    common_assignment = dict(assignment, strategy_version=v4.VERSION)
    common["economic_exposures"] = [{"driver": x["canonical_driver_phrase"], "metadata_evidence": x["metadata_evidence"], "transmission_channel": x["transmission_channel"]} for x in exposures]
    common["stage_elapsed_seconds"] = {"company_web": times["company_web"], "external_complement": times["external"] + times["provenance"]}
    v4.validate(common, common_assignment, payload)
    validate_instance(value, canonical_output_schema()["properties"]["strategy_telemetry"], "$.strategy_telemetry")
    return value


def leakage_scan(prompt, assignments, companies, *, root=None, benchmark_id=None):
    runtime = "\n".join(inspect.getsource(fn) for fn in (contract, query_plan, validate))
    scan_text = prompt + "\n" + runtime
    scan = v4.leakage_scan(scan_text, assignments, companies)
    hits = list(scan["hits"])
    forbidden = ("miss title", "gold event title", "common48 event mapping", "v2 found company")
    title_hits = [term for term in forbidden if term in scan_text.casefold()]
    url_hits = []
    if root is not None and benchmark_id is not None:
        gold_path = b._benchmark_dir(Path(root), benchmark_id) / "quality_v2" / "matcher_v2" / "gold_adjudication_v2.json"
        if gold_path.exists():
            titles, urls = set(), set()
            def collect(node):
                if isinstance(node, dict):
                    for key, item in node.items():
                        if isinstance(item, str) and key in {"headline", "title"} and len(item.strip()) >= 8:
                            titles.add(item.strip().casefold())
                        if isinstance(item, str) and key in {"source_url", "url"} and item.startswith(("http://", "https://")):
                            urls.add(item.strip())
                        collect(item)
                elif isinstance(node, list):
                    for item in node:
                        collect(item)
            collect(b._read_json(gold_path))
            folded = scan_text.casefold()
            title_hits.extend(title for title in titles if title in folded)
            url_hits.extend(url for url in urls if url in scan_text)
    hits.extend("gold_title_marker:" + term for term in title_hits)
    hits.extend("gold_url_literal" for _ in url_hits)
    return {**scan, "gold_title_leakage_count": len(title_hits),
            "gold_url_leakage_count": max(scan["gold_url_leakage_count"], len(url_hits)), "hits": sorted(set(hits)),
            "hit_count": len(set(hits)), "status": "FAIL" if hits else "PASS"}
