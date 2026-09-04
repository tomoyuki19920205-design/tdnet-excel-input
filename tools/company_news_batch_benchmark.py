#!/usr/bin/env python3
"""Isolated Company News batch-size benchmark harness.

This module never writes to the production Company News queue, inbox,
``decision_db.db``, or Supabase.  Runtime state is confined to
``data/news_work/batch_benchmark`` and results to
``data/news_benchmark_inbox``.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.news_monitor import NewsValidationError, normalize_ticker, validate_payload
from tools.company_news_atomic import atomic_write_json, atomic_write_text, replace_with_retry

BENCHMARK_SCHEMA = "company_news_batch_benchmark_v1"
RUN_SCHEMA = "company_news_batch_benchmark_run_v1"
CONTROL_SCHEMA = "company_news_batch_benchmark_control_v1"
SNAPSHOT_SCHEMA = "company_news_benchmark_snapshot_v1"
COMPANY_TELEMETRY_SCHEMA = "company_news_benchmark_telemetry_v1"
TASK_TELEMETRY_SCHEMA = "company_news_benchmark_task_telemetry_v1"
FAILURE_SCHEMA = "company_news_benchmark_failure_v1"
AUDIT_SCHEMA = "company_news_batch_benchmark_audit_v1"
INTEGRATION_SCHEMA = "company_news_batch_integration_fixture_v1"
REPEATABILITY_SCHEMA = "company_news_reference_repeatability_v1"
EVENT_UNION_SCHEMA = "company_news_quality_event_union_v1"
GOLD_ADJUDICATION_SCHEMA = "company_news_gold_adjudication_v1"
GOLD_AUDIT_SCHEMA = "company_news_gold_audit_v1"
MATCHER_V2_SCHEMA = "company_news_quality_matcher_v2"
EVENT_UNION_V2_SCHEMA = "company_news_quality_event_union_v2"
REPEATABILITY_V2_SCHEMA = "company_news_reference_repeatability_v2"
MATCHER_REVIEW_SCHEMA = "company_news_matcher_review_v2"
GOLD_PACKET_SCHEMA = "company_news_gold_adjudication_packet_v2"
GOLD_PACKET_MANIFEST_SCHEMA = "company_news_gold_packet_manifest_v2"
GOLD_PACKET_PROVENANCE_SCHEMA = "company_news_gold_packet_provenance_v2"
GOLD_RESPONSE_SCHEMA = "company_news_gold_adjudication_response_v2"
GOLD_MATERIAL_MATRIX_SCHEMA = "company_news_gold_material_discovery_matrix_v1"
GOLD_MATERIAL_MISS_ANALYSIS_SCHEMA = "company_news_gold_material_miss_analysis_v1"
STRATEGY_V2_VERSION = "company_news_research_strategy_v2"
STRATEGY_V2_RUN_NAME = "batch04v2"
STRATEGY_V2_TELEMETRY_SCHEMA = "company_news_benchmark_strategy_telemetry_v2"
STRATEGY_V3_VERSION = "company_news_research_strategy_v3"
STRATEGY_V3_RUN_NAME = "batch04v3"
GOLD_EXTENSION_PACKET_SCHEMA = "company_news_gold_extension_packet_v1"

TASK_COUNT = 8
COHORT_SIZE = 192
REFERENCE_SIZE = 48
REFERENCE_BATCH_SIZE = 1
ALLOWED_CANDIDATE_BATCH_SIZES = (2, 3, 4)
SUSPENDED_UPWARD_CANDIDATES = (8, 12, 16, 20, 24)
ALLOWED_BATCH_SIZES = (REFERENCE_BATCH_SIZE, *ALLOWED_CANDIDATE_BATCH_SIZES)
REFERENCE_RUN_NAMES = ("reference", "reference02")
QUALITY_RUN_LABELS = {
    "reference": "reference01",
    "reference02": "reference02",
    "batch02": "batch02",
    "batch04": "batch04",
}
GOLD_VERDICTS = (
    "VALID_MATERIAL",
    "VALID_NON_MATERIAL",
    "DUPLICATE",
    "MATCHER_FALSE_NEGATIVE",
    "INVALID_SOURCE",
    "OUT_OF_WINDOW",
    "UNKNOWN",
)
GOLD_PACKET_MIN_EVENTS = 8
GOLD_PACKET_MAX_EVENTS = 10
GOLD_EARNINGS_CHANNELS = (
    "revenue", "pricing", "volume", "margin", "cost", "capex", "capacity",
    "customer", "supplier", "regulation", "capital_allocation", "balance_sheet",
    "management", "industry", "other",
)
GOLD_DIRECTIONS = ("positive", "negative", "mixed", "neutral", "unknown")
GOLD_CONFIDENCES = ("HIGH", "MEDIUM", "LOW")
GOLD_SOURCE_TYPES = (
    "company_ir", "company_news", "tdnet", "edinet", "government",
    "customer_supplier_official", "industry_association", "reliable_news",
    "secondary", "other", "none",
)
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
    if run_name not in (*REFERENCE_RUN_NAMES, STRATEGY_V2_RUN_NAME, STRATEGY_V3_RUN_NAME) and (
        not match or int(match.group(1)) not in ALLOWED_CANDIDATE_BATCH_SIZES
    ):
        raise BenchmarkError("invalid benchmark run name")
    return _benchmark_dir(root, benchmark_id) / "runs" / run_name


def _history_filename(
    kind: str,
    *,
    benchmark_id: Any,
    run_name: Any,
    task_id: Any,
    run_id: Any,
    run_token: Any,
) -> str:
    if kind not in {"completed", "stale"}:
        raise BenchmarkError("invalid benchmark task history kind")
    identity = {
        "benchmark_id": benchmark_id,
        "run_name": run_name,
        "task_id": task_id,
        "run_id": run_id,
        "run_token": run_token,
    }
    if any(not isinstance(value, str) or not value for value in identity.values()):
        raise BenchmarkError("benchmark task history identity is incomplete")
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"{kind}-{hashlib.sha256(encoded).hexdigest()[:16]}.json"


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
        "initial_candidates": list(ALLOWED_CANDIDATE_BATCH_SIZES),
        "conditional_candidates": [],
        "suspended_upward_candidates": list(SUSPENDED_UPWARD_CANDIDATES),
        "hard_ceiling": max(ALLOWED_CANDIDATE_BATCH_SIZES),
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


def _strategy_v2_contract() -> dict[str, Any]:
    """Return the company-neutral runtime coverage and budget contract."""
    return {
        "strategy_version": STRATEGY_V2_VERSION,
        "required_stages": ["official_first", "targeted_second_pass"],
        "official_query_budget": {"reserved_minimum": 2, "maximum": 3},
        "external_query_budget": {"reserved_minimum": 1, "maximum": 3},
        "per_company_query_budget": {"reserved_minimum": 3, "maximum": 6},
        "driver_group_limit": 3,
        "candidate_event_limit": 20,
        "final_event_limit": 10,
        "official_query_templates": [
            '"<company_name>" <ticker> IR お知らせ 適時開示',
            '"<company_name>" <ticker> press release news',
            '"<company_name>" <ticker> site:go.jp',
        ],
        "driver_query_groups": {
            "demand_customer_order": ["<driver> 需要 受注", "<customer_industry> 設備投資 販売"],
            "pricing_cost_supply": ["<input_or_product> 価格 値上げ", "<input_or_product> 供給 コスト"],
            "industry_regulation_competition": ["<industry_driver> 規制 制度", "<industry_driver> 生産 設備投資 市況"],
        },
        "government_query_must_be_generic": True,
        "government_query_required_pattern": "site:go.jp",
        "official_coverage_required": True,
        "external_coverage_required": True,
        "no_news_requires_both_coverages": True,
        "window_review_must_complete_after_last_candidate": True,
        "stop_after_first_event_forbidden": True,
        "production_write_forbidden": True,
        "candidate_only_event_policy": "NOT_A_FALSE_POSITIVE_PENDING_BLIND_ADJUDICATION",
        "precision_status_after_run": "PROVISIONAL_PENDING_GOLD_EXTENSION",
        "quality_targets": {
            "critical_found_minimum": 7,
            "ir_tdnet_found_minimum": 12,
            "known_material_found_minimum": 24,
            "official_found_minimum": 20,
            "noncritical_found_minimum": 17,
            "companies_per_hour_minimum": 22.62,
            "companies_per_hour_preferred": 24.0,
            "task_p95_seconds_maximum": 2700,
            "task_max_seconds_maximum": 3300,
            "capacity_busy_skip_maximum": 0,
            "overlap_maximum": 0,
        },
    }


def _strategy_v2_firewall_scan(
    prompt_text: str, assignments_by_task: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Fail closed if post-run adjudication vocabulary leaks into runtime inputs."""
    forbidden_terms = (
        "found_by", "materiality_reason", "gold verdict", "gold event",
        "critical event list", "miss ticker", "reference01", "reference02",
    )
    prompt_lower = prompt_text.lower()
    hits = [term for term in forbidden_terms if term in prompt_lower]
    forbidden_keys = {"verdict", "found_by", "materiality_reason", "duplicate_of_cluster_id"}
    assignment_key_hits: list[str] = []
    for task_id, assignments in assignments_by_task.items():
        for assignment in assignments:
            for key in assignment:
                if key.lower() in forbidden_keys or "gold" in key.lower() or "miss" in key.lower():
                    assignment_key_hits.append(f"{task_id}:{assignment['assignment_id']}:{key}")
    company_branch_hits = re.findall(
        r"\bif\s+(?:ticker|company(?:_name)?)\s*(?:==|in)\s*", prompt_text, re.IGNORECASE
    )
    all_hits = [*hits, *assignment_key_hits, *company_branch_hits]
    return {
        "status": "PASS" if not all_hits else "FAIL",
        "hit_count": len(all_hits),
        "hits": all_hits,
        "company_specific_branch_count": len(company_branch_hits),
    }


def _prepare_named_run(
    root: Path,
    benchmark_id: str,
    cohort: dict[str, Any],
    selected: list[dict[str, Any]],
    batch_size: int,
    run_name: str,
    *,
    reference: bool,
    reference_replication_of: str | None = None,
    strategy_version: str | None = None,
    baseline_run_name: str | None = None,
    baseline_company_order_sha256: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    directory = _run_dir(root, benchmark_id, run_name)
    output_directory = _paths(root)["outputs"] / benchmark_id / run_name
    if directory.exists() or output_directory.exists():
        raise BenchmarkError(f"benchmark run already exists: {run_name}")
    assignments_by_task: dict[str, list[dict[str, Any]]] = {f"task{i:02d}": [] for i in range(1, 9)}
    for index, company in enumerate(selected):
        task_id = f"task{index % TASK_COUNT + 1:02d}"
        task_queue_position = len(assignments_by_task[task_id]) + 1
        assignment_id = f"{benchmark_id}-{run_name}-{task_id}-{task_queue_position:03d}-{company['ticker']}"
        prepared_assignment = {
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
        }
        if strategy_version:
            prepared_assignment["strategy_version"] = strategy_version
        assignments_by_task[task_id].append(prepared_assignment)
    company_order_sha256 = hashlib.sha256(
        "\n".join(company["ticker"] for company in selected).encode()
    ).hexdigest()
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
        "company_order_sha256": company_order_sha256,
    }
    if reference_replication_of:
        manifest["reference_replication_of"] = reference_replication_of
    if strategy_version:
        if strategy_version == STRATEGY_V3_VERSION:
            from tools import company_news_strategy_v3 as v3
            contract = v3.contract()
            prompt_text = v3.prompt_text(root)
            firewall = v3.leakage_scan(prompt_text, assignments_by_task, selected)
            manifest.update({"runtime_prompt_path": str((directory / "RESEARCH_PROMPT.md").resolve()),
                             "runtime_prompt_sha256": hashlib.sha256(prompt_text.encode()).hexdigest(),
                             "evaluation_policy": v3.evaluation_policy()})
        else:
            contract = _strategy_v2_contract()
            prompt_text = _paths(root)["benchmark_prompt"].read_text(encoding="utf-8")
            firewall = _strategy_v2_firewall_scan(prompt_text, assignments_by_task)
        if firewall["status"] != "PASS":
            raise BenchmarkError(f"strategy runtime Gold firewall failed: {firewall['hits']}")
        if baseline_company_order_sha256 != company_order_sha256:
            raise BenchmarkError("strategy company order differs from baseline batch04")
        manifest.update({
            "strategy_version": strategy_version,
            "strategy_contract": contract,
            "baseline_run_name": baseline_run_name,
            "baseline_company_order_sha256": baseline_company_order_sha256,
            "same_company_order_as_baseline": True,
            "gold_leakage_scan": firewall,
            "activation_gate": "READY_TO_ACTIVATE",
            "readiness_status": "READY_TO_ACTIVATE",
            "precision_status_after_run": contract["precision_status_after_run"],
            "candidate_only_event_policy": contract["candidate_only_event_policy"],
        })
    if strategy_version == STRATEGY_V3_VERSION:
        atomic_write_text(directory / "RESEARCH_PROMPT.md", prompt_text)
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


