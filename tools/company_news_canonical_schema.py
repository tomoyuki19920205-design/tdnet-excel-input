"""Canonical generated-output contract for Company News Strategy v5.

The schema is intentionally dependency-free: Codex consumes the JSON Schema
and the offline validator below consumes the same dictionary before the
semantic Strategy v5 validators run.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from lib import news_monitor


class SchemaContractError(ValueError):
    pass


def _string(*, enum=None, max_length=None, nullable=False):
    value: dict[str, Any] = {"type": ["string", "null"] if nullable else "string"}
    if enum is not None:
        value["enum"] = sorted(enum) + ([None] if nullable else [])
    if max_length is not None:
        value["maxLength"] = max_length
    return value


def _object(properties, required=(), *, additional=False):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": additional}


def _array(items, *, min_items=None, max_items=None):
    value = {"type": "array", "items": items}
    if min_items is not None:
        value["minItems"] = min_items
    if max_items is not None:
        value["maxItems"] = max_items
    return value


def canonical_output_schema(*, telemetry_schema: str, strategy_version: str,
                            channels: set[str], scope_labels: set[str],
                            exposure_statuses: set[str]) -> dict[str, Any]:
    """Build the sole generated-output schema from validator-owned constants."""
    nullable_string = _string(nullable=True)
    item_properties = {
        "headline": _string(max_length=500), "source_url": _string(max_length=2048),
        "published_at": _string(max_length=64, nullable=True),
        "published_date": _string(max_length=10, nullable=True),
        "published_precision": _string(enum=news_monitor.PUBLICATION_PRECISIONS),
        "valid_from": nullable_string,
        "valid_from_date": _string(max_length=10, nullable=True),
        "valid_from_precision": _string(enum=news_monitor.EFFECTIVE_DATE_PRECISIONS),
        "valid_until": nullable_string,
        "valid_until_date": _string(max_length=10, nullable=True),
        "valid_until_precision": _string(enum=news_monitor.EFFECTIVE_DATE_PRECISIONS),
        "source_type": _string(enum=news_monitor.SOURCE_TYPES),
        "source_name": _string(max_length=200), "category": _string(enum=news_monitor.CATEGORIES),
        "subcategory": nullable_string, "direction": _string(enum=news_monitor.DIRECTIONS),
        "importance": _string(enum=news_monitor.IMPORTANCE),
        "earnings_relevance": _string(enum=news_monitor.EARNINGS_RELEVANCE, max_length=64),
        "summary": _string(max_length=4000), "why_it_matters": _string(max_length=4000),
        "evidence_excerpt": nullable_string, "temporal_scope": _string(enum=news_monitor.TEMPORAL_SCOPE),
        "temporal_status": _string(enum=news_monitor.TEMPORAL_STATUS),
        "tags": _array(_string(max_length=100), max_items=50),
    }
    research = _object({
        "schema_version": {"const": news_monitor.SCHEMA_VERSION}, "run_id": _string(max_length=200),
        "task_id": _string(max_length=200), "ticker": _string(max_length=5),
        "collector_type": _string(max_length=100), "collector_version": nullable_string,
        "analysis_version": nullable_string, "checked_at": _string(max_length=64),
        "sources_checked_count": {"type": "integer", "minimum": 0},
        "items": _array(_object(item_properties, item_properties, additional=False)),
    }, ("schema_version", "run_id", "task_id", "ticker", "collector_type", "collector_version",
        "analysis_version", "checked_at", "sources_checked_count", "items"), additional=False)

    exposure_properties = {
        "canonical_driver_phrase": _string(), "metadata_evidence": _string(),
        "transmission_channel": _string(), "government_applicable": {"type": "boolean"},
        "government_applicability_reason": _string(), "product_context": nullable_string,
        "channel": {"type": ["string", "null"], "enum": sorted(channels) + [None]},
    }
    exposure = _object(exposure_properties, exposure_properties, additional=False)
    query_properties = {
        "channel": _string(enum=channels), "query": _string(), "executed": {"const": True},
        "result_count": {"type": "integer", "minimum": 0},
        "reached_sources": _array(_string(max_length=2048)), "window_relevance_checked": {"const": True},
        "exposure_index": {"type": ["integer", "null"], "minimum": 0},
        "canonical_driver_phrase": nullable_string,
        "driver_phrase_preserved": {"type": ["boolean", "null"]},
        "driver_phrase_preservation_reason": nullable_string,
    }
    query = _object(query_properties, query_properties, additional=False)
    channel_review_properties = {
        "channel": _string(enum=channels), "status": _string(enum={"CHECKED", "NOT_APPLICABLE", "PARTIAL"}),
        "evidence": _string(), "exposure_index": {"type": ["integer", "null"], "minimum": 0},
    }
    channel_review = _object(channel_review_properties, channel_review_properties, additional=False)
    source = _object({"url": _string(max_length=2048), "source_type": _string(),
                      "content_verified": {"const": True}, "evidence_excerpt": _string(max_length=1000)},
                     ("url", "source_type", "content_verified", "evidence_excerpt"), additional=False)
    document_properties = {
        "date": _string(max_length=10, nullable=True), "title": _string(max_length=500), "url": _string(max_length=2048),
        "final_url": nullable_string, "scope_label": _string(enum=scope_labels), "scope_reason": _string(),
        "material": {"type": "boolean"}, "body_status": _string(enum={"VERIFIED", "SKIPPED", "INACCESSIBLE"}),
        "attempt_evidence": nullable_string, "tdnet_same_content_verified": {"type": ["boolean", "null"]},
        "tdnet_url": nullable_string, "tdnet_identity_evidence": nullable_string,
        "incremental_information": {"type": ["boolean", "null"]},
        "incremental_information_evidence": nullable_string, "index_evidence": nullable_string,
    }
    document = _object(document_properties, document_properties, additional=False)
    candidate_properties = {
        "candidate_index": {"type": "integer", "minimum": 0}, "document_url": _string(max_length=2048),
        "selected_for_output": {"type": "boolean"}, "selection_reason": _string(),
        "source_url": nullable_string, "headline": nullable_string, "economic_transmission": nullable_string,
        "event_key": nullable_string,
    }
    candidate = _object(candidate_properties, candidate_properties, additional=False)
    telemetry_properties = {
        "schema_version": {"const": telemetry_schema}, "strategy_version": {"const": strategy_version},
        "assignment_id": _string(), "ticker": _string(max_length=5),
        "company_identity": _object({"verified_ticker": _string(max_length=5), "source_url": _string(max_length=2048),
                                     "evidence": _string()}, ("verified_ticker", "source_url", "evidence"), additional=False),
        "economic_exposures": _array(exposure, min_items=2, max_items=3),
        "queries": _array(query, min_items=1),
        "regulatory_themes": _array(_object({"theme": _string(), "exposure_index": {"type": "integer", "minimum": 0}},
                                             ("theme", "exposure_index"), additional=False), max_items=2),
        "query_budget_exceeded": {"type": "boolean"}, "channel_reviews": _array(channel_review),
        "no_news_exposure_coverage": _array(_object({"exposure_index": {"type": "integer", "minimum": 0},
                                                       "status": _string(enum=exposure_statuses), "evidence": _string()},
                                                      ("exposure_index", "status", "evidence"), additional=False), min_items=2, max_items=3),
        "company_web_review": _object({k: ({"const": True} if k in {"index_checked", "window_items_checked", "material_candidates_checked"}
                                               else {"type": "integer", "minimum": 0}) for k in
                                              ("index_checked", "window_items_checked", "material_candidates_checked",
                                               "background_pages_opened", "redundant_pages_opened", "old_business_pages_opened",
                                               "provenance_only_pages_opened")},
                                             ("index_checked", "window_items_checked", "material_candidates_checked",
                                              "background_pages_opened", "redundant_pages_opened", "old_business_pages_opened",
                                              "provenance_only_pages_opened"), additional=False),
        "documents": _array(document),
        "coverage_gaps": _array(_object({"url": _string(max_length=2048), "reason": _string(), "retry_required": {"const": True}},
                                              ("url", "reason", "retry_required"), additional=False)),
        "coverage_status": _string(enum={"COMPLETE", "PARTIAL"}), "source_provenance": _array(source, min_items=1),
        "candidate_provenance": _array(candidate), "candidate_count": {"type": "integer", "minimum": 0},
        "final_event_count": {"type": "integer", "minimum": 0}, "no_news_gate_pass": {"type": "boolean"},
        "stopped_after_first_event": {"const": False}, "window_review_complete": {"const": True},
        "stage_elapsed_seconds": _object({"company_web": {"type": "number", "minimum": 0},
                                           "external": {"type": "number", "minimum": 0},
                                           "provenance": {"type": "number", "minimum": 0}},
                                          ("company_web", "external", "provenance"), additional=False),
        "total_research_elapsed_seconds": {"type": "number", "minimum": 0},
    }
    telemetry = _object(telemetry_properties, telemetry_properties, additional=False)
    return {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
            "properties": {"research_result": research, "strategy_telemetry": telemetry},
            "required": ["research_result", "strategy_telemetry"], "additionalProperties": False}


def schema_sha256(schema: dict[str, Any]) -> str:
    raw = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def validate_instance(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate the JSON-Schema subset used by the canonical contract."""
    if "const" in schema and value != schema["const"]:
        raise SchemaContractError(f"{path} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaContractError(f"{path} must be one of {schema['enum']}")
    expected = schema.get("type")
    allowed = expected if isinstance(expected, list) else [expected] if expected else []
    checks = {"object": lambda x: isinstance(x, dict), "array": lambda x: isinstance(x, list),
              "string": lambda x: isinstance(x, str), "integer": lambda x: isinstance(x, int) and not isinstance(x, bool),
              "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
              "boolean": lambda x: isinstance(x, bool), "null": lambda x: x is None}
    if allowed and not any(checks[k](value) for k in allowed):
        raise SchemaContractError(f"{path} must have type {expected}")
    if value is None:
        return
    if isinstance(value, str) and len(value) > schema.get("maxLength", len(value)):
        raise SchemaContractError(f"{path} exceeds {schema['maxLength']} characters")
    if isinstance(value, (int, float)) and value < schema.get("minimum", value):
        raise SchemaContractError(f"{path} is below minimum")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            raise SchemaContractError(f"{path} has invalid item count")
        for index, item in enumerate(value):
            validate_instance(item, schema.get("items", {}), f"{path}[{index}]")
    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise SchemaContractError(f"{path} missing required keys: {', '.join(missing)}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise SchemaContractError(f"{path} has unexpected keys: {', '.join(extra)}")
        for key, item in value.items():
            if key in properties:
                validate_instance(item, properties[key], f"{path}.{key}")
