"""Run one bounded Phase1.9c evidence-gap recovery worker for ticker 6444."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import company_news_lightweight as lightweight
from tools.company_news_canonical_schema import validate_instance


BASELINE_TOKENS = 221_404
RECOVERY_HARD_LIMIT = 140_000
COMPANY_HARD_LIMIT = 500_000
COMPANY_PREFERRED_LIMIT = 400_000


@dataclass(frozen=True)
class RecoverySpec:
    worker_id: str
    target: str
    purpose: str
    findings: dict[str, Any]


BOOL_OR_NULL = {"type": ["boolean", "null"]}
TEXT_OR_NULL = {"type": ["string", "null"]}
SPECS = {
    "official-verification": RecoverySpec(
        "official-verification",
        "https://www.sanden.co.jp/ir/index.html and the official Sanden 2026 news/IR index",
        "Verify the official body identifies Sanden ticker 6444 and whether the official index is complete for 2026-08-25 through 2026-08-31.",
        {
            "type": "object", "additionalProperties": False,
            "required": ["identity_verified", "window_complete", "window_items", "detail"],
            "properties": {
                "identity_verified": {"type": "boolean"},
                "window_complete": BOOL_OR_NULL,
                "window_items": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 500}},
                "detail": {"type": "string", "maxLength": 1500},
            },
        },
    ),
    "incremental-ir-recovery": RecoverySpec(
        "incremental-ir-recovery", "https://www.sanden.co.jp/ir/event/brief.html",
        "Verify only the saved 2026-08-27 briefing body/material link and whether it adds a concrete non-TDNET economic delta.",
        {
            "type": "object", "additionalProperties": False,
            "required": ["body_verified", "publication_date", "incremental_information", "delta_evidence",
                         "direct_sanden_linkage", "non_tdnet_delta", "earnings_pathway", "material_event", "detail"],
            "properties": {
                "body_verified": {"type": "boolean"}, "publication_date": TEXT_OR_NULL,
                "incremental_information": BOOL_OR_NULL, "delta_evidence": TEXT_OR_NULL,
                "direct_sanden_linkage": BOOL_OR_NULL, "non_tdnet_delta": BOOL_OR_NULL,
                "earnings_pathway": TEXT_OR_NULL, "material_event": BOOL_OR_NULL,
                "detail": {"type": "string", "maxLength": 1500},
            },
        },
    ),
    "external-candidate-recovery": RecoverySpec(
        "external-candidate-recovery", "https://autotimes.jp/616043/",
        "Verify only the saved article body, publication date, direct Sanden linkage, scope, and concrete earnings pathway.",
        {
            "type": "object", "additionalProperties": False,
            "required": ["body_verified", "publication_date", "direct_sanden_linkage", "non_tdnet_delta",
                         "earnings_pathway", "material_event", "detail"],
            "properties": {
                "body_verified": {"type": "boolean"}, "publication_date": TEXT_OR_NULL,
                "direct_sanden_linkage": BOOL_OR_NULL, "non_tdnet_delta": BOOL_OR_NULL,
                "earnings_pathway": TEXT_OR_NULL, "material_event": BOOL_OR_NULL,
                "detail": {"type": "string", "maxLength": 1500},
            },
        },
    ),
}


def output_schema(spec: RecoverySpec) -> dict[str, Any]:
    call = {
        "type": "object", "additionalProperties": False,
        "required": ["query_id", "target", "executed", "result_count", "reached_urls",
                     "access_success", "evidence_ids"],
        "properties": {
            "query_id": {"type": "string", "maxLength": 100},
            "target": {"type": "string", "maxLength": 600},
            "executed": {"type": "boolean", "const": True},
            "result_count": {"type": "integer", "minimum": 0},
            "reached_urls": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 700}},
            "access_success": {"type": "boolean"},
            "evidence_ids": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 100}},
        },
    }
    card = {
        "type": "object", "additionalProperties": False,
        "required": ["evidence_id", "url", "title", "source_type", "publication_date",
                     "verification_status", "excerpt", "supports"],
        "properties": {
            "evidence_id": {"type": "string", "maxLength": 100}, "url": {"type": "string", "maxLength": 700},
            "title": {"type": "string", "maxLength": 500}, "source_type": {"type": "string", "maxLength": 100},
            "publication_date": TEXT_OR_NULL,
            "verification_status": {"type": "string", "enum": ["VERIFIED_BODY", "SNIPPET_ONLY", "ACCESS_FAILED"]},
            "excerpt": {"type": "string", "maxLength": 1000}, "supports": {"type": "string", "maxLength": 1000},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "additionalProperties": False,
        "required": ["worker_id", "target", "access_status", "calls", "evidence_cards", "findings"],
        "properties": {
            "worker_id": {"type": "string", "const": spec.worker_id},
            "target": {"type": "string", "const": spec.target},
            "access_status": {"type": "string", "enum": ["VERIFIED", "UNRESOLVED"]},
            "calls": {"type": "array", "minItems": 1, "maxItems": 2, "items": call},
            "evidence_cards": {"type": "array", "maxItems": 8, "items": card},
            "findings": spec.findings,
        },
    }


def recovery_telemetry_schema() -> dict[str, Any]:
    """Strict persisted telemetry contract; validated offline only."""
    nullable_integer = {"type": ["integer", "null"], "minimum": 0}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "additionalProperties": False,
        "required": ["worker_id", "input_tokens", "cached_input_tokens", "uncached_input_tokens",
                     "output_tokens", "reasoning_tokens", "total_tokens", "web_call_count",
                     "tool_call_count", "research_elapsed_seconds", "validation_status"],
        "properties": {
            "worker_id": {"type": "string"}, "input_tokens": nullable_integer,
            "cached_input_tokens": nullable_integer, "uncached_input_tokens": nullable_integer,
            "output_tokens": nullable_integer, "reasoning_tokens": nullable_integer,
            "total_tokens": nullable_integer, "web_call_count": {"type": "integer", "minimum": 0},
            "tool_call_count": {"type": "integer", "minimum": 0},
            "research_elapsed_seconds": {"type": "number", "minimum": 0},
            "validation_status": {"type": "string", "enum": ["PENDING", "PASS", "FAIL", "PROCESS_FAILED"]},
        },
    }


def optional_synthesis_schema() -> dict[str, Any]:
    """Reserved synthesis contract; Phase1.9d never executes it."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "additionalProperties": False, "required": ["result", "events", "reason"],
        "properties": {
            "result": {"type": "string", "enum": ["news_present", "no_news", "UNKNOWN"]},
            "events": {"type": "array", "maxItems": 4, "items": {
                "type": "object", "additionalProperties": False,
                "required": ["headline", "source_url", "material_meaning"],
                "properties": {
                    "headline": {"type": "string", "maxLength": 500},
                    "source_url": {"type": "string", "maxLength": 700},
                    "material_meaning": {"type": "string", "maxLength": 1200},
                },
            }},
            "reason": {"type": "string", "maxLength": 1500},
        },
    }