def prepare_run(
    root: Path,
    benchmark_id: str,
    batch_size: int,
    *,
    reference: bool = False,
    run_name: str | None = None,
    strategy: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if strategy is not None:
        if strategy not in (STRATEGY_V2_VERSION, STRATEGY_V3_VERSION):
            raise BenchmarkError("unsupported benchmark research strategy")
        strategy_run_name = STRATEGY_V3_RUN_NAME if strategy == STRATEGY_V3_VERSION else STRATEGY_V2_RUN_NAME
        if reference or batch_size != 4:
            raise BenchmarkError("research strategy requires non-reference batch size 4")
        if run_name not in (None, strategy_run_name):
            raise BenchmarkError(f"research strategy run_name must be {strategy_run_name}")
        cohort = _load_cohort(root, benchmark_id)
        if (
            cohort.get("search_from") != DEFAULT_SEARCH_FROM.isoformat()
            or cohort.get("search_to") != DEFAULT_SEARCH_TO.isoformat()
        ):
            raise BenchmarkError("research strategy v2 requires the fixed benchmark window")
        baseline_directory = _run_dir(root, benchmark_id, "batch04")
        baseline_manifest = _read_json(baseline_directory / "manifest.json")
        baseline_assignments = _ordered_run_assignments(baseline_directory)
        cohort_order = [str(company["ticker"]) for company in cohort["companies"]]
        if strategy == STRATEGY_V3_VERSION and (len(cohort_order) != COHORT_SIZE or len(set(cohort_order)) != COHORT_SIZE):
            raise BenchmarkError("research strategy v3 requires the same 192-company cohort")
        baseline_order = [str(assignment["ticker"]) for assignment in baseline_assignments]
        if (
            baseline_manifest.get("batch_size") != 4
            or baseline_manifest.get("company_count") != len(cohort_order)
            or baseline_manifest.get("search_from") != cohort["search_from"]
            or baseline_manifest.get("search_to") != cohort["search_to"]
            or baseline_order != cohort_order
        ):
            raise BenchmarkError("persisted batch04 does not match the fixed benchmark cohort")
        baseline_sha = hashlib.sha256("\n".join(baseline_order).encode()).hexdigest()
        selected = [{
            "ticker": assignment["ticker"],
            "company_name": assignment["company_name"],
            "sector": assignment.get("sector", "unknown"),
            "size_bucket": assignment.get("size_bucket", "unknown"),
            "prior_news_bucket": assignment.get("prior_news_bucket", "unscanned"),
        } for assignment in baseline_assignments]
        return _prepare_named_run(
            root, benchmark_id, cohort, selected, 4, strategy_run_name,
            reference=False, strategy_version=strategy,
            baseline_run_name="batch04", baseline_company_order_sha256=baseline_sha,
            now=now,
        )
    if (
        batch_size not in ALLOWED_BATCH_SIZES
        or (reference and batch_size != REFERENCE_BATCH_SIZE)
        or (not reference and batch_size == REFERENCE_BATCH_SIZE)
    ):
        raise BenchmarkError("candidate batch must be 2 through 4; reference batch must be 1")
    expected_run_name = "reference" if reference else f"batch{batch_size:02d}"
    if run_name not in (None, expected_run_name):
        raise BenchmarkError("run_name does not match the legacy batch configuration")
    cohort = _load_cohort(root, benchmark_id)
    run_name = expected_run_name
    selected = list(cohort["companies"])
    if reference:
        wanted = set(cohort["reference_tickers"])
        selected = [company for company in selected if company["ticker"] in wanted]
    return _prepare_named_run(
        root,
        benchmark_id,
        cohort,
        selected,
        batch_size,
        run_name,
        reference=reference,
        now=now,
    )


def _ordered_run_assignments(directory: Path) -> list[dict[str, Any]]:
    ordered: list[tuple[int, dict[str, Any]]] = []
    for task_index in range(TASK_COUNT):
        task_id = f"task{task_index + 1:02d}"
        queue = _read_json(directory / "tasks" / task_id / "queue.json")
        for assignment in queue.get("assignments", []):
            position = int(assignment["task_queue_position"])
            ordered.append(((position - 1) * TASK_COUNT + task_index, assignment))
    ordered.sort(key=lambda value: value[0])
    return [assignment for _, assignment in ordered]


def prepare_reference_replication(
    root: Path,
    benchmark_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Prepare an inert reference02 from the persisted reference01 sequence."""
    cohort = _load_cohort(root, benchmark_id)
    original_directory = _run_dir(root, benchmark_id, "reference")
    original_manifest = _read_json(original_directory / "manifest.json")
    if (
        original_manifest.get("reference") is not True
        or original_manifest.get("batch_size") != REFERENCE_BATCH_SIZE
        or original_manifest.get("search_from") != cohort.get("search_from")
        or original_manifest.get("search_to") != cohort.get("search_to")
    ):
        raise BenchmarkError("persisted reference run does not match the benchmark cohort")
    original = _ordered_run_assignments(original_directory)
    expected = list(cohort.get("reference_tickers", []))
    if len(original) != len(expected) or {item["ticker"] for item in original} != set(expected):
        raise BenchmarkError("persisted reference company set is incomplete")
    selected = [
        {
            "ticker": item["ticker"],
            "company_name": item["company_name"],
            "sector": item.get("sector", "unknown"),
            "size_bucket": item.get("size_bucket", "unknown"),
            "prior_news_bucket": item.get("prior_news_bucket", "unscanned"),
        }
        for item in original
    ]
    return _prepare_named_run(
        root,
        benchmark_id,
        cohort,
        selected,
        REFERENCE_BATCH_SIZE,
        "reference02",
        reference=True,
        reference_replication_of="reference",
        now=now,
    )


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
    if manifest.get("strategy_version") == STRATEGY_V3_VERSION:
        prompt_path = directory / "RESEARCH_PROMPT.md"
        if (Path(manifest.get("runtime_prompt_path", "")).resolve() != prompt_path.resolve()
                or hashlib.sha256(prompt_path.read_bytes()).hexdigest() != manifest.get("runtime_prompt_sha256")):
            raise BenchmarkError("strategy v3 frozen runtime prompt mismatch")
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
    if manifest["status"] == "completed" and not manifest.get("completed_at"):
        manifest["completed_at"] = status.get("completed_at") or _iso(now)
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
    result = {
        "mode": "benchmark",
        "task_id": task_id,
        "prompt_file": str(paths["benchmark_prompt"].resolve()),
        "benchmark_id": control["benchmark_id"],
        "run_name": control["run_name"],
        "batch_size": manifest["batch_size"],
        "output_directory": control["output_directory"],
    }
    if manifest.get("strategy_version"):
        result["strategy_version"] = manifest["strategy_version"]
        if manifest["strategy_version"] == STRATEGY_V3_VERSION:
            result["prompt_file"] = manifest["runtime_prompt_path"]
    return result


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


def _run_counts(run_directory: Path) -> tuple[dict[str, int], int]:
    counts: dict[str, int] = defaultdict(int)
    total = 0
    for task_index in range(1, TASK_COUNT + 1):
        queue = _task_queue(run_directory, f"task{task_index:02d}")
        for assignment in queue["assignments"]:
            counts[str(assignment["status"])] += 1
            total += 1
    return counts, total


def _actual_completed_at(run_directory: Path) -> str | None:
    """Return the last release containing real company work, never an idle poll."""
    productive = [
        record for record in _telemetry(run_directory, "runs.jsonl")
        if int(record.get("snapshot_company_count", 0)) > 0 and record.get("task_finished_at")
    ]
    if not productive:
        return None
    return max(productive, key=lambda record: datetime.fromisoformat(record["task_finished_at"]))["task_finished_at"]


def _finalize_run_if_complete(
    root: Path,
    run_directory: Path,
    manifest: dict[str, Any],
    *,
    completed_at_hint: datetime | None = None,
) -> dict[str, Any]:
    """Persist the terminal run state once all queues and task guards are settled."""
    counts, _ = _run_counts(run_directory)
    active_guards = list((run_directory / "tasks").glob("task??/active.json"))
    if counts["pending"] or active_guards:
        return manifest

    completed_at = manifest.get("completed_at")
    if not completed_at:
        completed_at = _iso(completed_at_hint) if completed_at_hint is not None else _actual_completed_at(run_directory)
    if not completed_at:
        raise BenchmarkError("completed benchmark run has no productive task completion telemetry")

    if manifest.get("status") != "completed" or manifest.get("completed_at") != completed_at:
        manifest = {**manifest, "status": "completed", "completed_at": completed_at}
        atomic_write_json(run_directory / "manifest.json", manifest)

    active = _paths(root)["active"]
    if active.exists():
        control = _read_json(active)
        if (
            control.get("benchmark_id") == manifest.get("benchmark_id")
            and control.get("run_name") == manifest.get("run_name")
        ):
            active.unlink()
    return manifest


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
            started = datetime.fromisoformat(str(existing["task_started_at"]).replace("Z", "+00:00"))
            _task_event(
                task_directory,
                "busy_skip",
                timestamp,
                benchmark_id=control["benchmark_id"],
                run_name=control["run_name"],
                run_id=existing.get("run_id"),
                run_token=existing.get("run_token"),
                guard_started_at=existing.get("task_started_at"),
                guard_expires_at=existing.get("expires_at"),
                guard_age_seconds=(timestamp - started).total_seconds(),
                reason="active_guard_unexpired",
                stale_before=False,
            )
            return {"status": "busy", "task_id": task_id, "assignments": [], "expires_at": existing["expires_at"]}
        history = task_directory / "history"
        history.mkdir(parents=True, exist_ok=True)
        stale_history_path = history / _history_filename(
            "stale",
            benchmark_id=control["benchmark_id"],
            run_name=control["run_name"],
            task_id=task_id,
            run_id=existing.get("run_id"),
            run_token=existing.get("run_token"),
        )
        if stale_history_path.exists():
            raise BenchmarkError("stale benchmark task history already exists")
        replace_with_retry(guard, stale_history_path)
        started = datetime.fromisoformat(str(existing["task_started_at"]).replace("Z", "+00:00"))
        _task_event(
            task_directory,
            "stale_guard_recovered",
            timestamp,
            benchmark_id=control["benchmark_id"],
            run_name=control["run_name"],
            run_id=existing.get("run_id"),
            run_token=existing.get("run_token"),
            guard_started_at=existing.get("task_started_at"),
            guard_expires_at=existing.get("expires_at"),
            guard_age_seconds=(timestamp - started).total_seconds(),
            reason="guard_ttl_expired",
            stale_before=True,
            stale_after=True,
            history_path=str(stale_history_path.resolve()),
        )

    queue = _task_queue(run_directory, task_id)
    pending = [assignment for assignment in queue["assignments"] if assignment.get("status") == "pending"]
    selected = pending[: int(manifest["batch_size"])]
    token = hashlib.sha256(os.urandom(32)).hexdigest()
    run_id = f"{control['benchmark_id']}-{control['run_name']}-{task_id}-{timestamp:%Y%m%dT%H%M%S%f}"
    output_directory = Path(control["output_directory"])
    assignments = []
    for position, assignment in enumerate(selected, start=1):
        filename = f"benchmark_{task_id}_{assignment['assignment_id']}.json"
        prepared = {
            **assignment,
            "slot_id": f"bslot{(int(task_id[-2:]) - 1) * int(manifest['batch_size']) + position:03d}",
            "company_position": position,
            "output_path": str((output_directory / filename).resolve()),
            "temp_path": str((output_directory / f"{filename}.tmp").resolve()),
        }
        if manifest.get("strategy_version") in (STRATEGY_V2_VERSION, STRATEGY_V3_VERSION):
            strategy_directory = run_directory / "strategy_telemetry" / task_id
            strategy_directory.mkdir(parents=True, exist_ok=True)
            strategy_filename = f"{assignment['assignment_id']}.json"
            prepared.update({
                "strategy_telemetry_path": str((strategy_directory / strategy_filename).resolve()),
                "strategy_telemetry_temp_path": str((strategy_directory / f"{strategy_filename}.tmp").resolve()),
            })
        assignments.append(prepared)
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
    if manifest.get("strategy_version") in (STRATEGY_V2_VERSION, STRATEGY_V3_VERSION):
        snapshot["strategy_version"] = manifest["strategy_version"]
        snapshot["strategy_contract"] = manifest["strategy_contract"]
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


def validate_strategy_v2_telemetry(
    value: dict[str, Any], assignment: dict[str, Any], event_count: int, result_type: str
) -> dict[str, Any]:
    """Validate benchmark-only coverage evidence before a v2 result is finalized."""
    required = {
        "schema_version", "strategy_version", "assignment_id", "ticker",
        "official_queries_attempted", "official_sources_found",
        "official_coverage_complete", "government_search_attempted",
        "external_queries_attempted", "external_sources_found",
        "external_coverage_complete", "driver_groups", "source_diversity",
        "candidate_event_count", "final_event_count", "no_news_gate_pass",
        "window_review_complete", "stopped_after_first_event",
        "official_stage_elapsed_seconds", "targeted_stage_elapsed_seconds",
        "total_research_elapsed_seconds", "candidate_provenance",
    }
    if value.get("schema_version") != STRATEGY_V2_TELEMETRY_SCHEMA or not required.issubset(value):
        raise BenchmarkError("invalid research strategy v2 telemetry schema")
    if value.get("strategy_version") != STRATEGY_V2_VERSION:
        raise BenchmarkError("research strategy v2 telemetry version mismatch")
    if value.get("assignment_id") != assignment["assignment_id"] or value.get("ticker") != assignment["ticker"]:
        raise BenchmarkError("research strategy v2 telemetry assignment mismatch")
    official_queries = value["official_queries_attempted"]
    external_queries = value["external_queries_attempted"]
    if not isinstance(official_queries, list) or any(not isinstance(query, str) or not query.strip() for query in official_queries):
        raise BenchmarkError("official_queries_attempted must contain non-empty strings")
    if not isinstance(external_queries, list) or any(not isinstance(query, str) or not query.strip() for query in external_queries):
        raise BenchmarkError("external_queries_attempted must contain non-empty strings")
    if not 2 <= len(official_queries) <= 3:
        raise BenchmarkError("official query budget must be 2 through 3")
    if not 1 <= len(external_queries) <= 3:
        raise BenchmarkError("external query budget must be 1 through 3")
    if not 3 <= len(official_queries) + len(external_queries) <= 6:
        raise BenchmarkError("per-company query budget must be 3 through 6")
    company_name = str(assignment.get("company_name", "")).strip()
    ticker = str(assignment["ticker"])
    if not company_name or any(company_name not in query or ticker not in query for query in official_queries):
        raise BenchmarkError("every official query must identify the assigned company and ticker")
    if value["government_search_attempted"] is not True or not any("site:go.jp" in query.lower() for query in official_queries):
        raise BenchmarkError("generic government search is required")
    for key in ("official_sources_found", "external_sources_found"):
        sources = value[key]
        if not isinstance(sources, list) or any(
            not isinstance(source, str) or not source.startswith(("http://", "https://"))
            for source in sources
        ):
            raise BenchmarkError(f"{key} must contain only HTTP(S) URLs")
    drivers = value["driver_groups"]
    if not isinstance(drivers, list) or not 1 <= len(drivers) <= 3:
        raise BenchmarkError("driver_groups must contain 1 through 3 dynamic groups")
    driver_required = {"driver", "query_group", "transmission_channel"}
    if any(
        not isinstance(driver, dict)
        or not driver_required.issubset(driver)
        or any(not isinstance(driver[key], str) or not driver[key].strip() for key in driver_required)
        or driver["query_group"] not in {
            "demand_customer_order", "pricing_cost_supply",
            "industry_regulation_competition",
        }
        for driver in drivers
    ):
        raise BenchmarkError("driver_groups entries are incomplete")
    normalized_drivers = [str(driver["driver"]).lower() for driver in drivers]
    if any(not any(driver in query.lower() for driver in normalized_drivers) for query in external_queries):
        raise BenchmarkError("each external query must include a dynamically derived driver")
    if value["official_coverage_complete"] is not True or value["external_coverage_complete"] is not True:
        raise BenchmarkError("both strategy v2 coverage stages must complete")
    if value["window_review_complete"] is not True or value["stopped_after_first_event"] is not False:
        raise BenchmarkError("full-window multi-event review must complete")
    if value["no_news_gate_pass"] is not True:
        raise BenchmarkError("strategy v2 no-news/coverage gate did not pass")
    if result_type == "no_news" and (
        value["official_coverage_complete"] is not True
        or value["external_coverage_complete"] is not True
    ):
        raise BenchmarkError("no_news is forbidden before both coverage stages complete")
    for key in ("candidate_event_count", "final_event_count"):
        if not isinstance(value[key], int) or value[key] < 0:
            raise BenchmarkError(f"{key} must be a non-negative integer")
    if value["candidate_event_count"] > 20 or value["final_event_count"] > 10:
        raise BenchmarkError("strategy event budget exceeds candidate/final limits")
    if value["final_event_count"] != event_count or value["candidate_event_count"] < event_count:
        raise BenchmarkError("strategy event counts disagree with the canonical payload")
    diversity = value["source_diversity"]
    if (
        not isinstance(diversity, list) or len(diversity) < 2
        or len(set(diversity)) != len(diversity)
        or any(not isinstance(item, str) or not item.strip() for item in diversity)
    ):
        raise BenchmarkError("source_diversity must contain at least two unique source types")
    for key in (
        "official_stage_elapsed_seconds", "targeted_stage_elapsed_seconds",
        "total_research_elapsed_seconds",
    ):
        if not isinstance(value[key], (int, float)) or isinstance(value[key], bool) or value[key] < 0:
            raise BenchmarkError(f"{key} must be non-negative")
    if value["total_research_elapsed_seconds"] < (
        value["official_stage_elapsed_seconds"] + value["targeted_stage_elapsed_seconds"]
    ):
        raise BenchmarkError("total research time cannot be shorter than its strategy stages")
    provenance = value["candidate_provenance"]
    if not isinstance(provenance, list) or len(provenance) != value["candidate_event_count"]:
        raise BenchmarkError("candidate_provenance must cover every candidate exactly once")
    stages = {"official_first", "targeted_second_pass", "existing_general"}
    indexes: set[int] = set()
    selected = 0
    for item in provenance:
        if not isinstance(item, dict) or not {
            "candidate_index", "stage", "selected_for_output", "headline", "source_url"
        }.issubset(item):
            raise BenchmarkError("candidate_provenance entry is incomplete")
        index = item["candidate_index"]
        if not isinstance(index, int) or index < 0 or index in indexes:
            raise BenchmarkError("candidate_provenance indexes must be unique non-negative integers")
        indexes.add(index)
        if item["stage"] not in stages:
            raise BenchmarkError("candidate_provenance stage is invalid")
        if not isinstance(item["selected_for_output"], bool):
            raise BenchmarkError("candidate_provenance selected_for_output must be boolean")
        selected += item["selected_for_output"]
        if not isinstance(item["headline"], str) or not item["headline"].strip():
            raise BenchmarkError("candidate_provenance headline must be non-empty")
        if not isinstance(item["source_url"], str) or not item["source_url"].startswith(("http://", "https://")):
            raise BenchmarkError("candidate_provenance source_url must be HTTP(S)")
    if indexes != set(range(value["candidate_event_count"])) or selected != event_count:
        raise BenchmarkError("candidate_provenance coverage/selection count mismatch")
    query_scan = _strategy_v2_firewall_scan("\n".join([*official_queries, *external_queries]), {})
    if query_scan["status"] != "PASS":
        raise BenchmarkError("post-run adjudication data leaked into research queries")
    return value


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
    _, run_directory, manifest = _active_run(root)
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
    strategy_telemetry: dict[str, Any] | None = None
    strategy_temp_path: Path | None = None
    strategy_output_path: Path | None = None
    if manifest.get("strategy_version") in (STRATEGY_V2_VERSION, STRATEGY_V3_VERSION) and not failure:
        strategy_temp_path = Path(assignment["strategy_telemetry_temp_path"])
        strategy_output_path = Path(assignment["strategy_telemetry_path"])
        if not _inside(strategy_temp_path, run_directory) or not _inside(strategy_output_path, run_directory):
            raise BenchmarkError("strategy telemetry path is not benchmark-isolated")
        if strategy_output_path.exists():
            raise BenchmarkError("strategy telemetry already exists; refusing overwrite")
        if manifest["strategy_version"] == STRATEGY_V3_VERSION:
            from tools.company_news_strategy_v3 import validate as validate_strategy_v3
            strategy_telemetry = validate_strategy_v3(_read_json(strategy_temp_path), assignment, payload)
        else:
            strategy_telemetry = validate_strategy_v2_telemetry(
                _read_json(strategy_temp_path), assignment, event_count, result_type
            )
    if strategy_telemetry is not None and strategy_temp_path is not None and strategy_output_path is not None:
        replace_with_retry(strategy_temp_path, strategy_output_path)
        try:
            replace_with_retry(temp_path, output_path)
        except OSError:
            replace_with_retry(strategy_output_path, strategy_temp_path)
            raise
    else:
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
    if manifest.get("strategy_version") == STRATEGY_V2_VERSION:
        telemetry["strategy_version"] = STRATEGY_V2_VERSION
        telemetry["strategy_telemetry_path"] = (
            str(strategy_output_path.resolve()) if strategy_output_path is not None else None
        )
        if strategy_telemetry is not None:
            telemetry.update({
                key: strategy_telemetry[key]
                for key in (
                    "official_queries_attempted", "official_sources_found",
                    "official_coverage_complete", "government_search_attempted",
                    "external_queries_attempted", "external_sources_found",
                    "external_coverage_complete", "driver_groups", "source_diversity",
                    "candidate_event_count", "final_event_count", "no_news_gate_pass",
                    "window_review_complete", "stopped_after_first_event",
                    "official_stage_elapsed_seconds", "targeted_stage_elapsed_seconds",
                    "total_research_elapsed_seconds", "candidate_provenance",
                )
            })
    if manifest.get("strategy_version") == STRATEGY_V3_VERSION:
        telemetry["strategy_version"] = STRATEGY_V3_VERSION
        telemetry["strategy_telemetry_path"] = str(strategy_output_path.resolve()) if strategy_output_path else None
        if strategy_telemetry is not None:
            # Validated v3 fields are namespaced; cannot overwrite measured times,
            # ownership, ticker or paths in canonical company telemetry.
            telemetry["strategy_v3"] = strategy_telemetry
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
    history_name = _history_filename(
        "completed",
        benchmark_id=snapshot.get("benchmark_id"),
        run_name=snapshot.get("run_name"),
        task_id=task_id,
        run_id=snapshot.get("run_id"),
        run_token=snapshot.get("run_token"),
    )
    history_path = history / history_name
    if history_path.exists():
        raise BenchmarkError("completed benchmark task history already exists")
    replace_with_retry(path, history_path)
    manifest = _finalize_run_if_complete(
        root,
        run_directory,
        manifest,
        completed_at_hint=finished if snapshot["assignments"] else None,
    )
    return {
        "status": "released",
        **record,
        "remaining": sum(a["status"] == "pending" for a in queue["assignments"]),
        "candidate_batch_size": manifest["batch_size"],
        "run_status": manifest["status"],
        "completed_at": manifest.get("completed_at"),
        "history_path": str(history_path.resolve()),
    }


def benchmark_status(root: Path, benchmark_id: str, run_name: str) -> dict[str, Any]:
    directory = _run_dir(root, benchmark_id, run_name)
    manifest = _read_json(directory / "manifest.json")
    counts, total = _run_counts(directory)
    logical_status = "completed" if counts["pending"] == 0 else manifest["status"]
    completed_at = manifest.get("completed_at") or (_actual_completed_at(directory) if logical_status == "completed" else None)
    return {
        "benchmark_id": benchmark_id,
        "run_name": run_name,
        "status": logical_status,
        "completed_at": completed_at,
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


def _ratio(value: float | None, reference: float | None) -> float | None:
    if value is None or reference is None or reference == 0:
        return None
    return value / reference


def _global_compression_metrics(
    reference_source_counts: list[float],
    candidate_source_counts: list[float],
    reference_research_times: list[float],
    candidate_research_times: list[float],
    event_recall: float | None,
) -> dict[str, Any]:
    reference_sources = {
        "average": statistics.mean(reference_source_counts) if reference_source_counts else None,
        "median": statistics.median(reference_source_counts) if reference_source_counts else None,
    }
    candidate_sources = {
        "average": statistics.mean(candidate_source_counts) if candidate_source_counts else None,
        "median": statistics.median(candidate_source_counts) if candidate_source_counts else None,
    }
    reference_research = _summary(reference_research_times)
    candidate_research = _summary(candidate_research_times)
    return {
        "reference_source_count_per_company": reference_sources,
        "candidate_source_count_per_company": candidate_sources,
        "source_count_ratio_vs_reference": _ratio(candidate_sources["average"], reference_sources["average"]),
        "reference_research_elapsed_seconds": reference_research,
        "candidate_research_elapsed_seconds": candidate_research,
        "research_time_ratio_vs_reference": _ratio(candidate_research["average"], reference_research["average"]),
        "event_recall_vs_reference": event_recall,
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


def _event_match_evidence(reference: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    same_ticker = reference["ticker"] == candidate["ticker"]
    raw_url_equal = reference["source_url"] == candidate["source_url"]
    normalized_url_equal = _normalized_url(reference["source_url"]) == _normalized_url(candidate["source_url"])
    ref_date = datetime.fromisoformat(reference["published_at"]).date()
    candidate_date = datetime.fromisoformat(candidate["published_at"]).date()
    date_delta_days = abs((ref_date - candidate_date).days)
    category_equal = reference["category"] == candidate["category"]
    headline_similarity = _jaccard(
        _text_tokens(reference["headline"]), _text_tokens(candidate["headline"])
    )
    substance_similarity = _jaccard(
        _text_tokens(reference["headline"] + " " + reference.get("summary", "")),
        _text_tokens(candidate["headline"] + " " + candidate.get("summary", "")),
    )
    score = max(headline_similarity, substance_similarity)
    if not same_ticker:
        matched, method, score = False, "ticker_mismatch", 0.0
    elif normalized_url_equal:
        matched, method, score = True, "normalized_url", 1.0
    elif date_delta_days != 0 or not category_equal:
        matched, method, score = False, "date_or_category_mismatch", score
    else:
        matched, method = score >= 0.55, "semantic_substance"
    return {
        "matched": matched,
        "method": method,
        "score": score,
        "raw_url_equal": raw_url_equal,
        "normalized_url_equal": normalized_url_equal,
        "date_delta_days": date_delta_days,
        "category_equal": category_equal,
        "headline_similarity": headline_similarity,
        "substance_similarity": substance_similarity,
    }


def _event_match(reference: dict[str, Any], candidate: dict[str, Any]) -> tuple[bool, str, float]:
    evidence = _event_match_evidence(reference, candidate)
    return evidence["matched"], evidence["method"], evidence["score"]


_LANGUAGE_PATH_TOKENS = {"en", "eng", "english", "ja", "jp", "japanese"}
_LANGUAGE_SUBDOMAINS = {"en", "eng", "english", "ja", "jp"}
_STABLE_QUERY_IDS = {"itemid", "articleid", "newsid", "documentid", "docid", "releaseid"}


def _canonical_host(value: str) -> str:
    labels = (urlsplit(value).hostname or "").casefold().split(".")
    while labels and labels[0] in {"www", "m", *_LANGUAGE_SUBDOMAINS}:
        labels.pop(0)
    return ".".join(labels)


def _normalize_headline_identity(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"(?:®|\(\s*r\s*\))", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bpdf\b", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"^\s*[\[【(（]\s*(?:press release|news release|プレスリリース|お知らせ)\s*[\]】)）]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", text)


def _document_purpose_key(event: dict[str, Any]) -> str | None:
    text = unicodedata.normalize("NFKC", event.get("headline", "")).casefold()
    if "決算短信" in text or re.search(r"\b(?:financial results|earnings report)\b", text):
        periods = re.findall(
            r"(?:令和|平成)?\d+年|\d+月|中間|第?[1-4][q四半期]|通期|interim|annual|q[1-4]",
            text,
        )
        return "earnings_summary:" + ":".join(periods)
    return None


def _canonical_identity_keys(event: dict[str, Any]) -> dict[str, Any]:
    source_url = str(event["source_url"])
    parts = urlsplit(source_url)
    host = _canonical_host(source_url)
    segments: list[str] = []
    for raw_segment in parts.path.split("/"):
        segment = unicodedata.normalize("NFKC", raw_segment).casefold().strip()
        if not segment or segment in _LANGUAGE_PATH_TOKENS:
            continue
        segment = re.sub(r"_(?:en|eng|english|ja|jp)$", "", segment)
        segment = re.sub(r"\.(?:html?|pdf)$", "", segment)
        if segment == "index":
            continue
        segments.append(segment)
    page_key = f"{host}/{'/'.join(segments)}"
    material = unicodedata.normalize("NFKC", source_url).casefold()
    disclosure_ids = set(re.findall(r"20\d{12}", material))
    stable_items = {
        f"{host}:{key.casefold()}:{value.casefold()}"
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if key.casefold() in _STABLE_QUERY_IDS and re.fullmatch(r"[0-9A-Za-z._-]{1,100}", value)
    }
    semantic_query = sorted(
        (key.casefold(), value.casefold())
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if key.casefold() not in {"dispmid", "display", "lang", "language", "fbclid", "gclid"}
        and not key.casefold().startswith("utm_")
    )
    if semantic_query:
        page_key += "?" + "&".join(f"{key}={value}" for key, value in semantic_query)
    path_locator = segments[-1] if segments else ""
    return {
        "normalized_url": _normalized_url(source_url),
        "canonical_host": host,
        "canonical_page_key": page_key,
        "disclosure_document_ids": sorted(disclosure_ids),
        "stable_item_keys": sorted(stable_items),
        "path_locator": path_locator,
        "normalized_headline": _normalize_headline_identity(event.get("headline", "")),
        "document_purpose_key": _document_purpose_key(event),
    }


def _event_identity_evidence_v2(
    left: dict[str, Any], right: dict[str, Any], *, same_run: bool = False
) -> dict[str, Any]:
    left_keys = _canonical_identity_keys(left)
    right_keys = _canonical_identity_keys(right)
    same_ticker = left["ticker"] == right["ticker"]
    left_date = datetime.fromisoformat(left["published_at"]).date()
    right_date = datetime.fromisoformat(right["published_at"]).date()
    date_delta = abs((left_date - right_date).days)
    headline_similarity = _jaccard(
        _text_tokens(left_keys["normalized_headline"]),
        _text_tokens(right_keys["normalized_headline"]),
    )
    substance_similarity = _jaccard(
        _text_tokens(left.get("headline", "") + " " + left.get("summary", "")),
        _text_tokens(right.get("headline", "") + " " + right.get("summary", "")),
    )
    exact_headline = bool(
        left_keys["normalized_headline"]
        and left_keys["normalized_headline"] == right_keys["normalized_headline"]
    )
    headline_containment = bool(
        min(len(left_keys["normalized_headline"]), len(right_keys["normalized_headline"])) >= 12
        and (
            left_keys["normalized_headline"] in right_keys["normalized_headline"]
            or right_keys["normalized_headline"] in left_keys["normalized_headline"]
        )
    )
    document_id_match = bool(
        set(left_keys["disclosure_document_ids"])
        & set(right_keys["disclosure_document_ids"])
    )
    stable_item_match = bool(
        set(left_keys["stable_item_keys"]) & set(right_keys["stable_item_keys"])
    )
    normalized_url_match = left_keys["normalized_url"] == right_keys["normalized_url"]
    canonical_page_match = (
        bool(left_keys["canonical_page_key"])
        and left_keys["canonical_page_key"] == right_keys["canonical_page_key"]
    )
    purpose_match = bool(
        left_keys["document_purpose_key"]
        and left_keys["document_purpose_key"] == right_keys["document_purpose_key"]
    )
    same_issuer = bool(
        left_keys["canonical_host"]
        and left_keys["canonical_host"] == right_keys["canonical_host"]
    )
    locator_conflict = bool(
        same_issuer
        and left_keys["path_locator"]
        and right_keys["path_locator"]
        and left_keys["path_locator"] != right_keys["path_locator"]
    )
    identity_signals: list[str] = []
    if normalized_url_match:
        identity_signals.append("exact_normalized_url")
    if document_id_match:
        identity_signals.append("canonical_disclosure_document_id")
    if stable_item_match:
        identity_signals.append("stable_query_item_id")
    if canonical_page_match:
        identity_signals.append("canonical_bilingual_or_page_path")
    if exact_headline:
        identity_signals.append("exact_normalized_headline")
    if headline_containment and not exact_headline:
        identity_signals.append("strong_headline_containment")
    if purpose_match:
        identity_signals.append("same_document_purpose")
    conflicting_signals: list[str] = []
    if locator_conflict:
        conflicting_signals.append("different_same_host_path_locator")
    if left.get("category") != right.get("category"):
        conflicting_signals.append("category_annotation_differs")
    if left.get("importance") != right.get("importance"):
        conflicting_signals.append("importance_annotation_differs")
    if left.get("earnings_relevance") != right.get("earnings_relevance"):
        conflicting_signals.append("earnings_relevance_annotation_differs")

    tier = 0
    confidence = "LOW"
    auto_merge = False
    method = "insufficient_identity"
    score = max(headline_similarity, substance_similarity)
    if not same_ticker:
        method = "ticker_mismatch"
    elif normalized_url_match or document_id_match or stable_item_match or canonical_page_match:
        tier, confidence, auto_merge, score = 1, "HIGH", True, 1.0
        method = identity_signals[0]
    elif same_run:
        if date_delta <= 1 and exact_headline and substance_similarity >= 0.90:
            tier, confidence, auto_merge, score = 2, "HIGH", True, 0.95
            method = "same_run_exact_headline_near_identical_substance"
        elif date_delta <= 1 and (headline_similarity >= 0.55 or substance_similarity >= 0.60):
            tier, confidence, method = 3, "MEDIUM", "same_run_semantic_review_only"
    elif date_delta <= 1 and purpose_match and same_issuer:
        tier, confidence, auto_merge, score = 2, "HIGH", True, 0.94
        method = "same_issuer_document_purpose"
    elif date_delta <= 1 and exact_headline:
        tier, confidence, auto_merge, score = 2, "HIGH", True, max(0.93, substance_similarity)
        method = "exact_headline_strong_identity"
    elif date_delta <= 1 and headline_containment and substance_similarity >= 0.45:
        tier, confidence, auto_merge, score = 2, "HIGH", True, max(0.90, substance_similarity)
        method = "headline_containment_and_substance"
    elif (
        date_delta <= 1
        and not locator_conflict
        and headline_similarity >= 0.86
        and substance_similarity >= 0.70
    ):
        tier, confidence, auto_merge = 2, "HIGH", True
        method = "near_exact_headline_and_substance"
    elif (
        date_delta == 0
        and not locator_conflict
        and same_issuer
        and headline_similarity >= 0.75
        and substance_similarity >= 0.80
    ):
        tier, confidence, auto_merge = 2, "HIGH", True
        method = "same_issuer_strong_substance"
    elif date_delta <= 1 and headline_similarity >= 0.45 and substance_similarity >= 0.45:
        tier, confidence = 3, "MEDIUM"
        method = "probable_semantic_match_review_only"
    return {
        "same_ticker": same_ticker,
        "same_run": same_run,
        "tier": tier,
        "confidence": confidence,
        "auto_merge": auto_merge,
        "method": method,
        "score": score,
        "date_delta_days": date_delta,
        "headline_similarity": headline_similarity,
        "substance_similarity": substance_similarity,
        "identity_signals": identity_signals,
        "conflicting_signals": conflicting_signals,
        "left_canonical_keys": left_keys,
        "right_canonical_keys": right_keys,
        "category_equal": left.get("category") == right.get("category"),
        "importance_equal": left.get("importance") == right.get("importance"),
        "earnings_relevance_equal": left.get("earnings_relevance") == right.get("earnings_relevance"),
    }


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


def _quality_v2_dir(root: Path, benchmark_id: str) -> Path:
    return _benchmark_dir(root, benchmark_id) / "quality_v2"


def _event_public_fields(event: dict[str, Any]) -> dict[str, Any]:
    source_url = str(event["source_url"])
    return {
        "ticker": event["ticker"],
        "date": datetime.fromisoformat(event["published_at"]).date().isoformat(),
        "published_at": event["published_at"],
        "headline": event["headline"],
        "category": event["category"],
        "source_url": source_url,
        "normalized_source_url": _normalized_url(source_url),
        "source_domain": (urlsplit(source_url).hostname or "").lower(),
        "source_type": event.get("source_type", "news"),
        "importance": event.get("importance"),
        "earnings_relevance": event.get("earnings_relevance"),
        "summary": event.get("summary", ""),
        "why_it_matters": event.get("why_it_matters", ""),
    }


def _build_event_union(
    run_events: dict[str, dict[str, list[dict[str, Any]]]],
    company_names: dict[str, str],
) -> list[dict[str, Any]]:
    """Build deterministic cross-run match clusters; no verdict is inferred."""
    members: list[dict[str, Any]] = []
    run_order = list(QUALITY_RUN_LABELS)
    for run_name in run_order:
        label = QUALITY_RUN_LABELS[run_name]
        for ticker in sorted(run_events.get(run_name, {})):
            for event_index, event in enumerate(run_events[run_name][ticker]):
                members.append({
                    "run_name": run_name,
                    "run_label": label,
                    "event_index": event_index,
                    **_event_public_fields(event),
                })

    parent = list(range(len(members)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left in range(len(members)):
        for right in range(left + 1, len(members)):
            if _event_match_evidence(members[left], members[right])["matched"]:
                union(left, right)

    grouped: dict[int, list[int]] = defaultdict(list)
    for index in range(len(members)):
        grouped[find(index)].append(index)

    clusters: list[dict[str, Any]] = []
    for indexes in grouped.values():
        cluster_members = [members[index] for index in indexes]
        cluster_members.sort(key=lambda value: (
            run_order.index(value["run_name"]), value["ticker"], value["event_index"]
        ))
        representative = cluster_members[0]
        fingerprint = json.dumps(
            sorted(
                (
                    member["run_label"], member["ticker"], member["normalized_source_url"],
                    member["published_at"], member["headline"],
                )
                for member in cluster_members
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        cluster_id = f"event-{hashlib.sha256(fingerprint.encode()).hexdigest()[:16]}"
        evidence: list[dict[str, Any]] = []
        for left in range(len(cluster_members)):
            for right in range(left + 1, len(cluster_members)):
                match = _event_match_evidence(cluster_members[left], cluster_members[right])
                evidence.append({
                    "left_run": cluster_members[left]["run_label"],
                    "left_event_index": cluster_members[left]["event_index"],
                    "right_run": cluster_members[right]["run_label"],
                    "right_event_index": cluster_members[right]["event_index"],
                    **match,
                })
        found_by = {
            label: any(member["run_label"] == label for member in cluster_members)
            for label in QUALITY_RUN_LABELS.values()
        }
        clusters.append({
            "match_cluster_id": cluster_id,
            "ticker": representative["ticker"],
            "company": company_names.get(representative["ticker"], ""),
            "date": representative["date"],
            "headline": representative["headline"],
            "category": representative["category"],
            "source_url": representative["source_url"],
            "source_domain": representative["source_domain"],
            "source_type": representative["source_type"],
            "importance": representative["importance"],
            "earnings_relevance": representative["earnings_relevance"],
            "found_by": found_by,
            "matcher_evidence": evidence,
            "members": cluster_members,
            "verdict": "UNKNOWN",
            "automated_false_positive": False,
        })
    clusters.sort(key=lambda value: (value["ticker"], value["date"], value["match_cluster_id"]))
    return clusters


def _count_agreement(
    left: dict[str, float], right: dict[str, float], tickers: list[str]
) -> dict[str, Any]:
    differences = [abs(float(left.get(ticker, 0)) - float(right.get(ticker, 0))) for ticker in tickers]
    exact = sum(difference == 0 for difference in differences)
    left_values = [float(left.get(ticker, 0)) for ticker in tickers]
    right_values = [float(right.get(ticker, 0)) for ticker in tickers]
    return {
        "company_count": len(tickers),
        "exact_match_count": exact,
        "exact_match_rate": exact / len(tickers) if tickers else 1.0,
        "mean_absolute_difference": statistics.mean(differences) if differences else 0.0,
        "left_average": statistics.mean(left_values) if left_values else None,
        "right_average": statistics.mean(right_values) if right_values else None,
        "right_to_left_average_ratio": _ratio(
            statistics.mean(right_values) if right_values else None,
            statistics.mean(left_values) if left_values else None,
        ),
    }


def _reference_repeatability_metrics(
    left_events: dict[str, list[dict[str, Any]]],
    right_events: dict[str, list[dict[str, Any]]],
    tickers: list[str],
    *,
    left_source_counts: dict[str, float] | None = None,
    right_source_counts: dict[str, float] | None = None,
    company_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    ticker_set = set(tickers)
    left_to_right, _, left_ambiguous = _quality_metrics(left_events, right_events, ticker_set)
    right_to_left, _, right_ambiguous = _quality_metrics(right_events, left_events, ticker_set)
    left_news = {ticker for ticker in tickers if left_events.get(ticker)}
    right_news = {ticker for ticker in tickers if right_events.get(ticker)}
    both_news = left_news & right_news
    both_no_news = ticker_set - (left_news | right_news)
    clusters = _build_event_union(
        {"reference": left_events, "reference02": right_events}, company_names or {}
    )
    matched_clusters = [
        cluster for cluster in clusters
        if cluster["found_by"]["reference01"] and cluster["found_by"]["reference02"]
    ]
    left_only = [
        cluster for cluster in clusters
        if cluster["found_by"]["reference01"] and not cluster["found_by"]["reference02"]
    ]
    right_only = [
        cluster for cluster in clusters
        if cluster["found_by"]["reference02"] and not cluster["found_by"]["reference01"]
    ]
    stable = (
        left_to_right["event_recall"] >= 0.90
        and right_to_left["event_recall"] >= 0.90
        and left_to_right["critical_event_recall"] == 1.0
        and right_to_left["critical_event_recall"] == 1.0
    )
    if stable:
        decision = "PASS"
    elif min(left_to_right["event_recall"], right_to_left["event_recall"]) < 0.90:
        decision = "QUALITY_METRIC_NOT_STABLE"
    else:
        decision = "WARN"
    return {
        "decision": decision,
        "thresholds": {
            "critical_event_recall": 1.0,
            "minimum_overall_event_recall": 0.90,
            "target_overall_event_recall": 0.95,
        },
        "reference01_to_reference02": left_to_right,
        "reference02_to_reference01": right_to_left,
        "ambiguous_matches": {
            "reference01_to_reference02": left_ambiguous,
            "reference02_to_reference01": right_ambiguous,
        },
        "news_presence_agreement": {
            "both_news_present": len(both_news),
            "both_no_news": len(both_no_news),
            "reference01_only_news": sorted(left_news - right_news),
            "reference02_only_news": sorted(right_news - left_news),
            "news_present_agreement_rate": len(both_news) / len(tickers) if tickers else 1.0,
            "no_news_agreement_rate": len(both_no_news) / len(tickers) if tickers else 1.0,
            "agreement_rate": (len(both_news) + len(both_no_news)) / len(tickers) if tickers else 1.0,
        },
        "event_count_agreement": _count_agreement(
            {ticker: len(left_events.get(ticker, [])) for ticker in tickers},
            {ticker: len(right_events.get(ticker, [])) for ticker in tickers},
            tickers,
        ),
        "source_count_agreement": _count_agreement(
            left_source_counts or {}, right_source_counts or {}, tickers
        ),
        "jaccard_event_similarity": len(matched_clusters) / len(clusters) if clusters else 1.0,
        "matched_event_clusters": len(matched_clusters),
        "matched_events": matched_clusters,
        "reference01_only_events": left_only,
        "reference02_only_events": right_only,
    }


def _observation_id(member: dict[str, Any]) -> str:
    return f"{member['run_label']}:{member['ticker']}:{int(member['event_index']):04d}"


def _strict_dedupe_run_v2(
    run_name: str, events: dict[str, list[dict[str, Any]]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    label = QUALITY_RUN_LABELS[run_name]
    members: list[dict[str, Any]] = []
    for ticker in sorted(events):
        for event_index, event in enumerate(events[ticker]):
            member = {
                "run_name": run_name,
                "run_label": label,
                "event_index": event_index,
                **_event_public_fields(event),
            }
            member["observation_id"] = _observation_id(member)
            member["canonical_keys"] = _canonical_identity_keys(member)
            members.append(member)
    parent = list(range(len(members)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    accepted: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for left in range(len(members)):
        for right in range(left + 1, len(members)):
            if members[left]["ticker"] != members[right]["ticker"]:
                continue
            evidence = _event_identity_evidence_v2(members[left], members[right], same_run=True)
            pair = {
                "left_observation_id": members[left]["observation_id"],
                "right_observation_id": members[right]["observation_id"],
                **evidence,
            }
            if evidence["auto_merge"]:
                left_root, right_root = find(left), find(right)
                if left_root != right_root:
                    parent[right_root] = left_root
                accepted.append(pair)
            elif evidence["confidence"] == "MEDIUM":
                review.append(pair)
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, member in enumerate(members):
        grouped[find(index)].append(member)
    nodes: list[dict[str, Any]] = []
    for group_members in grouped.values():
        group_members.sort(key=lambda item: item["observation_id"])
        node_hash = hashlib.sha256(
            "\n".join(item["observation_id"] for item in group_members).encode()
        ).hexdigest()[:16]
        nodes.append({
            "node_id": f"node-{node_hash}",
            "run_name": run_name,
            "run_label": label,
            "ticker": group_members[0]["ticker"],
            "representative": group_members[0],
            "members": group_members,
        })
    nodes.sort(key=lambda item: item["node_id"])
    return nodes, accepted, review


def _node_identity_evidence_v2(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    options = [
        _event_identity_evidence_v2(left_member, right_member)
        for left_member in left["members"]
        for right_member in right["members"]
    ]
    return max(
        options,
        key=lambda value: (
            value["auto_merge"], -value["tier"] if value["tier"] else -99,
            value["score"], value["method"],
        ),
    )


def _matcher_review_record(
    left: dict[str, Any],
    right: dict[str, Any],
    evidence: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    left_event, right_event = left["representative"], right["representative"]
    confidence = "MEDIUM" if reason == "one_to_one_conflict" else evidence["confidence"]
    return {
        "left_run": left["run_label"],
        "right_run": right["run_label"],
        "ticker": left_event["ticker"],
        "date": [left_event["date"], right_event["date"]],
        "headline_left": left_event["headline"],
        "headline_right": right_event["headline"],
        "url_left": left_event["source_url"],
        "url_right": right_event["source_url"],
        "canonical_keys": {
            "left": evidence["left_canonical_keys"],
            "right": evidence["right_canonical_keys"],
        },
        "score": evidence["score"],
        "identity_signals": evidence["identity_signals"],
        "conflicting_signals": [*evidence["conflicting_signals"], reason],
        "confidence": confidence,
        "suggested_action": "human_gold_review",
        "matcher_method": evidence["method"],
    }


def _annotation_consistency_v2(clusters: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {"category": 0, "importance": 0, "earnings_relevance": 0}
    agreements = {key: 0 for key in totals}
    for cluster in clusters:
        representatives: list[dict[str, Any]] = []
        seen_runs: set[str] = set()
        for member in cluster["members"]:
            if member["run_label"] not in seen_runs:
                seen_runs.add(member["run_label"])
                representatives.append(member)
        for left in range(len(representatives)):
            for right in range(left + 1, len(representatives)):
                for key in totals:
                    totals[key] += 1
                    agreements[key] += int(representatives[left].get(key) == representatives[right].get(key))
    return {
        f"{key}_agreement": {
            "agreement_count": agreements[key],
            "comparison_count": totals[key],
            "rate": agreements[key] / totals[key] if totals[key] else 1.0,
        }
        for key in totals
    }


def _build_event_union_v2(
    run_events: dict[str, dict[str, list[dict[str, Any]]]],
    company_names: dict[str, str],
) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    within_accepted: list[dict[str, Any]] = []
    within_review: list[dict[str, Any]] = []
    for run_name in QUALITY_RUN_LABELS:
        run_nodes, accepted, review = _strict_dedupe_run_v2(
            run_name, run_events.get(run_name, {})
        )
        nodes.extend(run_nodes)
        within_accepted.extend(accepted)
        within_review.extend(review)
    nodes.sort(key=lambda item: item["node_id"])
    parent = list(range(len(nodes)))
    cluster_runs = [{node["run_name"]} for node in nodes]

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    edges: list[tuple[int, int, dict[str, Any]]] = []
    reviews: list[dict[str, Any]] = []
    for left in range(len(nodes)):
        for right in range(left + 1, len(nodes)):
            if nodes[left]["run_name"] == nodes[right]["run_name"]:
                continue
            if nodes[left]["ticker"] != nodes[right]["ticker"]:
                continue
            evidence = _node_identity_evidence_v2(nodes[left], nodes[right])
            if evidence["auto_merge"]:
                edges.append((left, right, evidence))
            elif evidence["confidence"] == "MEDIUM":
                reviews.append(_matcher_review_record(
                    nodes[left], nodes[right], evidence, reason="probable_not_auto_merged"
                ))
    edges.sort(key=lambda item: (
        item[2]["tier"], -item[2]["score"],
        nodes[item[0]]["node_id"], nodes[item[1]]["node_id"],
    ))
    accepted_edges: list[dict[str, Any]] = []
    for left, right, evidence in edges:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            continue
        if cluster_runs[left_root] & cluster_runs[right_root]:
            reviews.append(_matcher_review_record(
                nodes[left], nodes[right], evidence, reason="one_to_one_conflict"
            ))
            continue
        parent[right_root] = left_root
        cluster_runs[left_root] |= cluster_runs[right_root]
        accepted_edges.append({
            "left_node_id": nodes[left]["node_id"],
            "right_node_id": nodes[right]["node_id"],
            **evidence,
        })

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, node in enumerate(nodes):
        grouped[find(index)].append(node)
    clusters: list[dict[str, Any]] = []
    for cluster_nodes in grouped.values():
        members = [member for node in cluster_nodes for member in node["members"]]
        members.sort(key=lambda item: item["observation_id"])
        representative = members[0]
        cluster_hash = hashlib.sha256(
            "\n".join(member["observation_id"] for member in members).encode()
        ).hexdigest()[:16]
        cluster_node_ids = {node["node_id"] for node in cluster_nodes}
        evidence = [
            edge for edge in accepted_edges
            if edge["left_node_id"] in cluster_node_ids and edge["right_node_id"] in cluster_node_ids
        ]
        found_by = {
            label: any(member["run_label"] == label for member in members)
            for label in QUALITY_RUN_LABELS.values()
        }
        clusters.append({
            "match_cluster_id": f"event-v2-{cluster_hash}",
            "ticker": representative["ticker"],
            "company": company_names.get(representative["ticker"], ""),
            "date": representative["date"],
            "headline": representative["headline"],
            "category": representative["category"],
            "source_url": representative["source_url"],
            "source_domain": representative["source_domain"],
            "source_type": representative["source_type"],
            "importance": representative["importance"],
            "earnings_relevance": representative["earnings_relevance"],
            "found_by": found_by,
            "matcher_evidence": evidence,
            "members": members,
            "verdict": "UNKNOWN",
            "automated_false_positive": False,
        })
    clusters.sort(key=lambda item: (item["ticker"], item["date"], item["match_cluster_id"]))
    reviews.sort(key=lambda item: (
        item["ticker"], item["date"], item["left_run"], item["right_run"],
        item["headline_left"], item["headline_right"],
    ))
    return {
        "events": clusters,
        "matcher_merge_candidates": reviews,
        "accepted_cross_run_edges": accepted_edges,
        "within_run_dedupe_edges": within_accepted,
        "within_run_review_candidates": within_review,
        "annotation_consistency": _annotation_consistency_v2(clusters),
        "same_run_multi_member_cluster_count": sum(
            any(
                sum(member["run_label"] == run for member in cluster["members"]) > 1
                for run in QUALITY_RUN_LABELS.values()
            )
            for cluster in clusters
        ),
    }


def _pairwise_metrics_v2(
    left_run: str,
    right_run: str,
    left_events: dict[str, list[dict[str, Any]]],
    right_events: dict[str, list[dict[str, Any]]],
    tickers: list[str],
) -> dict[str, Any]:
    union = _build_event_union_v2(
        {left_run: left_events, right_run: right_events}, {}
    )
    left_label = QUALITY_RUN_LABELS[left_run]
    right_label = QUALITY_RUN_LABELS[right_run]
    left_clusters = [cluster for cluster in union["events"] if cluster["found_by"][left_label]]
    right_clusters = [cluster for cluster in union["events"] if cluster["found_by"][right_label]]
    matched = [
        cluster for cluster in union["events"]
        if cluster["found_by"][left_label] and cluster["found_by"][right_label]
    ]
    critical_left = []
    for cluster in left_clusters:
        left_member = next(
            member for member in cluster["members"] if member["run_label"] == left_label
        )
        if left_member.get("importance") == "high" or left_member.get("earnings_relevance") == "direct":
            critical_left.append(cluster)
    critical_matched = [cluster for cluster in critical_left if cluster["found_by"][right_label]]
    left_news = {ticker for ticker in tickers if left_events.get(ticker)}
    right_news = {ticker for ticker in tickers if right_events.get(ticker)}
    return {
        "left_run": left_label,
        "right_run": right_label,
        "left_event_count": len(left_clusters),
        "right_event_count": len(right_clusters),
        "matched_event_count": len(matched),
        "event_recall": len(matched) / len(left_clusters) if left_clusters else 1.0,
        "critical_event_count": len(critical_left),
        "critical_event_recall": len(critical_matched) / len(critical_left) if critical_left else 1.0,
        "precision_against_left": len(matched) / len(right_clusters) if right_clusters else (1.0 if not left_clusters else 0.0),
        "jaccard_event_similarity": (
            len(matched) / (len(left_clusters) + len(right_clusters) - len(matched))
            if left_clusters or right_clusters else 1.0
        ),
        "no_news_false_negative_tickers": sorted(left_news - right_news),
        "news_presence_agreement_rate": (
            sum((ticker in left_news) == (ticker in right_news) for ticker in tickers) / len(tickers)
            if tickers else 1.0
        ),
        "ambiguous_match_count": len(union["matcher_merge_candidates"]),
        "same_run_multi_member_cluster_count": union["same_run_multi_member_cluster_count"],
        "annotation_consistency": union["annotation_consistency"],
        "left_only_events": [
            cluster for cluster in left_clusters if not cluster["found_by"][right_label]
        ],
        "right_only_events": [
            cluster for cluster in right_clusters if not cluster["found_by"][left_label]
        ],
    }


def _candidate_metrics_from_union_v2(clusters: list[dict[str, Any]], run_label: str) -> dict[str, Any]:
    reference_clusters = [
        cluster for cluster in clusters
        if cluster["found_by"]["reference01"] or cluster["found_by"]["reference02"]
    ]
    candidate_clusters = [cluster for cluster in clusters if cluster["found_by"][run_label]]
    matched = [cluster for cluster in reference_clusters if cluster["found_by"][run_label]]
    critical = [
        cluster for cluster in reference_clusters
        if any(
            member["run_label"] in {"reference01", "reference02"}
            and (member.get("importance") == "high" or member.get("earnings_relevance") == "direct")
            for member in cluster["members"]
        )
    ]
    critical_matched = [cluster for cluster in critical if cluster["found_by"][run_label]]
    reference_news = {cluster["ticker"] for cluster in reference_clusters}
    candidate_news = {cluster["ticker"] for cluster in candidate_clusters}
    return {
        "quality_status": "PROVISIONAL_REFERENCE_UNION_NOT_GOLD",
        "reference_union_event_count": len(reference_clusters),
        "candidate_event_count": len(candidate_clusters),
        "matched_event_count": len(matched),
        "event_recall": len(matched) / len(reference_clusters) if reference_clusters else 1.0,
        "critical_event_count": len(critical),
        "critical_event_recall": len(critical_matched) / len(critical) if critical else 1.0,
        "precision_against_reference_union": (
            len(matched) / len(candidate_clusters)
            if candidate_clusters else (1.0 if not reference_clusters else 0.0)
        ),
        "no_news_false_negative_tickers": sorted(reference_news - candidate_news),
        "final_quality_requires_gold_v2": True,
    }


def _union_version_comparison(
    v1_clusters: list[dict[str, Any]], v2_clusters: list[dict[str, Any]]
) -> dict[str, Any]:
    v1_by_observation: dict[str, str] = {}
    v2_by_observation: dict[str, str] = {}
    for cluster in v1_clusters:
        for member in cluster["members"]:
            v1_by_observation[_observation_id(member)] = cluster["match_cluster_id"]
    for cluster in v2_clusters:
        for member in cluster["members"]:
            v2_by_observation[member["observation_id"]] = cluster["match_cluster_id"]
    v1_to_v2: dict[str, set[str]] = defaultdict(set)
    v2_to_v1: dict[str, set[str]] = defaultdict(set)
    for observation_id in sorted(set(v1_by_observation) & set(v2_by_observation)):
        v1_to_v2[v1_by_observation[observation_id]].add(v2_by_observation[observation_id])
        v2_to_v1[v2_by_observation[observation_id]].add(v1_by_observation[observation_id])
    merged_groups = [
        {"v2_cluster_id": cluster_id, "v1_cluster_ids": sorted(cluster_ids)}
        for cluster_id, cluster_ids in v2_to_v1.items() if len(cluster_ids) > 1
    ]
    split_groups = [
        {"v1_cluster_id": cluster_id, "v2_cluster_ids": sorted(cluster_ids)}
        for cluster_id, cluster_ids in v1_to_v2.items() if len(cluster_ids) > 1
    ]
    return {
        "union_v1_cluster_count": len(v1_clusters),
        "union_v2_cluster_count": len(v2_clusters),
        "false_split_groups_merged_count": len(merged_groups),
        "false_splits_merged_count": sum(len(group["v1_cluster_ids"]) - 1 for group in merged_groups),
        "overmerge_clusters_split_count": len(split_groups),
        "overmerge_splits_created_count": sum(len(group["v2_cluster_ids"]) - 1 for group in split_groups),
        "merged_groups": merged_groups,
        "split_groups": split_groups,
    }


def _overmerge_audit_v2(
    v1_clusters: list[dict[str, Any]], v2_clusters: list[dict[str, Any]]
) -> dict[str, Any]:
    def same_run_members(cluster: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for member in cluster["members"]:
            by_run[member["run_label"]].append(member)
        return {run: values for run, values in by_run.items() if len(values) > 1}

    legacy = []
    for cluster in v1_clusters:
        repeated = same_run_members(cluster)
        if repeated:
            legacy.append({
                "match_cluster_id": cluster["match_cluster_id"],
                "ticker": cluster["ticker"],
                "same_run_members": repeated,
            })
    current = []
    for cluster in v2_clusters:
        repeated = same_run_members(cluster)
        if repeated:
            current.append({
                "match_cluster_id": cluster["match_cluster_id"],
                "ticker": cluster["ticker"],
                "same_run_members": repeated,
                "matcher_evidence": cluster["matcher_evidence"],
            })
    return {
        "schema_version": MATCHER_REVIEW_SCHEMA,
        "legacy_v1_same_run_multi_member_cluster_count": len(legacy),
        "legacy_v1_same_run_multi_member_clusters": legacy,
        "v2_same_run_multi_member_cluster_count": len(current),
        "v2_same_run_multi_member_clusters": current,
        "bluebon_2208_legacy_overmerge_detected": any(item["ticker"] == "2208" for item in legacy),
    }


def _persistent_false_negative_audit(clusters: list[dict[str, Any]]) -> dict[str, Any]:
    matcher_miss_details: list[dict[str, Any]] = []
    for cluster in clusters:
        found = cluster["found_by"]
        if not found.get("reference01"):
            continue
        missing = [run for run in ("batch02", "batch04") if not found.get(run)]
        if not missing:
            continue
        reference_member = next(
            member for member in cluster["members"] if member["run_label"] == "reference01"
        )
        candidate_results: dict[str, Any] = {}
        for run in missing:
            candidates = [
                member
                for other in clusters
                for member in other["members"]
                if member["run_label"] == run and member["ticker"] == cluster["ticker"]
            ]
            if not candidates:
                candidate_results[run] = {
                    "result": "no_event_for_ticker",
                    "inferred_reason": "candidate returned items=[] or no valid event",
                    "events": [],
                }
                continue
            ranked = sorted(
                (
                    (_event_match_evidence(reference_member, candidate), candidate)
                    for candidate in candidates
                ),
                key=lambda value: (
                    value[0]["matched"], value[0]["score"],
                    value[0]["normalized_url_equal"], -value[0]["date_delta_days"],
                ),
            )
            best_evidence, _ = ranked[-1]
            candidate_results[run] = {
                "result": "different_events_for_ticker",
                "inferred_reason": best_evidence["method"],
                "best_matcher_evidence": best_evidence,
                "events": [
                    {
                        "headline": candidate["headline"],
                        "date": candidate["date"],
                        "category": candidate["category"],
                        "source_url": candidate["source_url"],
                    }
                    for candidate in candidates
                ],
            }
        matcher_miss_details.append({
            "ticker": cluster["ticker"],
            "company": cluster["company"],
            "match_cluster_id": cluster["match_cluster_id"],
            "classification": (
                "persistent_batch02_batch04_false_negative"
                if len(missing) == 2 else f"{missing[0]}_only_false_negative"
            ),
            "missing_from": missing,
            "reference_event": {
                "date": reference_member["date"],
                "headline": reference_member["headline"],
                "source_url": reference_member["source_url"],
                "source_type": reference_member["source_type"],
                "category": reference_member["category"],
                "importance": reference_member["importance"],
                "earnings_relevance": reference_member["earnings_relevance"],
            },
            "candidate_result": candidate_results,
        })
    reference_tickers = {
        cluster["ticker"] for cluster in clusters if cluster["found_by"].get("reference01")
    }
    candidate_tickers = {
        run: {cluster["ticker"] for cluster in clusters if cluster["found_by"].get(run)}
        for run in ("batch02", "batch04")
    }
    batch02_missing = reference_tickers - candidate_tickers["batch02"]
    batch04_missing = reference_tickers - candidate_tickers["batch04"]
    persistent = batch02_missing & batch04_missing
    batch02_only = batch02_missing - batch04_missing
    batch04_only = batch04_missing - batch02_missing
    classification_by_ticker = {
        **{ticker: "persistent_batch02_batch04_false_negative" for ticker in persistent},
        **{ticker: "batch02_only_false_negative" for ticker in batch02_only},
        **{ticker: "batch04_only_false_negative" for ticker in batch04_only},
    }
    event_level_details = []
    for detail in matcher_miss_details:
        classification = classification_by_ticker.get(detail["ticker"])
        if classification is None:
            continue
        company_missing = (
            ["batch02", "batch04"] if detail["ticker"] in persistent
            else ["batch02"] if detail["ticker"] in batch02_only
            else ["batch04"]
        )
        event_level_details.append({
            **detail,
            "classification": classification,
            "company_level_missing_from": company_missing,
        })
    return {
        "classification_basis": "reference-news ticker with no candidate event for the ticker",
        "event_level_details": event_level_details,
        "persistent_tickers": sorted(persistent),
        "batch02_only_tickers": sorted(batch02_only),
        "batch04_only_tickers": sorted(batch04_only),
        "matcher_event_miss_details": matcher_miss_details,
    }


def _gold_template(benchmark_id: str, union: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    union_hash = hashlib.sha256(
        json.dumps(union, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    events = []
    for cluster in union["events"]:
        events.append({
            key: cluster[key]
            for key in (
                "match_cluster_id", "ticker", "company", "date", "headline", "category",
                "source_url", "source_domain", "source_type", "importance",
                "earnings_relevance", "found_by",
            )
        } | {
            "verdict": "UNKNOWN",
            "rationale": "",
            "official_source_url": None,
            "adjudicator": None,
            "adjudicated_at": None,
        })
    return {
        "schema_version": GOLD_ADJUDICATION_SCHEMA,
        "benchmark_id": benchmark_id,
        "source_union_sha256": union_hash,
        "created_at": _iso(now),
        "primary_source_priority": [
            "company_ir", "company_press_release", "tdnet", "edinet_or_sec",
            "government", "exchange", "other_official_source",
        ],
        "allowed_verdicts": list(GOLD_VERDICTS),
        "events": events,
    }


def validate_gold_adjudication(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") != GOLD_ADJUDICATION_SCHEMA:
        raise BenchmarkError("invalid gold adjudication schema_version")
    if not isinstance(value.get("benchmark_id"), str) or not value["benchmark_id"]:
        raise BenchmarkError("gold adjudication benchmark_id is required")
    if not isinstance(value.get("events"), list):
        raise BenchmarkError("gold adjudication events must be an array")
    required = {
        "match_cluster_id", "ticker", "company", "date", "headline", "category",
        "source_url", "source_domain", "source_type", "importance",
        "earnings_relevance", "found_by", "verdict", "rationale",
        "official_source_url", "adjudicator", "adjudicated_at",
    }
    seen: set[str] = set()
    expected_found_by = set(QUALITY_RUN_LABELS.values())
    for index, event in enumerate(value["events"]):
        if not isinstance(event, dict) or not required.issubset(event):
            raise BenchmarkError(f"gold events[{index}] is missing required fields")
        cluster_id = event["match_cluster_id"]
        if not isinstance(cluster_id, str) or not cluster_id or cluster_id in seen:
            raise BenchmarkError(f"gold events[{index}].match_cluster_id must be unique")
        seen.add(cluster_id)
        if event["verdict"] not in GOLD_VERDICTS:
            raise BenchmarkError(f"gold events[{index}].verdict is invalid")
        if set(event["found_by"]) != expected_found_by or any(
            not isinstance(flag, bool) for flag in event["found_by"].values()
        ):
            raise BenchmarkError(f"gold events[{index}].found_by is invalid")
        if not str(event["source_url"]).startswith(("http://", "https://")):
            raise BenchmarkError(f"gold events[{index}].source_url must be HTTP(S)")
        if event["verdict"] != "UNKNOWN":
            if not isinstance(event["rationale"], str) or not event["rationale"].strip():
                raise BenchmarkError(f"gold events[{index}].rationale is required after adjudication")
            if not isinstance(event["adjudicator"], str) or not event["adjudicator"].strip():
                raise BenchmarkError(f"gold events[{index}].adjudicator is required after adjudication")
            try:
                adjudicated_at = datetime.fromisoformat(str(event["adjudicated_at"]))
            except (TypeError, ValueError) as exc:
                raise BenchmarkError(f"gold events[{index}].adjudicated_at is invalid") from exc
            if adjudicated_at.tzinfo is None:
                raise BenchmarkError(f"gold events[{index}].adjudicated_at must include timezone")
        official_url = event.get("official_source_url")
        if official_url is not None and not str(official_url).startswith(("http://", "https://")):
            raise BenchmarkError(f"gold events[{index}].official_source_url must be HTTP(S) or null")
    return value


def _gold_metrics(value: dict[str, Any]) -> dict[str, Any]:
    validate_gold_adjudication(value)
    unknown = [event["match_cluster_id"] for event in value["events"] if event["verdict"] == "UNKNOWN"]
    if unknown:
        return {
            "available": False,
            "decision": "INCOMPLETE_ADJUDICATION",
            "unknown_event_count": len(unknown),
            "unknown_match_cluster_ids": unknown,
            "runs": {},
        }
    material = [event for event in value["events"] if event["verdict"] == "VALID_MATERIAL"]
    critical = [
        event for event in material
        if event["importance"] == "high" or event["earnings_relevance"] == "direct"
    ]
    material_tickers = {event["ticker"] for event in material}
    precision_verdicts = {
        "VALID_MATERIAL", "VALID_NON_MATERIAL", "INVALID_SOURCE", "OUT_OF_WINDOW"
    }
    runs: dict[str, Any] = {}
    for run in QUALITY_RUN_LABELS.values():
        found_material = [event for event in material if event["found_by"][run]]
        found_critical = [event for event in critical if event["found_by"][run]]
        precision_denominator = [
            event for event in value["events"]
            if event["found_by"][run] and event["verdict"] in precision_verdicts
        ]
        found_material_tickers = {event["ticker"] for event in found_material}
        found_any_tickers = {
            event["ticker"] for event in value["events"] if event["found_by"][run]
        }
        material_false_negative_tickers = sorted(material_tickers - found_material_tickers)
        no_news_false_negative_tickers = sorted(material_tickers - found_any_tickers)
        precision = (
            len(found_material) / len(precision_denominator)
            if precision_denominator else (1.0 if not material else 0.0)
        )
        runs[run] = {
            "gold_material_event_count": len(material),
            "found_gold_material_event_count": len(found_material),
            "gold_event_recall": len(found_material) / len(material) if material else 1.0,
            "critical_gold_event_count": len(critical),
            "critical_gold_event_recall": len(found_critical) / len(critical) if critical else 1.0,
            "precision_denominator_count": len(precision_denominator),
            "precision": precision,
            "gold_material_precision": precision,
            "material_company_false_negative_count": len(material_false_negative_tickers),
            "material_company_false_negative_tickers": material_false_negative_tickers,
            "no_news_false_negative_count": len(no_news_false_negative_tickers),
            "no_news_false_negative_tickers": no_news_false_negative_tickers,
        }
    return {
        "available": True,
        "decision": "COMPLETE",
        "unknown_event_count": 0,
        "gold_material_event_count": len(material),
        "critical_gold_event_count": len(critical),
        "runs": runs,
    }


def _completed_run_events(
    root: Path, benchmark_id: str, run_name: str
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    directory = _run_dir(root, benchmark_id, run_name)
    manifest = _read_json(directory / "manifest.json")
    status = benchmark_status(root, benchmark_id, run_name)
    if status["pending"] != 0 or status["failed"] != 0:
        raise BenchmarkError(f"{run_name} must complete without failures before quality v2 audit")
    events, _, invalid, errors = _load_success_outputs(Path(manifest["output_directory"]))
    if invalid:
        raise BenchmarkError(f"{run_name} contains invalid output: {'; '.join(errors)}")
    return manifest, events, _telemetry(directory, "companies.jsonl")


def audit_reference_repeatability(
    root: Path, benchmark_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    left_manifest, left_events, left_company = _completed_run_events(
        root, benchmark_id, "reference"
    )
    right_manifest, right_events, right_company = _completed_run_events(
        root, benchmark_id, "reference02"
    )
    left_order = [item["ticker"] for item in _ordered_run_assignments(
        _run_dir(root, benchmark_id, "reference")
    )]
    right_order = [item["ticker"] for item in _ordered_run_assignments(
        _run_dir(root, benchmark_id, "reference02")
    )]
    if left_order != right_order:
        raise BenchmarkError("reference01/reference02 company order differs")
    if (
        left_manifest.get("search_from") != right_manifest.get("search_from")
        or left_manifest.get("search_to") != right_manifest.get("search_to")
    ):
        raise BenchmarkError("reference01/reference02 search window differs")
    left_sources = {record["ticker"]: float(record["source_count"]) for record in left_company}
    right_sources = {record["ticker"]: float(record["source_count"]) for record in right_company}
    if set(left_sources) != set(left_order) or set(right_sources) != set(right_order):
        raise BenchmarkError("reference01/reference02 company telemetry is incomplete")
    names = {company["ticker"]: company["company_name"] for company in _load_cohort(root, benchmark_id)["companies"]}
    result = {
        "schema_version": REPEATABILITY_SCHEMA,
        "benchmark_id": benchmark_id,
        "captured_at": _iso(now),
        "runs": ["reference", "reference02"],
        "batch_size": 1,
        "company_count": len(left_order),
        "same_company_order": True,
        "search_from": left_manifest["search_from"],
        "search_to": left_manifest["search_to"],
        **_reference_repeatability_metrics(
            left_events,
            right_events,
            left_order,
            left_source_counts=left_sources,
            right_source_counts=right_sources,
            company_names=names,
        ),
    }
    atomic_write_json(_quality_v2_dir(root, benchmark_id) / "reference_repeatability.json", result)
    return result


def build_quality_union(
    root: Path, benchmark_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    cohort = _load_cohort(root, benchmark_id)
    reference_tickers = set(cohort["reference_tickers"])
    run_events: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for run_name in QUALITY_RUN_LABELS:
        _, events, _ = _completed_run_events(root, benchmark_id, run_name)
        run_events[run_name] = {
            ticker: values for ticker, values in events.items() if ticker in reference_tickers
        }
    company_names = {
        company["ticker"]: company["company_name"] for company in cohort["companies"]
    }
    clusters = _build_event_union(run_events, company_names)
    union = {
        "schema_version": EVENT_UNION_SCHEMA,
        "benchmark_id": benchmark_id,
        "created_at": _iso(now),
        "runs": list(QUALITY_RUN_LABELS.values()),
        "search_from": cohort["search_from"],
        "search_to": cohort["search_to"],
        "company_count": len(reference_tickers),
        "event_cluster_count": len(clusters),
        "matcher_contract": {
            "primary": "ticker + normalized URL",
            "secondary": "ticker + exact date + category + headline/substance Jaccard >= 0.55",
            "headline_only_matching_forbidden": True,
        },
        "candidate_only_events_are_not_false_positives": True,
        "events": clusters,
    }
    union["persistent_false_negative_audit"] = _persistent_false_negative_audit(clusters)
    directory = _quality_v2_dir(root, benchmark_id)
    union_path = directory / "event_union.json"
    gold_path = directory / "gold_adjudication.json"
    if union_path.exists() or gold_path.exists():
        raise BenchmarkError("quality v2 union/adjudication already exists; refusing overwrite")
    atomic_write_json(union_path, union)
    gold = _gold_template(benchmark_id, union, now=now)
    atomic_write_json(gold_path, gold)
    return {
        "schema_version": EVENT_UNION_SCHEMA,
        "benchmark_id": benchmark_id,
        "event_cluster_count": len(clusters),
        "event_union_path": str(union_path.resolve()),
        "gold_adjudication_path": str(gold_path.resolve()),
        "candidate_only_events_are_not_false_positives": True,
        "persistent_false_negative_audit": union["persistent_false_negative_audit"],
    }


def _quality_run_events(
    root: Path, benchmark_id: str
) -> tuple[dict[str, Any], dict[str, dict[str, list[dict[str, Any]]]]]:
    cohort = _load_cohort(root, benchmark_id)
    reference_tickers = set(cohort["reference_tickers"])
    run_events: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for run_name in QUALITY_RUN_LABELS:
        _, events, _ = _completed_run_events(root, benchmark_id, run_name)
        run_events[run_name] = {
            ticker: values for ticker, values in events.items() if ticker in reference_tickers
        }
    return cohort, run_events


def rebuild_quality_union_v2(
    root: Path, benchmark_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    cohort, run_events = _quality_run_events(root, benchmark_id)
    company_names = {
        company["ticker"]: company["company_name"] for company in cohort["companies"]
    }
    matcher = _build_event_union_v2(run_events, company_names)
    v1_path = _quality_v2_dir(root, benchmark_id) / "event_union.json"
    v1_union = _read_json(v1_path)
    comparison = _union_version_comparison(v1_union["events"], matcher["events"])
    persistent = _persistent_false_negative_audit(matcher["events"])
    tickers = [company["ticker"] for company in cohort["companies"] if company["ticker"] in set(cohort["reference_tickers"])]
    pairwise = {
        "batch02_vs_reference01": _pairwise_metrics_v2(
            "reference", "batch02", run_events["reference"], run_events["batch02"], tickers
        ),
        "batch02_vs_reference02": _pairwise_metrics_v2(
            "reference02", "batch02", run_events["reference02"], run_events["batch02"], tickers
        ),
        "batch04_vs_reference01": _pairwise_metrics_v2(
            "reference", "batch04", run_events["reference"], run_events["batch04"], tickers
        ),
        "batch04_vs_reference02": _pairwise_metrics_v2(
            "reference02", "batch04", run_events["reference02"], run_events["batch04"], tickers
        ),
    }
    telemetry_facts: dict[str, Any] = {}
    for run_name in QUALITY_RUN_LABELS:
        audit = _read_json(_run_dir(root, benchmark_id, run_name) / "audit.json")
        telemetry_facts[QUALITY_RUN_LABELS[run_name]] = {
            "average_source_count_per_company": audit.get("research", {}).get("average_source_count_per_company"),
            "company_research_elapsed_seconds": audit.get("runtime", {}).get("company_research_elapsed_seconds"),
        }
    core = {
        "schema_version": EVENT_UNION_V2_SCHEMA,
        "matcher_schema_version": MATCHER_V2_SCHEMA,
        "benchmark_id": benchmark_id,
        "runs": list(QUALITY_RUN_LABELS.values()),
        "search_from": cohort["search_from"],
        "search_to": cohort["search_to"],
        "company_count": len(tickers),
        "event_cluster_count": len(matcher["events"]),
        "processing_order": [
            "strict within-run dedupe", "canonical identity keys", "cross-run candidates",
            "HIGH-confidence auto match", "deterministic one-to-one assignment",
            "union clusters", "MEDIUM-confidence review artifact",
        ],
        "identity_contract": {
            "ticker_is_hard_requirement": True,
            "category_is_identity_gate": False,
            "importance_is_identity_signal": False,
            "earnings_relevance_is_identity_signal": False,
            "same_run_semantic_only_merge_forbidden": True,
            "only_high_confidence_is_auto_merged": True,
        },
        "version_comparison": comparison,
        "annotation_consistency": matcher["annotation_consistency"],
        "candidate_matching_metrics": {
            "pairwise": pairwise,
            "reference_union": {
                "batch02": _candidate_metrics_from_union_v2(matcher["events"], "batch02"),
                "batch04": _candidate_metrics_from_union_v2(matcher["events"], "batch04"),
            },
            "final_quality_requires_gold_v2": True,
        },
        "immutable_run_telemetry_facts": telemetry_facts,
        "run_telemetry_mutated": False,
        "persistent_false_negative_audit": persistent,
        "candidate_only_events_are_not_false_positives": True,
        "same_run_multi_member_cluster_count": matcher["same_run_multi_member_cluster_count"],
        "events": matcher["events"],
    }
    deterministic_hash = hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    union = {
        **core,
        "created_at": _iso(now),
        "deterministic_content_sha256": deterministic_hash,
    }
    merge_candidates = {
        "schema_version": MATCHER_REVIEW_SCHEMA,
        "matcher_schema_version": MATCHER_V2_SCHEMA,
        "benchmark_id": benchmark_id,
        "created_at": _iso(now),
        "candidate_count": len(matcher["matcher_merge_candidates"]),
        "only_high_confidence_auto_merged": True,
        "candidates": matcher["matcher_merge_candidates"],
    }
    overmerge = {
        **_overmerge_audit_v2(v1_union["events"], matcher["events"]),
        "benchmark_id": benchmark_id,
        "created_at": _iso(now),
        "version_comparison": comparison,
    }
    matcher_directory = _quality_v2_dir(root, benchmark_id) / "matcher_v2"
    output_paths = {
        "event_union_v2": matcher_directory / "event_union_v2.json",
        "gold_adjudication_v2": matcher_directory / "gold_adjudication_v2.json",
        "matcher_merge_candidates": matcher_directory / "matcher_merge_candidates.json",
        "matcher_overmerge_audit": matcher_directory / "matcher_overmerge_audit.json",
    }
    if any(path.exists() for path in output_paths.values()):
        raise BenchmarkError("matcher v2 output already exists; refusing overwrite")
    atomic_write_json(output_paths["event_union_v2"], union)
    atomic_write_json(output_paths["matcher_merge_candidates"], merge_candidates)
    atomic_write_json(output_paths["matcher_overmerge_audit"], overmerge)
    gold = _gold_template(benchmark_id, union, now=now)
    gold["matcher_schema_version"] = MATCHER_V2_SCHEMA
    gold["source_union_path"] = str(output_paths["event_union_v2"].resolve())
    atomic_write_json(output_paths["gold_adjudication_v2"], gold)
    return {
        "schema_version": EVENT_UNION_V2_SCHEMA,
        "benchmark_id": benchmark_id,
        "union_v1_cluster_count": comparison["union_v1_cluster_count"],
        "union_v2_cluster_count": comparison["union_v2_cluster_count"],
        "false_splits_merged_count": comparison["false_splits_merged_count"],
        "overmerge_clusters_split_count": comparison["overmerge_clusters_split_count"],
        "ambiguous_count": len(matcher["matcher_merge_candidates"]),
        "annotation_consistency": matcher["annotation_consistency"],
        "candidate_matching_metrics": core["candidate_matching_metrics"],
        "output_paths": {key: str(path.resolve()) for key, path in output_paths.items()},
        "existing_v1_artifacts_modified": False,
        "new_web_research": False,
        "final_quality_requires_gold_v2": True,
    }


def audit_reference_repeatability_v2(
    root: Path, benchmark_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    cohort, run_events = _quality_run_events(root, benchmark_id)
    tickers = [
        company["ticker"] for company in cohort["companies"]
        if company["ticker"] in set(cohort["reference_tickers"])
    ]
    forward = _pairwise_metrics_v2(
        "reference", "reference02", run_events["reference"], run_events["reference02"], tickers
    )
    reverse = _pairwise_metrics_v2(
        "reference02", "reference", run_events["reference02"], run_events["reference"], tickers
    )
    old_path = _quality_v2_dir(root, benchmark_id) / "reference_repeatability.json"
    old = _read_json(old_path)
    stable = (
        forward["event_recall"] >= 0.90
        and reverse["event_recall"] >= 0.90
        and forward["critical_event_recall"] == 1.0
        and reverse["critical_event_recall"] == 1.0
    )
    decision = "PASS" if stable else (
        "QUALITY_METRIC_NOT_STABLE"
        if min(forward["event_recall"], reverse["event_recall"]) < 0.90 else "WARN"
    )
    result = {
        "schema_version": REPEATABILITY_V2_SCHEMA,
        "matcher_schema_version": MATCHER_V2_SCHEMA,
        "benchmark_id": benchmark_id,
        "created_at": _iso(now),
        "decision": decision,
        "all_event_repeatability_not_material_gold_recall": True,
        "thresholds": {
            "overall_event_recall": 0.90,
            "critical_event_recall": 1.0,
        },
        "repeatability_v1": {
            "reference01_to_reference02": old["reference01_to_reference02"],
            "reference02_to_reference01": old["reference02_to_reference01"],
            "jaccard_event_similarity": old["jaccard_event_similarity"],
            "news_presence_agreement": old["news_presence_agreement"],
        },
        "repeatability_v2": {
            "reference01_to_reference02": forward,
            "reference02_to_reference01": reverse,
            "jaccard_event_similarity": forward["jaccard_event_similarity"],
            "news_presence_agreement_rate": forward["news_presence_agreement_rate"],
            "same_run_overmerge_count": forward["same_run_multi_member_cluster_count"],
            "ambiguous_count": forward["ambiguous_match_count"],
            "annotation_consistency": forward["annotation_consistency"],
        },
        "source_count_agreement": old.get("source_count_agreement"),
        "news_presence_requires_separate_review": True,
    }
    output = _quality_v2_dir(root, benchmark_id) / "matcher_v2" / "reference_repeatability_v2.json"
    if output.exists():
        raise BenchmarkError("reference repeatability v2 output already exists; refusing overwrite")
    atomic_write_json(output, result)
    return result


def _matcher_v2_directory(root: Path, benchmark_id: str) -> Path:
    return _quality_v2_dir(root, benchmark_id) / "matcher_v2"


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _packet_sha256(packet: dict[str, Any]) -> str:
    core = {key: value for key, value in packet.items() if key != "packet_sha256"}
    return _json_sha256(core)


def strategy_v2_known_gold_metrics(
    gold: dict[str, Any],
    discovered_gold_stages: dict[str, str],
    candidate_only_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Score known Gold only; novel candidates remain unscored until blind review."""
    by_id, groups = _gold_normalized_groups_v2(gold)
    material = {
        root_id for root_id in groups if by_id[root_id]["verdict"] == "VALID_MATERIAL"
    }
    critical = {
        root_id for root_id in material
        if by_id[root_id].get("importance") == "high"
        or by_id[root_id].get("earnings_relevance") == "direct"
    }
    official = {
        root_id for root_id in material
        if _gold_source_bucket(str(
            by_id[root_id].get("primary_source_type")
            or by_id[root_id].get("source_type") or "none"
        )) in {"official_company", "ir_tdnet", "government"}
    }
    ir_tdnet = {
        root_id for root_id in material
        if str(by_id[root_id].get("primary_source_type") or by_id[root_id].get("source_type"))
        in {"company_ir", "tdnet", "edinet"}
    }
    discovered = set(discovered_gold_stages)
    unknown_ids = discovered - set(groups)
    if unknown_ids:
        raise BenchmarkError(f"known-Gold stage map contains unknown clusters: {sorted(unknown_ids)}")
    invalid_stages = set(discovered_gold_stages.values()) - {
        "official_first", "targeted_second_pass", "existing_general"
    }
    if invalid_stages:
        raise BenchmarkError(f"invalid discovery stage: {sorted(invalid_stages)}")
    baseline_found = {
        root_id for root_id in material
        if any(member.get("found_by", {}).get("batch04") for member in groups[root_id])
    }

    def recall(target: set[str]) -> dict[str, Any]:
        found = target & discovered
        return {
            "target_count": len(target), "found": len(found),
            "missed": len(target - discovered),
            "recall": len(found) / len(target) if target else 1.0,
        }

    rescues = material & discovered - baseline_found
    return {
        "quality_status": "KNOWN_GOLD_RECALL_PROVISIONAL_PRECISION",
        "known_gold_material": recall(material),
        "known_gold_critical": recall(critical),
        "known_gold_official": recall(official),
        "known_gold_ir_tdnet": recall(ir_tdnet),
        "baseline_batch04_material_count": len(baseline_found),
        "baseline_regression_cluster_ids": sorted(baseline_found - discovered),
        "rescued_cluster_ids": sorted(rescues),
        "rescue_stage_counts": {
            stage: sum(discovered_gold_stages[root_id] == stage for root_id in rescues)
            for stage in ("official_first", "targeted_second_pass", "existing_general")
        },
        "candidate_only_event_count": len(candidate_only_events),
        "candidate_only_events_are_false_positives": False,
        "precision": None,
        "precision_status": "PROVISIONAL_PENDING_GOLD_EXTENSION",
    }


def build_gold_extension_packet_v2(
    benchmark_id: str, candidate_only_events: list[dict[str, Any]]
) -> dict[str, Any]:
    """Create a deterministic blind packet containing novel candidates only."""
    events: list[dict[str, Any]] = []
    for index, event in enumerate(candidate_only_events):
        required = {
            "ticker", "company", "date", "headline", "category", "source_url",
            "source_type", "importance", "earnings_relevance", "summary", "why_it_matters",
        }
        if not isinstance(event, dict) or not required.issubset(event):
            raise BenchmarkError(f"candidate-only event {index} is incomplete")
        if not str(event["source_url"]).startswith(("http://", "https://")):
            raise BenchmarkError(f"candidate-only event {index} source_url must be HTTP(S)")
        identity = _json_sha256({key: event[key] for key in sorted(required)})[:16]
        events.append({
            "cluster_id": f"gold-extension-{identity}",
            "ticker": event["ticker"],
            "company": event["company"],
            "date": event["date"],
            "canonical_headline": event["headline"],
            "category": event["category"],
            "importance": event["importance"],
            "earnings_relevance": event["earnings_relevance"],
            "summary": event["summary"],
            "why_it_matters": event["why_it_matters"],
            "source_urls": [{"url": event["source_url"], "source_type": event["source_type"]}],
        })
    events.sort(key=lambda item: (str(item["ticker"]), str(item["date"]), item["cluster_id"]))
    core = {
        "schema_version": GOLD_EXTENSION_PACKET_SCHEMA,
        "benchmark_id": benchmark_id,
        "packet_purpose": "blind_adjudication_of_candidate_only_events",
        "existing_gold_events_reincluded": False,
        "events": events,
    }
    packet_sha = _json_sha256(core)
    packet = {
        **core,
        "packet_id": f"gold_extension_{packet_sha[:16]}",
        "packet_sha256": packet_sha,
    }
    forbidden = {"found_by", "run_name", "run_label", "members", "matcher_evidence", "verdict"}
    if forbidden & set(json.dumps(packet, ensure_ascii=False).lower().split('"')):
        raise BenchmarkError("Gold extension packet is not blind")
    return packet


def _partition_gold_packet_groups(
    groups: list[tuple[str, list[dict[str, Any]]]],
) -> list[list[tuple[str, list[dict[str, Any]]]]]:
    """Partition contiguous ticker groups into deterministic 8-10 event packets."""
    cache: dict[int, list[list[tuple[str, list[dict[str, Any]]]]] | None] = {}

    def solve(index: int) -> list[list[tuple[str, list[dict[str, Any]]]]] | None:
        if index == len(groups):
            return []
        if index in cache:
            return cache[index]
        total = 0
        choices: list[list[list[tuple[str, list[dict[str, Any]]]]]] = []
        for end in range(index + 1, len(groups) + 1):
            total += len(groups[end - 1][1])
            if total > GOLD_PACKET_MAX_EVENTS:
                break
            if total < GOLD_PACKET_MIN_EVENTS:
                continue
            remainder = solve(end)
            if remainder is not None:
                choices.append([groups[index:end], *remainder])
        if not choices:
            cache[index] = None
        else:
            cache[index] = min(
                choices,
                key=lambda packets: (
                    len(packets),
                    sum(abs(sum(len(events) for _, events in packet) - 9) for packet in packets),
                    tuple(sum(len(events) for _, events in packet) for packet in packets),
                ),
            )
        return cache[index]

    result = solve(0)
    if result is None:
        raise BenchmarkError("cannot partition Gold events into 8-10 event company-safe packets")
    return result


def _blind_gold_event(cluster: dict[str, Any]) -> dict[str, Any]:
    members = sorted(cluster.get("members", []), key=lambda item: item["observation_id"])
    if not members:
        raise BenchmarkError(f"Gold cluster {cluster.get('match_cluster_id')} has no members")
    representative = members[0]
    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for member in members:
        url = str(member["source_url"])
        if url in seen_urls:
            continue
        seen_urls.add(url)
        sources.append({"url": url, "source_type": str(member["source_type"])})
    return {
        "cluster_id": cluster["match_cluster_id"],
        "ticker": cluster["ticker"],
        "company": cluster["company"],
        "event_date": cluster["date"],
        "canonical_headline": cluster["headline"],
        "summary": representative.get("summary", ""),
        "why_it_matters": representative.get("why_it_matters", ""),
        "source_urls": sources,
    }


def _gold_response_contract() -> dict[str, Any]:
    return {
        "schema_version": GOLD_RESPONSE_SCHEMA,
        "verdicts": list(GOLD_VERDICTS),
        "earnings_channels": list(GOLD_EARNINGS_CHANNELS),
        "directions": list(GOLD_DIRECTIONS),
        "confidences": list(GOLD_CONFIDENCES),
        "source_types": list(GOLD_SOURCE_TYPES),
        "required_item_fields": [
            "cluster_id", "verdict", "materiality_reason", "event_exists",
            "event_within_window", "company_specific", "duplicate_of_cluster_id",
            "primary_source_url", "primary_source_type", "primary_source_verified",
            "original_source_verified", "earnings_channel", "direction", "confidence",
            "notes", "evidence",
        ],
        "evidence_fields": ["url", "source_type", "supports"],
        "complete_packet_required": True,
    }


def _expected_gold_packets(union: dict[str, Any]) -> list[dict[str, Any]]:
    ordered = sorted(
        union["events"],
        key=lambda item: (item["ticker"], item["date"], item["match_cluster_id"]),
    )
    grouped: list[tuple[str, list[dict[str, Any]]]] = []
    for cluster in ordered:
        if grouped and grouped[-1][0] == cluster["ticker"]:
            grouped[-1][1].append(cluster)
        else:
            grouped.append((cluster["ticker"], [cluster]))
    partitions = _partition_gold_packet_groups(grouped)
    packets: list[dict[str, Any]] = []
    for index, partition in enumerate(partitions, start=1):
        events = [
            _blind_gold_event(cluster)
            for _, company_events in partition
            for cluster in company_events
        ]
        core = {
            "schema_version": GOLD_PACKET_SCHEMA,
            "benchmark_id": union["benchmark_id"],
            "packet_id": f"gold_packet_{index:03d}",
            "search_from": union["search_from"],
            "search_to": union["search_to"],
            "event_count": len(events),
            "blind_adjudication": True,
            "materiality_definition": (
                "VALID_MATERIAL can reasonably update an investor's assessment of revenue, "
                "profit, margin, orders, utilization, capacity, price, costs, customers, supply "
                "chain, regulation, finance, capital allocation, M&A, strategy, or competitiveness."
            ),
            "source_priority": [
                "company_ir", "company_news", "tdnet", "edinet", "government",
                "customer_supplier_official", "industry_association", "reliable_news",
                "secondary",
            ],
            "response_contract": _gold_response_contract(),
            "events": events,
        }
        packets.append({**core, "packet_sha256": _json_sha256(core)})
    return packets


def _assert_blind_packet(packet: dict[str, Any]) -> None:
    forbidden = {
        "found_by", "reference", "reference01", "reference02", "batch02", "batch04",
        "run_name", "run_label", "candidate_presence", "members", "matcher_evidence",
    }

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            overlap = forbidden & set(value)
            if overlap:
                raise BenchmarkError(f"Gold packet leaks blind fields: {sorted(overlap)}")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(packet)
    serialized = json.dumps(packet, ensure_ascii=False).casefold()
    leaked_labels = [
        label for label in ("reference01", "reference02", "batch02", "batch04")
        if label in serialized
    ]
    if leaked_labels:
        raise BenchmarkError(f"Gold packet leaks run labels: {leaked_labels}")


def build_gold_packets_v2(
    root: Path, benchmark_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    del now  # Packet content intentionally has no clock-dependent fields.
    matcher_directory = _matcher_v2_directory(root, benchmark_id)
    union_path = matcher_directory / "event_union_v2.json"
    union = _read_json(union_path)
    if union.get("schema_version") != EVENT_UNION_V2_SCHEMA:
        raise BenchmarkError("invalid matcher v2 event union")
    packets = _expected_gold_packets(union)
    for packet in packets:
        _assert_blind_packet(packet)
        if _packet_sha256(packet) != packet["packet_sha256"]:
            raise BenchmarkError("internal Gold packet SHA mismatch")
    cluster_ids = [event["cluster_id"] for packet in packets for event in packet["events"]]
    union_ids = [event["match_cluster_id"] for event in union["events"]]
    if len(cluster_ids) != len(set(cluster_ids)) or set(cluster_ids) != set(union_ids):
        raise BenchmarkError("Gold packet coverage must contain every cluster exactly once")
    packet_directory = matcher_directory / "gold_packets"
    blind_manifest_path = packet_directory / "manifest.json"
    provenance_path = matcher_directory / "gold_packet_provenance_manifest.json"
    blind_manifest = {
        "schema_version": GOLD_PACKET_MANIFEST_SCHEMA,
        "benchmark_id": benchmark_id,
        "event_union_sha256": _json_sha256(union),
        "packet_count": len(packets),
        "total_cluster_count": len(cluster_ids),
        "ordering": ["ticker", "date", "cluster_id"],
        "company_groups_are_not_split": True,
        "packets": [
            {
                "packet_id": packet["packet_id"],
                "packet_sha256": packet["packet_sha256"],
                "event_count": packet["event_count"],
                "filename": f"{packet['packet_id']}.json",
            }
            for packet in packets
        ],
    }
    provenance = {
        "schema_version": GOLD_PACKET_PROVENANCE_SCHEMA,
        "benchmark_id": benchmark_id,
        "event_union_sha256": _json_sha256(union),
        "join_only_after_gold_completion": True,
        "clusters": [
            {
                "cluster_id": cluster["match_cluster_id"],
                "found_by": cluster["found_by"],
            }
            for cluster in sorted(
                union["events"],
                key=lambda item: (item["ticker"], item["date"], item["match_cluster_id"]),
            )
        ],
    }
    expected_files = {
        packet_directory / f"{packet['packet_id']}.json": packet for packet in packets
    } | {blind_manifest_path: blind_manifest, provenance_path: provenance}
    existing = [path for path in expected_files if path.exists()]
    if existing:
        if len(existing) != len(expected_files) or any(
            _read_json(path) != value for path, value in expected_files.items()
        ):
            raise BenchmarkError("existing Gold packet artifacts differ; refusing overwrite")
        idempotent = True
    else:
        packet_directory.mkdir(parents=True, exist_ok=True)
        for path, value in expected_files.items():
            atomic_write_json(path, value)
        idempotent = False
    return {
        "schema_version": GOLD_PACKET_MANIFEST_SCHEMA,
        "benchmark_id": benchmark_id,
        "packet_count": len(packets),
        "clusters_per_packet": [packet["event_count"] for packet in packets],
        "total_cluster_count": len(cluster_ids),
        "blind_fields_verified": True,
        "idempotent": idempotent,
        "packet_directory": str(packet_directory.resolve()),
        "manifest_path": str(blind_manifest_path.resolve()),
        "provenance_manifest_path": str(provenance_path.resolve()),
    }


def _validate_gold_response_item(
    item: Any,
    *,
    index: int,
    all_cluster_ids: set[str],
) -> dict[str, Any]:
    required = set(_gold_response_contract()["required_item_fields"])
    if not isinstance(item, dict) or set(item) != required:
        raise BenchmarkError(f"Gold response items[{index}] fields do not match schema")
    cluster_id = item["cluster_id"]
    if not isinstance(cluster_id, str) or cluster_id not in all_cluster_ids:
        raise BenchmarkError(f"Gold response items[{index}] has unknown cluster_id")
    verdict = item["verdict"]
    if verdict not in GOLD_VERDICTS:
        raise BenchmarkError(f"Gold response items[{index}] has invalid verdict")
    if not isinstance(item["materiality_reason"], str) or not item["materiality_reason"].strip():
        raise BenchmarkError(f"Gold response items[{index}].materiality_reason is required")
    for key in ("event_exists", "event_within_window", "company_specific", "primary_source_verified", "original_source_verified"):
        if type(item[key]) is not bool:
            raise BenchmarkError(f"Gold response items[{index}].{key} must be boolean")
    duplicate_of = item["duplicate_of_cluster_id"]
    needs_target = verdict in {"DUPLICATE", "MATCHER_FALSE_NEGATIVE"}
    if needs_target:
        if duplicate_of not in all_cluster_ids or duplicate_of == cluster_id:
            raise BenchmarkError(f"Gold response items[{index}] requires a valid duplicate target")
    elif duplicate_of is not None:
        raise BenchmarkError(f"Gold response items[{index}] must not have duplicate target")
    primary_url = item["primary_source_url"]
    if primary_url is not None and not (
        isinstance(primary_url, str) and primary_url.startswith(("http://", "https://"))
    ):
        raise BenchmarkError(f"Gold response items[{index}].primary_source_url is invalid")
    if item["primary_source_verified"] and primary_url is None:
        raise BenchmarkError(f"Gold response items[{index}] verified primary source requires URL")
    if item["primary_source_type"] not in GOLD_SOURCE_TYPES:
        raise BenchmarkError(f"Gold response items[{index}].primary_source_type is invalid")
    if item["earnings_channel"] not in GOLD_EARNINGS_CHANNELS:
        raise BenchmarkError(f"Gold response items[{index}].earnings_channel is invalid")
    if item["direction"] not in GOLD_DIRECTIONS:
        raise BenchmarkError(f"Gold response items[{index}].direction is invalid")
    if item["confidence"] not in GOLD_CONFIDENCES:
        raise BenchmarkError(f"Gold response items[{index}].confidence is invalid")
    if not isinstance(item["notes"], str):
        raise BenchmarkError(f"Gold response items[{index}].notes must be a string")
    if not isinstance(item["evidence"], list):
        raise BenchmarkError(f"Gold response items[{index}].evidence must be an array")
    for evidence_index, evidence in enumerate(item["evidence"]):
        if not isinstance(evidence, dict) or set(evidence) != {"url", "source_type", "supports"}:
            raise BenchmarkError(
                f"Gold response items[{index}].evidence[{evidence_index}] fields do not match schema"
            )
        if not isinstance(evidence["url"], str) or not evidence["url"].startswith(("http://", "https://")):
            raise BenchmarkError(f"Gold response items[{index}].evidence[{evidence_index}].url is invalid")
        if evidence["source_type"] not in GOLD_SOURCE_TYPES:
            raise BenchmarkError(f"Gold response items[{index}].evidence[{evidence_index}].source_type is invalid")
        if not isinstance(evidence["supports"], str) or not evidence["supports"].strip():
            raise BenchmarkError(f"Gold response items[{index}].evidence[{evidence_index}].supports is required")
    return item


def validate_gold_adjudication_response_v2(
    value: dict[str, Any],
    packet: dict[str, Any],
    all_cluster_ids: set[str],
) -> dict[str, Any]:
    if set(value) != {
        "schema_version", "benchmark_id", "packet_id", "packet_sha256",
        "adjudicated_at", "items",
    }:
        raise BenchmarkError("Gold response fields do not match schema")
    if value.get("schema_version") != GOLD_RESPONSE_SCHEMA:
        raise BenchmarkError("invalid Gold response schema_version")
    if value.get("benchmark_id") != packet["benchmark_id"]:
        raise BenchmarkError("Gold response benchmark_id mismatch")
    if value.get("packet_id") != packet["packet_id"]:
        raise BenchmarkError("Gold response packet_id mismatch")
    if value.get("packet_sha256") != packet["packet_sha256"] or _packet_sha256(packet) != packet["packet_sha256"]:
        raise BenchmarkError("Gold response packet SHA mismatch")
    try:
        adjudicated_at = datetime.fromisoformat(str(value["adjudicated_at"]))
    except (TypeError, ValueError) as exc:
        raise BenchmarkError("Gold response adjudicated_at is invalid") from exc
    if adjudicated_at.tzinfo is None:
        raise BenchmarkError("Gold response adjudicated_at must include timezone")
    if not isinstance(value.get("items"), list):
        raise BenchmarkError("Gold response items must be an array")
    items = [
        _validate_gold_response_item(item, index=index, all_cluster_ids=all_cluster_ids)
        for index, item in enumerate(value["items"])
    ]
    item_ids = [item["cluster_id"] for item in items]
    expected_ids = [item["cluster_id"] for item in packet["events"]]
    if len(item_ids) != len(set(item_ids)):
        raise BenchmarkError("Gold response contains duplicate cluster_id")
    if set(item_ids) != set(expected_ids):
        raise BenchmarkError("Gold response must adjudicate every packet cluster exactly once")
    return value


def _validate_gold_v2_against_union(
    gold: dict[str, Any], union: dict[str, Any]
) -> dict[str, Any]:
    validate_gold_adjudication(gold)
    if gold.get("matcher_schema_version") != MATCHER_V2_SCHEMA:
        raise BenchmarkError("Gold v2 matcher identity mismatch")
    if gold.get("benchmark_id") != union.get("benchmark_id"):
        raise BenchmarkError("Gold v2 benchmark identity mismatch")
    if gold.get("source_union_sha256") != _json_sha256(union):
        raise BenchmarkError("Gold v2 source union SHA mismatch")
    gold_ids = [event["match_cluster_id"] for event in gold["events"]]
    union_ids = [event["match_cluster_id"] for event in union["events"]]
    if len(gold_ids) != len(set(gold_ids)) or set(gold_ids) != set(union_ids):
        raise BenchmarkError("Gold v2 must contain every union cluster exactly once")
    by_id = {event["match_cluster_id"]: event for event in gold["events"]}
    for event in gold["events"]:
        target = event.get("duplicate_of_cluster_id")
        if event["verdict"] in {"DUPLICATE", "MATCHER_FALSE_NEGATIVE"}:
            if target not in by_id or target == event["match_cluster_id"]:
                raise BenchmarkError("Gold v2 duplicate relationship is invalid")
        elif target is not None:
            raise BenchmarkError("Gold v2 non-duplicate event has a duplicate target")
    for event in gold["events"]:
        seen: set[str] = set()
        current = event
        while current["verdict"] in {"DUPLICATE", "MATCHER_FALSE_NEGATIVE"}:
            cluster_id = current["match_cluster_id"]
            if cluster_id in seen:
                raise BenchmarkError("Gold v2 duplicate relationship contains a cycle")
            seen.add(cluster_id)
            current = by_id[current["duplicate_of_cluster_id"]]
    return gold


def _gold_verdict_counts(gold: dict[str, Any]) -> dict[str, int]:
    return {
        verdict: sum(event["verdict"] == verdict for event in gold["events"])
        for verdict in GOLD_VERDICTS
    }


def gold_status_v2(root: Path, benchmark_id: str) -> dict[str, Any]:
    directory = _matcher_v2_directory(root, benchmark_id)
    union = _read_json(directory / "event_union_v2.json")
    gold = _validate_gold_v2_against_union(
        _read_json(directory / "gold_adjudication_v2.json"), union
    )
    counts = _gold_verdict_counts(gold)
    complete = counts["UNKNOWN"] == 0
    return {
        "schema_version": "company_news_gold_status_v2",
        "benchmark_id": benchmark_id,
        "status": "GOLD_COMPLETE" if complete else "GOLD_INCOMPLETE",
        "total_cluster_count": len(gold["events"]),
        "verdict_counts": counts,
        "imported_packet_count": len(gold.get("packet_imports", {})),
        "batch03_allowed": False if not complete else "REQUIRES_GOLD_METRIC_REVIEW",
    }


def _write_gold_backup(
    directory: Path, gold: dict[str, Any], content_sha256: str
) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
        raise BenchmarkError("Gold backup content SHA-256 is invalid")
    if _json_sha256(gold) != content_sha256:
        raise BenchmarkError("Gold backup content does not match its full SHA-256")
    backup_directory = directory / "backups"
    backup_directory.mkdir(parents=True, exist_ok=True)
    if not backup_directory.is_dir():
        raise BenchmarkError("Gold backup directory is unavailable")
    backup_path = backup_directory / f"g-{content_sha256[:16]}.json"
    if backup_path.exists():
        existing = _read_json(backup_path)
        if _json_sha256(existing) != content_sha256:
            raise BenchmarkError("Gold backup short-hash collision; refusing write")
    else:
        atomic_write_json(backup_path, gold)
    return backup_path


def import_gold_adjudication_v2(
    root: Path,
    benchmark_id: str,
    input_path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    directory = _matcher_v2_directory(root, benchmark_id)
    response = _read_json(input_path)
    packet_id = response.get("packet_id")
    if not isinstance(packet_id, str) or not re.fullmatch(r"gold_packet_\d{3}", packet_id):
        raise BenchmarkError("Gold response packet_id is invalid")
    packet = _read_json(directory / "gold_packets" / f"{packet_id}.json")
    union = _read_json(directory / "event_union_v2.json")
    all_cluster_ids = {event["match_cluster_id"] for event in union["events"]}
    validate_gold_adjudication_response_v2(response, packet, all_cluster_ids)
    gold_path = directory / "gold_adjudication_v2.json"
    gold = _validate_gold_v2_against_union(_read_json(gold_path), union)
    response_hash = _json_sha256(response)
    previous_import = gold.get("packet_imports", {}).get(packet_id)
    if previous_import and previous_import.get("response_sha256") == response_hash:
        return {
            **gold_status_v2(root, benchmark_id),
            "packet_id": packet_id,
            "idempotent": True,
            "gold_updated": False,
        }
    current_hash = _json_sha256(gold)
    updated = copy.deepcopy(gold)
    event_by_id = {event["match_cluster_id"]: event for event in updated["events"]}
    for item in response["items"]:
        target = event_by_id[item["cluster_id"]]
        target.update({
            "cluster_id": item["cluster_id"],
            "event_date": target["date"],
            "canonical_headline": target["headline"],
            "verdict": item["verdict"],
            "rationale": item["materiality_reason"],
            "materiality_reason": item["materiality_reason"],
            "event_exists": item["event_exists"],
            "event_within_window": item["event_within_window"],
            "company_specific": item["company_specific"],
            "duplicate_of_cluster_id": item["duplicate_of_cluster_id"],
            "primary_source_url": item["primary_source_url"],
            "primary_source_type": item["primary_source_type"],
            "primary_source_verified": item["primary_source_verified"],
            "original_source_verified": item["original_source_verified"],
            "official_source_url": item["primary_source_url"],
            "earnings_channel": item["earnings_channel"],
            "direction": item["direction"],
            "confidence": item["confidence"],
            "notes": item["notes"],
            "evidence": item["evidence"],
            "adjudicator": "independent_web_adjudicator_v2",
            "adjudicated_at": response["adjudicated_at"],
            "adjudication_packet_id": packet_id,
        })
    imports = dict(updated.get("packet_imports", {}))
    imports[packet_id] = {
        "packet_sha256": packet["packet_sha256"],
        "response_sha256": response_hash,
        "adjudicated_at": response["adjudicated_at"],
        "imported_at": _iso(now),
        "previous_gold_sha256": current_hash,
    }
    updated["packet_imports"] = dict(sorted(imports.items()))
    _validate_gold_v2_against_union(updated, union)
    backup_path = _write_gold_backup(directory, gold, current_hash)
    atomic_write_json(gold_path, updated)
    return {
        **gold_status_v2(root, benchmark_id),
        "packet_id": packet_id,
        "idempotent": False,
        "gold_updated": True,
        "backup_path": str(backup_path.resolve()),
        "backup_content_sha256": current_hash,
    }


def _gold_normalized_groups_v2(
    value: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Return Gold events grouped exactly as v2 scoring groups them."""
    by_id = {event["match_cluster_id"]: event for event in value["events"]}

    def root_id(event: dict[str, Any]) -> str:
        current = event
        seen: set[str] = set()
        while current["verdict"] in {"DUPLICATE", "MATCHER_FALSE_NEGATIVE"}:
            cluster_id = current["match_cluster_id"]
            if cluster_id in seen:
                raise BenchmarkError("Gold duplicate relationship contains a cycle")
            seen.add(cluster_id)
            target_id = current.get("duplicate_of_cluster_id")
            if target_id not in by_id:
                raise BenchmarkError("Gold duplicate relationship has unknown target")
            current = by_id[target_id]
        return current["match_cluster_id"]

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in value["events"]:
        groups[root_id(event)].append(event)
    return by_id, dict(groups)


def _gold_metrics_v2(value: dict[str, Any]) -> dict[str, Any]:
    counts = _gold_verdict_counts(value)
    if counts["UNKNOWN"]:
        return {
            "available": False,
            "decision": "GOLD_INCOMPLETE",
            "verdict_counts": counts,
            "unknown_event_count": counts["UNKNOWN"],
            "runs": {},
        }
    by_id, groups = _gold_normalized_groups_v2(value)
    material_roots = {
        root for root in groups if by_id[root]["verdict"] == "VALID_MATERIAL"
    }
    critical_roots = {
        root for root in material_roots
        if by_id[root]["importance"] == "high"
        or by_id[root]["earnings_relevance"] == "direct"
    }
    material_tickers = {by_id[root]["ticker"] for root in material_roots}
    scorable = {"VALID_MATERIAL", "VALID_NON_MATERIAL", "INVALID_SOURCE", "OUT_OF_WINDOW"}
    runs: dict[str, Any] = {}
    for run in QUALITY_RUN_LABELS.values():
        discovered_roots = {
            root for root, members in groups.items()
            if any(member["found_by"][run] for member in members)
        }
        found_material = material_roots & discovered_roots
        found_critical = critical_roots & discovered_roots
        denominator = {
            root for root in discovered_roots if by_id[root]["verdict"] in scorable
        }
        found_material_tickers = {by_id[root]["ticker"] for root in found_material}
        any_tickers = {
            member["ticker"]
            for members in groups.values()
            for member in members
            if member["found_by"][run]
        }
        runs[run] = {
            "gold_material_event_count": len(material_roots),
            "found_gold_material_event_count": len(found_material),
            "gold_event_recall": len(found_material) / len(material_roots) if material_roots else 1.0,
            "critical_gold_event_count": len(critical_roots),
            "critical_gold_event_recall": len(found_critical) / len(critical_roots) if critical_roots else 1.0,
            "precision_denominator_count": len(denominator),
            "gold_material_precision": len(found_material) / len(denominator) if denominator else (1.0 if not material_roots else 0.0),
            "material_company_false_negative_count": len(material_tickers - found_material_tickers),
            "material_company_false_negative_tickers": sorted(material_tickers - found_material_tickers),
            "no_news_false_negative_count": len(material_tickers - any_tickers),
            "no_news_false_negative_tickers": sorted(material_tickers - any_tickers),
        }
    return {
        "available": True,
        "decision": "GOLD_COMPLETE",
        "verdict_counts": counts,
        "gold_material_event_count": len(material_roots),
        "critical_gold_event_count": len(critical_roots),
        "deduplicated_gold_event_count": len(groups),
        "runs": runs,
    }


def audit_gold_adjudication(
    root: Path,
    benchmark_id: str,
    adjudication_path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    value = validate_gold_adjudication(_read_json(adjudication_path))
    if value["benchmark_id"] != benchmark_id:
        raise BenchmarkError("gold adjudication benchmark_id mismatch")
    union_path = _quality_v2_dir(root, benchmark_id) / "event_union.json"
    union = _read_json(union_path)
    expected_hash = hashlib.sha256(
        json.dumps(union, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if value.get("source_union_sha256") != expected_hash:
        raise BenchmarkError("gold adjudication does not match the current event union")
    throughput: dict[str, Any] = {}
    compression: dict[str, Any] = {}
    for run_name in ("batch02", "batch04"):
        audit_path = _run_dir(root, benchmark_id, run_name) / "audit.json"
        audit = _read_json(audit_path)
        fact = dict(audit.get("throughput", {}))
        estimated_hours = fact.get("estimated_hours_for_3800")
        fact["weekly_3800_feasible_with_8_tasks"] = (
            estimated_hours is not None and float(estimated_hours) <= 168
        )
        throughput[run_name] = fact
        compression[run_name] = audit.get("global_compression", {})
    result = {
        "schema_version": GOLD_AUDIT_SCHEMA,
        "benchmark_id": benchmark_id,
        "captured_at": _iso(now),
        "adjudication_path": str(adjudication_path.resolve()),
        **_gold_metrics(value),
        "reference_is_not_assumed_complete": True,
        "throughput_facts": throughput,
        "research_compression_facts": compression,
        "batch03_remains_on_hold": True,
    }
    atomic_write_json(_quality_v2_dir(root, benchmark_id) / "gold_audit.json", result)
    return result


def audit_gold_adjudication_v2(
    root: Path,
    benchmark_id: str,
    adjudication_path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    matcher_directory = _quality_v2_dir(root, benchmark_id) / "matcher_v2"
    union = _read_json(matcher_directory / "event_union_v2.json")
    value = _validate_gold_v2_against_union(_read_json(adjudication_path), union)
    throughput: dict[str, Any] = {}
    compression: dict[str, Any] = {}
    for run_name in ("batch02", "batch04"):
        audit = _read_json(_run_dir(root, benchmark_id, run_name) / "audit.json")
        fact = dict(audit.get("throughput", {}))
        estimated_hours = fact.get("estimated_hours_for_3800")
        fact["weekly_3800_feasible_with_8_tasks"] = (
            estimated_hours is not None and float(estimated_hours) <= 168
        )
        throughput[run_name] = fact
        compression[run_name] = audit.get("global_compression", {})
    result = {
        "schema_version": GOLD_AUDIT_SCHEMA,
        "matcher_schema_version": MATCHER_V2_SCHEMA,
        "benchmark_id": benchmark_id,
        "captured_at": _iso(now),
        "adjudication_path": str(adjudication_path.resolve()),
        **_gold_metrics_v2(value),
        "reference_is_not_assumed_complete": True,
        "throughput_facts": throughput,
        "research_compression_facts": compression,
        "batch03_remains_on_hold": True,
    }
    output = matcher_directory / "gold_audit_v2.json"
    atomic_write_json(output, result)
    return result


def _gold_source_bucket(source_type: str) -> str:
    mapping = {
        "company_news": "official_company",
        "customer_supplier_official": "official_company",
        "company_ir": "ir_tdnet",
        "tdnet": "ir_tdnet",
        "edinet": "ir_tdnet",
        "government": "government",
        "industry_association": "industry_research",
        "reliable_news": "external_news",
        "secondary": "external_news",
    }
    return mapping.get(source_type, "other")


def _quartile_summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "average": statistics.mean(values) if values else None,
        "median": statistics.median(values) if values else None,
        "p25": _percentile(values, 0.25),
        "p75": _percentile(values, 0.75),
    }


def _discovery_aggregate(
    rows: list[dict[str, Any]],
    *,
    labels: tuple[str, ...] = tuple(QUALITY_RUN_LABELS.values()),
) -> dict[str, Any]:
    result: dict[str, Any] = {"event_count": len(rows)}
    for label in labels:
        found = sum(bool(row[f"{label}_found"]) for row in rows)
        result[label] = {
            "found": found,
            "missed": len(rows) - found,
            "recall": found / len(rows) if rows else 1.0,
        }
    return result


def _grouped_discovery_analysis(
    rows: list[dict[str, Any]], key: str, order: tuple[str, ...] | None = None
) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row[key])].append(row)
    keys = list(order or ()) + sorted(set(groups) - set(order or ()))
    return {value: _discovery_aggregate(groups.get(value, [])) for value in keys}


def _task_elapsed_by_run_id(directory: Path) -> dict[str, float]:
    return {
        str(record["run_id"]): float(record["task_elapsed_seconds"])
        for record in _telemetry(directory, "runs.jsonl")
        if record.get("run_id") and record.get("task_elapsed_seconds") is not None
    }


def _company_telemetry_index(
    root: Path, benchmark_id: str, run_name: str, records: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    task_elapsed = _task_elapsed_by_run_id(_run_dir(root, benchmark_id, run_name))
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        ticker = str(record["ticker"])
        result[ticker] = {
            "research_elapsed_seconds": record.get("research_elapsed_seconds"),
            "source_count": record.get("source_count"),
            "task_elapsed_seconds": task_elapsed.get(str(record.get("run_id"))),
            "batch_position": record.get("company_position"),
            "returned_item_count": record.get("event_count"),
        }
    return result


def _run_observations(
    union_by_id: dict[str, dict[str, Any]],
    group: list[dict[str, Any]],
    label: str,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for gold_member in group:
        union_event = union_by_id[gold_member["match_cluster_id"]]
        for member in union_event.get("members", []):
            if member.get("run_label") != label:
                continue
            observations.append({
                key: member.get(key)
                for key in (
                    "observation_id", "headline", "source_url", "source_type",
                    "category", "importance", "earnings_relevance", "summary",
                )
            })
    return sorted(observations, key=lambda item: str(item.get("observation_id")))


def _miss_reason(
    row: dict[str, Any], label: str, material_count_for_company: int,
    run_medians: dict[str, dict[str, float | None]],
) -> dict[str, Any]:
    detail = row["run_details"][label]
    telemetry = detail["telemetry"]
    output_count = int(detail["returned_item_count"] or 0)
    source_bucket = row["source_bucket"]
    signals: list[str] = []
    if output_count == 0:
        reason, confidence = "NO_EVENT_RETURNED", "HIGH"
    elif material_count_for_company > 1:
        reason, confidence = "MULTI_EVENT_COMPANY_TRUNCATION", "MEDIUM"
    elif source_bucket in {"official_company", "ir_tdnet", "government"}:
        reason, confidence = "OFFICIAL_SOURCE_MISSED", "HIGH"
    elif source_bucket == "external_news":
        reason, confidence = "EXTERNAL_NEWS_MISSED", "MEDIUM"
    elif source_bucket == "industry_research":
        reason, confidence = "INDUSTRY_CONTEXT_MISSED", "MEDIUM"
    else:
        reason, confidence = "DIFFERENT_EVENT_ONLY", "LOW"
    research = telemetry.get("research_elapsed_seconds")
    sources = telemetry.get("source_count")
    if research is not None and run_medians[label]["research"] is not None:
        if float(research) < float(run_medians[label]["research"]):
            signals.append("research_time_below_run_median")
    if sources is not None and run_medians[label]["sources"] is not None:
        if float(sources) < float(run_medians[label]["sources"]):
            signals.append("source_count_below_run_median")
    if label in {"batch02", "batch04"} and signals:
        signals.append("possible_batch_compression")
        if confidence == "LOW":
            reason, confidence = "POSSIBLE_BATCH_COMPRESSION", "MEDIUM"
    strategy = {
        "official_company": "OFFICIAL_SITE_DETERMINISTIC_FETCH",
        "ir_tdnet": "IR_TDNET_DETERMINISTIC_FEED",
        "government": "GOVERNMENT_OFFICIAL_SEARCH",
        "industry_research": "INDUSTRY_KEYWORD_SEARCH",
        "external_news": "COMPANY_EXPOSURE_AND_NEWS_SEARCH",
        "other": "GENERAL_WEB_SECOND_PASS",
    }[source_bucket]
    return {
        "reason": reason,
        "reason_confidence": confidence,
        "signals": signals,
        "improvement_candidate": strategy,
    }


def _noise_category(row: dict[str, Any]) -> str:
    text = " ".join(str(row.get(key, "")) for key in ("headline", "category", "notes")).lower()
    if re.search(r"campaign|キャンペーン", text):
        return "campaign"
    if re.search(r"rating|レーティング|目標株価", text):
        return "rating"
    if re.search(r"blog|ブログ|勉強会|csr", text):
        return "blog"
    if re.search(r"会社紹介|企業紹介|profile|銘柄カルテ", text):
        return "profile"
    if not row.get("company_specific", True) or row.get("earnings_channel") == "industry":
        return "generic_industry"
    if row.get("category") in {"product_service", "new_product"}:
        return "minor_product"
    return "other"


def _markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _matrix_markdown(matrix: dict[str, Any]) -> str:
    lines = [
        "# Gold Material Discovery Matrix", "",
        f"Benchmark: `{matrix['benchmark_id']}`", "",
        "The rows use the same duplicate normalization as Gold v2 scoring.", "",
        "| Ticker | Date | Gold event | Source | Channel | Ref01 | Ref02 | Batch02 | Batch04 |",
        "|---|---|---|---|---|---:|---:|---:|---:|",
    ]
    for row in matrix["rows"]:
        lines.append("| " + " | ".join(_markdown_cell(value) for value in (
            row["ticker"], row["date"], row["canonical_headline"],
            row["primary_source_type"], row["earnings_channel"],
            "FOUND" if row["reference01_found"] else "MISSED",
            "FOUND" if row["reference02_found"] else "MISSED",
            "FOUND" if row["batch02_found"] else "MISSED",
            "FOUND" if row["batch04_found"] else "MISSED",
        )) + " |")
    return "\n".join(lines) + "\n"


def _analysis_markdown(analysis: dict[str, Any]) -> str:
    summary = analysis["summary"]
    official = analysis["official_source_safety_net"]
    lines = [
        "# Gold Material Miss Analysis", "", "## Executive Summary", "",
        (
            f"Gold is complete with {summary['material_event_count']} material events and "
            f"{summary['critical_event_count']} critical events. Reference01/reference02/"
            f"batch02/batch04 found {summary['run_results']['reference01']['found']}/"
            f"{summary['run_results']['reference02']['found']}/"
            f"{summary['run_results']['batch02']['found']}/"
            f"{summary['run_results']['batch04']['found']} material events respectively."
        ), "",
        (
            f"Batch04 missed {summary['run_results']['batch04']['missed']} events. "
            f"An official-first pass has a theoretical upper-bound rescue of "
            f"{official['theoretical_upper_bound_rescue_count']} events; this is not an "
            "observed recall gain and assumes every deterministic source retrieval succeeds."
        ), "",
        f"Recommendation: **{analysis['recommendation']['decision']}**. "
        f"{analysis['recommendation']['rationale']}", "",
        "## Four-run summary", "",
        "| Run | Found | Missed | Material recall | Critical recall | Non-critical recall |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label, facts in summary["run_results"].items():
        lines.append(
            f"| {label} | {facts['found']} | {facts['missed']} | {facts['recall']:.2%} | "
            f"{summary['critical'][label]['recall']:.2%} | "
            f"{summary['noncritical'][label]['recall']:.2%} |"
        )
    for title, key in (("Critical material events", "critical_analysis"),
                       ("Non-critical material events", "noncritical_analysis")):
        lines.extend([
            "", f"## {title}", "",
            "| Ticker | Date | Headline | Source | Channel | Direction | Ref01 | Ref02 | Batch02 | Batch04 |",
            "|---|---|---|---|---|---|---:|---:|---:|---:|",
        ])
        for row in analysis[key]["events"]:
            lines.append("| " + " | ".join(_markdown_cell(value) for value in (
                row["ticker"], row["date"], row["canonical_headline"],
                row["primary_source_type"], row["earnings_channel"], row["direction"],
                "FOUND" if row["reference01_found"] else "MISSED",
                "FOUND" if row["reference02_found"] else "MISSED",
                "FOUND" if row["batch02_found"] else "MISSED",
                "FOUND" if row["batch04_found"] else "MISSED",
            )) + " |")
    lines.extend(["", "## Reference disagreement", ""])
    for key, value in analysis["reference_disagreement"]["counts"].items():
        lines.append(f"- {key}: {value}")
    lines.extend([
        "", "## Source-type recall", "",
        "| Source bucket | Events | Ref01 | Ref02 | Batch02 | Batch04 |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for bucket, facts in analysis["source_type_analysis"].items():
        lines.append(
            f"| {bucket} | {facts['event_count']} | {facts['reference01']['recall']:.2%} | "
            f"{facts['reference02']['recall']:.2%} | {facts['batch02']['recall']:.2%} | "
            f"{facts['batch04']['recall']:.2%} |"
        )
    b2 = analysis["batch02_analysis"]
    b4 = analysis["batch04_analysis"]
    cross = analysis["batch02_vs_batch04"]["counts"]
    lines.extend([
        "", "## Batch compression indicators", "",
        f"- Both references found but batch02 missed: {len(b2['both_references_found_candidate_missed'])}",
        f"- Both references found but batch04 missed: {len(b4['both_references_found_candidate_missed'])}",
        f"- Batch02-only / Batch04-only: {cross['BATCH02_ONLY']} / {cross['BATCH04_ONLY']}",
        "", "## Multi-event companies", "",
        f"Multi-material companies: {analysis['multi_event_analysis']['company_count']}; "
        f"partial company-run observations: {analysis['multi_event_analysis']['partial_truncation_company_run_count']}.",
        "",
        "| Ticker | Company | Gold events | Ref01 | Ref02 | Batch02 | Batch04 |",
        "|---|---|---:|---|---|---|---|",
    ])
    for company in analysis["multi_event_analysis"]["companies"]:
        lines.append("| " + " | ".join(_markdown_cell(value) for value in (
            company["ticker"], company["company"], company["material_event_count"],
            company["runs"]["reference01"]["status"], company["runs"]["reference02"]["status"],
            company["runs"]["batch02"]["status"], company["runs"]["batch04"]["status"],
        )) + " |")
    lines.extend([
        "", "## Found versus missed telemetry", "",
        "| Run | Found research avg/median | Missed research avg/median | Found sources avg/median | Missed sources avg/median |",
        "|---|---:|---:|---:|---:|",
    ])
    for label, facts in analysis["telemetry_analysis"].items():
        fr, mr = facts["found_research_seconds"], facts["missed_research_seconds"]
        fs, ms = facts["found_source_count"], facts["missed_source_count"]
        lines.append(
            f"| {label} | {fr['average']:.2f}/{fr['median']:.2f} | "
            f"{mr['average']:.2f}/{mr['median']:.2f} | {fs['average']:.2f}/{fs['median']:.2f} | "
            f"{ms['average']:.2f}/{ms['median']:.2f} |"
        )
    lines.extend(["", "## Batch position", ""])
    for label in ("batch02", "batch04"):
        rendered = ", ".join(
            f"position {position}: {facts['found']}/{facts['found'] + facts['missed']} ({facts['recall']:.2%})"
            for position, facts in analysis["batch_position_analysis"][label].items()
        )
        lines.append(f"- {label}: {rendered}")
    remaining = analysis["external_news_second_pass"]
    lines.extend([
        "", "## Safety-net estimates", "",
        f"- Batch04 material misses: {official['batch04_missed_material_count']}",
        f"- Official-first theoretical rescue ceiling: {official['theoretical_upper_bound_rescue_count']}",
        f"- Remaining external/industry misses: {remaining['remaining_external_or_industry_miss_count']}",
        f"- Non-official targeted second-pass candidates: {remaining['targeted_second_pass_candidate_count']}",
        "", "## Precision noise", "",
    ])
    for label, facts in analysis["precision_noise_analysis"].items():
        categories = ", ".join(f"{key}={value}" for key, value in facts["categories"].items())
        lines.append(f"- {label}: {facts['total']} non-material discoveries ({categories})")
    lines.extend(["", "## Improvement scenarios", ""])
    for scenario in analysis["improvement_scenarios"]:
        lines.append(
            f"- **{scenario['scenario']}** — upper-bound recall: "
            f"{scenario.get('expected_recall_upper_bound')}; cost: {scenario['additional_research_cost']}; "
            f"throughput: {scenario['throughput_impact_direction']}; risk: {scenario['risk']}"
        )
    lines.extend(["", "## Interpretation guardrails", "",
                  "Miss reasons and rescue counts are artifact-based classifications, not new Web verification. ",
                  "The audit does not mutate Gold, run outputs, Production data, or scheduled tasks."])
    return "\n".join(lines) + "\n"


def audit_gold_material_misses_v2(
    root: Path, benchmark_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Build a read-only four-run audit of finalized Gold v2 material events."""
    directory = _matcher_v2_directory(root, benchmark_id)
    union = _read_json(directory / "event_union_v2.json")
    gold = _validate_gold_v2_against_union(
        _read_json(directory / "gold_adjudication_v2.json"), union
    )
    gold_audit = _read_json(directory / "gold_audit_v2.json")
    metrics = _gold_metrics_v2(gold)
    if metrics.get("decision") != "GOLD_COMPLETE":
        raise BenchmarkError("Gold v2 must be GOLD_COMPLETE before miss analysis")
    if metrics.get("gold_material_event_count") != 29 or metrics.get("critical_gold_event_count") != 7:
        raise BenchmarkError("Gold v2 material/critical integrity mismatch (expected 29/7)")
    if gold_audit.get("decision") != "GOLD_COMPLETE" or gold_audit.get("verdict_counts", {}).get("UNKNOWN") != 0:
        raise BenchmarkError("gold_audit_v2.json is not complete")

    by_id, groups = _gold_normalized_groups_v2(gold)
    union_by_id = {event["match_cluster_id"]: event for event in union["events"]}
    material_roots = sorted(
        (root_id for root_id in groups if by_id[root_id]["verdict"] == "VALID_MATERIAL"),
        key=lambda root_id: (by_id[root_id]["ticker"], by_id[root_id]["date"], root_id),
    )
    cohort = _load_cohort(root, benchmark_id)
    reference_tickers = set(cohort["reference_tickers"])
    run_events: dict[str, dict[str, list[Any]]] = {}
    telemetry: dict[str, dict[str, dict[str, Any]]] = {}
    for run_name, label in QUALITY_RUN_LABELS.items():
        _, events, company_records = _completed_run_events(root, benchmark_id, run_name)
        run_events[label] = {ticker: values for ticker, values in events.items() if ticker in reference_tickers}
        telemetry[label] = _company_telemetry_index(root, benchmark_id, run_name, company_records)
    run_medians = {
        label: {
            "research": statistics.median([
                float(value["research_elapsed_seconds"]) for value in values.values()
                if value.get("research_elapsed_seconds") is not None
            ]),
            "sources": statistics.median([
                float(value["source_count"]) for value in values.values()
                if value.get("source_count") is not None
            ]),
        }
        for label, values in telemetry.items()
    }
    material_per_ticker: dict[str, int] = defaultdict(int)
    for root_id in material_roots:
        material_per_ticker[str(by_id[root_id]["ticker"])] += 1

    rows: list[dict[str, Any]] = []
    for root_id in material_roots:
        gold_event = by_id[root_id]
        group = groups[root_id]
        ticker = str(gold_event["ticker"])
        source_type = str(gold_event.get("primary_source_type") or gold_event.get("source_type") or "none")
        row: dict[str, Any] = {
            "gold_event_id": root_id,
            "cluster_id": root_id,
            "normalized_cluster_ids": sorted(item["match_cluster_id"] for item in group),
            "ticker": ticker,
            "company": gold_event.get("company"),
            "date": gold_event.get("event_date") or gold_event.get("date"),
            "canonical_headline": gold_event.get("canonical_headline") or gold_event.get("headline"),
            "materiality_reason": gold_event.get("materiality_reason") or gold_event.get("rationale"),
            "earnings_channel": gold_event.get("earnings_channel"),
            "direction": gold_event.get("direction"),
            "confidence": gold_event.get("confidence"),
            "primary_source_url": gold_event.get("primary_source_url") or gold_event.get("official_source_url"),
            "primary_source_type": source_type,
            "source_bucket": _gold_source_bucket(source_type),
            "official_source": _gold_source_bucket(source_type) in {"official_company", "ir_tdnet", "government"},
            "critical": gold_event.get("importance") == "high" or gold_event.get("earnings_relevance") == "direct",
            "run_details": {},
        }
        found_by: list[str] = []
        missed_by: list[str] = []
        for label in QUALITY_RUN_LABELS.values():
            found = any(bool(member.get("found_by", {}).get(label)) for member in group)
            observations = _run_observations(union_by_id, group, label)
            detail = {
                "found": found,
                "observations": observations,
                "returned_item_count": len(run_events[label].get(ticker, [])),
                "telemetry": telemetry[label].get(ticker, {
                    "research_elapsed_seconds": None, "source_count": None,
                    "task_elapsed_seconds": None, "batch_position": None,
                    "returned_item_count": None,
                }),
            }
            row["run_details"][label] = detail
            row[f"{label}_found"] = found
            (found_by if found else missed_by).append(label)
        row["found_count_4_runs"] = len(found_by)
        row["found_by"] = found_by
        row["missed_by"] = missed_by
        row["miss_analysis"] = {
            label: _miss_reason(row, label, material_per_ticker[ticker], run_medians)
            for label in missed_by
        }
        rows.append(row)

    run_results = _discovery_aggregate(rows)
    expected_found = {"reference01": 25, "reference02": 22, "batch02": 19, "batch04": 17}
    for label, expected in expected_found.items():
        audit_run = gold_audit.get("runs", {}).get(label, {})
        actual = run_results[label]["found"]
        if actual != expected or audit_run.get("found_gold_material_event_count") != actual:
            raise BenchmarkError(f"{label} discovery flags disagree with Gold audit")
    critical_rows = [row for row in rows if row["critical"]]
    noncritical_rows = [row for row in rows if not row["critical"]]
    critical = _discovery_aggregate(critical_rows)
    noncritical = _discovery_aggregate(noncritical_rows)
    if any(critical[label]["found"] != 7 for label in QUALITY_RUN_LABELS.values()):
        raise BenchmarkError("critical discovery flags disagree with Gold audit")

    ref_categories: dict[str, list[str]] = defaultdict(list)
    batch_categories: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        r1, r2 = row["reference01_found"], row["reference02_found"]
        ref_key = "BOTH_FOUND" if r1 and r2 else "REF01_ONLY" if r1 else "REF02_ONLY" if r2 else "BOTH_MISSED"
        ref_categories[ref_key].append(row["gold_event_id"])
        b2, b4 = row["batch02_found"], row["batch04_found"]
        batch_key = "BOTH_FOUND" if b2 and b4 else "BATCH02_ONLY" if b2 else "BATCH04_ONLY" if b4 else "BOTH_MISSED"
        batch_categories[batch_key].append(row["gold_event_id"])

    def comparison(label: str) -> dict[str, Any]:
        both_refs_missed = [row for row in rows if row["reference01_found"] and row["reference02_found"] and not row[f"{label}_found"]]
        return {
            "reference01_found_candidate_missed": [row["gold_event_id"] for row in rows if row["reference01_found"] and not row[f"{label}_found"]],
            "reference02_found_candidate_missed": [row["gold_event_id"] for row in rows if row["reference02_found"] and not row[f"{label}_found"]],
            "both_references_found_candidate_missed": [row["gold_event_id"] for row in both_refs_missed],
            "neither_reference_found_candidate_found": [row["gold_event_id"] for row in rows if not row["reference01_found"] and not row["reference02_found"] and row[f"{label}_found"]],
            "both_reference_miss_details": [{
                "gold_event_id": row["gold_event_id"], "ticker": row["ticker"],
                "headline": row["canonical_headline"], "gold_source": row["primary_source_url"],
                "source_type": row["primary_source_type"], "channel": row["earnings_channel"],
                "direction": row["direction"],
                "reference01_sources": [value.get("source_url") for value in row["run_details"]["reference01"]["observations"]],
                "reference02_sources": [value.get("source_url") for value in row["run_details"]["reference02"]["observations"]],
                "candidate_returned_item_count": row["run_details"][label]["returned_item_count"],
                "candidate_telemetry": row["run_details"][label]["telemetry"],
            } for row in both_refs_missed],
        }

    multi_event: list[dict[str, Any]] = []
    partial_by_run = {label: 0 for label in QUALITY_RUN_LABELS.values()}
    for ticker in sorted(key for key, count in material_per_ticker.items() if count > 1):
        company_rows = [row for row in rows if row["ticker"] == ticker]
        run_status: dict[str, Any] = {}
        for label in QUALITY_RUN_LABELS.values():
            found = sum(row[f"{label}_found"] for row in company_rows)
            state = "ALL_FOUND" if found == len(company_rows) else "ALL_MISSED" if found == 0 else "PARTIAL"
            partial_by_run[label] += state == "PARTIAL"
            run_status[label] = {"found": found, "total": len(company_rows), "status": state}
        multi_event.append({
            "ticker": ticker, "company": company_rows[0]["company"],
            "material_event_count": len(company_rows), "runs": run_status,
        })

    returned_distribution: dict[str, Any] = {}
    for label in QUALITY_RUN_LABELS.values():
        counts = {"0": 0, "1": 0, "2": 0, "3+": 0}
        multi_counts = {"0": 0, "1": 0, "2": 0, "3+": 0}
        for ticker in sorted(reference_tickers):
            count = len(run_events[label].get(ticker, []))
            key = str(count) if count < 3 else "3+"
            counts[key] += 1
            if material_per_ticker.get(ticker, 0) > 1:
                multi_counts[key] += 1
        returned_distribution[label] = {"all_reference_companies": counts, "multi_material_companies": multi_counts}

    telemetry_analysis: dict[str, Any] = {}
    position_analysis: dict[str, Any] = {}
    for label in QUALITY_RUN_LABELS.values():
        found_research: list[float] = []
        missed_research: list[float] = []
        found_sources: list[float] = []
        missed_sources: list[float] = []
        found_task_elapsed: list[float] = []
        missed_task_elapsed: list[float] = []
        positions: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            facts = row["run_details"][label]["telemetry"]
            target_research = found_research if row[f"{label}_found"] else missed_research
            target_sources = found_sources if row[f"{label}_found"] else missed_sources
            target_task_elapsed = found_task_elapsed if row[f"{label}_found"] else missed_task_elapsed
            if facts.get("research_elapsed_seconds") is not None:
                target_research.append(float(facts["research_elapsed_seconds"]))
            if facts.get("source_count") is not None:
                target_sources.append(float(facts["source_count"]))
            if facts.get("task_elapsed_seconds") is not None:
                target_task_elapsed.append(float(facts["task_elapsed_seconds"]))
            if facts.get("batch_position") is not None:
                positions[int(facts["batch_position"])].append(row)
        telemetry_analysis[label] = {
            "found_research_seconds": _quartile_summary(found_research),
            "missed_research_seconds": _quartile_summary(missed_research),
            "found_source_count": _quartile_summary(found_sources),
            "missed_source_count": _quartile_summary(missed_sources),
            "found_task_elapsed_seconds": _quartile_summary(found_task_elapsed),
            "missed_task_elapsed_seconds": _quartile_summary(missed_task_elapsed),
        }
        position_analysis[label] = {
            str(position): _discovery_aggregate(position_rows, labels=(label,))[label]
            for position, position_rows in sorted(positions.items())
        }

    nonmaterial_roots = [root_id for root_id in groups if by_id[root_id]["verdict"] == "VALID_NON_MATERIAL"]
    precision_noise: dict[str, Any] = {}
    for label in QUALITY_RUN_LABELS.values():
        noise: dict[str, int] = defaultdict(int)
        for root_id in nonmaterial_roots:
            if any(member.get("found_by", {}).get(label) for member in groups[root_id]):
                noise[_noise_category(by_id[root_id])] += 1
        precision_noise[label] = {"total": sum(noise.values()), "categories": dict(sorted(noise.items()))}

    batch04_missed = [row for row in rows if not row["batch04_found"]]
    official_missed = [row for row in batch04_missed if row["official_source"]]
    remaining_external_industry = [
        row for row in batch04_missed
        if row["source_bucket"] in {"external_news", "industry_research"}
    ]
    targeted = [row for row in batch04_missed if not row["official_source"]]
    current_found = run_results["batch04"]["found"]
    official_upper = (current_found + len(official_missed)) / len(rows)
    scenarios = [
        {
            "scenario": "A_CURRENT_BATCH04", "expected_recall_upper_bound": run_results["batch04"]["recall"],
            "additional_research_cost": "none", "throughput_impact_direction": "none",
            "implementation_complexity": "none", "risk": "material recall remains below target",
        },
        {
            "scenario": "B_BATCH04_OFFICIAL_FIRST", "expected_recall_upper_bound": official_upper,
            "additional_research_cost": "moderate deterministic source pass", "throughput_impact_direction": "lower",
            "implementation_complexity": "medium", "risk": "upper bound assumes every official-source miss is rescued",
        },
        {
            "scenario": "C_OFFICIAL_FIRST_TARGETED_SECOND_PASS", "expected_recall_upper_bound": 1.0,
            "additional_research_cost": "high and source-dependent", "throughput_impact_direction": "lower",
            "implementation_complexity": "high", "risk": "theoretical ceiling only; Web stochasticity and noise may increase",
        },
        {
            "scenario": "D_SINGLE_COMPANY_FALLBACK_FOR_UNCERTAIN", "expected_recall_upper_bound": None,
            "additional_research_cost": "high only for selected companies", "throughput_impact_direction": "lower but bounded",
            "implementation_complexity": "high", "risk": "uncertainty selector must be validated to avoid silent misses",
        },
    ]
    source_analysis = _grouped_discovery_analysis(
        rows, "source_bucket",
        ("official_company", "ir_tdnet", "government", "industry_research", "external_news", "other"),
    )
    analysis = {
        "schema_version": GOLD_MATERIAL_MISS_ANALYSIS_SCHEMA,
        "benchmark_id": benchmark_id,
        "created_at": _iso(now),
        "summary": {
            "gold_status": "GOLD_COMPLETE", "material_event_count": len(rows),
            "critical_event_count": len(critical_rows), "noncritical_event_count": len(noncritical_rows),
            "run_results": {label: run_results[label] for label in QUALITY_RUN_LABELS.values()},
            "critical": {label: critical[label] for label in QUALITY_RUN_LABELS.values()},
            "noncritical": {label: noncritical[label] for label in QUALITY_RUN_LABELS.values()},
            "official_source": _discovery_aggregate([row for row in rows if row["official_source"]]),
            "external_news": source_analysis["external_news"],
            "industry_research": source_analysis["industry_research"],
            "multi_event_company": _discovery_aggregate([row for row in rows if material_per_ticker[row["ticker"]] > 1]),
        },
        "material_matrix": rows,
        "critical_analysis": {"summary": critical, "events": critical_rows},
        "noncritical_analysis": {"summary": noncritical, "events": noncritical_rows},
        "source_type_analysis": source_analysis,
        "primary_source_type_detail": _grouped_discovery_analysis(rows, "primary_source_type"),
        "channel_analysis": _grouped_discovery_analysis(rows, "earnings_channel", GOLD_EARNINGS_CHANNELS),
        "direction_analysis": _grouped_discovery_analysis(rows, "direction", GOLD_DIRECTIONS),
        "reference_disagreement": {
            "counts": {key: len(ref_categories.get(key, [])) for key in ("BOTH_FOUND", "REF01_ONLY", "REF02_ONLY", "BOTH_MISSED")},
            "event_ids": {key: ref_categories.get(key, []) for key in ("BOTH_FOUND", "REF01_ONLY", "REF02_ONLY", "BOTH_MISSED")},
        },
        "batch02_analysis": comparison("batch02"),
        "batch04_analysis": comparison("batch04"),
        "batch02_vs_batch04": {
            "counts": {key: len(batch_categories.get(key, [])) for key in ("BOTH_FOUND", "BATCH02_ONLY", "BATCH04_ONLY", "BOTH_MISSED")},
            "event_ids": {key: batch_categories.get(key, []) for key in ("BOTH_FOUND", "BATCH02_ONLY", "BATCH04_ONLY", "BOTH_MISSED")},
        },
        "multi_event_analysis": {
            "company_count": len(multi_event), "companies": multi_event,
            "partial_truncation_count_by_run": partial_by_run,
            "partial_truncation_company_run_count": sum(partial_by_run.values()),
            "returned_event_count_distribution": returned_distribution,
        },
        "telemetry_analysis": telemetry_analysis,
        "batch_position_analysis": position_analysis,
        "miss_reason_counts": {
            label: dict(sorted({reason: sum(
                row.get("miss_analysis", {}).get(label, {}).get("reason") == reason for row in rows
            ) for reason in sorted({
                row.get("miss_analysis", {}).get(label, {}).get("reason")
                for row in rows if label in row.get("miss_analysis", {})
            })}.items()))
            for label in QUALITY_RUN_LABELS.values()
        },
        "official_source_safety_net": {
            "official_source_event_count": sum(row["official_source"] for row in rows),
            "batch04_missed_material_count": len(batch04_missed),
            "theoretical_upper_bound_rescue_count": len(official_missed),
            "candidate_event_ids": [row["gold_event_id"] for row in official_missed],
            "theoretical_recall_upper_bound": official_upper,
        },
        "external_news_second_pass": {
            "remaining_external_or_industry_miss_count": len(remaining_external_industry),
            "event_ids": [row["gold_event_id"] for row in remaining_external_industry],
            "targeted_second_pass_candidate_count": len(targeted),
            "targeted_event_ids": [row["gold_event_id"] for row in targeted],
        },
        "critical_safety_net": {
            "all_runs_found_all_critical": True,
            "source_type_counts": dict(sorted((key, sum(row["primary_source_type"] == key for row in critical_rows)) for key in {row["primary_source_type"] for row in critical_rows})),
            "channel_counts": dict(sorted((key, sum(row["earnings_channel"] == key for row in critical_rows)) for key in {row["earnings_channel"] for row in critical_rows})),
            "official_source_count": sum(row["official_source"] for row in critical_rows),
            "interpretation": "All seven are high-importance or direct-earnings events and were found by every run; the artifact supports salience as an explanation, but does not prove causality.",
        },
        "precision_noise_analysis": precision_noise,
        "improvement_scenarios": scenarios,
        "recommendation": {
            "decision": "DO_NOT_RUN_BATCH03_YET",
            "rationale": "Both batch02 and batch04 miss substantial non-critical material events. An intermediate batch size cannot isolate or repair the observed source-strategy gaps; validate an official-first safety net and targeted second pass before spending another benchmark run.",
            "batch03_run_performed": False,
        },
        "safety": {
            "web_research_performed": False, "production_writes": 0,
            "scheduled_task_changes": 0, "gold_mutations": 0,
            "reference_or_batch_raw_mutations": 0, "batch03_runs": 0,
        },
    }
    matrix = {
        "schema_version": GOLD_MATERIAL_MATRIX_SCHEMA,
        "benchmark_id": benchmark_id,
        "created_at": analysis["created_at"],
        "gold_status": "GOLD_COMPLETE",
        "gold_event_count": len(rows),
        "duplicate_normalization": "same as _gold_metrics_v2",
        "run_summary": {label: run_results[label] for label in QUALITY_RUN_LABELS.values()},
        "rows": rows,
    }
    matrix_json = directory / "gold_material_discovery_matrix.json"
    matrix_md = directory / "gold_material_discovery_matrix.md"
    analysis_json = directory / "gold_material_miss_analysis.json"
    analysis_md = directory / "GOLD_MATERIAL_MISS_ANALYSIS.md"
    atomic_write_json(matrix_json, matrix)
    atomic_write_text(matrix_md, _matrix_markdown(matrix))
    atomic_write_json(analysis_json, analysis)
    atomic_write_text(analysis_md, _analysis_markdown(analysis))
    return {
        "schema_version": GOLD_MATERIAL_MISS_ANALYSIS_SCHEMA,
        "benchmark_id": benchmark_id,
        "gold_status": "GOLD_COMPLETE",
        "material_event_count": len(rows),
        "critical_event_count": len(critical_rows),
        "run_results": {label: run_results[label] for label in QUALITY_RUN_LABELS.values()},
        "recommendation": analysis["recommendation"]["decision"],
        "matrix_json": str(matrix_json.resolve()),
        "matrix_markdown": str(matrix_md.resolve()),
        "analysis_json": str(analysis_json.resolve()),
        "analysis_markdown": str(analysis_md.resolve()),
    }


def _task_events(directory: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for task in sorted((directory / "tasks").glob("task??")):
        events.extend({"task_id": task.name, **record} for record in _read_jsonl(task / "events.jsonl"))
    return events


def _busy_skip_incidents(directory: Path, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Correlate busy skips with later recovery of the exact same guard."""
    events = _task_events(directory)
    recoveries: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("event") == "stale_guard_recovered":
            recoveries[(str(event.get("task_id")), str(event.get("run_id")))].append(event)

    incidents: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") != "busy_skip":
            continue
        task_id = str(event["task_id"])
        run_id = str(event.get("run_id"))
        busy_at = datetime.fromisoformat(str(event["at"]).replace("Z", "+00:00"))
        recovery_options = sorted(
            recoveries.get((task_id, run_id), []), key=lambda value: value["at"]
        )
        recovery = next(
            (
                value for value in recovery_options
                if datetime.fromisoformat(str(value["at"]).replace("Z", "+00:00")) >= busy_at
            ),
            None,
        )

        guard: dict[str, Any] = {}
        history = directory / "tasks" / task_id / "history"
        short_stale_path = history / _history_filename(
            "stale",
            benchmark_id=event.get("benchmark_id"),
            run_name=event.get("run_name") or directory.name,
            task_id=task_id,
            run_id=run_id,
            run_token=event.get("run_token"),
        )
        legacy_stale_path = history / f"{run_id}.stale.json"
        stale_path = short_stale_path if short_stale_path.exists() else legacy_stale_path
        if stale_path.exists():
            guard = _read_json(stale_path)
        guard_started_at = event.get("guard_started_at") or guard.get("task_started_at")
        guard_expires_at = event.get("guard_expires_at") or guard.get("expires_at")
        guard_age = event.get("guard_age_seconds")
        if guard_age is None and guard_started_at:
            guard_age = (
                busy_at - datetime.fromisoformat(str(guard_started_at).replace("Z", "+00:00"))
            ).total_seconds()

        predecessors = [
            record for record in tasks
            if record.get("task_id") == task_id
            and guard_started_at
            and datetime.fromisoformat(record["task_finished_at"])
            <= datetime.fromisoformat(str(guard_started_at).replace("Z", "+00:00"))
        ]
        predecessor = max(predecessors, key=lambda value: value["task_finished_at"], default=None)
        last_activity_values = [
            value
            for state in guard.get("company_state", {}).values()
            for key, value in state.items()
            if key.endswith("_at") and isinstance(value, str)
        ]
        last_activity_at = max(last_activity_values, default=None)
        stale_before = bool(
            guard_expires_at
            and busy_at >= datetime.fromisoformat(str(guard_expires_at).replace("Z", "+00:00"))
        )
        classification = "recoverable_stale_guard_skip" if recovery else "capacity_busy_skip"
        incidents.append({
            "timestamp": event["at"],
            "task_id": task_id,
            "run_id": run_id,
            "run_token": event.get("run_token") or guard.get("run_token"),
            "guard_started_at": guard_started_at,
            "guard_expires_at": guard_expires_at,
            "guard_age_seconds": guard_age,
            "last_guard_activity_at": last_activity_at,
            "inactivity_seconds_at_skip": (
                (busy_at - datetime.fromisoformat(last_activity_at)).total_seconds()
                if last_activity_at else None
            ),
            "guarded_run_completed_at": None,
            "previous_run_id": predecessor.get("run_id") if predecessor else None,
            "previous_run_start": predecessor.get("task_started_at") if predecessor else None,
            "previous_run_end": predecessor.get("task_finished_at") if predecessor else None,
            "reason": event.get("reason", "active_guard_unexpired"),
            "stale_before": event.get("stale_before", stale_before),
            "stale_after": bool(recovery),
            "recovered_at": recovery.get("at") if recovery else None,
            "same_incident_as_stale_guard_recovery": bool(recovery),
            "classification": classification,
        })
    return incidents


def audit_run(root: Path, benchmark_id: str, run_name: str, *, now: datetime | None = None) -> dict[str, Any]:
    directory = _run_dir(root, benchmark_id, run_name)
    prior_audit = _read_json(directory / "audit.json") if (directory / "audit.json").exists() else None
    manifest = _read_json(directory / "manifest.json")
    manifest = _finalize_run_if_complete(root, directory, manifest)
    status = benchmark_status(root, benchmark_id, run_name)
    outputs = Path(manifest["output_directory"])
    candidate_events, valid_files, invalid_files, validation_errors = _load_success_outputs(outputs)
    company = _telemetry(directory, "companies.jsonl")
    tasks = _telemetry(directory, "runs.jsonl")
    task_durations = [float(record["task_elapsed_seconds"]) for record in tasks]
    task_duration_summary = _summary(task_durations)
    task_duration_source = "current_task_telemetry"
    if (
        prior_audit
        and prior_audit.get("schema_version") == AUDIT_SCHEMA
        and isinstance(prior_audit.get("runtime", {}).get("task_elapsed_seconds"), dict)
    ):
        # Preserve the originally captured performance fact.  A historical active
        # control may have admitted later zero-company polls, which are not new
        # observations of company-processing performance.
        task_duration_summary = prior_audit["runtime"]["task_elapsed_seconds"]
        task_duration_source = "initial_audit_snapshot"
    incidents = _busy_skip_incidents(directory, tasks)
    busy = len(incidents)
    capacity_busy = sum(record["classification"] == "capacity_busy_skip" for record in incidents)
    recoverable_busy = sum(record["classification"] == "recoverable_stale_guard_skip" for record in incidents)
    stale = sum(record.get("event") == "stale_guard_recovered" for record in _task_events(directory))
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
    research_times = [float(record["research_elapsed_seconds"]) for record in company]
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
            "task_elapsed_seconds": task_duration_summary,
            "task_elapsed_sample_source": task_duration_source,
            "busy_skip": busy,
            "capacity_busy_skip": capacity_busy,
            "recoverable_stale_guard_skip": recoverable_busy,
            "busy_skip_incidents": incidents,
            "stale_guard_recovery": stale,
            "overlap": overlap,
            "company_research_elapsed_seconds": _summary(research_times),
        },
        "local_storage_bytes": sum(path.stat().st_size for path in outputs.rglob("*") if path.is_file()) if outputs.exists() else 0,
        "production_storage_delta_expected": 0,
    }

    quality_available = False
    company_recall: dict[str, float] = {}
    ambiguous: list[dict[str, Any]] = []
    reference_source_counts = source_counts if manifest.get("reference") else []
    reference_research_times = research_times if manifest.get("reference") else []
    if not manifest.get("reference"):
        reference_directory = _run_dir(root, benchmark_id, "reference")
        if (reference_directory / "manifest.json").exists():
            reference_manifest = _read_json(reference_directory / "manifest.json")
            reference_status = benchmark_status(root, benchmark_id, "reference")
            if reference_status["pending"] == 0 and reference_status["failed"] == 0:
                reference_events, _, reference_invalid, _ = _load_success_outputs(Path(reference_manifest["output_directory"]))
                reference_company = _telemetry(reference_directory, "companies.jsonl")
                reference_source_counts = [
                    float(record["source_count"])
                    for record in reference_company
                    if record["result_type"] != "failure"
                ]
                reference_research_times = [
                    float(record["research_elapsed_seconds"])
                    for record in reference_company
                ]
                cohort = _load_cohort(root, benchmark_id)
                reference_tickers = set(cohort["reference_tickers"])
                quality, company_recall, ambiguous = _quality_metrics(reference_events, candidate_events, reference_tickers)
                quality_available = reference_invalid == 0
                result["quality"] = {"available": quality_available, **quality, "ambiguous_matches": ambiguous}
    if "quality" not in result:
        result["quality"] = {"available": False, "reason": "completed single-company reference run is required"}
    event_recall = 1.0 if manifest.get("reference") else result["quality"].get("event_recall")
    result["global_compression"] = _global_compression_metrics(
        reference_source_counts,
        source_counts,
        reference_research_times,
        research_times,
        event_recall,
    )

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
    if first_metrics["average_source_count"] and last_metrics["average_source_count"] is not None:
        position_pass &= last_metrics["average_source_count"] >= first_metrics["average_source_count"] * 0.8
    result["position_degradation"] = {"first_25_percent": first_metrics, "last_25_percent": last_metrics, "pass": position_pass}

    activated_at = datetime.fromisoformat(manifest["activated_at"])
    completed_at = datetime.fromisoformat(status["completed_at"]) if status.get("completed_at") else None
    elapsed = (completed_at - activated_at).total_seconds() if completed_at else None
    completed = status["completed"]
    per_hour = completed / (elapsed / 3600) if elapsed and elapsed > 0 else None
    result["throughput"] = {
        "elapsed_started_at": manifest["activated_at"],
        "elapsed_completed_at": status.get("completed_at"),
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
        "capacity_busy_skip_zero": capacity_busy == 0,
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

    reference_path = _run_dir(root, benchmark_id, "reference") / "audit.json"
    reference_audit = _read_json(reference_path) if reference_path.exists() else {}
    reference_sources = reference_audit.get("research", {}).get("average_source_count_per_company")
    reference_research_time = (
        reference_audit.get("runtime", {})
        .get("company_research_elapsed_seconds", {})
        .get("average")
    )
    source_ratios: dict[int, float | None] = {}
    research_time_ratios: dict[int, float | None] = {}
    event_recalls: dict[int, float | None] = {}
    if reference_audit:
        source_ratios[1] = 1.0 if reference_sources is not None else None
        research_time_ratios[1] = 1.0 if reference_research_time is not None else None
        event_recalls[1] = 1.0
    for batch, audit in audits.items():
        compression = audit.get("global_compression", {})
        current_sources = audit.get("research", {}).get("average_source_count_per_company")
        current_research_time = (
            audit.get("runtime", {})
            .get("company_research_elapsed_seconds", {})
            .get("average")
        )
        source_ratios[batch] = compression.get(
            "source_count_ratio_vs_reference",
            _ratio(current_sources, reference_sources),
        )
        research_time_ratios[batch] = compression.get(
            "research_time_ratio_vs_reference",
            _ratio(current_research_time, reference_research_time),
        )
        event_recalls[batch] = compression.get(
            "event_recall_vs_reference",
            audit.get("quality", {}).get("event_recall"),
        )

    recommended = 1
    next_candidates: list[int] = []
    for batch in ALLOWED_CANDIDATE_BATCH_SIZES:
        audit = audits.get(batch)
        if audit is None:
            next_candidates = [batch]
            break
        if audit.get("decision") != "PASS":
            break
        recommended = batch

    batch4 = audits.get(4)
    if batch4:
        criteria = batch4.get("criteria", {})
        runtime_pass = all(criteria.get(key) is True for key in (
            "p95_under_45_minutes",
            "max_under_55_minutes",
        ))
        reliability_pass = all(criteria.get(key) is True for key in (
            "complete",
            "schema_validity_100pct",
            "research_failure_zero",
            "source_validity_at_least_99pct",
            "capacity_busy_skip_zero",
            "overlap_zero",
        ))
        quality_pass = all(criteria.get(key) is True for key in (
            "critical_recall_100pct",
            "material_event_recall_at_least_95pct",
            "major_no_news_false_negative_zero",
        ))
        batch4_judgment = {
            "runtime": "PASS" if runtime_pass else "FAIL",
            "reliability": "PASS" if reliability_pass else "FAIL",
            "quality": "PASS" if quality_pass else "FAIL",
        }
    else:
        batch4_judgment = {"runtime": "NOT_AUDITED", "reliability": "NOT_AUDITED", "quality": "NOT_AUDITED"}
    batch4_quality_validated = batch4_judgment["quality"] == "PASS"

    integration_audits = []
    for path in _paths(root)["load"].glob("integration-batch??-*/integration_audit.json"):
        value = _read_json(path)
        if value.get("batch_size") == recommended:
            integration_audits.append(value)
    integration_pass = any(value.get("decision") == "PASS" for value in integration_audits)

    throughput_tradeoffs: dict[str, dict[str, Any]] = {}
    if reference_audit:
        throughput_tradeoffs["1"] = {
            "decision": "REFERENCE",
            "companies_per_hour": reference_audit.get("throughput", {}).get("companies_per_hour"),
            "estimated_hours_for_3800": reference_audit.get("throughput", {}).get("estimated_hours_for_3800"),
            "estimated_days_for_3800": reference_audit.get("throughput", {}).get("estimated_days_for_3800"),
            "weekly_buffer_hours": reference_audit.get("throughput", {}).get("weekly_buffer_hours"),
            "quality_failure_disqualifies_speed": False,
        }
    throughput_tradeoffs.update({
        str(batch): {
            "decision": audit.get("decision"),
            "companies_per_hour": audit.get("throughput", {}).get("companies_per_hour"),
            "estimated_hours_for_3800": audit.get("throughput", {}).get("estimated_hours_for_3800"),
            "estimated_days_for_3800": audit.get("throughput", {}).get("estimated_days_for_3800"),
            "weekly_buffer_hours": audit.get("throughput", {}).get("weekly_buffer_hours"),
            "quality_failure_disqualifies_speed": audit.get("decision") != "PASS",
        }
        for batch, audit in sorted(audits.items())
    })
    gold_path = _quality_v2_dir(root, benchmark_id) / "matcher_v2" / "gold_audit_v2.json"
    gold_audit = _read_json(gold_path) if gold_path.exists() else {}
    gold_complete = gold_audit.get("available") is True
    gold_decisions: dict[str, str] = {}
    if gold_complete:
        for run_name, metrics in gold_audit.get("runs", {}).items():
            gold_pass = (
                metrics.get("gold_event_recall", 0) >= 0.95
                and metrics.get("critical_gold_event_recall") == 1.0
                and metrics.get("no_news_false_negative_count") == 0
            )
            gold_decisions[run_name] = "PASS" if gold_pass else "FAIL"
    batch4_quality_validated = gold_decisions.get("batch04") == "PASS"
    result = {
        "benchmark_id": benchmark_id,
        "max_validated_batch_size": recommended,
        "recommended_batch_size": recommended,
        "candidate_decisions": {str(batch): audit.get("decision") for batch, audit in sorted(audits.items())},
        "source_count_ratio_vs_reference": {str(batch): ratio for batch, ratio in sorted(source_ratios.items())},
        "research_time_ratio_vs_reference": {str(batch): ratio for batch, ratio in sorted(research_time_ratios.items())},
        "event_recall_vs_reference": {str(batch): recall for batch, recall in sorted(event_recalls.items())},
        "throughput_tradeoffs": throughput_tradeoffs,
        "next_candidates": next_candidates,
        "candidate_order": list(ALLOWED_CANDIDATE_BATCH_SIZES),
        "suspended_upward_candidates": list(SUSPENDED_UPWARD_CANDIDATES),
        "selection_contract": "largest contiguous batch in 2->3->4 with full audit PASS; quality FAIL cannot be offset by speed",
        "batch04_formal_judgment": batch4_judgment,
        "production_batch_4_quality_validated": batch4_quality_validated,
        "single_reference_quality_is_provisional": True,
        "formal_quality_source": "gold_adjudication" if gold_complete else "pending_gold_adjudication",
        "gold_quality_decisions": gold_decisions,
        "upward_candidates_blocked": True,
        "upward_block_reason": "batch04_quality_fail" if not batch4_quality_validated else "downward_refinement_only",
        "research_architecture_redesign_required": audits.get(2, {}).get("decision") == "FAIL",
        "integration_validation_passed": integration_pass,
        "ready_for_production_change": (
            recommended in ALLOWED_CANDIDATE_BATCH_SIZES and integration_pass and gold_complete
        ),
        "human_review_required": True,
    }
    atomic_write_json(_benchmark_dir(root, benchmark_id) / "recommendation.json", result)
    return result


def prepare_integration_fixture(root: Path, batch_size: int, *, now: datetime | None = None) -> dict[str, Any]:
    if batch_size not in ALLOWED_BATCH_SIZES or batch_size == REFERENCE_BATCH_SIZE:
        raise BenchmarkError("integration fixture batch must be 2 through 4")
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
    run.add_argument("--run-name")
    run.add_argument("--strategy")
    run.add_argument("--reference", action="store_true")
    replication = commands.add_parser("prepare-reference-replication")
    replication.add_argument("--benchmark-id", required=True)
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
    repeatability = commands.add_parser("audit-reference-repeatability")
    repeatability.add_argument("--benchmark-id", required=True)
    event_union = commands.add_parser("build-quality-union")
    event_union.add_argument("--benchmark-id", required=True)
    event_union_v2 = commands.add_parser("rebuild-quality-union-v2")
    event_union_v2.add_argument("--benchmark-id", required=True)
    repeatability_v2 = commands.add_parser("audit-reference-repeatability-v2")
    repeatability_v2.add_argument("--benchmark-id", required=True)
    gold = commands.add_parser("audit-gold")
    gold.add_argument("--benchmark-id", required=True)
    gold.add_argument("--adjudication", type=Path, required=True)
    gold_v2 = commands.add_parser("audit-gold-v2")
    gold_v2.add_argument("--benchmark-id", required=True)
    gold_v2.add_argument("--adjudication", type=Path, required=True)
    packet_build = commands.add_parser("build-gold-packets-v2")
    packet_build.add_argument("--benchmark-id", required=True)
    packet_import = commands.add_parser("import-gold-adjudication-v2")
    packet_import.add_argument("--benchmark-id", required=True)
    packet_import.add_argument("--input", type=Path, required=True)
    gold_status = commands.add_parser("gold-status-v2")
    gold_status.add_argument("--benchmark-id", required=True)
    miss_audit = commands.add_parser("audit-gold-material-misses-v2")
    miss_audit.add_argument("--benchmark-id", required=True)
    for command in ("audit-strategy-v2-run", "build-gold-extension-v2", "import-gold-extension-v2"):
        post = commands.add_parser(command)
        post.add_argument("--benchmark-id", required=True)
        post.add_argument("--run-name", required=True)
        if command == "import-gold-extension-v2":
            post.add_argument("--input", type=Path, required=True)
        else:
            post.add_argument("--adjudication", type=Path)
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
            result = prepare_run(
                root, args.benchmark_id, args.batch_size, reference=args.reference,
                run_name=args.run_name, strategy=args.strategy,
            )
        elif args.command == "prepare-reference-replication":
            result = prepare_reference_replication(root, args.benchmark_id)
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
        elif args.command == "audit-reference-repeatability":
            result = audit_reference_repeatability(root, args.benchmark_id)
        elif args.command == "build-quality-union":
            result = build_quality_union(root, args.benchmark_id)
        elif args.command == "rebuild-quality-union-v2":
            result = rebuild_quality_union_v2(root, args.benchmark_id)
        elif args.command == "audit-reference-repeatability-v2":
            result = audit_reference_repeatability_v2(root, args.benchmark_id)
        elif args.command == "audit-gold":
            result = audit_gold_adjudication(
                root, args.benchmark_id, args.adjudication.resolve()
            )
        elif args.command == "audit-gold-v2":
            result = audit_gold_adjudication_v2(
                root, args.benchmark_id, args.adjudication.resolve()
            )
        elif args.command == "build-gold-packets-v2":
            result = build_gold_packets_v2(root, args.benchmark_id)
        elif args.command == "import-gold-adjudication-v2":
            result = import_gold_adjudication_v2(
                root, args.benchmark_id, args.input.resolve()
            )
        elif args.command == "gold-status-v2":
            result = gold_status_v2(root, args.benchmark_id)
        elif args.command == "audit-gold-material-misses-v2":
            result = audit_gold_material_misses_v2(root, args.benchmark_id)
        elif args.command in {"audit-strategy-v2-run", "build-gold-extension-v2", "import-gold-extension-v2"}:
            from tools.company_news_post_run_v2 import audit_strategy_v2_run, import_extension
            if args.command == "import-gold-extension-v2":
                result = import_extension(root, args.benchmark_id, args.run_name, args.input)
            else:
                result = audit_strategy_v2_run(
                    root, args.benchmark_id, args.run_name, args.adjudication,
                    build_extension=args.command == "build-gold-extension-v2",
                )
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
