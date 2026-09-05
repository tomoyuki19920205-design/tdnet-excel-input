"""Isolated, company-at-a-time runner for Company News Strategy v5.

The module deliberately has no production route.  It copies four immutable
assignments into a private experiment directory, launches one ephemeral model
context per assignment, and performs validation/finalization in Python.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.news_monitor import canonicalize_url, parse_datetime, validate_payload
from tools import company_news_strategy_v5 as strategy_v5
from tools.company_news_canonical_schema import validate_instance


SCHEMA = "company_news_lightweight_phase1_v1"
TELEMETRY_SCHEMA = "company_news_lightweight_token_telemetry_v1"
COHORT = ("7760", "6444", "4883", "5122")
BENCHMARK_ID = "batchbench-20260901T095759508419"
SOURCE_RUN = "batch04v5"
SOURCE_TASK = "task03"
STRATEGY = strategy_v5.VERSION
MAX_TOOL_CHARS = 12_000
MAX_TOOL_LINES = 160
MAX_EVIDENCE_SOURCES = 24
MAX_QUERY_RESULTS = 5
MAX_EVIDENCE_EXCERPT = 800
MAX_EVIDENCE_SUPPORTS = 400
MAX_WEB_WORKERS = 5
MAX_QUERIES_PER_WORKER = 2
WORKER_TELEMETRY_SCHEMA = "company_news_exposure_worker_telemetry_v1"
OFFICIAL_EVIDENCE_SOURCE_TYPES = frozenset({
    "official", "official_index", "official_report", "official_news",
    "company", "company_news", "company_ir", "ir", "company_official",
})
CONTROL_TOTAL_TOKENS = 8_584_237
CONTROL_INPUT_TOKENS = 8_545_419
CONTROL_CACHED_INPUT_TOKENS = 8_227_072
CONTROL_OUTPUT_TOKENS = 38_818


class LightweightError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvidenceCard:
    source_id: str
    url: str
    source_type: str
    title: str
    published_at: str | None
    excerpt: str
    supports: str
    exposure_id: int | None
    query_id: str
    verification_status: str


class EvidenceStore:
    """Company-local state that never retains a raw Web body."""

    def __init__(self, *, source_limit: int = MAX_EVIDENCE_SOURCES,
                 result_limit: int = MAX_QUERY_RESULTS):
        self.source_limit = source_limit
        self.result_limit = result_limit
        self._cards: dict[str, EvidenceCard] = {}
        self._queries: dict[str, str] = {}
        self._zero_result_signatures: set[str] = set()

    @staticmethod
    def _query_key(query: str) -> str:
        return " ".join(query.casefold().split())

    @staticmethod
    def _query_signature(query: str) -> str:
        aliases = {"prices": "price", "pricing": "price", "demands": "demand",
                   "regulatory": "regulation", "government": "regulation"}
        tokens = re.findall(r"[\w一-龯ぁ-んァ-ン]+", query.casefold())
        ignored = {"search", "latest", "news", "press", "the", "a", "an"}
        normalized = {aliases.get(token, token) for token in tokens
                      if token not in ignored and not re.fullmatch(r"20\d{2}(?:\d{2}){0,2}", token)}
        return " ".join(sorted(normalized))

    def register_query(self, query_id: str, query: str) -> bool:
        key = self._query_key(query)
        if key in self._queries or self._query_signature(query) in self._zero_result_signatures:
            return False
        self._queries[key] = query_id
        return True

    def register_zero_result(self, query: str) -> None:
        self._zero_result_signatures.add(self._query_signature(query))

    def bounded_results(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Expose only the configured number of already-ranked result records."""
        return [dict(item) for item in results[:self.result_limit]]

    @staticmethod
    def relevant_excerpt(body: str, terms: list[str], *, limit: int = MAX_EVIDENCE_EXCERPT) -> str:
        """Select relevant sentences first; bound only after evidence selection."""
        normalized_terms = [term.casefold() for term in terms if term.strip()]
        parts = [part.strip() for part in re.split(r"(?<=[。！？.!?])\s*|[\r\n]+", body) if part.strip()]
        ranked = sorted(enumerate(parts), key=lambda row: (
            -sum(term in row[1].casefold() for term in normalized_terms), row[0]))
        selected = [text for _, text in ranked if any(term in text.casefold() for term in normalized_terms)]
        if not selected:
            selected = parts[:1]
        excerpt = " ".join(selected)
        return bounded_text(excerpt, max_chars=limit, max_lines=20)

    def add(self, *, url: str, source_type: str, title: str, published_at: str | None,
            body: str, supports: str, exposure_id: int | None, query_id: str,
            verification_status: str, relevant_terms: list[str]) -> bool:
        normalized_url = canonicalize_url(url)
        if normalized_url in self._cards or len(self._cards) >= self.source_limit:
            return False
        excerpt = self.relevant_excerpt(body, relevant_terms)
        card = EvidenceCard(
            source_id=hashlib.sha256(normalized_url.encode()).hexdigest()[:16],
            url=normalized_url,
            source_type=bounded_text(source_type, max_chars=80, max_lines=1),
            title=bounded_text(title, max_chars=300, max_lines=2),
            published_at=published_at,
            excerpt=excerpt,
            supports=bounded_text(supports, max_chars=MAX_EVIDENCE_SUPPORTS, max_lines=6),
            exposure_id=exposure_id,
            query_id=query_id,
            verification_status=verification_status,
        )
        self._cards[normalized_url] = card
        return True

    def cards(self) -> list[dict[str, Any]]:
        return [asdict(card) for card in list(self._cards.values())[:self.source_limit]]

    def model_context(self) -> str:
        return json.dumps({"evidence_cards": self.cards()}, ensure_ascii=False, separators=(",", ":"))


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LightweightError(f"object required: {path}")
    return value


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bounded_text(value: str, *, max_chars: int = MAX_TOOL_CHARS, max_lines: int = MAX_TOOL_LINES) -> str:
    """Bound tool text before it can enter a model context."""
    lines = value.splitlines()
    clipped = "\n".join(lines[:max_lines])
    if len(clipped) > max_chars:
        clipped = clipped[:max_chars]
    if len(lines) > max_lines or len(value) > len(clipped):
        clipped += f"\n[TRUNCATED original_chars={len(value)} original_lines={len(lines)}]"
    return clipped


