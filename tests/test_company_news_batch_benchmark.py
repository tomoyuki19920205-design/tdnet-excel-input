import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.company_news_batch_benchmark import (
    AUDIT_SCHEMA,
    BENCHMARK_SCHEMA,
    BenchmarkError,
    _busy_skip_incidents,
    _global_compression_metrics,
    _quality_metrics,
    _summary,
    activate_run,
    audit_integration_fixture,
    audit_run,
    benchmark_status,
    finalize_company,
    prepare_benchmark,
    prepare_integration_fixture,
    prepare_run,
    recommendation,
    release_task,
    research_finish,
    research_start,
    route_task,
    select_cohort,
    snapshot_task,
)
from tools.company_news_inbox_worker import WorkerPaths, run_once
from tools.company_news_queue import task_slot_mapping

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 1, 9, 0, tzinfo=JST)


def _master(path: Path, count: int = 240) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE market_data_universe(
          date TEXT,ticker TEXT,company_name TEXT,sector33_name TEXT,market_name TEXT,
          is_ordinary_stock INTEGER,is_common_stock INTEGER
        );
        CREATE TABLE market_data(ticker TEXT,date TEXT,market_cap REAL);
        """
    )
    companies = []
    market = []
    for index in range(count):
        ticker = f"{1000 + index:04d}"
        companies.append(("2026-08-31", ticker, f"Company {index}", f"Sector {index % 33:02d}", f"Market {index % 3}", 1, 1))
        market.append((ticker, "2026-08-31", float((index + 1) * 1_000_000)))
    connection.executemany("INSERT INTO market_data_universe VALUES(?,?,?,?,?,?,?)", companies)
    connection.executemany("INSERT INTO market_data VALUES(?,?,?)", market)
    connection.commit()
    connection.close()


def _news_db(path: Path, count: int = 120) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE canonical_news_scan_runs(ticker TEXT,status TEXT);
        CREATE TABLE canonical_news_events(ticker TEXT);
        """
    )
    connection.executemany("INSERT INTO canonical_news_scan_runs VALUES(?, 'completed')", [(f"{1000 + i:04d}",) for i in range(count)])
    connection.executemany("INSERT INTO canonical_news_events VALUES(?)", [(f"{1000 + i:04d}",) for i in range(0, count, 2)])
    connection.commit()
    connection.close()


def _prepare(tmp_path: Path, count: int = 192, reference_size: int = 48):
    tmp_path.mkdir(parents=True, exist_ok=True)
    master = tmp_path / "master.db"
    news = tmp_path / "news.db"
    _master(master, max(240, count + 20))
    _news_db(news)
    cohort = prepare_benchmark(
        tmp_path,
        master,
        news,
        companies=count,
        reference_size=reference_size,
        search_from=datetime(2026, 8, 25).date(),
        search_to=datetime(2026, 8, 31).date(),
        now=NOW,
    )
    return cohort, master, news


def _payload(assignment: dict, *, item: bool = False) -> dict:
    items = []
    if item:
        items = [{
            "headline": f"Material event for {assignment['ticker']}",
            "published_at": "2026-08-30T10:00:00+09:00",
            "source_type": "ir",
            "source_name": "Company IR",
            "source_url": f"https://example.com/{assignment['ticker']}/event",
            "category": "guidance",
            "subcategory": None,
            "direction": "positive",
            "importance": "high",
            "earnings_relevance": "direct",
            "summary": "The company changed its earnings outlook.",
            "why_it_matters": "The disclosure directly changes expected earnings.",
            "evidence_excerpt": None,
            "temporal_scope": "fiscal_year",
            "valid_from": None,
            "valid_until": None,
            "temporal_status": "current",
            "tags": [],
        }]
    return {
        "schema_version": "company_news_v1",
        "run_id": assignment["assignment_id"],
        "ticker": assignment["ticker"],
        "checked_at": "2026-08-31T12:00:00+09:00",
        "collector_type": "chatgpt_work",
        "collector_version": "batch_benchmark_v1",
        "analysis_version": "company_news_v1",
        "task_id": assignment["assignment_id"],
        "sources_checked_count": 3,
        "items": items,
    }


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _complete_snapshot(tmp_path: Path, task_id: str, snapshot: dict, started: datetime) -> datetime:
    cursor = started
    for assignment in snapshot["assignments"]:
        research_start(tmp_path, task_id, snapshot["run_token"], assignment["assignment_id"], now=cursor)
        cursor += timedelta(seconds=10)
        research_finish(tmp_path, task_id, snapshot["run_token"], assignment["assignment_id"], now=cursor)
        cursor += timedelta(seconds=1)
        _write_json(Path(assignment["temp_path"]), _payload(assignment))
        finalize_company(tmp_path, task_id, snapshot["run_token"], assignment["assignment_id"], now=cursor)
        cursor += timedelta(seconds=1)
    release_task(tmp_path, task_id, snapshot["run_token"], now=cursor)
    return cursor