def strict_schema_errors(schema: Any, path: str = "$") -> list[str]:
    """Lint the strict structured-output subset used by all recovery schemas."""
    if not isinstance(schema, dict) or not schema:
        return [f"{path}: empty or non-object schema node"]
    errors: list[str] = []
    if "const" in schema and "type" not in schema:
        errors.append(f"{path}: const requires explicit type")
    if "enum" in schema and "type" not in schema:
        errors.append(f"{path}: enum requires explicit type")
    node_type = schema.get("type")
    permits_object = node_type == "object" or isinstance(node_type, list) and "object" in node_type
    permits_array = node_type == "array" or isinstance(node_type, list) and "array" in node_type
    if permits_object:
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            errors.append(f"{path}: object requires properties")
        else:
            required = schema.get("required")
            if not isinstance(required, list) or set(required) != set(properties):
                errors.append(f"{path}: required must exactly match properties")
            if schema.get("additionalProperties") is not False:
                errors.append(f"{path}: object requires additionalProperties=false")
            for key, child in properties.items():
                errors.extend(strict_schema_errors(child, f"{path}.properties.{key}"))
    if permits_array:
        if "items" not in schema:
            errors.append(f"{path}: array requires items")
        else:
            errors.extend(strict_schema_errors(schema["items"], f"{path}.items"))
    for union in ("anyOf", "oneOf"):
        if union in schema:
            branches = schema[union]
            if not isinstance(branches, list) or not branches:
                errors.append(f"{path}: {union} requires non-empty branches")
            else:
                for index, child in enumerate(branches):
                    errors.extend(strict_schema_errors(child, f"{path}.{union}[{index}]"))
    if not any(key in schema for key in ("type", "anyOf", "oneOf", "$ref")):
        errors.append(f"{path}: schema node has no explicit type")
    return errors


def valid_fixture(spec: RecoverySpec) -> dict[str, Any]:
    if spec.worker_id == "official-verification":
        findings = {"identity_verified": True, "window_complete": True,
                    "window_items": [], "detail": "verified"}
    elif spec.worker_id == "incremental-ir-recovery":
        findings = {"body_verified": True, "publication_date": "2026-08-27",
                    "incremental_information": False, "delta_evidence": None,
                    "direct_sanden_linkage": True, "non_tdnet_delta": False,
                    "earnings_pathway": None, "material_event": False, "detail": "verified"}
    else:
        findings = {"body_verified": True, "publication_date": "2026-08-26",
                    "direct_sanden_linkage": False, "non_tdnet_delta": True,
                    "earnings_pathway": None, "material_event": False, "detail": "verified"}
    return {
        "worker_id": spec.worker_id, "target": spec.target, "access_status": "VERIFIED",
        "calls": [{"query_id": f"{spec.worker_id}-q1", "target": spec.target,
                   "executed": True, "result_count": 1, "reached_urls": [spec.target],
                   "access_success": True, "evidence_ids": [f"{spec.worker_id}-s1"]}],
        "evidence_cards": [{"evidence_id": f"{spec.worker_id}-s1", "url": spec.target,
                            "title": "fixture", "source_type": "official_company",
                            "publication_date": None, "verification_status": "VERIFIED_BODY",
                            "excerpt": "bounded fixture", "supports": "schema acceptance"}],
        "findings": findings,
    }