def _source_paths(root: Path) -> tuple[Path, Path, Path]:
    run = root / "data" / "news_work" / "batch_benchmark" / BENCHMARK_ID / "runs" / SOURCE_RUN
    queue = run / "tasks" / SOURCE_TASK / "queue.json"
    outputs = root / "data" / "news_benchmark_inbox" / BENCHMARK_ID / SOURCE_RUN
    return run / "manifest.json", queue, outputs


def prepare_phase1(root: Path, experiment: Path) -> dict[str, Any]:
    """Copy only cohort assignment metadata into an isolated four-item queue."""
    root, experiment = root.resolve(), experiment.resolve()
    manifest_path, queue_path, outputs = _source_paths(root)
    manifest = _read_json(manifest_path)
    if manifest.get("strategy_version") != STRATEGY:
        raise LightweightError("source run is not Strategy v5")
    source_queue = _read_json(queue_path)
    by_ticker = {str(row.get("ticker")): row for row in source_queue.get("assignments", [])}
    if set(COHORT) - set(by_ticker):
        raise LightweightError("fixed Phase 1 cohort is incomplete")
    assignments: list[dict[str, Any]] = []
    for position, ticker in enumerate(COHORT, start=1):
        source = by_ticker[ticker]
        if source.get("status") not in {"news_present", "no_news"}:
            raise LightweightError(f"control is not terminal: {ticker}")
        assignment_id = f"token-efficiency-v1-{position:02d}-{ticker}"
        control_output = outputs / f"benchmark_task03_{source['assignment_id']}.json"
        if not control_output.exists():
            raise LightweightError(f"control output missing: {ticker}")
        assignments.append({
            "assignment_id": assignment_id,
            "ticker": ticker,
            "company_name": source["company_name"],
            "sector": source.get("sector"),
            "size_bucket": source.get("size_bucket"),
            "prior_news_bucket": source.get("prior_news_bucket"),
            "search_from": source["search_from"],
            "search_to": source["search_to"],
            "strategy_version": STRATEGY,
            "position": position,
            "status": "pending",
            "control_assignment_id": source["assignment_id"],
            "control_output_path": str(control_output.resolve()),
            "control_output_sha256": _sha256(control_output),
        })
    queue = {"schema_version": SCHEMA, "status": "prepared", "cohort": list(COHORT), "assignments": assignments}
    _atomic_json(experiment / "queue.json", queue)
    _atomic_json(experiment / "manifest.json", {
        "schema_version": SCHEMA,
        "benchmark_id": BENCHMARK_ID,
        "source_run": SOURCE_RUN,
        "source_task": SOURCE_TASK,
        "strategy_version": STRATEGY,
        "company_limit": 4,
        "production_write_forbidden": True,
        "source_manifest_sha256": _sha256(manifest_path),
        "source_queue_sha256": _sha256(queue_path),
        "status": "prepared",
    })
    return queue


