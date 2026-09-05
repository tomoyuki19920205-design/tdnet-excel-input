"""Isolated, company-at-a-time runner for Company News Strategy v5.

The module deliberately has no production route.  It copies four immutable
assignments into a private experiment directory, launches one ephemeral model
context per assignment, and performs validation/finalization in Python.
"""
from __future__ import annotations

import argparse
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

from lib.news_monitor import canonicalize_url, validate_payload
from tools import company_news_strategy_v5 as strategy_v5


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

    @staticmethod
    def _query_key(query: str) -> str:
        return " ".join(query.casefold().split())

    def register_query(self, query_id: str, query: str) -> bool:
        key = self._query_key(query)
        if key in self._queries:
            return False
        self._queries[key] = query_id
        return True

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


def compact_contract(assignment: dict[str, Any]) -> str:
    """Runtime-only contract. It contains no control result or evaluation data."""
    assignment_json = json.dumps(assignment, ensure_ascii=False, separators=(",", ":"))
    return f"""You are researching exactly one Japanese listed company for a read-only benchmark.
Return only the JSON object required by the canonical output schema. Use web search/open tools; do not use shell, repo search, prior company context, Gold, or saved benchmark results. The schema, including enum spelling and required nested keys, is enforced by --output-schema.

ASSIGNMENT={assignment_json}

Apply Company News Strategy v5 without weakening research semantics:
- Fixed window {assignment['search_from']} through {assignment['search_to']}; NON_TDNET only.
- Verify ticker identity from an official company source. Review the bounded official news/press/IR index and all relevant in-window material candidates.
- Derive 2 or 3 economic exposures. Preserve every canonical_driver_phrase literally in at least one executed exposure query. External/counterparty queries need not contain the company name.
- Review concrete customer, supplier, product demand, pricing/cost, capacity/order and industry transmission. For regulation, financing, rate, tariff, subsidy, safety or procurement exposure, run exactly one focused government query.
- Incremental IR is eligible only for added pricing, volume, order, capacity, customer, margin, supply or capex information. Exclude TDNET-equivalent and trivial items. Continue the window after finding an event.
- no_news requires official coverage plus at least two exposure-appropriate complement channels and semantic coverage for every exposure.
- Every claimed source must be opened and content_verified. Keep snippets concise. Do not fabricate future facts; report research failure if the fixed future window cannot be verified.

Use checked_at with a timezone. earnings_relevance is only direct, likely, general, context, or unknown; explanations belong in why_it_matters. Query channels are only official, external_media, government, counterparty, or incremental_ir. Link every exposure query and regulatory theme to its exposure_index. Candidate entries must map selected payload headline/source URL. Documents must be in the fixed window; index-only no-news evidence may use the window end date. Python performs all validation; do not attempt a second-pass repair.
"""


def output_schema() -> dict[str, Any]:
    return strategy_v5.canonical_output_schema()


def codex_command(root: Path, schema_path: Path, result_path: Path, prompt: str) -> list[str]:
    """Expose native Web search while removing unrelated research tools."""
    disabled = ("shell_tool", "unified_exec", "apps", "plugins", "computer_use",
                "browser_use", "browser_use_external", "browser_use_full_cdp_access",
                "image_generation", "multi_agent")
    command = ["codex", "--search", "exec", "--ephemeral", "--ignore-user-config"]
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
    usage_node: Any = terminal if terminal else events
    input_tokens = _numbers(usage_node, {"input_tokens"})
    cached = _numbers(usage_node, {"cached_input_tokens", "cached_tokens"})
    output = _numbers(usage_node, {"output_tokens"})
    reasoning = _numbers(usage_node, {"reasoning_tokens", "reasoning_output_tokens"})
    completed_items = [event.get("item", {}) for event in events if event.get("type") == "item.completed"]
    tool_types = {"command_execution", "mcp_tool_call", "dynamic_tool_call", "tool_call", "web_search", "web_fetch"}
    tool_calls = sum(str(item.get("type", "")) in tool_types for item in completed_items)
    web_calls = sum("web" in str(item.get("type", "")).lower()
                    or re.search(r"\b(search_query|open)\b", json.dumps(item, ensure_ascii=False), re.I) is not None
                    for item in completed_items)
    model_calls = len(terminal) or sum(1 for event in events if event.get("type") in {"turn.started", "response.started"}) or 1
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
        "cached_input_tokens": min(cached, input_tokens),
        "uncached_input_tokens": max(0, input_tokens - cached),
        "output_tokens": output,
        "reasoning_tokens": reasoning,
        "total_tokens": input_tokens + output,
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


def _atomic_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(_redact_event(event), ensure_ascii=False, separators=(",", ":")) + "\n"
                   for event in events)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def codex_runner(root: Path, experiment: Path, assignment: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    company_dir = experiment / "companies" / f"{assignment['position']:02d}-{assignment['ticker']}"
    company_dir.mkdir(parents=True, exist_ok=True)
    schema_path, result_path = company_dir / "output-schema.json", company_dir / "last-message.json"
    _atomic_json(schema_path, output_schema())
    command = codex_command(root, schema_path, result_path, compact_contract(assignment))
    started = time.monotonic()
    completed = subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    elapsed = time.monotonic() - started
    event_lines = completed.stdout.splitlines()
    events: list[dict[str, Any]] = []
    for line in event_lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    _atomic_jsonl(company_dir / "raw-events.jsonl", events)
    _atomic_json(company_dir / "execution-summary.json", {
        "exit_code": completed.returncode,
        "stderr": bounded_text(completed.stderr, max_chars=8_000, max_lines=80),
        "event_types": [event.get("type") for event in events],
        "errors": [event for event in events if event.get("type") in {"error", "turn.failed"}],
    })
    measured = token_telemetry(events, elapsed)
    measured["canonical_schema_sha256"] = strategy_v5.canonical_schema_sha256()
    measured["validation_status"] = "PENDING" if completed.returncode == 0 and result_path.exists() else "RESEARCH_PROCESS_FAILED"
    _atomic_json(company_dir / "measured-token-telemetry.json", measured)
    if completed.returncode != 0 or not result_path.exists():
        raise LightweightError(f"Codex research failed for {assignment['ticker']}: exit={completed.returncode}")
    response = _read_json(result_path)
    store = EvidenceStore()
    strategy = response.get("strategy_telemetry", {})
    if isinstance(strategy, dict):
        for index, query in enumerate(strategy.get("queries", [])):
            if isinstance(query, dict) and isinstance(query.get("query"), str):
                store.register_query(f"q{index + 1}", query["query"])
        for source in strategy.get("source_provenance", []):
            if not isinstance(source, dict) or not isinstance(source.get("url"), str):
                continue
            excerpt = source.get("evidence_excerpt", "")
            store.add(url=source["url"], source_type=str(source.get("source_type", "other")),
                      title=str(source.get("title", source["url"])), published_at=source.get("published_at"),
                      body=excerpt, supports=excerpt, exposure_id=source.get("exposure_index"),
                      query_id=str(source.get("query_id", "unlinked")),
                      verification_status="VERIFIED" if source.get("content_verified") is True else "UNVERIFIED",
                      relevant_terms=[])
    _atomic_json(company_dir / "evidence-state.json", {"cards": store.cards(), "raw_body_retained": False})
    return response, measured


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
