#!/usr/bin/env python3
"""Isolated Company News batch-size benchmark harness.

This module never writes to the production Company News queue, inbox,
``decision_db.db``, or Supabase.  Runtime state is confined to
``data/news_work/batch_benchmark`` and results to
``data/news_benchmark_inbox``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.news_monitor import NewsValidationError, normalize_ticker, validate_payload
from tools.company_news_atomic import atomic_write_json, replace_with_retry

BENCHMARK_SCHEMA = "company_news_batch_benchmark_v1"
RUN_SCHEMA = "company_news_batch_benchmark_run_v1"
CONTROL_SCHEMA = "company_news_batch_benchmark_control_v1"
SNAPSHOT_SCHEMA = "company_news_benchmark_snapshot_v1"
COMPANY_TELEMETRY_SCHEMA = "company_news_benchmark_telemetry_v1"
TASK_TELEMETRY_SCHEMA = "company_news_benchmark_task_telemetry_v1"
FAILURE_SCHEMA = "company_news_benchmark_failure_v1"
AUDIT_SCHEMA = "company_news_batch_benchmark_audit_v1"
INTEGRATION_SCHEMA = "company_news_batch_integration_fixture_v1"

TASK_COUNT = 8
COHORT_SIZE = 192
REFERENCE_SIZE = 48
INITIAL_CANDIDATES = (4, 8, 12, 16)
EXPANSION_CANDIDATES = (20, 24)
ALLOWED_BATCH_SIZES = (1, *range(4, 25))
DEFAULT_SEARCH_FROM = date(2026, 8, 25)
DEFAULT_SEARCH_TO = date(2026, 8, 31)
_JST = timezone(timedelta(hours=9))
_TASK_RE = re.compile(r"^task(0[1-8])$")


class BenchmarkError(RuntimeError):
    pass


def _now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(_JST)
    if value.tzinfo is None:
        raise BenchmarkError("benchmark timestamps must include a timezone")
    return value.astimezone(_JST)


def _iso(now: datetime | None = None) -> str:
    return _now(now).isoformat(timespec="milliseconds")


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BenchmarkError(f"invalid ISO date: {value}") from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f"{path} must contain an object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    values: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BenchmarkError(f"invalid JSONL {path}:{number}: {exc}") from exc
        if not isinstance(value, dict):
            raise BenchmarkError(f"{path}:{number} must contain an object")
        values.append(value)
    return values


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _paths(root: Path) -> dict[str, Path]:
    root = root.resolve()
    work = root / "data" / "news_work" / "batch_benchmark"
    return {
        "root": root,
        "work": work,
        "active": work / "active.json",
        "outputs": root / "data" / "news_benchmark_inbox",
        "load": root / "data" / "news_benchmark_load",
        "production_prompt": root / "data" / "news_work",
        "benchmark_prompt": root / "data" / "news_work" / "SCHEDULED_TASK_BATCH_BENCHMARK_PROMPT.txt",
    }


def _task_id(value: str) -> str:
    if not _TASK_RE.fullmatch(value):
        raise BenchmarkError("task_id must be task01 through task08")
    return value


def _benchmark_dir(root: Path, benchmark_id: str) -> Path:
    if not re.fullmatch(r"batchbench-[A-Za-z0-9_-]+", benchmark_id):
        raise BenchmarkError("invalid benchmark_id")
    return _paths(root)["work"] / benchmark_id


def _run_dir(root: Path, benchmark_id: str, run_name: str) -> Path:
    match = re.fullmatch(r"batch(\d{2})", run_name)
    if run_name != "reference" and (not match or not 4 <= int(match.group(1)) <= 24):
        raise BenchmarkError("invalid benchmark run name")
    return _benchmark_dir(root, benchmark_id) / "runs" / run_name


def _normalize_master_ticker(value: Any) -> str | None:
    raw = str(value).strip().upper()
    if len(raw) == 5 and raw.endswith("0"):
        raw = raw[:4]
    try:
        return normalize_ticker(raw)
    except NewsValidationError:
        return None


def _history_buckets(news_db: Path) -> dict[str, str]:
    if not news_db.exists():
        return {}
    connection = sqlite3.connect(news_db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        scans: set[str] = set()
        events: set[str] = set()
        if "canonical_news_scan_runs" in tables:
            scans = {str(row[0]) for row in connection.execute("SELECT DISTINCT ticker FROM canonical_news_scan_runs")}
        if "canonical_news_events" in tables:
            events = {str(row[0]) for row in connection.execute("SELECT DISTINCT ticker FROM canonical_news_events")}
        return {ticker: ("news_present" if ticker in events else "no_news") for ticker in scans}
    finally:
        connection.close()


def select_cohort(master_db: Path, news_db: Path, count: int = COHORT_SIZE) -> list[dict[str, Any]]:
    """Select a deterministic sector/size/history-stratified company cohort."""
    if count < TASK_COUNT:
        raise BenchmarkError("cohort must contain at least eight companies")
    if not master_db.exists():
        raise BenchmarkError(f"company master does not exist: {master_db}")
    connection = sqlite3.connect(master_db.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        universe = list(
            connection.execute(
                "SELECT ticker,company_name,COALESCE(sector33_name,'unknown') AS sector,"
                "COALESCE(market_name,'unknown') AS market "
                "FROM market_data_universe WHERE date=(SELECT MAX(date) FROM market_data_universe) "
                "AND company_name IS NOT NULL AND TRIM(company_name)<>'' "
                "AND (is_ordinary_stock=1 OR is_common_stock=1) ORDER BY ticker"
            )
        )
        cap_by_ticker: dict[str, float] = {}
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_data'"
        ).fetchone():
            for row in connection.execute(
                "SELECT m.ticker,m.market_cap FROM market_data m JOIN "
                "(SELECT ticker,MAX(date) AS max_date FROM market_data "
                " WHERE market_cap IS NOT NULL AND market_cap>0 GROUP BY ticker) latest "
                "ON latest.ticker=m.ticker AND latest.max_date=m.date"
            ):
                ticker = _normalize_master_ticker(row[0])
                if ticker:
                    cap_by_ticker[ticker] = float(row[1])
    finally:
        connection.close()

    history = _history_buckets(news_db)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in universe:
        ticker = _normalize_master_ticker(row["ticker"])
        if not ticker or ticker in seen:
            continue
        candidates.append({
            "ticker": ticker,
            "company_name": str(row["company_name"]).strip(),
            "sector": str(row["sector"] or "unknown").strip() or "unknown",
            "market": str(row["market"] or "unknown").strip() or "unknown",
            "market_cap": cap_by_ticker.get(ticker),
            "prior_news_bucket": history.get(ticker, "unscanned"),
        })
        seen.add(ticker)
    if len(candidates) < count:
        raise BenchmarkError(f"company master provided only {len(candidates)} eligible companies")

    ranked = sorted((c for c in candidates if c["market_cap"]), key=lambda c: c["market_cap"])
    cap_rank = {company["ticker"]: index for index, company in enumerate(ranked)}
    for company in candidates:
        rank = cap_rank.get(company["ticker"])
        if rank is None:
            company["size_bucket"] = "unknown"
        elif rank < len(ranked) / 3:
            company["size_bucket"] = "small"
        elif rank < len(ranked) * 2 / 3:
            company["size_bucket"] = "mid"
        else:
            company["size_bucket"] = "large"

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for company in candidates:
        groups[(company["sector"], company["size_bucket"], company["prior_news_bucket"])].append(company)
    for key, values in groups.items():
        values.sort(key=lambda c: hashlib.sha256(f"batch-benchmark-v1:{key}:{c['ticker']}".encode()).hexdigest())
    keys = sorted(groups, key=lambda key: hashlib.sha256(f"batch-benchmark-key:{key}".encode()).hexdigest())
    selected: list[dict[str, Any]] = []
    index = 0
    while len(selected) < count:
        added = False
        for key in keys:
            if index < len(groups[key]):
                selected.append(groups[key][index])
                added = True
                if len(selected) == count:
                    break
        if not added:
            break
        index += 1
    return selected


def _stratified_subset(companies: list[dict[str, Any]], count: int) -> list[str]:
    ordered = sorted(
        companies,
        key=lambda c: (
            c.get("size_bucket", "unknown"),
            c.get("prior_news_bucket", "unscanned"),
            c.get("sector", "unknown"),
            hashlib.sha256(f"batch-reference-v1:{c['ticker']}".encode()).hexdigest(),
        ),
    )
    # Evenly spaced deterministic picks retain all available strata better than a prefix.
    indexes = [math.floor(index * len(ordered) / count) for index in range(count)]
    return [ordered[index]["ticker"] for index in indexes]


def prepare_benchmark(
    root: Path,
    master_db: Path,
    news_db: Path,
    *,
    companies: int = COHORT_SIZE,
    reference_size: int = REFERENCE_SIZE,
    search_from: date = DEFAULT_SEARCH_FROM,
    search_to: date = DEFAULT_SEARCH_TO,
    now: datetime | None = None,
) -> dict[str, Any]:
    if search_to < search_from:
        raise BenchmarkError("search_to precedes search_from")
    if reference_size < 1 or reference_size > companies:
        raise BenchmarkError("reference_size must be between 1 and cohort size")
    timestamp = _now(now)
    benchmark_id = f"batchbench-{timestamp:%Y%m%dT%H%M%S%f}"
    directory = _benchmark_dir(root, benchmark_id)
    if directory.exists():
        raise BenchmarkError(f"benchmark already exists: {benchmark_id}")
    cohort = select_cohort(master_db, news_db, companies)
    reference = _stratified_subset(cohort, reference_size)
    value = {
        "schema_version": BENCHMARK_SCHEMA,
        "benchmark_mode": True,
        "benchmark_id": benchmark_id,
        "status": "prepared",
        "created_at": _iso(timestamp),
        "task_count": TASK_COUNT,
        "cohort_size": len(cohort),
        "reference_size": len(reference),
        "search_from": search_from.isoformat(),
        "search_to": search_to.isoformat(),
        "initial_candidates": list(INITIAL_CANDIDATES),
        "conditional_candidates": list(EXPANSION_CANDIDATES),
        "hard_ceiling": 24,
        "selection_contract": "deterministic_sector_market_cap_tercile_prior_news_stratified_v1",
        "reference_tickers": reference,
        "companies": cohort,
    }
    atomic_write_json(directory / "cohort.json", value)
    return value


def _load_cohort(root: Path, benchmark_id: str) -> dict[str, Any]:
    value = _read_json(_benchmark_dir(root, benchmark_id) / "cohort.json")
    if value.get("schema_version") != BENCHMARK_SCHEMA or value.get("benchmark_mode") is not True:
        raise BenchmarkError("invalid benchmark cohort contract")
    return value


def prepare_run(
    root: Path,
    benchmark_id: str,
    batch_size: int,
    *,
    reference: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    if batch_size not in ALLOWED_BATCH_SIZES or (reference and batch_size != 1) or (not reference and batch_size == 1):
        raise BenchmarkError("candidate batch must be 4 through 24; reference batch must be 1")
    cohort = _load_cohort(root, benchmark_id)
    run_name = "reference" if reference else f"batch{batch_size:02d}"
    directory = _run_dir(root, benchmark_id, run_name)
    if directory.exists():
        raise BenchmarkError(f"benchmark run already exists: {run_name}")
    selected = cohort["companies"]
    if reference:
        wanted = set(cohort["reference_tickers"])
        selected = [company for company in selected if company["ticker"] in wanted]
    output_directory = _paths(root)["outputs"] / benchmark_id / run_name
    assignments_by_task: dict[str, list[dict[str, Any]]] = {f"task{i:02d}": [] for i in range(1, 9)}
    for index, company in enumerate(selected):
        task_id = f"task{index % TASK_COUNT + 1:02d}"
        task_queue_position = len(assignments_by_task[task_id]) + 1
        assignment_id = f"{benchmark_id}-{run_name}-{task_id}-{task_queue_position:03d}-{company['ticker']}"
        assignments_by_task[task_id].append({
            "assignment_id": assignment_id,
            "ticker": company["ticker"],
            "company_name": company["company_name"],
            "sector": company.get("sector", "unknown"),
            "size_bucket": company.get("size_bucket", "unknown"),
            "prior_news_bucket": company.get("prior_news_bucket", "unscanned"),
            "task_id": task_id,
            "task_queue_position": task_queue_position,
            "search_from": cohort["search_from"],
            "search_to": cohort["search_to"],
            "status": "pending",
        })
    manifest = {
        "schema_version": RUN_SCHEMA,
        "benchmark_mode": True,
        "benchmark_id": benchmark_id,
        "run_name": run_name,
        "reference": reference,
        "status": "prepared",
        "created_at": _iso(now),
        "task_count": TASK_COUNT,
        "batch_size": batch_size,
        "company_count": len(selected),
        "search_from": cohort["search_from"],
        "search_to": cohort["search_to"],
        "output_directory": str(output_directory.resolve()),
        "production_write_forbidden": True,
    }
    atomic_write_json(directory / "manifest.json", manifest)
    for task_id, assignments in assignments_by_task.items():
        atomic_write_json(directory / "tasks" / task_id / "queue.json", {
            "schema_version": RUN_SCHEMA,
            "benchmark_id": benchmark_id,
            "run_name": run_name,
            "task_id": task_id,
            "batch_size": batch_size,
            "assignments": assignments,
        })
    output_directory.mkdir(parents=True, exist_ok=True)
    return manifest


def _validate_manifest(root: Path, value: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    required = ("benchmark_id", "run_name", "output_directory")
    if value.get("schema_version") != CONTROL_SCHEMA or value.get("benchmark_mode") is not True:
        raise BenchmarkError("active benchmark control is invalid; fail closed")
    if any(not isinstance(value.get(key), str) or not value[key] for key in required):
        raise BenchmarkError("active benchmark control is incomplete; fail closed")
    directory = _run_dir(root, value["benchmark_id"], value["run_name"])
    manifest = _read_json(directory / "manifest.json")
    if manifest.get("schema_version") != RUN_SCHEMA or manifest.get("benchmark_mode") is not True:
        raise BenchmarkError("benchmark manifest is invalid; fail closed")
    if manifest.get("benchmark_id") != value["benchmark_id"] or manifest.get("run_name") != value["run_name"]:
        raise BenchmarkError("benchmark control/manifest identity mismatch; fail closed")
    expected_output = (_paths(root)["outputs"] / value["benchmark_id"] / value["run_name"]).resolve()
    actual_output = Path(value["output_directory"]).resolve()
    if actual_output != expected_output or not _inside(actual_output, _paths(root)["outputs"]):
        raise BenchmarkError("benchmark output_directory is not isolated; fail closed")
    if Path(manifest["output_directory"]).resolve() != expected_output:
        raise BenchmarkError("benchmark manifest output_directory mismatch; fail closed")
    return directory, manifest


def activate_run(root: Path, benchmark_id: str, run_name: str, *, now: datetime | None = None) -> dict[str, Any]:
    paths = _paths(root)
    if paths["active"].exists():
        raise BenchmarkError("another benchmark run is already active")
    directory = _run_dir(root, benchmark_id, run_name)
    manifest = _read_json(directory / "manifest.json")
    if manifest.get("status") != "prepared" or manifest.get("benchmark_mode") is not True:
        raise BenchmarkError("only a prepared isolated benchmark run can be activated")
    control = {
        "schema_version": CONTROL_SCHEMA,
        "benchmark_mode": True,
        "benchmark_id": benchmark_id,
        "run_name": run_name,
        "output_directory": manifest["output_directory"],
        "activated_at": _iso(now),
    }
    _validate_manifest(root, control)
    atomic_write_json(paths["active"], control)
    manifest["status"] = "active"
    manifest["activated_at"] = control["activated_at"]
    atomic_write_json(directory / "manifest.json", manifest)
    return control


def deactivate_run(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    active = _paths(root)["active"]
    if not active.exists():
        return {"status": "already_inactive"}
    control = _read_json(active)
    directory, manifest = _validate_manifest(root, control)
    status = benchmark_status(root, control["benchmark_id"], control["run_name"])
    manifest["status"] = "completed" if status["pending"] == 0 else "paused"
    manifest["deactivated_at"] = _iso(now)
    atomic_write_json(directory / "manifest.json", manifest)
    active.unlink()
    return {"status": manifest["status"], "benchmark_id": control["benchmark_id"], "run_name": control["run_name"]}


def route_task(root: Path, task_id: str) -> dict[str, Any]:
    task_id = _task_id(task_id)
    paths = _paths(root)
    if not paths["active"].exists():
        return {
            "mode": "production",
            "task_id": task_id,
            "prompt_file": str((paths["production_prompt"] / f"SCHEDULED_TASK_{task_id.upper()}_PROMPT.txt").resolve()),
            "reason": "no active benchmark control",
        }
    control = _read_json(paths["active"])
    _, manifest = _validate_manifest(root, control)
    if manifest.get("status") != "active":
        raise BenchmarkError("benchmark control points to a non-active run; fail closed")
    return {
        "mode": "benchmark",
        "task_id": task_id,
        "prompt_file": str(paths["benchmark_prompt"].resolve()),
        "benchmark_id": control["benchmark_id"],
        "run_name": control["run_name"],
        "batch_size": manifest["batch_size"],
        "output_directory": control["output_directory"],
    }


def _active_run(root: Path) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    active = _paths(root)["active"]
    if not active.exists():
        raise BenchmarkError("no active benchmark run")
    control = _read_json(active)
    directory, manifest = _validate_manifest(root, control)
    if manifest.get("status") != "active":
        raise BenchmarkError("benchmark is not active")
    return control, directory, manifest


def _task_directory(run_directory: Path, task_id: str) -> Path:
    return run_directory / "tasks" / _task_id(task_id)


def _task_queue(run_directory: Path, task_id: str) -> dict[str, Any]:
    return _read_json(_task_directory(run_directory, task_id) / "queue.json")


def _task_event(task_directory: Path, event: str, now: datetime, **details: Any) -> None:
    _append_jsonl(task_directory / "events.jsonl", {"at": _iso(now), "event": event, **details})


def snapshot_task(
    root: Path,
    task_id: str,
    *,
    now: datetime | None = None,
    guard_ttl: timedelta = timedelta(hours=2),
) -> dict[str, Any]:
    timestamp = _now(now)
    control, run_directory, manifest = _active_run(root)
    task_directory = _task_directory(run_directory, task_id)
    guard = task_directory / "active.json"
    task_directory.mkdir(parents=True, exist_ok=True)
    if guard.exists():
        existing = _read_json(guard)
        try:
            expires = datetime.fromisoformat(str(existing["expires_at"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError) as exc:
            raise BenchmarkError("invalid benchmark task guard; fail closed") from exc
        if expires > timestamp:
            _task_event(task_directory, "busy_skip", timestamp, benchmark_id=control["benchmark_id"], run_id=existing.get("run_id"))
            return {"status": "busy", "task_id": task_id, "assignments": [], "expires_at": existing["expires_at"]}
        history = task_directory / "history"
        history.mkdir(parents=True, exist_ok=True)
        replace_with_retry(guard, history / f"{existing.get('run_id', 'unknown')}.stale.json")
        _task_event(task_directory, "stale_guard_recovered", timestamp, benchmark_id=control["benchmark_id"], run_id=existing.get("run_id"))

    queue = _task_queue(run_directory, task_id)
    pending = [assignment for assignment in queue["assignments"] if assignment.get("status") == "pending"]
    selected = pending[: int(manifest["batch_size"])]
    token = hashlib.sha256(os.urandom(32)).hexdigest()
    run_id = f"{control['benchmark_id']}-{control['run_name']}-{task_id}-{timestamp:%Y%m%dT%H%M%S%f}"
    output_directory = Path(control["output_directory"])
    assignments = []
    for position, assignment in enumerate(selected, start=1):
        filename = f"benchmark_{task_id}_{assignment['assignment_id']}.json"
        assignments.append({
            **assignment,
            "slot_id": f"bslot{(int(task_id[-2:]) - 1) * int(manifest['batch_size']) + position:03d}",
            "company_position": position,
            "output_path": str((output_directory / filename).resolve()),
            "temp_path": str((output_directory / f"{filename}.tmp").resolve()),
        })
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA,
        "benchmark_mode": True,
        "benchmark_id": control["benchmark_id"],
        "run_name": control["run_name"],
        "task_id": task_id,
        "run_id": run_id,
        "run_token": token,
        "batch_size": manifest["batch_size"],
        "task_started_at": _iso(timestamp),
        "expires_at": _iso(timestamp + guard_ttl),
        "assignments": assignments,
        "company_state": {},
    }
    encoded = (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode()
    descriptor: int | None = None
    try:
        descriptor = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(descriptor, encoded)
    except FileExistsError as exc:
        raise BenchmarkError("benchmark task guard was acquired concurrently") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return {"status": "snapshot_created", **snapshot}


def _owned_guard(root: Path, task_id: str, run_token: str) -> tuple[Path, dict[str, Any]]:
    _, run_directory, _ = _active_run(root)
    path = _task_directory(run_directory, task_id) / "active.json"
    value = _read_json(path)
    if value.get("run_token") != run_token:
        raise BenchmarkError("run_token does not own benchmark task guard")
    return path, value


def _assignment(snapshot: dict[str, Any], assignment_id: str) -> dict[str, Any]:
    matches = [value for value in snapshot["assignments"] if value.get("assignment_id") == assignment_id]
    if len(matches) != 1:
        raise BenchmarkError("assignment is not in immutable task snapshot")
    return matches[0]


def research_start(
    root: Path, task_id: str, run_token: str, assignment_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    path, snapshot = _owned_guard(root, task_id, run_token)
    assignment = _assignment(snapshot, assignment_id)
    state = snapshot.setdefault("company_state", {})
    if assignment_id in state:
        raise BenchmarkError("company research timing already started")
    state[assignment_id] = {"research_started_at": _iso(now)}
    atomic_write_json(path, snapshot)
    return {"status": "research_started", "assignment_id": assignment_id, **state[assignment_id], "company_position": assignment["company_position"]}


def research_finish(
    root: Path, task_id: str, run_token: str, assignment_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    path, snapshot = _owned_guard(root, task_id, run_token)
    _assignment(snapshot, assignment_id)
    state = snapshot.setdefault("company_state", {}).get(assignment_id)
    if not state or "research_started_at" not in state:
        raise BenchmarkError("company research was not started")
    if "research_finished_at" in state:
        raise BenchmarkError("company research timing already finished")
    finished = _now(now)
    started = datetime.fromisoformat(state["research_started_at"])
    state["research_finished_at"] = _iso(finished)
    state["research_elapsed_seconds"] = (finished - started).total_seconds()
    atomic_write_json(path, snapshot)
    return {"status": "research_finished", "assignment_id": assignment_id, **state}


def _validate_failure(payload: dict[str, Any], assignment: dict[str, Any], snapshot: dict[str, Any]) -> tuple[int, int, str]:
    required = ("task_id", "slot_id", "assignment_id", "ticker", "company_name", "error_type", "error_message", "sources_attempted", "created_at")
    if payload.get("schema_version") != FAILURE_SCHEMA or any(key not in payload for key in required):
        raise BenchmarkError("invalid benchmark failure sidecar")
    if payload["task_id"] != snapshot["task_id"] or payload["slot_id"] != assignment["slot_id"]:
        raise BenchmarkError("benchmark failure task/slot mismatch")
    if payload["assignment_id"] != assignment["assignment_id"] or payload["ticker"] != assignment["ticker"]:
        raise BenchmarkError("benchmark failure assignment mismatch")
    if payload["company_name"] != assignment["company_name"]:
        raise BenchmarkError("benchmark failure company_name mismatch")
    if any(not isinstance(payload[key], str) or not payload[key].strip() for key in ("error_type", "error_message")):
        raise BenchmarkError("benchmark failure error_type/error_message must be non-empty strings")
    if not isinstance(payload["sources_attempted"], list) or any(
        not isinstance(source, str) or not source.strip() for source in payload["sources_attempted"]
    ):
        raise BenchmarkError("benchmark failure sources_attempted must be an array")
    datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
    return len(payload["sources_attempted"]), 0, "failure"


def _validate_success(payload: dict[str, Any], assignment: dict[str, Any]) -> tuple[int, int, str]:
    run = validate_payload(payload)
    if payload.get("run_id") != assignment["assignment_id"] or payload.get("task_id") != assignment["assignment_id"]:
        raise BenchmarkError("benchmark payload run_id/task_id mismatch")
    if payload.get("ticker") != assignment["ticker"]:
        raise BenchmarkError("benchmark payload ticker mismatch")
    search_from = _parse_date(assignment["search_from"])
    search_to = _parse_date(assignment["search_to"])
    for event in run.events:
        published = datetime.fromisoformat(event["published_at"]).date()
        if not search_from <= published <= search_to:
            raise BenchmarkError("benchmark event falls outside fixed search window")
    source_count = payload.get("sources_checked_count")
    if not isinstance(source_count, int) or source_count < 0:
        raise BenchmarkError("benchmark payload requires non-negative sources_checked_count")
    return source_count, len(run.events), "news_present" if run.events else "no_news"


def finalize_company(
    root: Path,
    task_id: str,
    run_token: str,
    assignment_id: str,
    *,
    failure: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    path, snapshot = _owned_guard(root, task_id, run_token)
    assignment = _assignment(snapshot, assignment_id)
    state = snapshot.setdefault("company_state", {}).get(assignment_id)
    if not state or "research_finished_at" not in state:
        raise BenchmarkError("research-finish is required before finalization")
    if state.get("finalized"):
        raise BenchmarkError("company result is already finalized")
    temp_path = Path(assignment["temp_path"])
    output_path = Path(assignment["output_path"])
    isolated_root = _paths(root)["outputs"]
    if not _inside(temp_path, isolated_root) or not _inside(output_path, isolated_root):
        raise BenchmarkError("benchmark result path is not isolated")
    if output_path.exists():
        raise BenchmarkError("benchmark final result already exists; refusing overwrite")
    payload = _read_json(temp_path)
    validated_at = _now(now)
    try:
        source_count, event_count, result_type = (
            _validate_failure(payload, assignment, snapshot) if failure else _validate_success(payload, assignment)
        )
    except (NewsValidationError, ValueError) as exc:
        raise BenchmarkError(f"benchmark result validation failed: {exc}") from exc
    replace_with_retry(temp_path, output_path)
    saved_at = _now(now) if now is not None else _now()
    started = datetime.fromisoformat(state["research_started_at"])
    research_finished = datetime.fromisoformat(state["research_finished_at"])
    telemetry = {
        "schema_version": COMPANY_TELEMETRY_SCHEMA,
        "benchmark_id": snapshot["benchmark_id"],
        "run_name": snapshot["run_name"],
        "run_id": snapshot["run_id"],
        "task_id": snapshot["task_id"],
        "slot_id": assignment["slot_id"],
        "assignment_id": assignment_id,
        "ticker": assignment["ticker"],
        "company_name": assignment["company_name"],
        "company_position": assignment["company_position"],
        "research_started_at": state["research_started_at"],
        "research_finished_at": state["research_finished_at"],
        "research_elapsed_seconds": state["research_elapsed_seconds"],
        "validation_finished_at": _iso(validated_at),
        "output_saved_at": _iso(saved_at),
        "save_elapsed_seconds": (saved_at - validated_at).total_seconds(),
        "company_total_elapsed_seconds": (saved_at - started).total_seconds(),
        "post_research_elapsed_seconds": (saved_at - research_finished).total_seconds(),
        "source_count": source_count,
        "event_count": event_count,
        "result_type": result_type,
        "output_path": str(output_path.resolve()),
    }
    _, run_directory, _ = _active_run(root)
    _append_jsonl(_task_directory(run_directory, task_id) / "companies.jsonl", telemetry)
    state.update({"finalized": True, "result_type": result_type, "output_saved_at": telemetry["output_saved_at"]})
    atomic_write_json(path, snapshot)
    return {"status": "finalized", **telemetry}


def release_task(
    root: Path, task_id: str, run_token: str, *, now: datetime | None = None
) -> dict[str, Any]:
    path, snapshot = _owned_guard(root, task_id, run_token)
    states = snapshot.get("company_state", {})
    missing = [a["assignment_id"] for a in snapshot["assignments"] if not states.get(a["assignment_id"], {}).get("finalized")]
    if missing:
        raise BenchmarkError(f"cannot release task; unfinished assignments: {', '.join(missing)}")
    finished = _now(now)
    started = datetime.fromisoformat(snapshot["task_started_at"])
    _, run_directory, manifest = _active_run(root)
    task_directory = _task_directory(run_directory, task_id)
    company_records = [
        record for record in _read_jsonl(task_directory / "companies.jsonl")
        if record.get("run_id") == snapshot["run_id"]
    ]
    record = {
        "schema_version": TASK_TELEMETRY_SCHEMA,
        "benchmark_id": snapshot["benchmark_id"],
        "run_name": snapshot["run_name"],
        "run_id": snapshot["run_id"],
        "task_id": task_id,
        "batch_size": snapshot["batch_size"],
        "snapshot_company_count": len(snapshot["assignments"]),
        "task_started_at": snapshot["task_started_at"],
        "first_company_started_at": min((r["research_started_at"] for r in company_records), default=None),
        "last_company_finished_at": max((r["output_saved_at"] for r in company_records), default=None),
        "task_finished_at": _iso(finished),
        "task_elapsed_seconds": (finished - started).total_seconds(),
        "research_elapsed_sum": sum(float(r["research_elapsed_seconds"]) for r in company_records),
        "save_elapsed_sum": sum(float(r["save_elapsed_seconds"]) for r in company_records),
        "success_count": sum(r["result_type"] != "failure" for r in company_records),
        "failure_count": sum(r["result_type"] == "failure" for r in company_records),
    }
    _append_jsonl(task_directory / "runs.jsonl", record)
    queue_path = task_directory / "queue.json"
    queue = _read_json(queue_path)
    results = {record["assignment_id"]: record["result_type"] for record in company_records}
    for assignment in queue["assignments"]:
        if assignment["assignment_id"] in results:
            assignment["status"] = results[assignment["assignment_id"]]
    atomic_write_json(queue_path, queue)
    history = task_directory / "history"
    history.mkdir(parents=True, exist_ok=True)
    history_name = f"completed-{hashlib.sha256(snapshot['run_id'].encode()).hexdigest()[:16]}.json"
    history_path = history / history_name
    if history_path.exists():
        raise BenchmarkError("completed benchmark task history already exists")
    replace_with_retry(path, history_path)
    return {
        "status": "released",
        **record,
        "remaining": sum(a["status"] == "pending" for a in queue["assignments"]),
        "candidate_batch_size": manifest["batch_size"],
        "history_path": str(history_path.resolve()),
    }


def benchmark_status(root: Path, benchmark_id: str, run_name: str) -> dict[str, Any]:
    directory = _run_dir(root, benchmark_id, run_name)
    manifest = _read_json(directory / "manifest.json")
    counts: dict[str, int] = defaultdict(int)
    total = 0
    for task_index in range(1, 9):
        queue = _task_queue(directory, f"task{task_index:02d}")
        for assignment in queue["assignments"]:
            counts[str(assignment["status"])] += 1
            total += 1
    return {
        "benchmark_id": benchmark_id,
        "run_name": run_name,
        "status": manifest["status"],
        "batch_size": manifest["batch_size"],
        "total": total,
        "pending": counts["pending"],
        "news_present": counts["news_present"],
        "no_news": counts["no_news"],
        "failed": counts["failure"],
        "completed": total - counts["pending"],
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _summary(values: list[float]) -> dict[str, float | None]:
    return {
        "average": statistics.mean(values) if values else None,
        "p50": statistics.median(values) if values else None,
        "p95": _percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def _normalized_url(value: str) -> str:
    parts = urlsplit(value)
    path = (parts.path or "/").rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _text_tokens(value: str) -> set[str]:
    compact = re.sub(r"[^0-9A-Za-z\u3040-\u30ff\u3400-\u9fff]+", "", value.casefold())
    tokens = {compact[index:index + 2] for index in range(max(0, len(compact) - 1))}
    tokens.update(re.findall(r"[a-z0-9]+", value.casefold()))
    return tokens


def _jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left or right else 1.0


def _event_match(reference: dict[str, Any], candidate: dict[str, Any]) -> tuple[bool, str, float]:
    if reference["ticker"] != candidate["ticker"]:
        return False, "ticker_mismatch", 0.0
    if _normalized_url(reference["source_url"]) == _normalized_url(candidate["source_url"]):
        return True, "normalized_url", 1.0
    ref_date = datetime.fromisoformat(reference["published_at"]).date()
    candidate_date = datetime.fromisoformat(candidate["published_at"]).date()
    if ref_date != candidate_date or reference["category"] != candidate["category"]:
        return False, "date_or_category_mismatch", 0.0
    headline = _jaccard(_text_tokens(reference["headline"]), _text_tokens(candidate["headline"]))
    substance = _jaccard(
        _text_tokens(reference["headline"] + " " + reference.get("summary", "")),
        _text_tokens(candidate["headline"] + " " + candidate.get("summary", "")),
    )
    score = max(headline, substance)
    return (score >= 0.55), "semantic_substance", score


def _load_success_outputs(directory: Path) -> tuple[dict[str, list[dict[str, Any]]], int, int, list[str]]:
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    valid = 0
    invalid = 0
    errors: list[str] = []
    for path in sorted(Path(directory).glob("benchmark_task??_*.json")):
        payload = _read_json(path)
        if payload.get("schema_version") == FAILURE_SCHEMA:
            continue
        try:
            run = validate_payload(payload)
            valid += 1
            events[payload["ticker"]].extend(run.events)
        except (NewsValidationError, KeyError, ValueError) as exc:
            invalid += 1
            errors.append(f"{path.name}: {exc}")
    return events, valid, invalid, errors


def _telemetry(directory: Path, filename: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for task in sorted((directory / "tasks").glob("task??")):
        values.extend(_read_jsonl(task / filename))
    return values


def _quality_metrics(
    reference_events: dict[str, list[dict[str, Any]]],
    candidate_events: dict[str, list[dict[str, Any]]],
    reference_tickers: set[str],
) -> tuple[dict[str, Any], dict[str, float], list[dict[str, Any]]]:
    matched_reference = 0
    matched_candidate: set[tuple[str, int]] = set()
    critical_total = 0
    critical_matched = 0
    ambiguous: list[dict[str, Any]] = []
    company_recall: dict[str, float] = {}
    for ticker in sorted(reference_tickers):
        refs = reference_events.get(ticker, [])
        candidates = candidate_events.get(ticker, [])
        company_matches = 0
        for reference_index, reference in enumerate(refs):
            options = []
            for candidate_index, candidate in enumerate(candidates):
                if (ticker, candidate_index) in matched_candidate:
                    continue
                matched, method, score = _event_match(reference, candidate)
                if matched:
                    options.append((score, method, candidate_index))
            is_critical = reference.get("importance") == "high" or reference.get("earnings_relevance") == "direct"
            critical_total += int(is_critical)
            if options:
                options.sort(reverse=True)
                best = options[0]
                matched_reference += 1
                company_matches += 1
                matched_candidate.add((ticker, best[2]))
                critical_matched += int(is_critical)
                if len(options) > 1 and options[0][0] == options[1][0]:
                    ambiguous.append({"ticker": ticker, "reference_index": reference_index, "candidate_indexes": [value[2] for value in options], "score": best[0]})
        company_recall[ticker] = company_matches / len(refs) if refs else 1.0
    total_reference = sum(len(reference_events.get(ticker, [])) for ticker in reference_tickers)
    total_candidate = sum(len(candidate_events.get(ticker, [])) for ticker in reference_tickers)
    news_reference = [ticker for ticker in reference_tickers if reference_events.get(ticker)]
    no_news_false_negatives = [ticker for ticker in news_reference if not candidate_events.get(ticker)]
    quality = {
        "reference_event_count": total_reference,
        "candidate_event_count": total_candidate,
        "matched_event_count": matched_reference,
        "event_recall": matched_reference / total_reference if total_reference else 1.0,
        "critical_event_count": critical_total,
        "critical_event_recall": critical_matched / critical_total if critical_total else 1.0,
        "precision": len(matched_candidate) / total_candidate if total_candidate else (1.0 if total_reference == 0 else 0.0),
        "no_news_false_negative_count": len(no_news_false_negatives),
        "no_news_false_negative_rate": len(no_news_false_negatives) / len(news_reference) if news_reference else 0.0,
        "no_news_false_negative_tickers": no_news_false_negatives,
        "ambiguous_match_count": len(ambiguous),
    }
    return quality, company_recall, ambiguous


def audit_run(root: Path, benchmark_id: str, run_name: str, *, now: datetime | None = None) -> dict[str, Any]:
    directory = _run_dir(root, benchmark_id, run_name)
    manifest = _read_json(directory / "manifest.json")
    status = benchmark_status(root, benchmark_id, run_name)
    outputs = Path(manifest["output_directory"])
    candidate_events, valid_files, invalid_files, validation_errors = _load_success_outputs(outputs)
    company = _telemetry(directory, "companies.jsonl")
    tasks = _telemetry(directory, "runs.jsonl")
    task_durations = [float(record["task_elapsed_seconds"]) for record in tasks]
    busy = sum(record.get("event") == "busy_skip" for task in (directory / "tasks").glob("task??") for record in _read_jsonl(task / "events.jsonl"))
    stale = sum(record.get("event") == "stale_guard_recovered" for task in (directory / "tasks").glob("task??") for record in _read_jsonl(task / "events.jsonl"))
    overlap = 0
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in tasks:
        by_task[record["task_id"]].append(record)
    for records in by_task.values():
        ordered = sorted(records, key=lambda r: r["task_started_at"])
        overlap += sum(
            datetime.fromisoformat(current["task_started_at"]) < datetime.fromisoformat(previous["task_finished_at"])
            for previous, current in zip(ordered, ordered[1:])
        )
    source_counts = [float(record["source_count"]) for record in company if record["result_type"] != "failure"]
    news_records = [record for record in company if record["result_type"] == "news_present"]
    event_counts = [float(record["event_count"]) for record in news_records]
    urls = [event["source_url"] for values in candidate_events.values() for event in values]
    valid_urls = sum(urlsplit(url).scheme in {"http", "https"} and bool(urlsplit(url).hostname) for url in urls)
    result: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA,
        "captured_at": _iso(now),
        "benchmark_id": benchmark_id,
        "run_name": run_name,
        "batch_size": manifest["batch_size"],
        "status": status,
        "schema": {"valid_files": valid_files, "invalid_files": invalid_files, "validity_rate": valid_files / (valid_files + invalid_files) if valid_files + invalid_files else None, "errors": validation_errors},
        "research": {
            "failure_count": status["failed"],
            "source_url_validity_rate": valid_urls / len(urls) if urls else 1.0,
            "average_source_count_per_company": statistics.mean(source_counts) if source_counts else None,
            "median_source_count_per_company": statistics.median(source_counts) if source_counts else None,
            "average_events_per_news_present_company": statistics.mean(event_counts) if event_counts else 0.0,
            "news_present_rate": status["news_present"] / status["completed"] if status["completed"] else None,
        },
        "runtime": {
            "task_elapsed_seconds": _summary(task_durations),
            "busy_skip": busy,
            "stale_guard_recovery": stale,
            "overlap": overlap,
            "company_research_elapsed_seconds": _summary([float(record["research_elapsed_seconds"]) for record in company]),
        },
        "local_storage_bytes": sum(path.stat().st_size for path in outputs.rglob("*") if path.is_file()) if outputs.exists() else 0,
        "production_storage_delta_expected": 0,
    }

    quality_available = False
    company_recall: dict[str, float] = {}
    ambiguous: list[dict[str, Any]] = []
    if not manifest.get("reference"):
        reference_directory = _run_dir(root, benchmark_id, "reference")
        if (reference_directory / "manifest.json").exists():
            reference_manifest = _read_json(reference_directory / "manifest.json")
            reference_status = benchmark_status(root, benchmark_id, "reference")
            if reference_status["pending"] == 0 and reference_status["failed"] == 0:
                reference_events, _, reference_invalid, _ = _load_success_outputs(Path(reference_manifest["output_directory"]))
                cohort = _load_cohort(root, benchmark_id)
                reference_tickers = set(cohort["reference_tickers"])
                quality, company_recall, ambiguous = _quality_metrics(reference_events, candidate_events, reference_tickers)
                quality_available = reference_invalid == 0
                result["quality"] = {"available": quality_available, **quality, "ambiguous_matches": ambiguous}
    if "quality" not in result:
        result["quality"] = {"available": False, "reason": "completed single-company reference run is required"}

    batch_size = int(manifest["batch_size"])
    first_limit = max(1, math.ceil(batch_size * 0.25))
    last_start = math.floor(batch_size * 0.75) + 1
    first = [record for record in company if int(record["company_position"]) <= first_limit]
    last = [record for record in company if int(record["company_position"]) >= last_start]
    def position_group(records: list[dict[str, Any]]) -> dict[str, Any]:
        tickers = [record["ticker"] for record in records]
        recalls = [company_recall[ticker] for ticker in tickers if ticker in company_recall]
        return {
            "company_count": len(records),
            "event_recall": statistics.mean(recalls) if recalls else None,
            "average_source_count": statistics.mean([float(r["source_count"]) for r in records]) if records else None,
            "average_research_elapsed_seconds": statistics.mean([float(r["research_elapsed_seconds"]) for r in records]) if records else None,
        }
    first_metrics, last_metrics = position_group(first), position_group(last)
    position_pass = True
    if first_metrics["event_recall"] is not None and last_metrics["event_recall"] is not None:
        position_pass &= last_metrics["event_recall"] >= first_metrics["event_recall"] - 0.05
    if first_metrics["average_source_count"]:
        position_pass &= last_metrics["average_source_count"] >= first_metrics["average_source_count"] * 0.8
    result["position_degradation"] = {"first_25_percent": first_metrics, "last_25_percent": last_metrics, "pass": position_pass}

    starts = [datetime.fromisoformat(record["task_started_at"]) for record in tasks]
    ends = [datetime.fromisoformat(record["task_finished_at"]) for record in tasks]
    elapsed = (max(ends) - min(starts)).total_seconds() if starts and ends else None
    completed = status["completed"]
    per_hour = completed / (elapsed / 3600) if elapsed and elapsed > 0 else None
    result["throughput"] = {
        "observed_elapsed_seconds": elapsed,
        "companies_per_hour": per_hour,
        "companies_per_day": per_hour * 24 if per_hour is not None else None,
        "estimated_hours_for_3800": 3800 / per_hour if per_hour else None,
        "estimated_days_for_3800": 3800 / per_hour / 24 if per_hour else None,
        "weekly_buffer_hours": 168 - 3800 / per_hour if per_hour else None,
    }
    runtime = result["runtime"]["task_elapsed_seconds"]
    criteria = {
        "complete": status["pending"] == 0,
        "schema_validity_100pct": result["schema"]["validity_rate"] == 1.0,
        "research_failure_zero": status["failed"] == 0,
        "source_validity_at_least_99pct": result["research"]["source_url_validity_rate"] >= 0.99,
        "p95_under_45_minutes": runtime["p95"] is not None and runtime["p95"] < 2700,
        "max_under_55_minutes": runtime["max"] is not None and runtime["max"] < 3300,
        "busy_skip_zero": busy == 0,
        "overlap_zero": overlap == 0,
        "position_degradation_absent": position_pass,
    }
    if quality_available:
        criteria.update({
            "critical_recall_100pct": result["quality"]["critical_event_recall"] == 1.0,
            "material_event_recall_at_least_95pct": result["quality"]["event_recall"] >= 0.95,
            "major_no_news_false_negative_zero": result["quality"]["no_news_false_negative_count"] == 0,
        })
    result["criteria"] = criteria
    result["decision"] = "PASS" if quality_available and all(criteria.values()) else ("FAIL" if any(value is False for value in criteria.values()) else "WARN")
    atomic_write_json(directory / "audit.json", result)
    return result


def recommendation(root: Path, benchmark_id: str) -> dict[str, Any]:
    audits: dict[int, dict[str, Any]] = {}
    for path in (_benchmark_dir(root, benchmark_id) / "runs").glob("batch??/audit.json"):
        match = re.fullmatch(r"batch(\d{2})", path.parent.name)
        if match:
            audits[int(match.group(1))] = _read_json(path)
    baseline_sources = audits.get(4, {}).get("research", {}).get("average_source_count_per_company")
    source_ratios: dict[int, float | None] = {}
    for batch, audit in audits.items():
        current = audit.get("research", {}).get("average_source_count_per_company")
        source_ratios[batch] = current / baseline_sources if current is not None and baseline_sources else None
    passed = sorted(
        batch for batch, audit in audits.items()
        if audit.get("decision") == "PASS" and (source_ratios[batch] is None or source_ratios[batch] >= 0.8)
    )
    max_validated = max(passed) if passed else None
    conservative = [
        batch for batch in passed
        if (audits[batch].get("runtime", {}).get("task_elapsed_seconds", {}).get("p95") or math.inf) <= 2400
        and audits[batch].get("quality", {}).get("event_recall", 0) >= 0.97
        and audits[batch].get("quality", {}).get("critical_event_recall", 0) == 1.0
    ]
    recommended = max(conservative) if conservative else max_validated
    next_candidates: list[int] = []
    if 16 in audits and audits[16].get("decision") == "PASS":
        if 20 not in audits:
            next_candidates = [20]
        elif audits[20].get("decision") == "PASS" and 24 not in audits:
            next_candidates = [24]
    elif 16 in audits and audits[16].get("decision") == "FAIL":
        lower = max((batch for batch in passed if batch < 16), default=4)
        midpoint = (lower + 16) // 2
        if midpoint not in audits and midpoint in ALLOWED_BATCH_SIZES:
            next_candidates = [midpoint]
    integration_audits = []
    for path in _paths(root)["load"].glob("integration-batch??-*/integration_audit.json"):
        value = _read_json(path)
        if value.get("batch_size") == recommended:
            integration_audits.append(value)
    integration_pass = any(value.get("decision") == "PASS" for value in integration_audits)
    result = {
        "benchmark_id": benchmark_id,
        "max_validated_batch_size": max_validated,
        "recommended_batch_size": recommended,
        "candidate_decisions": {str(batch): audit.get("decision") for batch, audit in sorted(audits.items())},
        "source_count_ratio_vs_batch4": {str(batch): ratio for batch, ratio in sorted(source_ratios.items())},
        "next_candidates": next_candidates,
        "selection_contract": "largest quality/reliability PASS with p95<=40m and >=97% material recall preferred",
        "integration_validation_passed": integration_pass,
        "ready_for_production_change": recommended is not None and integration_pass,
        "human_review_required": True,
    }
    atomic_write_json(_benchmark_dir(root, benchmark_id) / "recommendation.json", result)
    return result


def prepare_integration_fixture(root: Path, batch_size: int, *, now: datetime | None = None) -> dict[str, Any]:
    if batch_size not in ALLOWED_BATCH_SIZES or batch_size == 1:
        raise BenchmarkError("integration fixture batch must be 4 through 24")
    timestamp = _now(now)
    fixture_id = f"integration-batch{batch_size:02d}-{timestamp:%Y%m%dT%H%M%S%f}"
    directory = _paths(root)["load"] / fixture_id
    inbox = directory / "inbox"
    inbox.mkdir(parents=True, exist_ok=False)
    file_count = TASK_COUNT * batch_size
    for index in range(file_count):
        ticker = f"{1000 + index:04d}"
        item = [] if index % 2 else [{
            "headline": f"Synthetic benchmark event {index}", "published_at": "2026-08-31T09:00:00+09:00",
            "source_type": "other", "source_name": "Synthetic Fixture", "source_url": f"https://example.invalid/{fixture_id}/{index}",
            "category": "other", "subcategory": None, "direction": "neutral", "importance": "low",
            "earnings_relevance": "context", "summary": "Synthetic isolated load fixture.",
            "why_it_matters": "Exercises file burst handling without production data.", "evidence_excerpt": None,
            "temporal_scope": "historical", "valid_from": None, "valid_until": None,
            "temporal_status": "historical", "tags": ["synthetic"],
        }]
        atomic_write_json(inbox / f"fixture_{index:03d}.json", {
            "schema_version": "company_news_v1", "run_id": f"{fixture_id}-{index:03d}", "ticker": ticker,
            "checked_at": "2026-08-31T10:00:00+09:00", "collector_type": "benchmark_fixture",
            "collector_version": "batch_integration_v1", "analysis_version": "company_news_v1",
            "task_id": f"{fixture_id}-{index:03d}", "sources_checked_count": 1, "items": item,
        })
    manifest = {
        "schema_version": INTEGRATION_SCHEMA,
        "fixture_id": fixture_id,
        "batch_size": batch_size,
        "file_count": file_count,
        "fixture_inbox": str(inbox.resolve()),
        "production_inbox_forbidden": str((root / "data" / "news_inbox").resolve()),
        "requires_temporary_sqlite_copy": True,
        "supabase_mode": "disabled_or_test_endpoint_only",
        "tdnet_mode": "fixture_only",
        "metrics_template": {
            "worker_run_duration_seconds": None, "max_inbox_backlog": None, "sqlite_lock_wait_seconds": None,
            "winerror_5_count": None, "winerror_32_count": None, "sync_retry_count": None,
            "ignore_new_skip_count": None, "worker_unrecovered_failure_count": None,
            "sqlite_locked_error_count": None, "sqlite_busy_timeout_count": None,
            "sqlite_write_starvation_count": None, "tdnet_scheduler_delay_seconds": None,
            "tdnet_ingest_baseline_seconds": None, "tdnet_process_baseline_seconds": None,
            "tdnet_notify_baseline_seconds": None, "tdnet_ingest_duration_seconds": None,
            "tdnet_process_duration_seconds": None, "tdnet_notify_duration_seconds": None,
            "tdnet_lock_timeout_count": None, "tdnet_duplicate_process_count": None,
            "visible_console_count": None, "supabase_sync_duration_seconds": None,
            "supabase_unrecovered_failure_count": None,
        },
    }
    atomic_write_json(directory / "manifest.json", manifest)
    return manifest


def audit_integration_fixture(manifest_path: Path, result_path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    manifest = _read_json(manifest_path)
    result = _read_json(result_path)
    if manifest.get("schema_version") != INTEGRATION_SCHEMA:
        raise BenchmarkError("invalid integration fixture manifest")
    if result.get("fixture_id") != manifest.get("fixture_id"):
        raise BenchmarkError("integration result fixture_id mismatch")
    required = set(manifest["metrics_template"])
    metrics = result.get("metrics")
    if not isinstance(metrics, dict) or any(key not in metrics or metrics[key] is None for key in required):
        raise BenchmarkError("integration result must populate every required metric")
    zero_metrics = (
        "winerror_5_count", "winerror_32_count", "sync_retry_count", "ignore_new_skip_count",
        "worker_unrecovered_failure_count", "sqlite_locked_error_count", "sqlite_busy_timeout_count",
        "sqlite_write_starvation_count",
        "tdnet_lock_timeout_count", "tdnet_duplicate_process_count", "visible_console_count",
        "supabase_unrecovered_failure_count",
    )
    criteria = {f"{key}_zero": metrics[key] == 0 for key in zero_metrics}
    criteria["inbox_backlog_bounded"] = 0 <= metrics["max_inbox_backlog"] <= manifest["file_count"]
    criteria["sqlite_lock_wait_bounded"] = 0 <= metrics["sqlite_lock_wait_seconds"] < 5
    criteria["worker_completed"] = metrics["worker_run_duration_seconds"] > 0
    criteria["tdnet_scheduler_delay_bounded"] = 0 <= metrics["tdnet_scheduler_delay_seconds"] < 60
    for phase in ("ingest", "process", "notify"):
        baseline = metrics.get(f"tdnet_{phase}_baseline_seconds")
        loaded = metrics[f"tdnet_{phase}_duration_seconds"]
        criteria[f"tdnet_{phase}_no_material_slowdown"] = baseline is None or baseline <= 0 or loaded <= baseline * 1.2
    criteria["supabase_sync_completed"] = metrics["supabase_sync_duration_seconds"] >= 0
    audit = {
        "schema_version": "company_news_batch_integration_audit_v1",
        "captured_at": _iso(now),
        "fixture_id": manifest["fixture_id"],
        "batch_size": manifest["batch_size"],
        "metrics": metrics,
        "criteria": criteria,
        "decision": "PASS" if all(criteria.values()) else "FAIL",
        "production_data_used": False,
    }
    atomic_write_json(manifest_path.parent / "integration_audit.json", audit)
    return audit


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--companies", type=int, default=COHORT_SIZE)
    prepare.add_argument("--reference-size", type=int, default=REFERENCE_SIZE)
    prepare.add_argument("--search-from", default=DEFAULT_SEARCH_FROM.isoformat())
    prepare.add_argument("--search-to", default=DEFAULT_SEARCH_TO.isoformat())
    prepare.add_argument("--master-db", type=Path)
    prepare.add_argument("--news-db", type=Path)
    run = commands.add_parser("prepare-run")
    run.add_argument("--benchmark-id", required=True)
    run.add_argument("--batch-size", type=int, required=True)
    run.add_argument("--reference", action="store_true")
    activate = commands.add_parser("activate-run")
    activate.add_argument("--benchmark-id", required=True)
    activate.add_argument("--run-name", required=True)
    commands.add_parser("deactivate-run")
    route = commands.add_parser("route")
    route.add_argument("--task-id", required=True)
    snap = commands.add_parser("snapshot")
    snap.add_argument("--task-id", required=True)
    start = commands.add_parser("research-start")
    finish = commands.add_parser("research-finish")
    finalize = commands.add_parser("finalize-company")
    release = commands.add_parser("release-task")
    for command in (start, finish, finalize):
        command.add_argument("--task-id", required=True)
        command.add_argument("--run-token", required=True)
        command.add_argument("--assignment-id", required=True)
    finalize.add_argument("--failure", action="store_true")
    release.add_argument("--task-id", required=True)
    release.add_argument("--run-token", required=True)
    status = commands.add_parser("status")
    status.add_argument("--benchmark-id", required=True)
    status.add_argument("--run-name", required=True)
    audit = commands.add_parser("audit")
    audit.add_argument("--benchmark-id", required=True)
    audit.add_argument("--run-name", required=True)
    recommend = commands.add_parser("recommend")
    recommend.add_argument("--benchmark-id", required=True)
    integration = commands.add_parser("prepare-integration-fixture")
    integration.add_argument("--batch-size", type=int, required=True)
    integration_audit = commands.add_parser("audit-integration-fixture")
    integration_audit.add_argument("--manifest", type=Path, required=True)
    integration_audit.add_argument("--result", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "prepare":
            result = prepare_benchmark(
                root, (args.master_db or root / "data" / "jquants.db").resolve(),
                (args.news_db or root / "decision_db.db").resolve(), companies=args.companies,
                reference_size=args.reference_size, search_from=_parse_date(args.search_from), search_to=_parse_date(args.search_to),
            )
        elif args.command == "prepare-run":
            result = prepare_run(root, args.benchmark_id, args.batch_size, reference=args.reference)
        elif args.command == "activate-run":
            result = activate_run(root, args.benchmark_id, args.run_name)
        elif args.command == "deactivate-run":
            result = deactivate_run(root)
        elif args.command == "route":
            result = route_task(root, args.task_id)
        elif args.command == "snapshot":
            result = snapshot_task(root, args.task_id)
        elif args.command == "research-start":
            result = research_start(root, args.task_id, args.run_token, args.assignment_id)
        elif args.command == "research-finish":
            result = research_finish(root, args.task_id, args.run_token, args.assignment_id)
        elif args.command == "finalize-company":
            result = finalize_company(root, args.task_id, args.run_token, args.assignment_id, failure=args.failure)
        elif args.command == "release-task":
            result = release_task(root, args.task_id, args.run_token)
        elif args.command == "status":
            result = benchmark_status(root, args.benchmark_id, args.run_name)
        elif args.command == "audit":
            result = audit_run(root, args.benchmark_id, args.run_name)
        elif args.command == "recommend":
            result = recommendation(root, args.benchmark_id)
        elif args.command == "prepare-integration-fixture":
            result = prepare_integration_fixture(root, args.batch_size)
        else:
            result = audit_integration_fixture(args.manifest.resolve(), args.result.resolve())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (BenchmarkError, NewsValidationError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
