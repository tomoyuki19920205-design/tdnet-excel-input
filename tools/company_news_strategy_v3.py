"""Benchmark-only document coverage, focused external discovery and validation.

This module does not fetch sources or execute research. It defines the generic
runtime contract and verifies persisted, evidence-backed coverage before save.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from tools import company_news_batch_benchmark as b

VERSION = "company_news_research_strategy_v3"
RUN_NAME = "batch04v3"
SCHEMA = "company_news_benchmark_strategy_telemetry_v3"
PROMPT_NAME = "company_news_research_strategy_v3_prompt.md"
STAGES = {"official_first", "regulatory_pass", "targeted_second_pass", "company_external_check"}
DOCUMENT_TYPES = {"pdf", "html", "attachment", "alternate_document"}
TRIGGERS = {"material_candidate_zero", "source_diversity_insufficient", "explicit_external_exposure", "external_driver_uncovered"}
# Business taxonomy only, never company identifiers or regulator/agency names.
REGULATORY = {
    "transport": (("transport", "logistics", "運輸", "陸運", "海運", "空運", "倉庫"), ("安全", "許可")),
    "finance": (("finance", "bank", "insurance", "銀行", "証券", "保険", "金融"), ("処分", "認可")),
    "healthcare": (("healthcare", "medical", "医薬", "医療"), ("承認", "回収")),
    "energy": (("energy", "utility", "電力", "ガス", "石油"), ("安全", "認可")),
    "environment": (("recycling", "environment", "リサイクル", "環境", "廃棄"), ("許可", "採択")),
    "construction": (("construction", "建設", "建材", "不動産"), ("許可", "命令")),
    "telecom": (("telecom", "通信"), ("行政", "届出")),
    "food": (("food", "食品", "食料"), ("回収", "安全")),
}


def require(ok, message):
    if not ok:
        raise b.BenchmarkError("strategy v3: " + message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def url(value):
    return text(value) and urlsplit(value).scheme in {"http", "https"} and bool(urlsplit(value).hostname)


def count(value):
    return type(value) is int and value >= 0


def stamp(value):
    require(text(value), "timestamp missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "timestamp must have timezone")
    return parsed


def contract():
    return {"strategy_version": VERSION, "required_stages": ["official_first", "focused_external_pass"],
        "official_query_budget": {"reserved_minimum": 2, "maximum": 3},
        "external_query_budget": {"reserved_minimum": 1, "maximum": 3},
        "per_company_query_budget": {"reserved_minimum": 3, "maximum": 6},
        "document_follow_counts_as_query": False, "budget_transfer_between_companies": False,
        "regulatory_theme_limit": 2, "driver_group_limit": 3, "candidate_event_limit": 20, "final_event_limit": 10,
        "official_coverage_required": True, "document_coverage_required": True, "external_coverage_required": True,
        "explicit_zero_item_scan_required": True, "candidate_only_event_policy": "NOT_A_FALSE_POSITIVE_PENDING_BLIND_ADJUDICATION",
        "precision_status_after_run": "PROVISIONAL_PENDING_BLIND_ADJUDICATION", "production_write_forbidden": True}


def evaluation_policy():
    """Post-run policy; intentionally not copied into the runtime snapshot."""
    return {"benchmark_role": "REGRESSION_DEVELOPMENT_BENCHMARK", "critical_target": 7, "critical_required": 7,
        "ir_tdnet_target": 12, "ir_tdnet_required": 12, "common48_material_target": 36,
        "material_minimum": 25, "material_preferred": 27, "precision_minimum": .45, "precision_preferred": .47,
        "task_p95_less_than_seconds": 2700, "task_max_less_than_seconds": 3300,
        "conditional_companies_per_hour_preferred": 24,
        "same_cohort_pass_authorizes_production": False,
        "fresh_unseen_gate": {"required": True, "different_week": True, "different_company_sample": True,
                              "blind_adjudication": True, "execute_now": False},
        "scheduler_reliability": "SEPARATE_GATE"}


def regulatory_themes(metadata):
    """Deterministic themes from supplied sector/business exposure, not identity."""
    context = " ".join(str(metadata.get(k, "")) for k in ("sector", "business_description", "regulated_business")).lower()
    matches = []
    for theme, (keywords, terms) in REGULATORY.items():
        hits = [word for word in keywords if word in context]
        if hits:
            matches.append({"theme": theme, "exposure": hits[0], "terms": list(terms)})
    return matches[:2]


def regulatory_query(company_name, ticker, theme):
    require(theme["theme"] in REGULATORY, "unsupported regulatory theme")
    terms = theme["terms"]
    require(1 <= len(terms) <= 2 and set(terms) <= set(REGULATORY[theme["theme"]][1]), "unfocused regulatory terms")
    return f'"{company_name}" {ticker} {theme["exposure"]} ' + " ".join(terms) + " (site:go.jp OR site:lg.jp)"


def targeted_allowed(driver, triggers):
    return bool(isinstance(driver, dict) and text(driver.get("business_exposure")) and
        text(driver.get("transmission_channel")) and text(driver.get("driver_reason")) and
        url(driver.get("exposure_source_url")) and triggers and set(triggers) <= TRIGGERS)


def prompt_text(root):
    return (root / "config" / PROMPT_NAME).read_text(encoding="utf-8").replace("{{REPO}}", str(root.resolve()))


def leakage_scan(prompt, assignments, companies):
    result = b._strategy_v2_firewall_scan(prompt, assignments)
    hits = list(result["hits"])
    # Scan the generic prompt separately from legitimate per-company assignments.
    for company in companies:
        for key in ("ticker", "company_name"):
            value = str(company[key])
            if len(value) >= 3 and re.search(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])", prompt):
                hits.append("literal_company_identity:" + key)
    require(not re.search(r"https?://[^\s)>]+\.go\.jp/", prompt), "agency-specific URL in generic prompt")
    # Nested assignment keys are checked too; runtime inputs never ingest adjudication.
    forbidden = {"found_by", "verdict", "materiality_reason", "duplicate_of_cluster_id", "matcher_evidence"}
    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in forbidden or "gold" in key.lower() or "miss" in key.lower():
                    hits.append("forbidden_assignment_key:" + key)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(assignments)
    return {**result, "status": "PASS" if not hits else "FAIL", "hits": sorted(set(hits)),
            "hit_count": len(set(hits)), "company_hardcode_count": sum(x.startswith("literal_company_identity:") for x in hits),
            "agency_hardcode_count": 0, "scope": "Generic source template and runtime assignment structure; no adjudication artifacts loaded."}


def validate(value, assignment, payload):
    """Fail-closed normal finalization. Failure sidecars use the existing path."""
    required = {"schema_version", "strategy_version", "assignment_id", "ticker", "official_index_found", "official_index_url",
        "official_indexes", "official_items_in_window", "official_items_followed", "official_items_skipped", "official_follow_ratio",
        "official_document_coverage_complete", "official_coverage_complete", "official_zero_items_confirmed", "window_scan_complete",
        "official_queries_attempted", "external_queries_attempted", "external_coverage_complete", "external_sources_found",
        "business_metadata", "regulatory_themes", "regulatory_review_complete", "regulatory_candidates_reviewed",
        "driver_groups", "targeted_pass_triggers", "targeted_pass_skip_reason", "source_provenance", "source_diversity",
        "candidate_event_count", "final_event_count", "candidate_provenance", "no_news_gate_pass", "stopped_after_first_event",
        "official_stage_elapsed_seconds", "targeted_stage_elapsed_seconds", "regulatory_stage_elapsed_seconds", "total_research_elapsed_seconds"}
    require(isinstance(value, dict) and required <= value.keys(), "incomplete telemetry schema")
    v = value
    require(v["schema_version"] == SCHEMA and v["strategy_version"] == VERSION and assignment.get("strategy_version") == VERSION, "version mismatch")
    require(v["assignment_id"] == assignment["assignment_id"] and v["ticker"] == assignment["ticker"], "assignment mismatch")
    for key in ("official_index_found", "official_document_coverage_complete", "official_coverage_complete", "window_scan_complete",
                "external_coverage_complete", "regulatory_review_complete", "no_news_gate_pass"):
        require(v[key] is True, key + " must be complete")
    require(v["stopped_after_first_event"] is False, "multi-document scan cannot stop at first event")
    require(type(v["official_zero_items_confirmed"]) is bool, "zero-item confirmation must be boolean")
    sources = v["source_provenance"]
    require(isinstance(sources, list) and sources, "source provenance missing")
    source_map = {}
    for s in sources:
        require(isinstance(s, dict) and url(s.get("url")) and text(s.get("source_type")) and
                s.get("content_verified") is True and text(s.get("evidence_excerpt")), "source body evidence required")
        require(s["url"] not in source_map, "duplicate provenance URL")
        stamp(s.get("checked_at"))
        source_map[s["url"]] = s
    indexes = v["official_indexes"]
    require(url(v["official_index_url"]) and isinstance(indexes, list) and indexes, "official index evidence missing")
    index_urls = set()
    for index in indexes:
        require(index.get("url") in source_map and index.get("enumeration_complete") is True and
                index.get("window_from") == assignment["search_from"] and index.get("window_to") == assignment["search_to"] and
                text(index.get("scan_evidence")), "index window enumeration incomplete")
        require(index["url"] not in index_urls, "duplicate index")
        index_urls.add(index["url"])
    require(v["official_index_url"] in index_urls, "primary index not scanned")
    items = v["official_items_in_window"]
    require(isinstance(items, list), "window items must be array")
    item_map = {}
    start, end = date.fromisoformat(assignment["search_from"]), date.fromisoformat(assignment["search_to"])
    for item in items:
        require(isinstance(item, dict) and text(item.get("title")) and url(item.get("href")) and
                item.get("document_type") in DOCUMENT_TYPES and item.get("index_url") in index_urls and
                type(item.get("requires_follow")) is bool, "window item date/title/href/type/follow decision missing")
        require(start <= date.fromisoformat(item["date"]) <= end, "enumerated item outside window")
        require(item["href"] not in item_map, "duplicate enumerated document")
        if urlsplit(item["href"]).path.lower().endswith(".pdf"):
            require(item["document_type"] == "pdf", "PDF URL must not be relabelled HTML")
        if item["document_type"] != "html":
            require(item["requires_follow"], "linked PDF/attachment must be followed")
        item_map[item["href"]] = item
    require(v["official_zero_items_confirmed"] == (not items), "explicit zero-window confirmation inconsistent")
    followed, skipped = v["official_items_followed"], v["official_items_skipped"]
    require(isinstance(followed, list) and isinstance(skipped, list), "follow/skip arrays required")
    followed_ids, skipped_ids = set(), set()
    for follow in followed:
        href = follow.get("href")
        require(href in item_map and href not in followed_ids and follow.get("final_url") in source_map and
                follow.get("content_verified") is True and text(follow.get("evidence_excerpt")), "document body follow evidence missing")
        require(follow.get("document_type") == item_map[href]["document_type"], "follow document type mismatch")
        require(follow["final_url"] not in index_urls, "index reach is not document follow-through")
        followed_ids.add(href)
    for skip in skipped:
        href = skip.get("href")
        require(href in item_map and href not in skipped_ids and not item_map[href]["requires_follow"] and
                skip.get("reason_code") == "index_sufficient_nonmaterial" and text(skip.get("reason")), "required document cannot be skipped")
        skipped_ids.add(href)
    require(not followed_ids & skipped_ids and followed_ids | skipped_ids == set(item_map), "document coverage incomplete")
    ratio = len(followed_ids)/len(items) if items else 1.0
    require(type(v["official_follow_ratio"]) in (int, float) and math.isfinite(v["official_follow_ratio"]) and
            abs(v["official_follow_ratio"]-ratio) < 1e-9, "document follow ratio mismatch")
    official, external = v["official_queries_attempted"], v["external_queries_attempted"]
    require(isinstance(official, list) and 2 <= len(official) <= 3 and all(text(q) for q in official), "official query budget")
    require(isinstance(external, list) and 1 <= len(external) <= 3 and 3 <= len(official)+len(external) <= 6, "per-company query budget")
    name, ticker = assignment["company_name"], assignment["ticker"]
    require(all(name in q and ticker in q for q in official), "official queries must identify own company")
    metadata = v["business_metadata"]
    require(isinstance(metadata, dict) and text(metadata.get("business_description")) and
            metadata.get("source_url") in source_map, "business exposure needs source evidence")
    themes = v["regulatory_themes"]
    require(themes == regulatory_themes(metadata), "regulatory themes must derive from metadata (maximum two)")
    drivers = v["driver_groups"]
    triggers = v["targeted_pass_triggers"]
    require(isinstance(drivers, list) and len(drivers) <= 3 and isinstance(triggers, list) and set(triggers) <= TRIGGERS, "driver/trigger limit")
    for driver in drivers:
        require(targeted_allowed(driver, triggers) and driver["exposure_source_url"] in source_map, "driver economic-link evidence required")
    theme_seen = set()
    targeted = 0
    query_texts = list(official)
    for query in external:
        require(isinstance(query, dict) and text(query.get("query")) and text(query.get("reason")), "external query provenance missing")
        query_texts.append(query["query"])
        kind = query.get("kind")
        if kind == "regulatory_pass":
            idx = query.get("theme_index")
            require(type(idx) is int and 0 <= idx < len(themes), "regulatory theme index invalid")
            require(query["query"] == regulatory_query(name,ticker,themes[idx]), "regulatory query must use focused generic template")
            theme_seen.add(idx)
        elif kind == "targeted_second_pass":
            idx = query.get("driver_index")
            require(type(idx) is int and 0 <= idx < len(drivers), "targeted pass requires grounded driver")
            require(drivers[idx]["business_exposure"] in query["query"], "generic sector query prohibited")
            targeted += 1
        else:
            require(kind == "company_external_check" and name in query["query"] and ticker in query["query"], "external check must be company-specific")
    require(theme_seen == set(range(len(themes))), "focused regulatory exposure pass incomplete")
    require(targeted > 0 or text(v["targeted_pass_skip_reason"]), "pruned targeted pass requires explanation")
    require(not targeted or drivers and triggers and not v["targeted_pass_skip_reason"], "targeted trigger/skip conflict")
    require(isinstance(v["external_sources_found"],list) and all(u in source_map for u in v["external_sources_found"]), "external sources lack body evidence")
    require(v["external_sources_found"] or all(text(q.get("result_evidence")) for q in external), "empty external results need search evidence")
    require(isinstance(v["source_diversity"],list) and set(v["source_diversity"]) == {s["source_type"] for s in sources}, "source diversity does not match evidence")
    require(b._strategy_v2_firewall_scan("\n".join(query_texts), {})["status"] == "PASS", "runtime query provenance leakage")
    final_items = payload["items"]
    require(count(v["candidate_event_count"]) and count(v["final_event_count"]) and
            len(final_items) == v["final_event_count"] <= 10 and len(final_items) <= v["candidate_event_count"] <= 20, "candidate/final counts invalid")
    candidates = v["candidate_provenance"]
    require(isinstance(candidates,list) and len(candidates)==v["candidate_event_count"], "candidate coverage incomplete")
    selected, indexes_seen = [], set()
    for c in candidates:
        idx = c.get("candidate_index")
        require(count(idx) and idx not in indexes_seen and c.get("stage") in STAGES and type(c.get("selected_for_output")) is bool and
                text(c.get("headline")) and c.get("source_url") in source_map and text(c.get("selection_reason")), "candidate provenance missing")
        require(c.get("information_type") in {"event", "generic_profile"}, "candidate information type missing")
        if c["selected_for_output"]:
            require(c["information_type"] != "generic_profile" and text(c.get("economic_transmission")), "generic profile cannot be finalized as event")
            selected.append((c["headline"], b._normalized_url(c["source_url"])))
        indexes_seen.add(idx)
    require(indexes_seen == set(range(len(candidates))), "candidate indexes not contiguous")
    require(sorted(selected) == sorted((x["headline"],b._normalized_url(x["source_url"])) for x in final_items), "selected evidence must match final items")
    official_material = [c for c in candidates if c["stage"] == "official_first" and c["selected_for_output"]]
    if "material_candidate_zero" in triggers:
        require(not official_material, "material-zero trigger contradicts official selected candidates")
    if "source_diversity_insufficient" in triggers:
        require(count(v.get("source_diversity_before_targeted")) and v["source_diversity_before_targeted"] < 2,
                "diversity trigger requires recorded pre-pass deficit")
    if "external_driver_uncovered" in triggers:
        require(v.get("uncovered_driver_indexes") and all(type(i) is int and 0 <= i < len(drivers) for i in v["uncovered_driver_indexes"]),
                "uncovered-driver trigger requires explicit driver indexes")
    regulatory = v["regulatory_candidates_reviewed"]
    require(isinstance(regulatory,list), "regulatory candidate review missing")
    expected_regulatory = {u for u,s in source_map.items() if urlsplit(u).hostname.endswith((".go.jp", ".lg.jp")) or s["source_type"] == "official_regulator"}
    require({r.get("url") for r in regulatory} == expected_regulatory and len(regulatory) == len(expected_regulatory), "every regulator source must be considered")
    for r in regulatory:
        require(r.get("decision") in {"candidate", "no_event", "duplicate"} and text(r.get("reason")), "regulatory selection rationale missing")
        if r["decision"] in {"candidate","duplicate"}:
            require(r.get("candidate_index") in indexes_seen, "regulatory candidate link missing")
    time_keys=("official_stage_elapsed_seconds","targeted_stage_elapsed_seconds","regulatory_stage_elapsed_seconds","total_research_elapsed_seconds")
    require(all(type(v[k]) in (int,float) and math.isfinite(v[k]) and v[k]>=0 for k in time_keys), "invalid stage time")
    require(v[time_keys[-1]] >= sum(v[k] for k in time_keys[:-1]), "stage time sum inconsistent")
    require(payload.get("sources_checked_count") == len(sources), "source count must equal unique verified provenance")
    return v