def test_fixed_deterministic_stratified_cohort_and_reference(tmp_path):
    master = tmp_path / "master.db"
    news = tmp_path / "news.db"
    _master(master)
    _news_db(news)

    first = select_cohort(master, news, 192)
    second = select_cohort(master, news, 192)
    assert first == second
    assert len({company["ticker"] for company in first}) == 192
    assert len({company["sector"] for company in first}) == 33
    assert {company["size_bucket"] for company in first} == {"small", "mid", "large"}
    assert {company["prior_news_bucket"] for company in first} >= {"news_present", "no_news"}

    prepared, _, _ = _prepare(tmp_path / "prepared")
    assert prepared["schema_version"] == BENCHMARK_SCHEMA
    assert prepared["cohort_size"] == 192
    assert len(prepared["reference_tickers"]) == 48
    assert prepared["search_from"] == "2026-08-25"
    assert prepared["search_to"] == "2026-08-31"
    assert prepared["initial_candidates"] == [2, 3, 4]
    assert prepared["suspended_upward_candidates"] == [8, 12, 16, 20, 24]


@pytest.mark.parametrize("batch_size", [2, 3, 4])
def test_candidate_mapping_uses_eight_full_tasks_without_production_slots(tmp_path, batch_size):
    cohort, _, _ = _prepare(tmp_path)
    manifest = prepare_run(tmp_path, cohort["benchmark_id"], batch_size, now=NOW)
    assert manifest["batch_size"] == batch_size
    assert manifest["company_count"] == 192
    run_dir = tmp_path / "data" / "news_work" / "batch_benchmark" / cohort["benchmark_id"] / "runs" / f"batch{batch_size:02d}"
    for task_index in range(1, 9):
        queue = json.loads((run_dir / "tasks" / f"task{task_index:02d}" / "queue.json").read_text(encoding="utf-8"))
        assert len(queue["assignments"]) == 24
    assert Path(manifest["output_directory"]).is_relative_to(tmp_path / "data" / "news_benchmark_inbox")


@pytest.mark.parametrize(
    ("batch_size", "expected_slots"),
    ((2, ["bslot015", "bslot016"]), (3, ["bslot022", "bslot023", "bslot024"])),
)
def test_batch2_and_batch3_snapshot_mapping_and_company_position(tmp_path, batch_size, expected_slots):
    cohort, _, _ = _prepare(tmp_path, count=32, reference_size=8)
    run = prepare_run(tmp_path, cohort["benchmark_id"], batch_size, now=NOW)
    activate_run(tmp_path, cohort["benchmark_id"], run["run_name"], now=NOW)
    snapshot = snapshot_task(tmp_path, "task08", now=NOW)

    assert len(snapshot["assignments"]) == batch_size
    assert [assignment["company_position"] for assignment in snapshot["assignments"]] == list(range(1, batch_size + 1))
    assert [assignment["slot_id"] for assignment in snapshot["assignments"]] == expected_slots


def test_upward_candidate_prepare_is_suspended_after_batch4_quality_failure(tmp_path):
    cohort, _, _ = _prepare(tmp_path)
    for batch_size in (8, 12, 16, 20, 24):
        with pytest.raises(BenchmarkError, match="2 through 4"):
            prepare_run(tmp_path, cohort["benchmark_id"], batch_size, now=NOW)


def test_reference_run_is_single_company_batch_on_fixed_48_company_subset(tmp_path):
    cohort, _, _ = _prepare(tmp_path)
    manifest = prepare_run(tmp_path, cohort["benchmark_id"], 1, reference=True, now=NOW)
    assert manifest["reference"] is True
    assert manifest["batch_size"] == 1
    assert manifest["company_count"] == 48