def prompt(spec: RecoverySpec) -> str:
    return "\n".join((
        "Return only the JSON object required by the supplied schema.",
        "This is one bounded evidence-gap recovery for Sanden Corporation (ticker 6444).",
        f"TARGET={spec.target}", f"PURPOSE={spec.purpose}",
        "Use native Web only. Make at most TWO Web tool calls total. Never use shell, repo, apps, plugins, prior history, benchmark data, or Gold.",
        "Open/fetch the exact saved target first. A second call may only follow a link on that same target or an official sanden.co.jp identity/news index needed for the stated purpose.",
        "Do not search broadly, paraphrase the target into a general search, discover other candidates, retry a failed call, or research another company.",
        "VERIFIED_BODY requires actual page/document body. A search snippet, result excerpt, HTTP error, or cache miss is not verified body.",
        "Report every actual Web call once in calls. Use actual result_count; use 0 when the call returned no results. Never invent a call, timestamp, URL, or evidence.",
        "Evidence cards must be short. If body access fails, preserve the gap and return access_status UNRESOLVED.",
        "A material event requires direct Sanden linkage, a non-TDNET delta, and a concrete commercial, operational, or earnings pathway.",
    ))


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def recovery_root(root: Path) -> Path:
    return (root / "artifacts" / "token_efficiency_phase18b_7760" / "companies" / "02-6444"
            / "phase19d-official-recovery")


def cumulative_tokens(base: Path) -> int:
    total = 0
    for path in base.glob("workers/*/telemetry.json"):
        value = _read(path).get("total_tokens")
        if not isinstance(value, int):
            raise RuntimeError(f"exact token usage unavailable: {path}")
        total += value
    return total


def run_worker(root: Path, worker_id: str) -> dict[str, Any]:
    spec = SPECS[worker_id]
    base = recovery_root(root)
    stage = base / "workers" / worker_id
    if stage.exists():
        raise RuntimeError(f"artifact collision; worker will not rerun: {stage}")
    prior = cumulative_tokens(base)
    if prior >= RECOVERY_HARD_LIMIT:
        raise RuntimeError(f"recovery budget exhausted before worker: {prior}")
    guard = base / "active-guard.json"
    if guard.exists():
        raise RuntimeError(f"active guard exists: {guard}")
    stage.mkdir(parents=True)
    schema = output_schema(spec)
    schema_path, result_path = stage / "output-schema.json", stage / "last-message.json"
    _write(schema_path, schema)
    _write(guard, {"worker_id": worker_id, "created_at": datetime.now(timezone.utc).isoformat()})
    command = lightweight.codex_command(root, schema_path, result_path, prompt(spec), web_enabled=True)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    try:
        completed = subprocess.run(command, text=True, encoding="utf-8", errors="replace",
                                   capture_output=True, check=False)
        elapsed = time.monotonic() - started
        completed_at = datetime.now(timezone.utc).isoformat()
        events = lightweight._parse_events(completed.stdout)
        lightweight._atomic_jsonl(stage / "raw-events.sanitized.jsonl", events)
        telemetry = lightweight.token_telemetry(events, elapsed)
        telemetry.update({"worker_id": worker_id, "started_at": started_at, "completed_at": completed_at,
                          "validation_status": "PENDING"})
        _write(stage / "telemetry.json", telemetry)
        _write(stage / "execution-summary.json", {
            "exit_code": completed.returncode,
            "stderr": lightweight.bounded_text(completed.stderr, max_chars=8000, max_lines=80),
            "event_types": [event.get("type") for event in events],
        })
        if completed.returncode != 0 or not result_path.exists():
            telemetry["validation_status"] = "PROCESS_FAILED"
            _write(stage / "telemetry.json", telemetry)
            raise RuntimeError(f"worker process failed: {worker_id} exit={completed.returncode}")
        response = _read(result_path)
        try:
            validate_instance(response, schema)
            if telemetry.get("total_tokens") is None:
                raise RuntimeError("exact token usage unavailable")
            if telemetry.get("web_call_count", 0) > 2:
                raise RuntimeError(f"Web call budget exceeded: {telemetry['web_call_count']}")
            if len(response["calls"]) != telemetry.get("web_call_count"):
                raise RuntimeError("reported calls do not equal actual Web call telemetry")
            if prior + telemetry["total_tokens"] > RECOVERY_HARD_LIMIT:
                raise RuntimeError("recovery token hard limit exceeded")
        except Exception:
            telemetry["validation_status"] = "FAIL"
            _write(stage / "telemetry.json", telemetry)
            raise
        telemetry["validation_status"] = "PASS"
        telemetry["evidence_cards_returned"] = len(response["evidence_cards"])
        _write(stage / "telemetry.json", telemetry)
        return {"worker": response, "telemetry": telemetry,
                "recovery_tokens": prior + telemetry["total_tokens"],
                "company_tokens": BASELINE_TOKENS + prior + telemetry["total_tokens"]}
    finally:
        guard.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worker", choices=tuple(SPECS))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    result = run_worker(args.root.resolve(), args.worker)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
