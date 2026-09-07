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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib import news_monitor
from lib.news_monitor import canonicalize_url, parse_datetime, validate_payload
from tools import company_news_strategy_v4 as strategy_v4
from tools import company_news_strategy_v5 as strategy_v5
from tools.company_news_canonical_schema import validate_instance
from tools.company_news_overlap_scope import (
    OverlapScopeError,
    apply_ir_nightly_overlap,
    assert_incremental_ir_candidate_allowed,
)


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
INDEX_ONLY_SOURCE_TYPES = frozenset({"official_index", "listing", "index"})
DOCUMENT_PROVENANCE_STATES = frozenset({
    "DISCOVERED_IN_INDEX", "URL_DISCOVERED", "BODY_FETCH_ATTEMPTED",
    "BODY_VERIFIED", "ACCESS_FAILED",
})
CONTROL_TOTAL_TOKENS = 8_584_237
CONTROL_INPUT_TOKENS = 8_545_419
CONTROL_CACHED_INPUT_TOKENS = 8_227_072
CONTROL_OUTPUT_TOKENS = 38_818


class LightweightError(RuntimeError):
    pass


class PublicationAuthorityError(LightweightError):
    """The generated event cannot be bound to saved authoritative evidence."""


@dataclass(frozen=True)
class EvidenceCard:
    evidence_id: str
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

    @staticmethod
    def evidence_id(url: str) -> str:
        normalized_url = canonicalize_url(url)
        return "evidence-" + hashlib.sha256(normalized_url.encode()).hexdigest()[:24]

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
        digest = hashlib.sha256(normalized_url.encode()).hexdigest()
        card = EvidenceCard(
            evidence_id="evidence-" + digest[:24],
            source_id=digest[:16],
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

    def evidence_id_for_url(self, url: str) -> str | None:
        card = self._cards.get(canonicalize_url(url))
        return card.evidence_id if card is not None else None

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
    # Company identity is a frozen Python-side authority in the multi-company
    # runner.  Keep the field for canonical-schema compatibility, but permit the
    # synthesis transport to return null before the registry value is injected.
    schema["properties"]["strategy_telemetry"]["properties"]["company_identity"]["type"] = [
        "object", "null"]
    item = schema["properties"]["research_result"]["properties"]["items"]["items"]
    item["properties"]["supporting_evidence_ids"] = {
        "type": "array", "items": {"type": "string", "maxLength": 80},
        "minItems": 1, "maxItems": MAX_EVIDENCE_SOURCES,
    }
    item["required"].append("supporting_evidence_ids")
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
                       elapsed_by_kind: dict[str, float], *,
                       registry_identity_authoritative: bool = False) -> str:
    minimal = {key: assignment.get(key) for key in
               ("assignment_id", "ticker", "company_name", "search_from", "search_to")}
    payload = {"assignment": minimal,
               "economic_exposures": discovery["economic_exposures"], "queries": queries,
               "evidence_cards": store.cards(), "stage_elapsed_seconds": elapsed_by_kind}
    if not registry_identity_authoritative:
        payload["company_identity"] = discovery["company_identity"]
    return "\n".join((
        "Synthesize the canonical Company News Strategy v5 JSON object from the bounded input only.",
        "No Web or tools are available. Do not invent facts, URLs, queries, evidence, or elapsed times.",
        "Map verified cards to source_provenance/documents/candidates and final events; no_news requires complete evidence.",
        "Every final event must list exact supporting_evidence_ids from BOUNDED_INPUT. Never invent or alter an evidence ID.",
        ("Set strategy_telemetry.company_identity to null. Python injects the frozen registry identity; "
         "do not select or infer an identity URL."
         if registry_identity_authoritative else
         "Use only the supplied company identity; Python still validates its official evidence."),
        "Use the exact canonical schema. Python performs validation/finalization and no LLM retry is allowed.",
        "BOUNDED_INPUT=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    ))


ISOLATED_CODEX_STATE_POLICY = {
    "profile": "worker-isolated-sqlite-v1",
    "sqlite_location": "worker_artifact_directory",
    "ephemeral": True,
    "history_persistence": "none",
    "shared_sqlite": False,
    "codex_home_unchanged": True,
    "auth_copy_forbidden": True,
}


def _state_component(value: str, *, label: str) -> str:
    """Accept one short path component and reject traversal or aliases."""
    if (not isinstance(value, str) or not value or len(value) > 80
            or value in {".", ".."} or "/" in value or "\\" in value
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is None):
        raise LightweightError(f"invalid isolated state {label}")
    return value


def validate_worker_sqlite_home(root: Path, worker_dir: Path) -> Path:
    """Validate an existing worker artifact directory for child Codex state."""
    workspace = root.resolve()
    target = worker_dir.resolve()
    try:
        target.relative_to(workspace)
    except ValueError as exc:
        raise LightweightError("isolated sqlite_home is outside workspace") from exc
    if not target.is_dir():
        raise LightweightError("isolated sqlite_home worker directory is missing")
    forbidden = ("state_5.sqlite", "state_5.sqlite-wal", "state_5.sqlite-shm", "auth.json")
    if any((target / name).exists() for name in forbidden):
        raise LightweightError(f"isolated sqlite_home collision: {target}")
    try:
        probe = target / ".parent-write-probe"
        with probe.open("x", encoding="utf-8") as handle:
            handle.write("")
        probe.unlink()
    except OSError as exc:
        raise LightweightError(f"isolated sqlite_home initialization failed: {exc}") from exc
    return target


def isolated_sqlite_home(root: Path, company_dir: Path, worker_id: str) -> Path:
    """Use the already-created unique worker artifact directory as sqlite_home."""
    workspace = root.resolve()
    company = company_dir.resolve()
    try:
        company.relative_to(workspace)
    except ValueError as exc:
        raise LightweightError("isolated state company directory is outside workspace") from exc
    stage_id = _state_component(worker_id, label="worker id")
    return validate_worker_sqlite_home(root, company / "workers" / stage_id)


def codex_command(root: Path, schema_path: Path, result_path: Path, prompt: str,
                  *, sqlite_home: Path, web_enabled: bool = True) -> list[str]:
    """Create one ephemeral context; only Web workers receive native Web."""
    disabled = ("shell_tool", "unified_exec", "apps", "plugins", "computer_use",
                "browser_use", "browser_use_external", "browser_use_full_cdp_access",
                "image_generation", "multi_agent")
    command = ["codex"]
    if web_enabled:
        command.append("--search")
    sqlite_path = sqlite_home.resolve()
    try:
        sqlite_path.relative_to(root.resolve())
    except ValueError as exc:
        raise LightweightError("sqlite_home must remain inside workspace") from exc
    command.extend(("exec", "--ephemeral", "--ignore-user-config", "--strict-config",
                    "-c", "sqlite_home=" + json.dumps(str(sqlite_path)),
                    "-c", "history.persistence=none"))
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
                        prompt: str, schema: dict[str, Any], *, web_enabled: bool,
                        precreated_worker_dir: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    stage_id = spec.worker_id if spec else "final-synthesis"
    if precreated_worker_dir is None:
        stage_dir = company_dir / "workers" / stage_id
        try:
            stage_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise LightweightError(
                f"worker artifact directory collision or initialization failure: {exc}") from exc
        sqlite_home = isolated_sqlite_home(root, company_dir, stage_id)
    else:
        stage_dir = precreated_worker_dir.resolve()
        sqlite_home = validate_worker_sqlite_home(root, stage_dir)
    schema_path, result_path = stage_dir / "output-schema.json", stage_dir / "last-message.json"
    _atomic_json(schema_path, schema)
    command = codex_command(root, schema_path, result_path, prompt,
                            sqlite_home=sqlite_home, web_enabled=web_enabled)
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
        "sqlite_home": str(sqlite_home),
        "sqlite_state_path": str((sqlite_home / "state_5.sqlite").resolve()),
        "sqlite_state_file_created": (sqlite_home / "state_5.sqlite").is_file(),
        "sqlite_state_policy": copy.deepcopy(ISOLATED_CODEX_STATE_POLICY),
        "started_at": started_at,
        "completed_at": completed_at,
        "validation_status": "PENDING" if completed.returncode == 0 and result_path.exists() else "PROCESS_FAILED",
    })
    _atomic_json(stage_dir / "telemetry.json", telemetry)
    _atomic_json(stage_dir / "execution-summary.json", {
        "exit_code": completed.returncode,
        "stderr": bounded_text(completed.stderr, max_chars=8_000, max_lines=80),
        "event_types": [event.get("type") for event in events],
        "sqlite_home": str(sqlite_home),
        "sqlite_state_path": str((sqlite_home / "state_5.sqlite").resolve()),
        "global_codex_home_changed": False,
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


def inject_runtime_identity(response: dict[str, Any], assignment: dict[str, Any]) -> dict[str, Any]:
    """Bind payload execution identity to the acquired private assignment."""
    assignment_id = assignment.get("assignment_id")
    ticker = assignment.get("ticker")
    if not isinstance(assignment_id, str) or not assignment_id.strip():
        raise LightweightError("assignment runtime identity is missing")
    if not isinstance(ticker, str) or not ticker.strip():
        raise LightweightError("assignment ticker identity is missing")
    repaired = copy.deepcopy(response)
    payload = repaired.get("research_result")
    strategy = repaired.get("strategy_telemetry")
    if not isinstance(payload, dict) or not isinstance(strategy, dict):
        raise LightweightError("canonical response is incomplete")
    if payload.get("ticker") != ticker or strategy.get("ticker") != ticker:
        raise LightweightError("model ticker conflicts with acquired assignment")
    if strategy.get("assignment_id") != assignment_id:
        raise LightweightError("model assignment identity conflicts with acquired assignment")
    payload["run_id"] = assignment_id
    payload["task_id"] = assignment_id
    return repaired


def apply_source_count_authority(response: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize and deduplicate verified provenance before deriving its count."""
    repaired = copy.deepcopy(response)
    payload = repaired.get("research_result")
    strategy = repaired.get("strategy_telemetry")
    if not isinstance(payload, dict) or not isinstance(strategy, dict):
        raise LightweightError("canonical response is incomplete")
    sources = strategy.get("source_provenance")
    if not isinstance(sources, list):
        raise LightweightError("canonical source provenance is missing")
    deduped: list[dict[str, Any]] = []
    by_url: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise LightweightError(f"source_provenance[{index}] is invalid")
        canonical_url = canonicalize_url(source.get("url"))
        if not canonical_url or source.get("content_verified") is not True:
            raise LightweightError(f"source_provenance[{index}] is not verified")
        normalized = {**source, "url": canonical_url}
        previous = by_url.get(canonical_url)
        if previous is not None:
            comparable = ("source_type", "content_verified", "evidence_excerpt")
            if any(previous.get(key) != normalized.get(key) for key in comparable):
                raise LightweightError(f"conflicting duplicate source provenance: {canonical_url}")
            continue
        by_url[canonical_url] = normalized
        deduped.append(normalized)
    strategy["source_provenance"] = deduped
    payload["sources_checked_count"] = len(deduped)
    return repaired


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


def derive_document_provenance_state(document: dict[str, Any],
                                     evidence_cards: list[dict[str, Any]]) -> str:
    """Derive body provenance from bounded evidence without trusting synthesis labels."""
    document_url = canonicalize_url(document.get("url"))
    matching = [card for card in evidence_cards if isinstance(card, dict)
                and canonicalize_url(card.get("url")) == document_url]
    explicit = {card.get("document_provenance_state") for card in matching
                if card.get("document_provenance_state") in DOCUMENT_PROVENANCE_STATES}
    if len(explicit) > 1:
        raise LightweightError("conflicting explicit document provenance states")
    if explicit:
        return next(iter(explicit))
    body_cards = [card for card in matching if card.get("verification_status") == "VERIFIED"
                  and card.get("source_type") not in INDEX_ONLY_SOURCE_TYPES
                  and isinstance(card.get("excerpt"), str) and card.get("excerpt").strip()]
    if body_cards:
        return "BODY_VERIFIED"
    if document.get("body_status") == "INACCESSIBLE":
        return "ACCESS_FAILED"
    index_cards = [card for card in matching if card.get("source_type") in INDEX_ONLY_SOURCE_TYPES]
    if index_cards:
        title = re.sub(r"\s+", " ", str(document.get("title") or "")).strip().casefold()
        if title and any(
                re.sub(r"\s+", " ", str(card.get("title") or "")).strip().casefold() == title
                and card.get("exact_url_from_saved_evidence") is True for card in index_cards):
            return "URL_DISCOVERED"
        return "DISCOVERED_IN_INDEX"
    if document.get("body_status") == "VERIFIED":
        raise LightweightError("body VERIFIED lacks actual-content evidence")
    return "URL_DISCOVERED"


def classify_recall_materiality(facts: dict[str, Any]) -> str:
    """Classify a verified recall from structured facts, never from its title alone."""
    if facts.get("body_verified") is not True:
        return "UNKNOWN"
    required = (
        "direct_company_linkage", "actual_product_recall", "affected_product",
        "commercial_or_operational_impact", "meaningful_earnings_pathway",
    )
    return "PASS" if all(facts.get(key) for key in required) else "FAIL"


def apply_document_provenance_authority(
        response: dict[str, Any], evidence_cards: list[dict[str, Any]],
        ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Downgrade index-only documents and emit a separate provenance ledger."""
    repaired = copy.deepcopy(response)
    payload = repaired.get("research_result")
    strategy = repaired.get("strategy_telemetry")
    if not isinstance(payload, dict) or not isinstance(strategy, dict):
        raise LightweightError("canonical response is incomplete")
    ledger = []
    non_body_urls = set()
    for index, document in enumerate(strategy.get("documents", [])):
        state = derive_document_provenance_state(document, evidence_cards)
        document_url = canonicalize_url(document.get("url"))
        matching_cards = [card for card in evidence_cards if isinstance(card, dict)
                          and canonicalize_url(card.get("url")) == document_url]
        if (document.get("scope_label") == "OUT_OF_SCOPE_TRIVIAL"
                and not str(document.get("index_evidence") or "").strip()):
            authoritative = next((card for card in matching_cards
                                  if _card_has_authoritative_content(card)), None)
            if authoritative is not None:
                document["index_evidence"] = authoritative["excerpt"]
        ledger.append({"document_index": index, "url": document_url, "state": state})
        if state != "BODY_VERIFIED":
            non_body_urls.add(document_url)
            document["body_status"] = "INACCESSIBLE"
            document["final_url"] = None
            document["attempt_evidence"] = (
                f"Python provenance authority: {state}; actual document body is not verified.")
    provenance = strategy.get("source_provenance")
    if not isinstance(provenance, list):
        raise LightweightError("canonical source provenance is missing")
    strategy["source_provenance"] = [row for row in provenance
        if not (canonicalize_url(row.get("url")) in non_body_urls
                and row.get("source_type") in INDEX_ONLY_SOURCE_TYPES)]
    payload["sources_checked_count"] = len(strategy["source_provenance"])
    return repaired, ledger


def normalize_documents_from_evidence(response: dict[str, Any], evidence_cards: list[dict[str, Any]],
                                      assignment: dict[str, Any]) -> dict[str, Any]:
    """Bind document dates/URLs to evidence and exclude out-of-window background."""
    repaired = copy.deepcopy(response)
    strategy = repaired.get("strategy_telemetry")
    if not isinstance(strategy, dict) or not isinstance(strategy.get("documents"), list):
        raise LightweightError("canonical documents are missing")
    start = date.fromisoformat(assignment["search_from"])
    end = date.fromisoformat(assignment["search_to"])
    candidates = strategy.get("candidate_provenance", [])
    selected_urls = {canonicalize_url(row.get("document_url")) for row in candidates
                     if isinstance(row, dict) and row.get("selected_for_output") is True}
    normalized = []
    for index, document in enumerate(strategy["documents"]):
        if not isinstance(document, dict):
            raise LightweightError(f"documents[{index}] is invalid")
        document_url = canonicalize_url(document.get("url"))
        all_matching = [card for card in evidence_cards if isinstance(card, dict)
                        and canonicalize_url(card.get("url")) == document_url]
        matching = [card for card in all_matching if card.get("verification_status") == "VERIFIED"]
        if not all_matching:
            raise LightweightError(f"documents[{index}] has no evidence card")
        if not matching and document.get("body_status") != "INACCESSIBLE":
            raise LightweightError(f"documents[{index}] has no verified evidence card")
        source_dates: set[str] = set()
        for card in matching:
            value = card.get("published_at")
            if value is None:
                continue
            if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                try:
                    source_dates.add(date.fromisoformat(value).isoformat())
                except ValueError as exc:
                    raise LightweightError(f"documents[{index}] evidence date is invalid") from exc
            else:
                exact = parse_datetime(value, f"documents[{index}] evidence published_at")
                assert exact is not None
                source_dates.add(str(value).strip()[:10])
        existing = document.get("date") if matching else None
        if existing in (None, ""):
            document_date = next(iter(source_dates)) if len(source_dates) == 1 else None
        else:
            try:
                document_date = date.fromisoformat(existing).isoformat()
            except (TypeError, ValueError) as exc:
                raise LightweightError(f"documents[{index}] date is invalid") from exc
            if source_dates and source_dates != {document_date}:
                raise LightweightError(f"documents[{index}] date conflicts with verified evidence")
        if len(source_dates) > 1:
            raise LightweightError(f"documents[{index}] has conflicting verified dates")
        if document_date is not None and not (start <= date.fromisoformat(document_date) <= end):
            if document_url in selected_urls:
                raise LightweightError(f"documents[{index}] candidate is outside the assigned window")
            continue
        document["date"] = document_date
        document["url"] = document_url
        if document.get("body_status") == "VERIFIED":
            document["final_url"] = document_url
        normalized.append(document)
    strategy["documents"] = normalized
    retained_urls = {row["url"] for row in normalized}
    retained_candidates = [row for row in candidates
                           if canonicalize_url(row.get("document_url")) in retained_urls]
    for index, candidate in enumerate(retained_candidates):
        candidate["candidate_index"] = index
        candidate["document_url"] = canonicalize_url(candidate.get("document_url"))
        if candidate.get("source_url"):
            candidate["source_url"] = canonicalize_url(candidate.get("source_url"))
    strategy["candidate_provenance"] = retained_candidates
    strategy["candidate_count"] = len(retained_candidates)
    return repaired


_POLICY_UNCERTAINTY = re.compile(
    r"(?:direct (?:company )?(?:impact|linkage)|effect on .{0,40}|orders?|revenue|earnings|"
    r"(?:受注|売上|業績|直接(?:的)?影響).{0,30})(?:not (?:established|verified|confirmed)|"
    r"unverified|未確認|確認されていない|確認できない)", re.I)
_POLICY_DIRECT = re.compile(
    r"(?:was selected|was awarded|contracted|customer adopted|supply agreement|subsidy recipient|"
    r"採択(?:された|を受け)|受注(?:した|を獲得)|供給契約|顧客採用|補助対象事業者)", re.I)


def apply_materiality_authority(response: dict[str, Any], evidence_cards: list[dict[str, Any]],
                                *, company_name: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Reject selected general context lacking affirmative company linkage."""
    repaired = copy.deepcopy(response)
    payload = repaired.get("research_result")
    strategy = repaired.get("strategy_telemetry")
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list) or not isinstance(strategy, dict):
        raise LightweightError("canonical materiality state is incomplete")
    documents = {canonicalize_url(row.get("url")): row for row in strategy.get("documents", [])}
    retained_items = []
    decisions: list[dict[str, Any]] = []
    selected_candidates = [row for row in strategy.get("candidate_provenance", [])
                           if row.get("selected_for_output") is True]
    for item in payload["items"]:
        item_url = canonicalize_url(item.get("source_url"))
        candidate = next((row for row in selected_candidates
                          if canonicalize_url(row.get("source_url")) == item_url
                          and row.get("headline") == item.get("headline")), None)
        document = documents.get(canonicalize_url(candidate.get("document_url"))) if candidate else None
        cards = _supporting_cards_for_item(item, evidence_cards, index=len(decisions),
                                           require_body=False)
        evidence_text = " ".join(str(value or "") for value in (
            item.get("summary"), item.get("why_it_matters"), item.get("evidence_excerpt"),
            candidate.get("economic_transmission") if candidate else None,
            *[card.get("supports") for card in cards],
        ))
        context_scope = bool(document and document.get("scope_label") in {
            "IN_SCOPE_GOVERNMENT", "IN_SCOPE_EXTERNAL_MEDIA", "IN_SCOPE_COUNTERPARTY"})
        explicit_low_context = item.get("importance") == "low" and item.get("earnings_relevance") == "context"
        uncertainty = bool(re.search(
            r"(?:unverified|not (?:verified|established|confirmed)|inferred|未確認|確認できない)",
            evidence_text, re.I))
        direct_linkage = (company_name.casefold() in evidence_text.casefold()
                          and bool(_POLICY_DIRECT.search(evidence_text))
                          and not uncertainty
                          and not bool(_POLICY_UNCERTAINTY.search(evidence_text)))
        reject = bool(context_scope and (explicit_low_context or uncertainty) and not direct_linkage)
        decisions.append({"headline": item.get("headline"), "source_url": item_url,
                          "context_scope": context_scope, "direct_company_linkage": direct_linkage,
                          "decision": "REJECT_GENERAL_CONTEXT" if reject else "KEEP"})
        if not reject:
            retained_items.append(item)
            continue
        if candidate is None or document is None:
            raise LightweightError("selected policy event lacks candidate/document provenance")
        candidate.update(selected_for_output=False, event_key=None,
                         selection_reason="Python materiality authority: general context lacks affirmative direct company linkage.")
        document["material"] = False
    payload["items"] = retained_items
    strategy["final_event_count"] = sum(row.get("selected_for_output") is True
                                        for row in strategy.get("candidate_provenance", []))
    return repaired, decisions


def apply_candidate_selection_authority(response: dict[str, Any]) -> dict[str, Any]:
    """Collapse one event's supporting candidates onto its unique primary source."""
    repaired = copy.deepcopy(response)
    strategy = repaired.get("strategy_telemetry")
    payload = repaired.get("research_result")
    if not isinstance(strategy, dict) or not isinstance(payload, dict):
        raise LightweightError("canonical candidate selection state is incomplete")
    candidates = strategy.get("candidate_provenance")
    if not isinstance(candidates, list):
        raise LightweightError("candidate provenance is missing")
    groups: dict[tuple[Any, str, Any], list[dict[str, Any]]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("selected_for_output") is not True:
            continue
        source_url = canonicalize_url(candidate.get("source_url"))
        key = (candidate.get("event_key"), source_url, candidate.get("headline"))
        groups.setdefault(key, []).append(candidate)
    for rows in groups.values():
        if len(rows) < 2:
            continue
        primary = [row for row in rows if canonicalize_url(row.get("document_url")) ==
                   canonicalize_url(row.get("source_url"))]
        if len(primary) != 1:
            raise LightweightError("duplicate selected candidates lack unique primary source")
        for row in rows:
            if row is primary[0]:
                continue
            row["selected_for_output"] = False
            row["selection_reason"] = (
                "Python candidate authority: retained as supporting context for the "
                "same event's unique primary-source candidate. " +
                str(row.get("selection_reason") or ""))
    strategy["final_event_count"] = sum(
        row.get("selected_for_output") is True for row in candidates if isinstance(row, dict))
    return repaired


def _card_has_authoritative_content(card: dict[str, Any]) -> bool:
    return (card.get("verification_status") == "VERIFIED"
            and card.get("source_type") not in INDEX_ONLY_SOURCE_TYPES
            and isinstance(card.get("excerpt"), str) and bool(card["excerpt"].strip()))


def _supporting_cards_for_item(item: dict[str, Any], evidence_cards: list[dict[str, Any]], *,
                               index: int, require_body: bool = True) -> list[dict[str, Any]]:
    """Resolve exact evidence IDs, with a strict unique-URL legacy fallback."""
    requested = item.get("supporting_evidence_ids")
    if requested is not None:
        if (not isinstance(requested, list) or not requested
                or any(not isinstance(value, str) or not value for value in requested)
                or len(set(requested)) != len(requested)):
            raise PublicationAuthorityError(
                f"items[{index}] supporting_evidence_ids must be a non-empty unique list")
        by_id: dict[str, dict[str, Any]] = {}
        for card in evidence_cards:
            if not isinstance(card, dict) or not isinstance(card.get("evidence_id"), str):
                continue
            evidence_id = card["evidence_id"]
            if evidence_id in by_id:
                raise PublicationAuthorityError(f"ambiguous evidence_id: {evidence_id}")
            by_id[evidence_id] = card
        unknown = [value for value in requested if value not in by_id]
        if unknown:
            raise PublicationAuthorityError(
                f"items[{index}] references unknown evidence_id: {unknown[0]}")
        selected = [by_id[value] for value in requested]
    else:
        item_url = canonicalize_url(item.get("source_url"))
        selected = [card for card in evidence_cards if isinstance(card, dict)
                    and canonicalize_url(card.get("url")) == item_url]
        if len(selected) != 1:
            raise PublicationAuthorityError(
                f"items[{index}] canonical URL fallback is not unique")
    if require_body and any(not _card_has_authoritative_content(card) for card in selected):
        raise PublicationAuthorityError(
            f"items[{index}] evidence does not satisfy authoritative content verification")
    item_url = canonicalize_url(item.get("source_url"))
    source_cards = [card for card in selected
                    if canonicalize_url(card.get("url")) == item_url]
    if len(source_cards) != 1:
        raise PublicationAuthorityError(
            f"items[{index}] has no unique source evidence for canonical URL")
    return selected


def _canonical_event_source_type(source_type: Any) -> str:
    value = str(source_type or "").casefold()
    if value in {"government"}:
        return "government"
    if value in {"research", "exchange"}:
        return value
    if value in OFFICIAL_EVIDENCE_SOURCE_TYPES:
        return "ir" if "ir" in value or "report" in value else "company"
    if value in {"external_media", "news", "media", "counterparty"}:
        return "news"
    return "other"


def restore_publication_from_evidence(response: dict[str, Any],
                                      evidence_cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Restore source/publication authority by evidence ID, never presentation title."""
    repaired = copy.deepcopy(response)
    payload = repaired.get("research_result")
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise LightweightError("canonical research items are missing")
    for index, item in enumerate(payload["items"]):
        matching = _supporting_cards_for_item(item, evidence_cards, index=index)
        source = next(card for card in matching
                      if canonicalize_url(card.get("url")) == canonicalize_url(item.get("source_url")))
        item["source_url"] = canonicalize_url(source["url"])
        item["source_type"] = _canonical_event_source_type(source.get("source_type"))
        item["evidence_excerpt"] = source["excerpt"]
        item.pop("supporting_evidence_ids", None)
        strategy = repaired.get("strategy_telemetry")
        if isinstance(strategy, dict) and "candidate_provenance" in strategy:
            provenance = [row for row in strategy.get("source_provenance", [])
                          if isinstance(row, dict)
                          and canonicalize_url(row.get("url")) == item["source_url"]]
            if len(provenance) != 1:
                raise PublicationAuthorityError(
                    f"items[{index}] has no unique canonical source provenance")
            provenance[0].update(
                url=item["source_url"], source_type=source.get("source_type"),
                content_verified=True, evidence_excerpt=source["excerpt"])
            candidates = [row for row in strategy.get("candidate_provenance", [])
                          if isinstance(row, dict) and row.get("selected_for_output") is True
                          and canonicalize_url(row.get("source_url")) == item["source_url"]]
            if len(candidates) != 1:
                raise PublicationAuthorityError(
                    f"items[{index}] has no unique selected candidate provenance")
            candidates[0]["source_url"] = item["source_url"]
        raw_values = {source.get("published_at")}
        if None in raw_values:
            raw_values.remove(None)
        if not raw_values:
            item.update(published_at=None, published_date=None, published_precision="unknown")
            continue
        instants: set[str] = set()
        date_only: set[str] = set()
        for value in raw_values:
            if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                date_only.add(value)
                continue
            instants.add(parse_datetime(value, f"items[{index}] evidence published_at"))
        if not instants:
            if len(date_only) != 1:
                raise LightweightError(f"items[{index}] has conflicting authoritative publication dates")
            item.update(published_at=None, published_date=next(iter(date_only)), published_precision="date")
            continue
        if len(instants) != 1:
            raise LightweightError(f"items[{index}] has conflicting authoritative publication instants")
        authoritative = next(iter(instants))
        if date_only and {value[:10] for value in instants} != date_only:
            raise LightweightError(f"items[{index}] publication date conflicts with authoritative instant")
        item.update(published_at=authoritative,
                    published_date=(next(iter(date_only)) if date_only else str(next(iter(raw_values))).strip()[:10]),
                    published_precision="datetime")
    return repaired


def apply_incremental_ir_authority(response: dict[str, Any],
                                   evidence_cards: list[dict[str, Any]],
                                   discovery: dict[str, Any]) -> dict[str, Any]:
    """Make EvidenceStore provenance authoritative for incremental-IR scope.

    A verified company index used only for coverage is Company Web provenance,
    not an incremental disclosure.  The aggregator may correct that metadata
    classification when discovery explicitly says incremental IR is not
    applicable.  It never converts a material document or invents delta text.
    """
    applicable = discovery.get("incremental_ir_applicable")
    if type(applicable) is not bool:
        raise LightweightError("incremental IR applicability authority is missing")
    repaired = copy.deepcopy(response)
    documents = repaired.get("strategy_telemetry", {}).get("documents")
    if not isinstance(documents, list):
        raise LightweightError("canonical documents are missing")
    for index, document in enumerate(documents):
        try:
            assert_incremental_ir_candidate_allowed(document)
        except OverlapScopeError as exc:
            raise LightweightError(str(exc)) from exc
        if document.get("scope_label") != "IN_SCOPE_INCREMENTAL_IR":
            continue
        if applicable:
            strategy_v4.scope_decision(document)
            continue
        if document.get("incremental_information") is not False:
            raise LightweightError(
                f"documents[{index}] conflicts with no-increment EvidenceStore authority")
        if document.get("material") is not False:
            raise LightweightError(
                f"documents[{index}] material scope cannot be reclassified without delta evidence")
        document_url = canonicalize_url(document.get("url"))
        matching = [card for card in evidence_cards if isinstance(card, dict)
                    and card.get("verification_status") == "VERIFIED"
                    and canonicalize_url(card.get("url")) == document_url]
        if not matching or any(card.get("source_type") not in {
                "official_index", "official_news", "official_report"} for card in matching):
            raise LightweightError(
                f"documents[{index}] lacks verified Company Web provenance")
        document["scope_label"] = "IN_SCOPE_COMPANY_WEB"
        document["scope_reason"] = (
            "Verified official Company Web source retained for coverage; "
            "EvidenceStore records no incremental IR delta.")
    return repaired


def restore_effective_precision(response: dict[str, Any]) -> dict[str, Any]:
    """Upgrade saved exact/date/null effective values without making a time."""
    repaired = copy.deepcopy(response)
    payload = repaired.get("research_result")
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise LightweightError("canonical research items are missing")
    for index, item in enumerate(payload["items"]):
        for boundary in ("valid_from", "valid_until"):
            date_key = f"{boundary}_date"
            precision_key = f"{boundary}_precision"
            if date_key in item or precision_key in item:
                news_monitor.normalize_effective_fields(item, boundary, allow_legacy=False)
                continue
            raw = item.get(boundary)
            if raw is None:
                item.update({boundary: None, date_key: None, precision_key: "unknown"})
            elif isinstance(raw, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                try:
                    effective_date = date.fromisoformat(raw).isoformat()
                except ValueError as exc:
                    raise LightweightError(
                        f"items[{index}] {boundary} is not a valid calendar date") from exc
                item.update({boundary: None, date_key: effective_date, precision_key: "date"})
            else:
                effective_at = parse_datetime(raw, f"items[{index}] {boundary}")
                item.update({boundary: effective_at, date_key: None, precision_key: "datetime"})
    return repaired


def restore_published_at_from_evidence(response: dict[str, Any],
                                       evidence_cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Backward-compatible name for precision-aware evidence restoration."""
    return restore_publication_from_evidence(response, evidence_cards)


def build_search_record_registry(
        worker_runs: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[dict[str, Any]]:
    """Build query provenance only from validated worker output with observed Web calls."""
    registry: list[dict[str, Any]] = []
    for output, telemetry in worker_runs:
        worker_id = telemetry.get("worker_id")
        if not isinstance(worker_id, str) or not worker_id or worker_id == "final-synthesis":
            raise LightweightError("search worker identity is missing")
        queries = output.get("queries")
        cards = output.get("evidence_cards")
        if not isinstance(queries, list) or not isinstance(cards, list):
            raise LightweightError(f"search worker output is incomplete: {worker_id}")
        executed = [query for query in queries if isinstance(query, dict) and query.get("executed") is True]
        web_calls = telemetry.get("web_call_count")
        if executed and (not isinstance(web_calls, int) or web_calls <= 0):
            raise LightweightError(f"executed query has no observed Web call: {worker_id}")
        for position, query in enumerate(queries, 1):
            if not isinstance(query, dict) or query.get("executed") is not True:
                continue
            if query.get("exposure_index") != telemetry.get("exposure_id"):
                raise LightweightError(f"query exposure does not match worker telemetry: {worker_id}")
            query_id = f"{worker_id}-q{position}"
            evidence_ids = [card.get("evidence_id") or card.get("source_id") for card in cards
                            if isinstance(card, dict) and card.get("query_id") == query_id
                            and isinstance(card.get("evidence_id") or card.get("source_id"), str)]
            registry.append({
                "query_id": query_id,
                "worker_id": worker_id,
                "exposure_id": telemetry.get("exposure_id"),
                "canonical_channel": query.get("channel"),
                "query_text": query.get("query"),
                "executed": True,
                "result_count": query.get("result_count"),
                "evidence_ids": evidence_ids,
                "reached_sources": copy.deepcopy(query.get("reached_sources")),
                "window_relevance_checked": query.get("window_relevance_checked"),
                "canonical_driver_phrase": query.get("canonical_driver_phrase"),
                "driver_phrase_preserved": query.get("driver_phrase_preserved"),
                "driver_phrase_preservation_reason": query.get("driver_phrase_preservation_reason"),
            })
    if len({row["query_id"] for row in registry}) != len(registry):
        raise LightweightError("duplicate search registry query_id")
    return registry


def apply_search_record_registry(response: dict[str, Any], registry: list[dict[str, Any]],
                                 discovery: dict[str, Any]) -> dict[str, Any]:
    """Project actual searches and channel coverage into the strict canonical contract."""
    repaired = copy.deepcopy(response)
    strategy = repaired.get("strategy_telemetry")
    if not isinstance(strategy, dict) or not isinstance(strategy.get("channel_reviews"), list):
        raise LightweightError("canonical search telemetry is incomplete")
    if any(row.get("executed") is not True for row in registry):
        raise LightweightError("search registry contains an unexecuted query")
    actual_channels = {row.get("canonical_channel") for row in registry}
    actual_links = {(row.get("canonical_channel"), row.get("exposure_id")) for row in registry}
    strategy["queries"] = [{
        "channel": row["canonical_channel"],
        "query": row["query_text"],
        "executed": True,
        "result_count": row["result_count"],
        "reached_sources": row["reached_sources"],
        "window_relevance_checked": row["window_relevance_checked"],
        "exposure_index": row["exposure_id"],
        "canonical_driver_phrase": row["canonical_driver_phrase"],
        "driver_phrase_preserved": row["driver_phrase_preserved"],
        "driver_phrase_preservation_reason": row["driver_phrase_preservation_reason"],
    } for row in registry]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for review in strategy["channel_reviews"]:
        if not isinstance(review, dict) or not isinstance(review.get("channel"), str):
            raise LightweightError("invalid channel review")
        grouped.setdefault(review["channel"], []).append(review)
    normalized = []
    for channel, reviews in grouped.items():
        statuses = [review.get("status") for review in reviews]
        supported_checked = [review for review in reviews if review.get("status") == "CHECKED"
                             and ((channel == "official" and channel in actual_channels)
                                  or (channel, review.get("exposure_index")) in actual_links)]
        if channel not in actual_channels:
            status = ("NOT_APPLICABLE" if channel == "incremental_ir"
                      and discovery.get("incremental_ir_applicable") is False else "PARTIAL")
        else:
            status = "CHECKED" if supported_checked else ("PARTIAL" if "PARTIAL" in statuses or "CHECKED" in statuses
                                                           else "NOT_APPLICABLE")
        selected = supported_checked[0] if status == "CHECKED" else next(
            (review for review in reviews if review.get("status") == status), reviews[0])
        evidence = " | ".join(dict.fromkeys(str(review.get("evidence", "")).strip()
                                              for review in reviews if str(review.get("evidence", "")).strip()))
        normalized.append({"channel": channel, "status": status, "evidence": evidence,
                           "exposure_index": selected.get("exposure_index")})
    strategy["channel_reviews"] = normalized
    return repaired


def derive_coverage_state(response: dict[str, Any], registry: list[dict[str, Any]],
                          discovery: dict[str, Any],
                          evidence_cards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Derive search and document coverage from recorded execution state."""
    strategy = response.get("strategy_telemetry")
    payload = response.get("research_result")
    if not isinstance(strategy, dict) or not isinstance(payload, dict):
        raise LightweightError("canonical response is incomplete")
    exposures = discovery.get("economic_exposures")
    if not isinstance(exposures, list) or not 2 <= len(exposures) <= 3:
        raise LightweightError("coverage applicability map is missing")
    if any(not isinstance(row, dict) or row.get("executed") is not True for row in registry):
        raise LightweightError("coverage authority requires executed SearchRecordRegistry rows")

    requirements: list[dict[str, Any]] = []

    def add_requirement(exposure_id: int | None, channel: str,
                        applicable: bool, required: bool) -> None:
        matches = [row for row in registry
                   if row.get("canonical_channel") == channel
                   and row.get("exposure_id") == exposure_id]
        planned = 1 if required else 0
        executed = len(matches)
        checked = bool(matches) and all(row.get("window_relevance_checked") is True for row in matches)
        satisfied = not required or (executed >= planned and checked)
        reason = None
        if not satisfied:
            reason = (f"Required {channel} query for exposure {exposure_id} has no executed "
                      "SearchRecordRegistry record." if not matches else
                      f"Required {channel} query for exposure {exposure_id} lacks window review.")
        requirements.append({
            "exposure_id": exposure_id, "channel": channel,
            "applicable": applicable, "required": required,
            "planned_query_count": planned, "executed_query_count": executed,
            "result_count": sum(row.get("result_count", 0) for row in matches),
            "evidence_card_count": len({evidence_id for row in matches
                                        for evidence_id in row.get("evidence_ids", [])}),
            "checked": checked, "gap": not satisfied, "gap_reason": reason,
        })

    add_requirement(None, "official", True, True)
    for index, exposure in enumerate(exposures):
        primary = exposure.get("channel") or "external_media"
        if primary not in {"external_media", "counterparty"}:
            raise LightweightError(f"exposure {index} has invalid complement applicability")
        for channel in ("external_media", "counterparty"):
            applicable = channel == primary
            add_requirement(index, channel, applicable, applicable)
        government = exposure.get("government_applicable")
        if type(government) is not bool:
            raise LightweightError(f"exposure {index} government applicability is missing")
        add_requirement(index, "government", government, government)
    incremental = discovery.get("incremental_ir_applicable")
    if type(incremental) is not bool:
        raise LightweightError("incremental IR applicability is missing")
    add_requirement(None, "incremental_ir", incremental, incremental)

    gaps = [{
        "gap_type": "query_execution", "url": None,
        "channel": row["channel"], "exposure_index": row["exposure_id"],
        "reason": row["gap_reason"], "retry_required": True,
    } for row in requirements if row["gap"]]
    cards = evidence_cards or []
    for index, document in enumerate(strategy.get("documents", [])):
        state = derive_document_provenance_state(document, cards) if cards else (
            "BODY_VERIFIED" if document.get("body_status") == "VERIFIED" else "ACCESS_FAILED")
        if (document.get("scope_label") in strategy_v4.REQUIRED_BODY_SCOPE
                and document.get("material") is True and state != "BODY_VERIFIED"):
            reason = document.get("attempt_evidence")
            if not isinstance(reason, str) or not reason.strip():
                raise LightweightError(
                    f"documents[{index}] unresolved material lacks attempt evidence")
            gaps.append({
                "gap_type": "document_access", "url": document.get("url"),
                "channel": None, "exposure_index": None,
                "reason": reason.strip(), "retry_required": True,
            })

    channels = {row["channel"] for row in requirements}
    reviews = []
    for channel in sorted(channels):
        rows = [row for row in requirements if row["channel"] == channel]
        applicable_rows = [row for row in rows if row["applicable"]]
        if not applicable_rows:
            status = "NOT_APPLICABLE"
        elif all(row["checked"] for row in applicable_rows):
            status = "CHECKED"
        else:
            status = "PARTIAL"
        exposure_ids = [row["exposure_id"] for row in applicable_rows
                        if row["exposure_id"] is not None]
        reviews.append({
            "channel": channel, "status": status,
            "evidence": (f"Python coverage authority: {sum(row['executed_query_count'] for row in rows)} "
                         f"executed SearchRecordRegistry record(s); "
                         f"{sum(row['evidence_card_count'] for row in rows)} linked evidence card(s)."),
            "exposure_index": exposure_ids[0] if exposure_ids else None,
        })

    exposure_coverage = []
    for index in range(len(exposures)):
        rows = [row for row in requirements if row["exposure_id"] == index and row["required"]]
        complete = rows and all(row["checked"] for row in rows)
        found = any(row["evidence_card_count"] for row in rows)
        status = "SEARCHED_FOUND" if complete and found else (
            "SEARCHED_NO_RESULT" if complete else "NOT_APPLICABLE")
        exposure_coverage.append({
            "exposure_index": index, "status": status,
            "evidence": (f"Python coverage authority: {sum(row['executed_query_count'] for row in rows)} "
                         f"of {sum(row['planned_query_count'] for row in rows)} required query record(s); "
                         f"{sum(row['evidence_card_count'] for row in rows)} linked evidence card(s)."),
        })

    checked_channels = {review["channel"] for review in reviews if review["status"] == "CHECKED"}
    no_news_gate = (not gaps and "official" in checked_channels
                    and len(checked_channels & {"external_media", "counterparty", "government"}) >= 2
                    and not payload.get("items"))
    return {
        "coverage_status": "PARTIAL" if gaps else "COMPLETE",
        "coverage_gaps": gaps, "coverage_matrix": requirements,
        "channel_reviews": reviews, "no_news_exposure_coverage": exposure_coverage,
        "no_news_gate_pass": no_news_gate,
        "query_budget_exceeded": len(registry) > 6,
        "search_provenance_status": "PASS",
        "executed_query_count": len(registry), "fake_gap_count": 0,
    }


def apply_coverage_authority(response: dict[str, Any], registry: list[dict[str, Any]],
                             discovery: dict[str, Any],
                             evidence_cards: list[dict[str, Any]] | None = None,
                             ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replace synthesis-derived coverage fields with Python-derived state."""
    repaired = copy.deepcopy(response)
    state = derive_coverage_state(repaired, registry, discovery, evidence_cards)
    strategy = repaired["strategy_telemetry"]
    for key in ("coverage_status", "coverage_gaps", "channel_reviews",
                "no_news_exposure_coverage", "no_news_gate_pass", "query_budget_exceeded"):
        strategy[key] = copy.deepcopy(state[key])
    return repaired, state


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
        card["evidence_id"] = store.evidence_id_for_url(card["url"])
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


def run_company(root: Path, experiment: Path, assignment: dict[str, Any], *,
                official_context: dict[str, Any] | None = None,
                usage_checkpoint: Callable[[dict[str, Any]], None] | None = None,
                registry_identity_authoritative: bool = False,
                ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Canonical isolated company execution path used by single and multi runners.

    ``official_context`` contains only deterministic, bounded output from the
    non-LLM official resolver.  Raw response bodies are never accepted here.
    """
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
    synthesis_prompt = synthesis_contract(
        assignment, discovery, queries, store, elapsed,
        registry_identity_authoritative=registry_identity_authoritative)
    search_registry = build_search_record_registry(list(zip(outputs, telemetry_rows)))
    response, synthesis_telemetry = _run_isolated_stage(
        root, company_dir, None, synthesis_prompt, synthesis_output_schema(), web_enabled=False)
    telemetry_rows.append(synthesis_telemetry)
    measured = aggregate_worker_telemetry(telemetry_rows)
    if official_context:
        measured["non_llm"] = copy.deepcopy(official_context.get("telemetry", {}))
        measured["position"] = assignment.get("position")
    _atomic_json(company_dir / "measured-token-telemetry.json", measured)
    _atomic_json(company_dir / "evidence-state.json", {
        "cards": store.cards(), "raw_body_retained": False,
        "cross_worker_payload": "bounded_evidence_cards_only",
        "document_provenance": [], "search_record_registry": search_registry,
    })
    if usage_checkpoint is not None:
        usage_checkpoint(copy.deepcopy(measured))
    response = inject_runtime_identity(response, assignment)
    response = inject_runtime_metadata(response, completed_at=synthesis_telemetry.get("completed_at"))
    if not registry_identity_authoritative:
        response = inject_identity_provenance(response, discovery, store.cards())
    response = apply_source_count_authority(response)
    response = apply_search_record_registry(response, search_registry, discovery)
    response = normalize_documents_from_evidence(response, store.cards(), assignment)
    response = apply_candidate_selection_authority(response)
    response, document_provenance = apply_document_provenance_authority(response, store.cards())
    response = apply_ir_nightly_overlap(response)
    response = apply_incremental_ir_authority(response, store.cards(), discovery)
    response, _ = apply_materiality_authority(
        response, store.cards(), company_name=assignment["company_name"])
    response, _ = apply_coverage_authority(response, search_registry, discovery, store.cards())
    response = restore_publication_from_evidence(response, store.cards())
    response = restore_effective_precision(response)
    _atomic_json(company_dir / "evidence-state.json", {
        "cards": store.cards(), "raw_body_retained": False,
        "cross_worker_payload": "bounded_evidence_cards_only",
        "document_provenance": document_provenance,
        "search_record_registry": search_registry,
    })
    return response, measured


# Historical public name retained for the Phase1 harness and callers.
codex_runner = run_company


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


def finalize(experiment: Path, run_token: str, response: dict[str, Any], telemetry: dict[str, Any], *,
             identity_registry=None, official_window_coverage=None) -> dict[str, Any]:
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
        validation_context = {}
        if identity_registry is not None:
            validation_context["identity_registry"] = identity_registry
        if official_window_coverage is not None:
            validation_context["official_window_coverage"] = official_window_coverage
        strategy_v5.validate(strategy, assignment, payload, **validation_context)
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