def test_route_is_production_by_default_and_malformed_control_fails_closed(tmp_path):
    result = route_task(tmp_path, "task01")
    assert result["mode"] == "production"
    assert result["prompt_file"].endswith("SCHEDULED_TASK_TASK01_PROMPT.txt")

    active = tmp_path / "data" / "news_work" / "batch_benchmark" / "active.json"
    _write_json(active, {"benchmark_mode": True})
    with pytest.raises(BenchmarkError, match="fail closed"):
        route_task(tmp_path, "task01")


def test_company_and_task_timing_telemetry_and_atomic_isolated_save(tmp_path):
    cohort, _, _ = _prepare(tmp_path, count=32, reference_size=8)
    run = prepare_run(tmp_path, cohort["benchmark_id"], 4, now=NOW)
    activate_run(tmp_path, cohort["benchmark_id"], run["run_name"], now=NOW)
    snapshot = snapshot_task(tmp_path, "task01", now=NOW)
    assignment = snapshot["assignments"][0]
    assert len(snapshot["assignments"]) == 4

    research_start(tmp_path, "task01", snapshot["run_token"], assignment["assignment_id"], now=NOW + timedelta(seconds=1))
    research_finish(tmp_path, "task01", snapshot["run_token"], assignment["assignment_id"], now=NOW + timedelta(seconds=31))
    _write_json(Path(assignment["temp_path"]), _payload(assignment, item=True))
    finalized = finalize_company(
        tmp_path, "task01", snapshot["run_token"], assignment["assignment_id"], now=NOW + timedelta(seconds=35)
    )
    assert finalized["research_elapsed_seconds"] == 30
    assert finalized["company_total_elapsed_seconds"] == 34
    assert finalized["company_position"] == 1
    assert finalized["source_count"] == 3
    assert finalized["event_count"] == 1
    assert finalized["result_type"] == "news_present"
    assert Path(assignment["output_path"]).exists()
    assert not Path(assignment["temp_path"]).exists()
    assert Path(assignment["output_path"]).is_relative_to(tmp_path / "data" / "news_benchmark_inbox")

    # Finish the remaining immutable snapshot so task release can be audited.
    for offset, current in enumerate(snapshot["assignments"][1:], start=2):
        research_start(tmp_path, "task01", snapshot["run_token"], current["assignment_id"], now=NOW + timedelta(seconds=offset * 40))
        research_finish(tmp_path, "task01", snapshot["run_token"], current["assignment_id"], now=NOW + timedelta(seconds=offset * 40 + 20))
        _write_json(Path(current["temp_path"]), _payload(current))
        finalize_company(tmp_path, "task01", snapshot["run_token"], current["assignment_id"], now=NOW + timedelta(seconds=offset * 40 + 25))
    released = release_task(tmp_path, "task01", snapshot["run_token"], now=NOW + timedelta(minutes=4))
    assert released["snapshot_company_count"] == 4
    assert released["task_elapsed_seconds"] == 240
    assert released["success_count"] == 4
    assert released["failure_count"] == 0
    assert released["first_company_started_at"]
    assert released["last_company_finished_at"]
    assert released["research_elapsed_sum"] == 90