def preflight(experiment: Path) -> dict[str, Any]:
    """Acquire one assignment without invoking an agent; empty means launch zero."""
    experiment = experiment.resolve()
    manifest = _read_json(experiment / "manifest.json")
    queue_path = experiment / "queue.json"
    queue = _read_json(queue_path)
    if manifest.get("company_limit") != 4 or queue.get("cohort") != list(COHORT):
        raise LightweightError("experiment routing/identity mismatch")
    guard = experiment / "guard.json"
    if guard.exists():
        return {"status": "busy", "agent_launch": False}
    assignment = next((row for row in queue["assignments"] if row.get("status") == "pending"), None)
    if assignment is None:
        return {"status": "empty", "agent_launch": False}
    token = hashlib.sha256(os.urandom(32)).hexdigest()
    lock = {"schema_version": SCHEMA, "run_token": token, "assignment_id": assignment["assignment_id"],
            "created_at": datetime.now(timezone.utc).isoformat()}
    guard.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(guard, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        return {"status": "busy", "agent_launch": False}
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(lock, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    assignment["status"] = "running"
    assignment["run_token"] = token
    _atomic_json(queue_path, queue)
    return {"status": "acquired", "agent_launch": True, "run_token": token,
            "assignment": {k: assignment.get(k) for k in (
                "assignment_id", "ticker", "company_name", "sector", "size_bucket",
                "prior_news_bucket", "search_from", "search_to", "strategy_version", "position")}}


def output_schema() -> dict[str, Any]:
    return strategy_v5.canonical_output_schema()


@dataclass(frozen=True)
class WorkerSpec:
    worker_id: str
    kind: str
    exposure_id: int | None
    exposure: dict[str, Any] | None
    queries: tuple[dict[str, Any], ...]


def _strict_object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise LightweightError(f"unsupported JSON value type: {type(value).__name__}")


def api_compatible_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Add API-required explicit types to a semantic-equivalent schema copy."""
    value = copy.deepcopy(schema)

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if "type" not in node and not any(key in node for key in ("anyOf", "oneOf", "allOf", "$ref")):
            if "const" in node:
                node["type"] = _json_type(node["const"])
            elif "enum" in node and node["enum"]:
                types = sorted({_json_type(item) for item in node["enum"]})
                node["type"] = types[0] if len(types) == 1 else types
        for child in node.get("properties", {}).values():
            visit(child)
        if isinstance(node.get("items"), dict):
            visit(node["items"])
        for keyword in ("anyOf", "oneOf", "allOf"):
            for child in node.get(keyword, []):
                visit(child)

    visit(value)
    return value


def lint_api_schema(schema: dict[str, Any]) -> list[str]:
    """Return structural errors that would make strict output-schema unsafe."""
    errors: list[str] = []

    def visit(node: Any, path: str, *, is_property: bool = False) -> None:
        if not isinstance(node, dict):
            errors.append(f"{path}: schema node must be object")
            return
        alternatives = any(key in node for key in ("anyOf", "oneOf", "allOf", "$ref"))
        if is_property and "type" not in node and not alternatives:
            errors.append(f"{path}: property schema needs explicit type")
        if ("const" in node or "enum" in node) and "type" not in node and not alternatives:
            errors.append(f"{path}: const/enum schema needs explicit type")
        types = node.get("type", [])
        types = [types] if isinstance(types, str) else types
        if "object" in types:
            if not isinstance(node.get("properties"), dict):
                errors.append(f"{path}: object needs properties")
            else:
                missing = set(node.get("required", [])) - set(node["properties"])
                if missing:
                    errors.append(f"{path}: required missing from properties: {sorted(missing)}")
                if node.get("additionalProperties") is not False:
                    errors.append(f"{path}: object must set additionalProperties=false")
        if "array" in types and not isinstance(node.get("items"), dict):
            errors.append(f"{path}: array needs items schema")
        for name, child in node.get("properties", {}).items():
            visit(child, f"{path}.properties.{name}", is_property=True)
        if isinstance(node.get("items"), dict):
            visit(node["items"], f"{path}.items")
        for keyword in ("anyOf", "oneOf", "allOf"):
            for index, child in enumerate(node.get(keyword, [])):
                visit(child, f"{path}.{keyword}[{index}]")

    visit(schema, "$")
    return errors


def synthesis_output_schema() -> dict[str, Any]:
    """API transport form; Python still validates the unchanged canonical schema."""
    schema = api_compatible_schema(output_schema())
    # checked_at is operational scan metadata.  The synthesis transport may emit
    # an empty string or null, but the runner replaces it with its authoritative
    # completion timestamp before canonical validation.
    schema["properties"]["research_result"]["properties"]["checked_at"]["type"] = ["string", "null"]
    errors = lint_api_schema(schema)
    if errors:
        raise LightweightError("invalid synthesis API schema: " + "; ".join(errors))
    return schema


def evidence_card_schema() -> dict[str, Any]:
    return _strict_object({
        "source_id": {"type": "string", "maxLength": 80},
        "query_id": {"type": "string", "maxLength": 80},
        "exposure_id": {"type": ["integer", "null"], "minimum": 0},
        "url": {"type": "string", "maxLength": 2048},
        "source_type": {"type": "string", "maxLength": 80},
        "title": {"type": "string", "maxLength": 300},
        "published_at": {"type": ["string", "null"], "maxLength": 64},
        "excerpt": {"type": "string", "maxLength": MAX_EVIDENCE_EXCERPT},
        "supports": {"type": "string", "maxLength": MAX_EVIDENCE_SUPPORTS},
        "verification_status": {"type": "string", "enum": ["VERIFIED", "UNVERIFIED"]},
    })


def worker_output_schema(spec: WorkerSpec) -> dict[str, Any]:
    """Use canonical exposure/query definitions while keeping worker output bounded."""
    telemetry = output_schema()["properties"]["strategy_telemetry"]["properties"]
    company_identity = copy.deepcopy(telemetry["company_identity"])
    company_identity["type"] = ["object", "null"]
    properties = {
        "worker_id": {"type": "string", "maxLength": 80},
        "kind": {"type": "string", "enum": ["company_web", "exposure", "government", "incremental_ir"]},
        "company_identity": company_identity,
        "economic_exposures": copy.deepcopy(telemetry["economic_exposures"]),
        "exposure_id": {"type": ["integer", "null"], "minimum": 0},
        "queries": {"type": "array", "items": copy.deepcopy(telemetry["queries"]["items"]),
                    "minItems": 1, "maxItems": MAX_QUERIES_PER_WORKER},
        "evidence_cards": {"type": "array", "items": evidence_card_schema(),
                           "maxItems": MAX_QUERY_RESULTS * MAX_QUERIES_PER_WORKER},
        "zero_result_signatures": {"type": "array", "items": {"type": "string", "maxLength": 500},
                                   "maxItems": MAX_QUERIES_PER_WORKER},
        "incremental_ir_applicable": {"type": "boolean"},
    }
    schema = {"$schema": "http://json-schema.org/draft-07/schema#", **_strict_object(properties)}
    # Discovery alone derives the canonical 2..3 exposures. Other workers carry none.
    if spec.kind != "company_web":
        schema["properties"]["economic_exposures"]["minItems"] = 0
        schema["properties"]["economic_exposures"]["maxItems"] = 0
    schema = api_compatible_schema(schema)
    errors = lint_api_schema(schema)
    if errors:
        raise LightweightError("invalid worker API schema: " + "; ".join(errors))
    return schema


def compact_contract(assignment: dict[str, Any]) -> str:
    """Compatibility alias for the company-Web worker's minimal contract."""
    return worker_contract(WorkerSpec("company-web", "company_web", None, None, ({
        "query_id": "company-web-q1", "channel": "official",
        "query": f"{assignment['company_name']} news press IR {assignment['search_from']} {assignment['search_to']}",
        "exposure_index": None, "canonical_driver_phrase": None,
    },)), assignment)


def worker_contract(spec: WorkerSpec, assignment: dict[str, Any]) -> str:
    minimal = {key: assignment.get(key) for key in
               ("assignment_id", "ticker", "company_name", "search_from", "search_to")}
    payload = {"assignment": minimal, "worker_id": spec.worker_id, "kind": spec.kind,
               "exposure_id": spec.exposure_id, "exposure": spec.exposure,
               "queries": list(spec.queries)}
    return "\n".join((
        "Research one bounded NON_TDNET slice. Return only the worker JSON schema object.",
        "Use native Web only; do not use shell, repo search, Gold, benchmark output, prior company context, or prior worker history.",
        "Execute only the supplied 1 or 2 queries; never retry, paraphrase, broaden, or add a query.",
        "Open only results needed to verify evidence. Return bounded evidence cards; omit raw page bodies.",
        "For company_web, verify ticker identity, review the official index/window, and derive 2 or 3 major economic exposures.",
        "Each exposure channel must be external_media or counterparty; government is a second query on that exposure.",
        "For an exposure worker, research only its supplied exposure. Do not discuss or infer other exposures.",
        "Each executed query record must use its supplied query/channel/linkage verbatim and report actual result metadata.",
        "No repair/reconcile pass is allowed. Python validates and stops on any failure.",
        "INPUT=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    ))


def synthesis_contract(assignment: dict[str, Any], discovery: dict[str, Any],
                       queries: list[dict[str, Any]], store: EvidenceStore,
                       elapsed_by_kind: dict[str, float]) -> str:
    minimal = {key: assignment.get(key) for key in
               ("assignment_id", "ticker", "company_name", "search_from", "search_to")}
    payload = {"assignment": minimal, "company_identity": discovery["company_identity"],
               "economic_exposures": discovery["economic_exposures"], "queries": queries,
               "evidence_cards": store.cards(), "stage_elapsed_seconds": elapsed_by_kind}
    return "\n".join((
        "Synthesize the canonical Company News Strategy v5 JSON object from the bounded input only.",
        "No Web or tools are available. Do not invent facts, URLs, queries, evidence, or elapsed times.",
        "Map verified cards to source_provenance/documents/candidates and final events; no_news requires complete evidence.",
        "Use the exact canonical schema. Python performs validation/finalization and no LLM retry is allowed.",
        "BOUNDED_INPUT=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    ))


def codex_command(root: Path, schema_path: Path, result_path: Path, prompt: str,
                  *, web_enabled: bool = True) -> list[str]:
    """Create one ephemeral context; only Web workers receive native Web."""
    disabled = ("shell_tool", "unified_exec", "apps", "plugins", "computer_use",
                "browser_use", "browser_use_external", "browser_use_full_cdp_access",
                "image_generation", "multi_agent")
    command = ["codex"]
    if web_enabled:
        command.append("--search")
    command.extend(("exec", "--ephemeral", "--ignore-user-config"))
    for feature in disabled:
        command.extend(("--disable", feature))
    command.extend(("--json", "--color", "never", "--sandbox", "read-only",
                    "--output-schema", str(schema_path), "--output-last-message", str(result_path),
                    "-C", str(root.resolve()), prompt))
    return command


def _numbers(node: Any, names: set[str]) -> int:
    total = 0
    if isinstance(node, dict):
        for key, value in node.items():
            if key in names and isinstance(value, int) and not isinstance(value, bool):
                total += value
            elif isinstance(value, (dict, list)):
                total += _numbers(value, names)
    elif isinstance(node, list):
        total += sum(_numbers(value, names) for value in node)
    return total


def token_telemetry(events: list[dict[str, Any]], elapsed: float) -> dict[str, Any]:
    """Normalize Codex JSONL usage without feeding event history back to the model."""
    terminal = [event for event in events if event.get("type") in {"turn.completed", "task.completed"}]
    usage_node: Any = terminal
    input_tokens = _numbers(usage_node, {"input_tokens"}) if terminal else None
    cached = _numbers(usage_node, {"cached_input_tokens", "cached_tokens"}) if terminal else None
    output = _numbers(usage_node, {"output_tokens"}) if terminal else None
    reasoning = _numbers(usage_node, {"reasoning_tokens", "reasoning_output_tokens"}) if terminal else None
    completed_items = [event.get("item", {}) for event in events if event.get("type") == "item.completed"]
    tool_types = {"command_execution", "mcp_tool_call", "dynamic_tool_call", "tool_call", "web_search", "web_fetch"}
    tool_calls = sum(str(item.get("type", "")) in tool_types for item in completed_items)
    web_calls = sum("web" in str(item.get("type", "")).lower()
                    or re.search(r"\b(search_query|open)\b", json.dumps(item, ensure_ascii=False), re.I) is not None
                    for item in completed_items)
    model_calls = len(terminal) if terminal else None
    tool_events = []
    for event in events:
        if event.get("type") != "item.completed" or not isinstance(event.get("item"), dict):
            continue
        item = event["item"]
        if str(item.get("type", "")) not in tool_types:
            continue
        serialized = json.dumps(item, ensure_ascii=False)
        query = item.get("query") or item.get("arguments", {}).get("query") if isinstance(item.get("arguments"), dict) else item.get("query")
        tool_events.append({
            "tool_type": str(item.get("type", "unknown")),
            "query": query if isinstance(query, str) else None,
            "result_count": item.get("result_count") if isinstance(item.get("result_count"), int) else None,
            "model_visible_chars": len(serialized),
        })
    return {
        "schema_version": TELEMETRY_SCHEMA,
        "input_tokens": input_tokens,
        "cached_input_tokens": min(cached, input_tokens) if terminal else None,
        "uncached_input_tokens": max(0, input_tokens - cached) if terminal else None,
        "output_tokens": output,
        "reasoning_tokens": reasoning,
        "total_tokens": input_tokens + output if terminal else None,
        "model_call_count": model_calls,
        "tool_call_count": tool_calls,
        "web_call_count": web_calls,
        "research_elapsed_seconds": elapsed,
        "post_research_model_calls": 0,
        "post_research_tokens": 0,
        "turn_started_count": sum(event.get("type") == "turn.started" for event in events),
        "turn_completed_count": len(terminal),
        "turn_completed_usage_raw": [event.get("usage") for event in terminal],
        "thread_ids": [event.get("thread_id") for event in events if event.get("type") == "thread.started" and event.get("thread_id")],
        "tool_events": tool_events,
    }


_SECRET_KEYS = {"authorization", "api_key", "apikey", "access_token", "refresh_token", "cookie", "set-cookie", "run_token"}


def _redact_event(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.casefold() in _SECRET_KEYS else _redact_event(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_event(item) for item in value]
    return value


def _sanitize_event(value: dict[str, Any]) -> dict[str, Any]:
    """Keep usage/call telemetry but discard native tool bodies from persisted JSONL."""
    cleaned = _redact_event(value)
    if cleaned.get("type") == "item.completed" and isinstance(cleaned.get("item"), dict):
        item = dict(cleaned["item"])
        tool_types = {"command_execution", "mcp_tool_call", "dynamic_tool_call", "tool_call",
                      "web_search", "web_fetch"}
        if str(item.get("type", "")) in tool_types:
            for key in ("content", "output", "result", "body", "text"):
                if key in item:
                    item[key] = "[OMITTED_RAW_TOOL_CONTENT]"
        cleaned = {**cleaned, "item": item}
    return cleaned


def _atomic_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(_sanitize_event(event), ensure_ascii=False, separators=(",", ":")) + "\n"
                   for event in events)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _parse_events(stdout: str) -> list[dict[str, Any]]:
    events = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _run_isolated_stage(root: Path, company_dir: Path, spec: WorkerSpec | None,
                        prompt: str, schema: dict[str, Any], *, web_enabled: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    stage_id = spec.worker_id if spec else "final-synthesis"
    stage_dir = company_dir / "workers" / stage_id
    stage_dir.mkdir(parents=True, exist_ok=True)
    schema_path, result_path = stage_dir / "output-schema.json", stage_dir / "last-message.json"
    _atomic_json(schema_path, schema)
    command = codex_command(root, schema_path, result_path, prompt, web_enabled=web_enabled)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    completed = subprocess.run(command, text=True, encoding="utf-8", errors="replace",
                               capture_output=True, check=False)
    elapsed = time.monotonic() - started
    completed_at = datetime.now(timezone.utc).isoformat()
    events = _parse_events(completed.stdout)
    _atomic_jsonl(stage_dir / "raw-events.sanitized.jsonl", events)
    telemetry = token_telemetry(events, elapsed)
    telemetry.update({
        "schema_version": WORKER_TELEMETRY_SCHEMA,
        "worker_id": stage_id,
        "exposure_id": spec.exposure_id if spec else None,
        "kind": spec.kind if spec else "final_synthesis",
        "model_visible_chars": sum(item["model_visible_chars"] for item in telemetry["tool_events"]),
        "started_at": started_at,
        "completed_at": completed_at,
        "validation_status": "PENDING" if completed.returncode == 0 and result_path.exists() else "PROCESS_FAILED",
    })
    _atomic_json(stage_dir / "telemetry.json", telemetry)
    _atomic_json(stage_dir / "execution-summary.json", {
        "exit_code": completed.returncode,
        "stderr": bounded_text(completed.stderr, max_chars=8_000, max_lines=80),
        "event_types": [event.get("type") for event in events],
    })
    if completed.returncode != 0 or not result_path.exists():
        raise LightweightError(f"isolated stage failed: {stage_id} exit={completed.returncode}")
    response = _read_json(result_path)
    try:
        validate_instance(response, schema)
        if spec is not None:
            _validate_worker_response_contract(response, spec)
    except Exception:
        telemetry["validation_status"] = "FAIL"
        _atomic_json(stage_dir / "telemetry.json", telemetry)
        raise
    telemetry["validation_status"] = "PASS"
    telemetry["evidence_cards_returned"] = len(response.get("evidence_cards", []))
    _atomic_json(stage_dir / "telemetry.json", telemetry)
    return response, telemetry


def inject_runtime_metadata(response: dict[str, Any], *, completed_at: Any) -> dict[str, Any]:
    """Set operational metadata from the runner before canonical validation."""
    if not isinstance(response, dict) or not isinstance(response.get("research_result"), dict):
        raise LightweightError("RESEARCH_RESULT object is incomplete")
    if completed_at is None:
        raise LightweightError("authoritative execution completed_at is missing")
    authoritative = parse_datetime(completed_at, "execution completed_at")
    injected = copy.deepcopy(response)
    injected["research_result"]["checked_at"] = authoritative
    return injected


def inject_identity_provenance(response: dict[str, Any], discovery: dict[str, Any],
                               evidence_cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Bind verified Company Web identity evidence to canonical provenance."""
    identity = discovery.get("company_identity") if isinstance(discovery, dict) else None
    if not isinstance(identity, dict):
        raise LightweightError("verified Company Web identity is missing")
    identity_url = canonicalize_url(identity.get("source_url"))
    cards = [card for card in evidence_cards if isinstance(card, dict)
             and card.get("verification_status") == "VERIFIED"
             and str(card.get("query_id", "")).startswith("company-web-")
             and card.get("source_type") in OFFICIAL_EVIDENCE_SOURCE_TYPES
             and canonicalize_url(card.get("url")) == identity_url]
    if not cards:
        raise LightweightError("official Company Web identity evidence is missing")
    card = cards[0]
    repaired = copy.deepcopy(response)
    payload = repaired.get("research_result")
    strategy = repaired.get("strategy_telemetry")
    if not isinstance(payload, dict) or not isinstance(strategy, dict):
        raise LightweightError("canonical response is incomplete")
    provenance = strategy.get("source_provenance")
    if not isinstance(provenance, list):
        raise LightweightError("canonical source provenance is missing")
    matches = [row for row in provenance if isinstance(row, dict)
               and canonicalize_url(row.get("url")) == identity_url]
    if len(matches) > 1:
        raise LightweightError("duplicate canonical identity provenance")
    authoritative_source = {
        "url": identity_url,
        "source_type": "company_official",
        "content_verified": True,
        "evidence_excerpt": card["excerpt"],
    }
    if matches:
        provenance[provenance.index(matches[0])] = authoritative_source
    else:
        provenance.append(authoritative_source)
    strategy["company_identity"] = {
        "verified_ticker": identity.get("verified_ticker"),
        "source_url": identity_url,
        "evidence": identity.get("evidence"),
    }
    payload["sources_checked_count"] = len(provenance)
    return repaired


def restore_published_at_from_evidence(response: dict[str, Any],
                                       evidence_cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Restore exact publication instants only; never invent time or timezone."""
    repaired = copy.deepcopy(response)
    payload = repaired.get("research_result")
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise LightweightError("canonical research items are missing")
    for index, item in enumerate(payload["items"]):
        matching = [card for card in evidence_cards if isinstance(card, dict)
                    and card.get("verification_status") == "VERIFIED"
                    and canonicalize_url(card.get("url")) == canonicalize_url(item.get("source_url"))
                    and card.get("title") == item.get("headline")]
        if not matching:
            raise LightweightError(f"items[{index}] has no authoritative evidence card")
        raw_values = {card.get("published_at") for card in matching}
        if None in raw_values:
            raw_values.remove(None)
        if not raw_values:
            raise LightweightError(f"items[{index}] authoritative published_at is unknown")
        instants: set[str] = set()
        date_only: set[str] = set()
        for value in raw_values:
            if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                date_only.add(value)
                continue
            instants.add(parse_datetime(value, f"items[{index}] evidence published_at"))
        if not instants:
            raise LightweightError(
                f"items[{index}] authoritative published_at is date-only; time and timezone unavailable")
        if len(instants) != 1:
            raise LightweightError(f"items[{index}] has conflicting authoritative publication instants")
        authoritative = next(iter(instants))
        if date_only and {value[:10] for value in instants} != date_only:
            raise LightweightError(f"items[{index}] publication date conflicts with authoritative instant")
        item["published_at"] = authoritative
    return repaired


def _validate_worker_response_contract(response: dict[str, Any], spec: WorkerSpec) -> None:
    """Reject added/rephrased queries and cross-slice fields without an LLM repair."""
    if response.get("worker_id") != spec.worker_id or response.get("kind") != spec.kind:
        raise LightweightError(f"worker identity mismatch: {spec.worker_id}")
    if response.get("exposure_id") != spec.exposure_id:
        raise LightweightError(f"worker exposure identity mismatch: {spec.worker_id}")
    expected = [(row["query_id"], row["channel"], row["query"], row.get("exposure_index"))
                for row in spec.queries]
    actual = [(row.get("query_id"), row.get("channel"), row.get("query"), row.get("exposure_index"))
              for row in response["queries"]]
    # query_id is a worker-card linkage field, not part of the canonical query object.
    if len(actual) != len(expected):
        raise LightweightError(f"worker changed fixed query count: {spec.worker_id}")
    if any(row[0] is None for row in actual):
        actual = [(expected[index][0], *row[1:]) for index, row in enumerate(actual)]
    if actual != expected:
        raise LightweightError(f"worker changed fixed query plan: {spec.worker_id}")
    if spec.kind == "company_web":
        if response["company_identity"] is None or not 2 <= len(response["economic_exposures"]) <= 3:
            raise LightweightError("company Web discovery contract incomplete")
        invalid_channels = {row.get("channel") for row in response["economic_exposures"]} - {
            "external_media", "counterparty", None}
        if invalid_channels:
            raise LightweightError(f"invalid exposure channels: {sorted(invalid_channels)}")
    elif response["company_identity"] is not None or response["economic_exposures"]:
        raise LightweightError(f"cross-exposure context returned: {spec.worker_id}")
    allowed_query_ids = {row["query_id"] for row in spec.queries}
    if any(card["query_id"] not in allowed_query_ids for card in response["evidence_cards"]):
        raise LightweightError(f"unplanned evidence-card query link: {spec.worker_id}")


def _query_record(query_id: str, row: dict[str, Any]) -> dict[str, Any]:
    return {"query_id": query_id, "channel": row["channel"], "query": row["query"],
            "exposure_index": row.get("exposure_index"),
            "canonical_driver_phrase": row.get("canonical_driver_phrase")}


def plan_exposure_workers(assignment: dict[str, Any], discovery: dict[str, Any],
                          store: EvidenceStore) -> list[WorkerSpec]:
    exposures = discovery["economic_exposures"]
    plan = strategy_v5.query_plan(assignment["company_name"], exposures,
                                  search_from=assignment["search_from"], search_to=assignment["search_to"])
    specs: list[WorkerSpec] = []
    for index, exposure in enumerate(exposures):
        rows = [row for row in plan if row.get("exposure_index") == index]
        queries = []
        for number, row in enumerate(rows[:MAX_QUERIES_PER_WORKER], start=1):
            query_id = f"exposure-{index + 1}-q{number}"
            if store.register_query(query_id, row["query"]):
                queries.append(_query_record(query_id, row))
        if not queries:
            raise LightweightError(f"no unique query for exposure {index}")
        specs.append(WorkerSpec(f"exposure-{index + 1}", "exposure", index,
                                copy.deepcopy(exposure), tuple(queries)))
    if discovery["incremental_ir_applicable"]:
        query = f"{assignment['company_name']} IR {assignment['search_from']} {assignment['search_to']} pricing volume order capacity customer margin supply capex"
        if store.register_query("incremental-ir-q1", query):
            specs.append(WorkerSpec("incremental-ir", "incremental_ir", None, None, ({
                "query_id": "incremental-ir-q1", "channel": "incremental_ir", "query": query,
                "exposure_index": None, "canonical_driver_phrase": None,
            },)))
    if 1 + len(specs) > MAX_WEB_WORKERS:
        raise LightweightError(f"Web worker limit exceeded: {1 + len(specs)} > {MAX_WEB_WORKERS}")
    if any(len(spec.queries) > MAX_QUERIES_PER_WORKER for spec in specs):
        raise LightweightError("query limit exceeded")
    return specs


def _add_cards(store: EvidenceStore, response: dict[str, Any]) -> None:
    for card in response.get("evidence_cards", []):
        store.add(url=card["url"], source_type=card["source_type"], title=card["title"],
                  published_at=card["published_at"], body=card["excerpt"], supports=card["supports"],
                  exposure_id=card["exposure_id"], query_id=card["query_id"],
                  verification_status=card["verification_status"], relevant_terms=[])
    for query in response.get("zero_result_signatures", []):
        store.register_zero_result(query)


def aggregate_worker_telemetry(rows: list[dict[str, Any]]) -> dict[str, Any]:
    additive = ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens",
                "reasoning_tokens", "total_tokens", "model_call_count", "tool_call_count",
                "web_call_count", "research_elapsed_seconds", "model_visible_chars")
    def sum_known(key: str) -> int | float | None:
        values = [row.get(key) for row in rows]
        return None if any(value is None for value in values) else sum(values)
    value = {key: sum_known(key) for key in additive}
    value.update({"schema_version": TELEMETRY_SCHEMA, "workers": rows,
                  "worker_count": sum(row.get("kind") != "final_synthesis" for row in rows),
                  "synthesis_model_call_count": sum(row.get("model_call_count", 0) for row in rows
                                                    if row.get("kind") == "final_synthesis"),
                  "post_research_model_calls": 0, "post_research_tokens": 0,
                  "thread_ids": [thread for row in rows for thread in row.get("thread_ids", [])],
                  "canonical_schema_sha256": strategy_v5.canonical_schema_sha256(),
                  "validation_status": "PENDING"})
    return value


def codex_runner(root: Path, experiment: Path, assignment: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run Web slices in independent processes and synthesize from cards only."""
    company_dir = experiment / "companies" / f"{assignment['position']:02d}-{assignment['ticker']}"
    company_dir.mkdir(parents=True, exist_ok=True)
    store = EvidenceStore()
    official_query = f"{assignment['company_name']} news press IR {assignment['search_from']} {assignment['search_to']}"
    store.register_query("company-web-q1", official_query)
    discovery_spec = WorkerSpec("company-web", "company_web", None, None, ({
        "query_id": "company-web-q1", "channel": "official", "query": official_query,
        "exposure_index": None, "canonical_driver_phrase": None,
    },))
    discovery, discovery_telemetry = _run_isolated_stage(
        root, company_dir, discovery_spec, worker_contract(discovery_spec, assignment),
        worker_output_schema(discovery_spec), web_enabled=True)
    _add_cards(store, discovery)
    outputs = [discovery]
    telemetry_rows = [discovery_telemetry]
    for spec in plan_exposure_workers(assignment, discovery, store):
        response, telemetry = _run_isolated_stage(
            root, company_dir, spec, worker_contract(spec, assignment),
            worker_output_schema(spec), web_enabled=True)
        _add_cards(store, response)
        outputs.append(response)
        telemetry_rows.append(telemetry)
    queries = [query for output in outputs for query in output["queries"]]
    elapsed = {
        "company_web": discovery_telemetry["research_elapsed_seconds"],
        "external": sum(row["research_elapsed_seconds"] for row in telemetry_rows[1:]),
        "provenance": 0.0,
    }
    synthesis_prompt = synthesis_contract(assignment, discovery, queries, store, elapsed)
    response, synthesis_telemetry = _run_isolated_stage(
        root, company_dir, None, synthesis_prompt, synthesis_output_schema(), web_enabled=False)
    response = inject_runtime_metadata(response, completed_at=synthesis_telemetry.get("completed_at"))
    response = inject_identity_provenance(response, discovery, store.cards())
    response = restore_published_at_from_evidence(response, store.cards())
    telemetry_rows.append(synthesis_telemetry)
    measured = aggregate_worker_telemetry(telemetry_rows)
    _atomic_json(company_dir / "measured-token-telemetry.json", measured)
    _atomic_json(company_dir / "evidence-state.json", {
        "cards": store.cards(), "raw_body_retained": False,
        "cross_worker_payload": "bounded_evidence_cards_only",
    })
    return response, measured


def offline_cost_model(worker_counts: tuple[int, ...] = (2, 3, 4, 5)) -> list[dict[str, Any]]:
    """Conservative pre-run budget based on Phase1.5 telemetry and hard worker caps."""
    observed_startup_floor = 24_600
    startup = 30_000
    worker_prompt = 8_000
    web_interaction = 25_000
    card_output = 6_000
    synthesis = 115_000
    rows = []
    for count in worker_counts:
        total = count * (startup + worker_prompt + web_interaction + card_output) + synthesis
        rows.append({"worker_count": count,
                     "observed_startup_floor_reference": count * observed_startup_floor,
                     "startup_fixed": count * startup,
                     "worker_prompt_input": count * worker_prompt,
                     "web_interaction": count * web_interaction,
                     "evidence_card_output": count * card_output,
                     "final_synthesis": synthesis, "python_processing": 0,
                     "conservative_upper_total": total,
                     "hard_500k": "GO" if total <= 500_000 else "NO_GO",
                     "preferred_400k": "GO" if total <= 400_000 else "NO_GO"})
    return rows


def _owned(experiment: Path, run_token: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    guard_path = experiment / "guard.json"
    guard = _read_json(guard_path)
    if guard.get("run_token") != run_token:
        raise LightweightError("guard ownership mismatch")
    queue = _read_json(experiment / "queue.json")
    assignment = next((x for x in queue["assignments"] if x["assignment_id"] == guard["assignment_id"]), None)
    if assignment is None or assignment.get("status") != "running" or assignment.get("run_token") != run_token:
        raise LightweightError("running assignment identity mismatch")
    return guard_path, queue, assignment


def finalize(experiment: Path, run_token: str, response: dict[str, Any], telemetry: dict[str, Any]) -> dict[str, Any]:
    """Validate, save and atomically transition the private queue; always release guard."""
    experiment = experiment.resolve()
    guard_path, queue, assignment = _owned(experiment, run_token)
    try:
        payload = response.get("research_result")
        strategy = response.get("strategy_telemetry")
        if not isinstance(payload, dict) or not isinstance(strategy, dict):
            raise LightweightError("RESEARCH_RESULT object is incomplete")
        strategy_v5.validate_generated_response(response)
        validate_payload(payload)
        if payload.get("run_id") != assignment["assignment_id"] or payload.get("task_id") != assignment["assignment_id"]:
            raise LightweightError("payload assignment mismatch")
        if payload.get("ticker") != assignment["ticker"]:
            raise LightweightError("payload ticker mismatch")
        strategy_v5.validate(strategy, assignment, payload)
        if set(telemetry) < {
            "input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens", "reasoning_tokens",
            "model_call_count", "tool_call_count", "web_call_count", "research_elapsed_seconds",
            "post_research_model_calls", "post_research_tokens", "total_tokens",
        }:
            raise LightweightError("token telemetry incomplete")
        company_dir = experiment / "companies" / f"{assignment['position']:02d}-{assignment['ticker']}"
        result_path, strategy_path, telemetry_path = company_dir / "research-result.json", company_dir / "strategy-telemetry.json", company_dir / "token-telemetry.json"
        if any(path.exists() for path in (result_path, strategy_path, telemetry_path)):
            raise LightweightError("artifact collision")
        telemetry = {**telemetry, "validation_status": "PASS",
                     "canonical_schema_sha256": strategy_v5.canonical_schema_sha256()}
        _atomic_json(result_path, payload)
        _atomic_json(strategy_path, strategy)
        _atomic_json(telemetry_path, {**telemetry, "assignment_id": assignment["assignment_id"], "ticker": assignment["ticker"]})
        _atomic_json(company_dir / "validation-result.json", {"status": "PASS", "schema_sha256": strategy_v5.canonical_schema_sha256()})
        assignment.update(status="completed", result_type="news_present" if payload.get("items") else "no_news",
                          event_count=len(payload.get("items", [])), result_path=str(result_path), telemetry_path=str(telemetry_path))
    except Exception as exc:
        company_dir = experiment / "companies" / f"{assignment['position']:02d}-{assignment['ticker']}"
        _atomic_json(company_dir / "validation-result.json", {
            "status": "FAIL", "error_type": type(exc).__name__, "error_message": str(exc),
            "schema_sha256": strategy_v5.canonical_schema_sha256(),
        })
        measured_path = company_dir / "measured-token-telemetry.json"
        if measured_path.exists():
            measured = _read_json(measured_path)
            measured.update(validation_status="FAIL", validation_error_type=type(exc).__name__,
                            validation_error_message=str(exc))
            _atomic_json(measured_path, measured)
        assignment.update(status="failed", error_type=type(exc).__name__, error_message=str(exc))
        _atomic_json(experiment / "queue.json", queue)
        guard_path.unlink(missing_ok=True)
        raise
    _atomic_json(experiment / "queue.json", queue)
    guard_path.unlink(missing_ok=True)
    return assignment


def run_next(root: Path, experiment: Path, runner: Callable[[Path, Path, dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]] = codex_runner) -> dict[str, Any]:
    snapshot = preflight(experiment)
    if not snapshot.get("agent_launch"):
        return snapshot
    try:
        response, telemetry = runner(root.resolve(), experiment.resolve(), snapshot["assignment"])
        return {"status": "completed", "assignment": finalize(experiment, snapshot["run_token"], response, telemetry)}
    except Exception:
        guard = experiment / "guard.json"
        if guard.exists():
            try:
                _, queue, assignment = _owned(experiment, snapshot["run_token"])
                assignment.update(status="failed", error_type="ResearchFailure", error_message="research process failed")
                _atomic_json(experiment / "queue.json", queue)
            finally:
                guard.unlink(missing_ok=True)
        raise


def aggregate(experiment: Path) -> dict[str, Any]:
    queue = _read_json(experiment / "queue.json")
    rows = queue["assignments"]
    token_rows = [_read_json(Path(row["telemetry_path"])) for row in rows if row.get("status") == "completed"]
    totals = {key: sum(row.get(key, 0) for row in token_rows) for key in (
        "input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens", "reasoning_tokens",
        "total_tokens", "model_call_count", "tool_call_count", "web_call_count", "post_research_model_calls", "post_research_tokens")}
    completed = sum(row.get("status") == "completed" for row in rows)
    failed = sum(row.get("status") == "failed" for row in rows)
    report = {
        "schema_version": SCHEMA,
        "completed": completed, "failed": failed, "pending": sum(row.get("status") == "pending" for row in rows),
        "running": sum(row.get("status") == "running" for row in rows), "guard_present": (experiment / "guard.json").exists(),
        "control": {"total_tokens": CONTROL_TOTAL_TOKENS, "input_tokens": CONTROL_INPUT_TOKENS,
                    "cached_input_tokens": CONTROL_CACHED_INPUT_TOKENS, "output_tokens": CONTROL_OUTPUT_TOKENS,
                    "tokens_per_company": CONTROL_TOTAL_TOKENS / 4},
        "treatment": {**totals, "tokens_per_company": totals["total_tokens"] / completed if completed else None},
        "companies": [{k: row.get(k) for k in ("position", "ticker", "status", "result_type", "event_count", "error_type", "error_message")} for row in rows],
    }
    if completed == 4:
        treatment = totals["total_tokens"]
        report["comparison"] = {
            "token_reduction_percent": (1 - treatment / CONTROL_TOTAL_TOKENS) * 100,
            "cached_input_reduction_percent": (1 - totals["cached_input_tokens"] / CONTROL_CACHED_INPUT_TOKENS) * 100,
            "post_research_reduction_percent": 100.0,
            "position_1_to_4_input_growth_percent": ((_read_json(Path(rows[3]["telemetry_path"]))["input_tokens"] / max(1, _read_json(Path(rows[0]["telemetry_path"]))["input_tokens"])) - 1) * 100,
        }
    _atomic_json(experiment / "ab-report.json", report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--experiment", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    sub.add_parser("run-next")
    sub.add_parser("aggregate")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "prepare":
        result = prepare_phase1(args.root, args.experiment)
    elif args.command == "run-next":
        result = run_next(args.root, args.experiment)
    else:
        result = aggregate(args.experiment)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