def test_completed_run_auto_finalizes_once_all_snapshots_are_released(tmp_path):
    cohort, _, _ = _prepare(tmp_path, count=8, reference_size=8)
    run = prepare_run(tmp_path, cohort["benchmark_id"], 4, now=NOW)
    activate_run(tmp_path, cohort["benchmark_id"], run["run_name"], now=NOW)
    expected_completed_at = None
    for index in range(1, 9):
        task_id = f"task{index:02d}"
        snapshot = snapshot_task(tmp_path, task_id, now=NOW + timedelta(minutes=index))
        expected_completed_at = _complete_snapshot(
            tmp_path, task_id, snapshot, NOW + timedelta(minutes=index, seconds=1)
        )

    status = benchmark_status(tmp_path, cohort["benchmark_id"], run["run_name"])
    manifest_path = (
        tmp_path / "data" / "news_work" / "batch_benchmark" / cohort["benchmark_id"]
        / "runs" / "batch04" / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert status["status"] == "completed"
    assert status["completed"] == 8
    assert status["pending"] == 0
    assert manifest["status"] == "completed"
    assert manifest["completed_at"] == expected_completed_at.isoformat(timespec="milliseconds")
    assert not (tmp_path / "data" / "news_work" / "batch_benchmark" / "active.json").exists()


def test_completed_at_and_throughput_do_not_depend_on_late_audit_capture(tmp_path):
    cohort, _, _ = _prepare(tmp_path, count=8, reference_size=8)
    run = prepare_run(tmp_path, cohort["benchmark_id"], 4, now=NOW)
    activate_run(tmp_path, cohort["benchmark_id"], run["run_name"], now=NOW)
    for index in range(1, 9):
        task_id = f"task{index:02d}"
        snapshot = snapshot_task(tmp_path, task_id, now=NOW + timedelta(minutes=index))
        _complete_snapshot(tmp_path, task_id, snapshot, NOW + timedelta(minutes=index, seconds=1))

    first = audit_run(tmp_path, cohort["benchmark_id"], run["run_name"], now=NOW + timedelta(hours=2))
    second = audit_run(tmp_path, cohort["benchmark_id"], run["run_name"], now=NOW + timedelta(hours=9))
    assert first["captured_at"] != second["captured_at"]
    assert first["status"]["completed_at"] == second["status"]["completed_at"]
    assert first["throughput"] == second["throughput"]


def test_candidate_audit_emits_global_compression_against_completed_reference(tmp_path):
    cohort, _, _ = _prepare(tmp_path, count=8, reference_size=8)
    reference = prepare_run(tmp_path, cohort["benchmark_id"], 1, reference=True, now=NOW)
    activate_run(tmp_path, cohort["benchmark_id"], reference["run_name"], now=NOW)
    for index in range(1, 9):
        task_id = f"task{index:02d}"
        snapshot = snapshot_task(tmp_path, task_id, now=NOW + timedelta(minutes=index))
        _complete_snapshot(tmp_path, task_id, snapshot, NOW + timedelta(minutes=index, seconds=1))
    audit_run(tmp_path, cohort["benchmark_id"], "reference", now=NOW + timedelta(hours=1))

    candidate = prepare_run(tmp_path, cohort["benchmark_id"], 2, now=NOW + timedelta(hours=2))
    activate_run(tmp_path, cohort["benchmark_id"], candidate["run_name"], now=NOW + timedelta(hours=2))
    for index in range(1, 9):
        task_id = f"task{index:02d}"
        snapshot = snapshot_task(tmp_path, task_id, now=NOW + timedelta(hours=2, minutes=index))
        _complete_snapshot(
            tmp_path,
            task_id,
            snapshot,
            NOW + timedelta(hours=2, minutes=index, seconds=1),
        )

    audit = audit_run(tmp_path, cohort["benchmark_id"], "batch02", now=NOW + timedelta(hours=3))
    compression = audit["global_compression"]
    assert compression["source_count_ratio_vs_reference"] == 1.0
    assert compression["research_time_ratio_vs_reference"] == 1.0
    assert compression["event_recall_vs_reference"] == 1.0
    assert compression["candidate_research_elapsed_seconds"] == {
        "average": 10.0,
        "p50": 10.0,
        "p95": 10.0,
        "max": 10.0,
    }


def test_busy_skip_recovered_for_same_guard_is_classified_as_recoverable(tmp_path):
    cohort, _, _ = _prepare(tmp_path, count=8, reference_size=8)
    run = prepare_run(tmp_path, cohort["benchmark_id"], 4, now=NOW)
    activate_run(tmp_path, cohort["benchmark_id"], run["run_name"], now=NOW)
    first = snapshot_task(tmp_path, "task01", now=NOW)
    assignment = first["assignments"][0]
    research_start(tmp_path, "task01", first["run_token"], assignment["assignment_id"], now=NOW + timedelta(seconds=1))
    research_finish(tmp_path, "task01", first["run_token"], assignment["assignment_id"], now=NOW + timedelta(seconds=10))

    assert snapshot_task(tmp_path, "task01", now=NOW + timedelta(hours=1))["status"] == "busy"
    snapshot_task(tmp_path, "task01", now=NOW + timedelta(hours=2, seconds=1))
    run_dir = (
        tmp_path / "data" / "news_work" / "batch_benchmark" / cohort["benchmark_id"]
        / "runs" / "batch04"
    )
    incidents = _busy_skip_incidents(run_dir, [])
    assert len(incidents) == 1
    assert incidents[0]["run_id"] == first["run_id"]
    assert incidents[0]["run_token"] == first["run_token"]
    assert incidents[0]["guard_age_seconds"] == 3600
    assert incidents[0]["stale_before"] is False
    assert incidents[0]["stale_after"] is True
    assert incidents[0]["same_incident_as_stale_guard_recovery"] is True
    assert incidents[0]["classification"] == "recoverable_stale_guard_skip"


def test_release_creates_short_history_parent_and_missing_source_stays_fail_closed(tmp_path):
    cohort, _, _ = _prepare(tmp_path, count=32, reference_size=8)
    run = prepare_run(tmp_path, cohort["benchmark_id"], 1, reference=True, now=NOW)
    activate_run(tmp_path, cohort["benchmark_id"], run["run_name"], now=NOW)
    snapshot = snapshot_task(tmp_path, "task01", now=NOW)
    assignment = snapshot["assignments"][0]
    active = (
        tmp_path / "data" / "news_work" / "batch_benchmark" / cohort["benchmark_id"]
        / "runs" / "reference" / "tasks" / "task01" / "active.json"
    )
    history = active.parent / "history"
    assert active.exists()
    assert not history.exists()

    research_start(tmp_path, "task01", snapshot["run_token"], assignment["assignment_id"], now=NOW + timedelta(seconds=1))
    research_finish(tmp_path, "task01", snapshot["run_token"], assignment["assignment_id"], now=NOW + timedelta(seconds=11))
    _write_json(Path(assignment["temp_path"]), _payload(assignment))
    finalize_company(tmp_path, "task01", snapshot["run_token"], assignment["assignment_id"], now=NOW + timedelta(seconds=12))

    released = release_task(tmp_path, "task01", snapshot["run_token"], now=NOW + timedelta(seconds=13))
    completed = Path(released["history_path"])
    assert history.is_dir()
    assert completed.parent == history
    assert completed.name.startswith("completed-")
    assert len(completed.name) == len("completed-") + 16 + len(".json")
    assert completed.exists()
    assert not active.exists()

    with pytest.raises(BenchmarkError, match="invalid JSON file"):
        release_task(tmp_path, "task01", snapshot["run_token"], now=NOW + timedelta(seconds=14))


def test_immutable_snapshot_never_adds_n_plus_one_company(tmp_path):
    cohort, _, _ = _prepare(tmp_path)
    run = prepare_run(tmp_path, cohort["benchmark_id"], 4, now=NOW)
    activate_run(tmp_path, cohort["benchmark_id"], run["run_name"], now=NOW)
    first = snapshot_task(tmp_path, "task01", now=NOW)
    original_ids = [assignment["assignment_id"] for assignment in first["assignments"]]
    assert len(original_ids) == 4
    # The task queue contains 24 companies, but the active snapshot remains exactly four.
    queue_path = tmp_path / "data" / "news_work" / "batch_benchmark" / cohort["benchmark_id"] / "runs" / "batch04" / "tasks" / "task01" / "queue.json"
    assert len(json.loads(queue_path.read_text(encoding="utf-8"))["assignments"]) == 24
    assert [assignment["assignment_id"] for assignment in first["assignments"]] == original_ids
    assert snapshot_task(tmp_path, "task01", now=NOW + timedelta(minutes=10))["status"] == "busy"


def test_benchmark_harness_cannot_mutate_production_queue_db_or_supabase(tmp_path, monkeypatch):
    production_queue = tmp_path / "data" / "news_work" / "queue" / "queue_state.json"
    production_db = tmp_path / "decision_db.db"
    _write_json(production_queue, {"queue_id": "production", "queue_status": "completed"})
    sqlite3.connect(production_db).execute("CREATE TABLE sentinel(value TEXT)").connection.close()
    queue_before = production_queue.read_bytes()
    db_before = production_db.read_bytes()
    monkeypatch.setenv("SUPABASE_POSTGRES_URL", "must-not-be-used")

    master = tmp_path / "master.db"
    _master(master)
    cohort = prepare_benchmark(tmp_path, master, production_db, companies=32, reference_size=8, now=NOW)
    prepare_run(tmp_path, cohort["benchmark_id"], 4, now=NOW)

    assert production_queue.read_bytes() == queue_before
    assert production_db.read_bytes() == db_before
    assert not (tmp_path / "data" / "news_inbox").exists()


def test_default_worker_ignores_benchmark_inbox(tmp_path):
    benchmark_file = tmp_path / "data" / "news_benchmark_inbox" / "isolated.json"
    _write_json(benchmark_file, {"schema_version": "company_news_v1"})
    paths = WorkerPaths.from_values(tmp_path)
    called = []
    result = run_once(paths, sync_func=lambda *_args, **_kwargs: called.append(True))
    assert paths.inbox == (tmp_path / "data" / "news_inbox").resolve()
    assert called == []
    assert benchmark_file.exists()
    assert result["detected"] == 0


def test_quality_fixture_calculates_recall_precision_critical_and_false_negative():
    def event(ticker: str, suffix: str, *, critical: bool = False) -> dict:
        return {
            "ticker": ticker,
            "headline": f"Guidance revision {suffix}",
            "summary": "Earnings guidance changed materially.",
            "source_url": f"https://example.com/{ticker}/{suffix}",
            "published_at": "2026-08-30T10:00:00+09:00",
            "category": "guidance",
            "importance": "high" if critical else "medium",
            "earnings_relevance": "direct" if critical else "likely",
        }

    reference = {"1000": [event("1000", "a", critical=True), event("1000", "b")], "1001": [event("1001", "c")]}
    unrelated = event("1000", "extra")
    unrelated["category"] = "demand"
    candidate = {"1000": [event("1000", "a", critical=True), unrelated]}
    quality, company_recall, ambiguous = _quality_metrics(reference, candidate, {"1000", "1001"})
    assert quality["event_recall"] == pytest.approx(1 / 3)
    assert quality["critical_event_recall"] == 1.0
    assert quality["precision"] == pytest.approx(1 / 2)
    assert quality["no_news_false_negative_count"] == 1
    assert company_recall == {"1000": 0.5, "1001": 0.0}
    assert ambiguous == []


def test_global_compression_metrics_compare_source_depth_time_and_event_recall():
    result = _global_compression_metrics(
        [7.0, 7.0, 7.0, 8.0],
        [4.0, 5.0],
        [40.0, 50.0, 60.0],
        [20.0, 30.0],
        0.95,
    )

    assert result["reference_source_count_per_company"] == {"average": 7.25, "median": 7.0}
    assert result["candidate_source_count_per_company"] == {"average": 4.5, "median": 4.5}
    assert result["source_count_ratio_vs_reference"] == pytest.approx(4.5 / 7.25)
    assert result["reference_research_elapsed_seconds"] == {
        "average": 50.0,
        "p50": 50.0,
        "p95": 60.0,
        "max": 60.0,
    }
    assert result["candidate_research_elapsed_seconds"] == {
        "average": 25.0,
        "p50": 25.0,
        "p95": 30.0,
        "max": 30.0,
    }
    assert result["research_time_ratio_vs_reference"] == 0.5
    assert result["event_recall_vs_reference"] == 0.95


def _recommendation_audit(decision, *, event_recall, critical_recall, no_news_false_negatives):
    return {
        "schema_version": AUDIT_SCHEMA,
        "decision": decision,
        "criteria": {
            "complete": True,
            "schema_validity_100pct": True,
            "research_failure_zero": True,
            "source_validity_at_least_99pct": True,
            "p95_under_45_minutes": True,
            "max_under_55_minutes": True,
            "capacity_busy_skip_zero": True,
            "overlap_zero": True,
            "critical_recall_100pct": critical_recall == 1.0,
            "material_event_recall_at_least_95pct": event_recall >= 0.95,
            "major_no_news_false_negative_zero": no_news_false_negatives == 0,
        },
        "research": {"average_source_count_per_company": 6.0},
        "runtime": {
            "task_elapsed_seconds": {"p95": 1800, "max": 2400},
            "company_research_elapsed_seconds": {"average": 40.0},
        },
        "quality": {
            "event_recall": event_recall,
            "critical_event_recall": critical_recall,
            "no_news_false_negative_count": no_news_false_negatives,
        },
        "throughput": {
            "companies_per_hour": 24.0,
            "estimated_hours_for_3800": 3800 / 24,
            "estimated_days_for_3800": 3800 / 24 / 24,
            "weekly_buffer_hours": 168 - 3800 / 24,
        },
    }


def test_downward_recommendation_selects_batch3_and_batch4_fail_blocks_upward(tmp_path):
    assert _summary([600, 1200, 2700, 3300]) == {"average": 1950, "p50": 1950.0, "p95": 3300, "max": 3300}
    cohort, _, _ = _prepare(tmp_path, count=32, reference_size=8)
    benchmark_dir = tmp_path / "data" / "news_work" / "batch_benchmark" / cohort["benchmark_id"]
    _write_json(benchmark_dir / "runs" / "reference" / "audit.json", {
        "schema_version": AUDIT_SCHEMA,
        "research": {"average_source_count_per_company": 7.2},
        "runtime": {"company_research_elapsed_seconds": {"average": 50.0}},
    })
    for batch in (2, 3, 4):
        prepare_run(tmp_path, cohort["benchmark_id"], batch, now=NOW)
        audit = _recommendation_audit(
            "FAIL" if batch == 4 else "PASS",
            event_recall=0.5344827586206896 if batch == 4 else 0.98,
            critical_recall=0.75 if batch == 4 else 1.0,
            no_news_false_negatives=7 if batch == 4 else 0,
        )
        _write_json(benchmark_dir / "runs" / f"batch{batch:02d}" / "audit.json", audit)
    # Even a stray historical upward audit cannot override the batch04 quality stop.
    _write_json(
        benchmark_dir / "runs" / "batch08" / "audit.json",
        _recommendation_audit("PASS", event_recall=0.99, critical_recall=1.0, no_news_false_negatives=0),
    )

    result = recommendation(tmp_path, cohort["benchmark_id"])
    assert result["max_validated_batch_size"] == 3
    assert result["recommended_batch_size"] == 3
    assert result["next_candidates"] == []
    assert result["suspended_upward_candidates"] == [8, 12, 16, 20, 24]
    assert result["upward_candidates_blocked"] is True
    assert result["batch04_formal_judgment"] == {
        "runtime": "PASS",
        "reliability": "PASS",
        "quality": "FAIL",
    }
    assert result["production_batch_4_quality_validated"] is False
    assert result["throughput_tradeoffs"]["4"]["quality_failure_disqualifies_speed"] is True
    assert result["throughput_tradeoffs"]["1"]["decision"] == "REFERENCE"
    assert result["source_count_ratio_vs_reference"]["1"] == 1.0
    assert result["research_time_ratio_vs_reference"]["1"] == 1.0
    assert result["event_recall_vs_reference"]["1"] == 1.0
    assert result["source_count_ratio_vs_reference"]["4"] == pytest.approx(6.0 / 7.2)
    assert result["research_time_ratio_vs_reference"]["4"] == 0.8
    assert result["event_recall_vs_reference"]["4"] == pytest.approx(0.5344827586206896)


def test_existing_batch4_fail_requests_batch2_before_any_other_candidate(tmp_path):
    cohort, _, _ = _prepare(tmp_path, count=32, reference_size=8)
    benchmark_dir = tmp_path / "data" / "news_work" / "batch_benchmark" / cohort["benchmark_id"]
    prepare_run(tmp_path, cohort["benchmark_id"], 4, now=NOW)
    _write_json(
        benchmark_dir / "runs" / "batch04" / "audit.json",
        _recommendation_audit(
            "FAIL",
            event_recall=0.5344827586206896,
            critical_recall=0.75,
            no_news_false_negatives=7,
        ),
    )

    result = recommendation(tmp_path, cohort["benchmark_id"])
    assert result["recommended_batch_size"] == 1
    assert result["next_candidates"] == [2]
    assert result["upward_candidates_blocked"] is True
    assert result["upward_block_reason"] == "batch04_quality_fail"
    assert result["production_batch_4_quality_validated"] is False


@pytest.mark.parametrize(
    ("decisions", "expected", "redesign"),
    [
        ({2: "PASS", 3: "FAIL"}, 2, False),
        ({2: "FAIL"}, 1, True),
    ],
)
def test_downward_recommendation_falls_back_to_last_contiguous_quality_pass(
    tmp_path, decisions, expected, redesign
):
    cohort, _, _ = _prepare(tmp_path, count=32, reference_size=8)
    benchmark_dir = tmp_path / "data" / "news_work" / "batch_benchmark" / cohort["benchmark_id"]
    for batch, decision in decisions.items():
        prepare_run(tmp_path, cohort["benchmark_id"], batch, now=NOW)
        audit = _recommendation_audit(
            decision,
            event_recall=0.98 if decision == "PASS" else 0.90,
            critical_recall=1.0,
            no_news_false_negatives=0 if decision == "PASS" else 1,
        )
        _write_json(benchmark_dir / "runs" / f"batch{batch:02d}" / "audit.json", audit)

    result = recommendation(tmp_path, cohort["benchmark_id"])
    assert result["recommended_batch_size"] == expected
    assert result["max_validated_batch_size"] == expected
    assert result["next_candidates"] == []
    assert result["research_architecture_redesign_required"] is redesign


def test_integration_load_fixture_is_isolated_and_has_system_impact_metrics(tmp_path):
    result = prepare_integration_fixture(tmp_path, 3, now=NOW)
    assert result["file_count"] == 24
    assert Path(result["fixture_inbox"]).is_relative_to(tmp_path / "data" / "news_benchmark_load")
    assert len(list(Path(result["fixture_inbox"]).glob("*.json"))) == 24
    assert result["requires_temporary_sqlite_copy"] is True
    assert result["supabase_mode"] == "disabled_or_test_endpoint_only"
    assert {"worker_run_duration_seconds", "sqlite_lock_wait_seconds", "tdnet_process_duration_seconds", "supabase_sync_duration_seconds"} <= set(result["metrics_template"])
    result_path = Path(result["fixture_inbox"]).parent / "human_result.json"
    metrics = {key: 0 for key in result["metrics_template"]}
    metrics.update({
        "worker_run_duration_seconds": 12,
        "max_inbox_backlog": 24,
        "sqlite_lock_wait_seconds": 0.2,
        "tdnet_ingest_duration_seconds": 10,
        "tdnet_process_duration_seconds": 20,
        "tdnet_notify_duration_seconds": 2,
        "supabase_sync_duration_seconds": 5,
        "tdnet_ingest_baseline_seconds": 9,
        "tdnet_process_baseline_seconds": 18,
        "tdnet_notify_baseline_seconds": 2,
    })
    _write_json(result_path, {"fixture_id": result["fixture_id"], "metrics": metrics})
    audit = audit_integration_fixture(result_path.parent / "manifest.json", result_path, now=NOW)
    assert audit["decision"] == "PASS"
    assert audit["production_data_used"] is False


def test_production_eight_by_four_mapping_remains_backward_compatible(tmp_path):
    assert task_slot_mapping(8, 4) == {
        f"task{task:02d}": [f"slot{slot:02d}" for slot in range((task - 1) * 4 + 1, task * 4 + 1)]
        for task in range(1, 9)
    }
    assert route_task(tmp_path, "task08")["mode"] == "production"


def test_setup_files_route_fail_closed_without_changing_schedules():
    work = Path(__file__).resolve().parents[1] / "data" / "news_work"
    minutes = ("00", "08", "15", "23", "30", "38", "45", "53")
    for index, minute in enumerate(minutes, start=1):
        setup = (work / f"SCHEDULED_TASK_SETUP_TASK{index:02d}.txt").read_text(encoding="utf-8")
        assert f"route --task-id task{index:02d}" in setup
        assert f"毎時{minute}分" in setup
        assert "mode=production" in setup
        assert "mode=benchmark" in setup
        assert "fail closed" in setup
    benchmark_prompt = (work / "SCHEDULED_TASK_BATCH_BENCHMARK_PROMPT.txt").read_text(encoding="utf-8")
    assert "data/news_benchmark_inbox" in benchmark_prompt
    assert "data/news_inboxへの保存" in benchmark_prompt
    assert "snapshot_company_countを超えるN+1社目" in benchmark_prompt
