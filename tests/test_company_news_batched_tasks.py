import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.company_news_inbox_worker import WorkerPaths, run_once
from tools.company_news_queue import QueuePaths, initialize_pilot, queue_status, task_slot_mapping
from tools.company_news_task_batch import release_task, snapshot_task
from tools.company_news_work_bridge import BridgePaths, expected_failure_path, expected_output_path

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 8, 30, 8, 0, tzinfo=JST)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _master(path: Path, count: int = 240) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE companies(ticker_code TEXT, name_ja TEXT, sector33_name TEXT)")
    connection.executemany(
        "INSERT INTO companies VALUES(?,?,?)",
        [
            (f"{2000 + index:04d}", f"Company {index:03d}", f"Sector {index % 33:02d}")
            for index in range(1, count + 1)
        ],
    )
    connection.commit()
    connection.close()


def _setup(
    tmp_path: Path,
    *,
    count: int = 100,
    activate: bool = True,
    batch_size: int = 3,
):
    master = tmp_path / "master.db"
    db = tmp_path / "news.db"
    _master(master, max(240, count + 40))
    queue = QueuePaths.from_values(tmp_path)
    bridge = BridgePaths.from_root(tmp_path)
    result = initialize_pilot(
        queue,
        bridge,
        master,
        db,
        activate=activate,
        now=NOW,
        count=count,
        task_count=8,
        batch_size=batch_size,
        stratified=True,
    )
    return queue, bridge, WorkerPaths.from_values(tmp_path, db=db), result


def _bridge(root: Path, slot_id: str) -> BridgePaths:
    return BridgePaths.from_root(root, slot_id=slot_id)


def _assignment(root: Path, slot_id: str) -> dict:
    return json.loads(_bridge(root, slot_id).assignment.read_text(encoding="utf-8"))


def _entries(queue: QueuePaths) -> list[dict]:
    return [json.loads(line) for line in queue.entries.read_text(encoding="utf-8").splitlines() if line]


def _payload(assignment: dict, *, items: list | None = None) -> dict:
    return {
        "schema_version": "company_news_v1",
        "run_id": assignment["assignment_id"],
        "ticker": assignment["ticker"],
        "checked_at": "2026-08-30T08:00:00+09:00",
        "collector_type": "chatgpt_work",
        "task_id": assignment["assignment_id"],
        "sources_checked_count": 1,
        "items": [] if items is None else items,
    }


def _save_success(root: Path, slot_id: str) -> dict:
    assignment = _assignment(root, slot_id)
    _write_json(expected_output_path(_bridge(root, slot_id), assignment), _payload(assignment))
    return assignment


def _save_failure(root: Path, slot_id: str) -> dict:
    assignment = _assignment(root, slot_id)
    value = {
        "schema_version": "company_news_work_failure_v1",
        "task_id": assignment["scheduled_task_id"],
        "slot_id": slot_id,
        "assignment_id": assignment["assignment_id"],
        "ticker": assignment["ticker"],
        "queue_id": assignment["queue_id"],
        "error_type": "source_unavailable",
        "error_message": "Required source could not be checked",
        "sources_attempted": ["https://example.com/unavailable"],
        "created_at": "2026-08-30T08:00:00+09:00",
    }
    _write_json(expected_failure_path(_bridge(root, slot_id), assignment), value)
    return assignment


def _sync(*_):
    return {"canonical_news_events": 0, "canonical_news_scan_runs": 1}


def test_twenty_four_slot_initial_fill_is_unique_and_task_mapped(tmp_path):
    queue, bridge, _, result = _setup(tmp_path)
    assert result["logical_slot_count"] == 24
    assert result["task_slots"] == task_slot_mapping(8, 3)
    assigned = [entry for entry in _entries(queue) if entry["status"] == "assigned"]
    assert len(assigned) == 24
    assert len({entry["ticker"] for entry in assigned}) == 24
    assert len({entry["queue_position"] for entry in assigned}) == 24
    assert len({entry["assignment_id"] for entry in assigned}) == 24
    status = queue_status(queue, bridge)
    assert status["active"] == 24
    assert status["pending"] == 76
    assert all(len(slots) == 3 for slots in status["tasks"].values())


def test_thirty_two_slot_initial_fill_is_unique_and_task_mapped(tmp_path):
    queue, bridge, _, result = _setup(tmp_path, count=200, batch_size=4)
    assert result["logical_slot_count"] == 32
    assert result["task_slots"] == task_slot_mapping(8, 4)
    assert result["task_slots"] == {
        f"task{index:02d}": [f"slot{slot:02d}" for slot in range((index - 1) * 4 + 1, index * 4 + 1)]
        for index in range(1, 9)
    }
    assigned = [entry for entry in _entries(queue) if entry["status"] == "assigned"]
    assert len(assigned) == 32
    assert len({entry["ticker"] for entry in assigned}) == 32
    assert len({entry["queue_position"] for entry in assigned}) == 32
    assert len({entry["assignment_id"] for entry in assigned}) == 32
    status = queue_status(queue, bridge)
    assert status["active"] == 32
    assert status["pending"] == 168
    assert all(len(slots) == 4 for slots in status["tasks"].values())


def test_task_snapshot_never_picks_refill_as_fourth_company(tmp_path):
    _, _, worker, _ = _setup(tmp_path)
    snapshot = snapshot_task(tmp_path, "task01", now=NOW)
    original_ids = [item["assignment_id"] for item in snapshot["assignments"]]
    assert len(original_ids) == 3
    _save_success(tmp_path, "slot01")
    run_once(worker, sync_func=_sync)
    refill = _assignment(tmp_path, "slot01")
    assert refill["assignment_id"] not in original_ids
    assert [item["assignment_id"] for item in snapshot["assignments"]] == original_ids
    overlapping = snapshot_task(tmp_path, "task01", now=NOW + timedelta(hours=1))
    assert overlapping["status"] == "busy"
    release_task(tmp_path, "task01", snapshot["run_token"], success_count=1, now=NOW + timedelta(minutes=10))
    next_run = snapshot_task(tmp_path, "task01", now=NOW + timedelta(hours=2, minutes=1))
    assert refill["assignment_id"] in {item["assignment_id"] for item in next_run["assignments"]}


def test_eight_by_four_snapshot_never_picks_refill_as_fifth_company(tmp_path):
    _, _, worker, _ = _setup(tmp_path, count=200, batch_size=4)
    snapshot = snapshot_task(tmp_path, "task01", now=NOW)
    original_ids = [item["assignment_id"] for item in snapshot["assignments"]]
    assert len(original_ids) == 4

    _save_success(tmp_path, "slot01")
    run_once(worker, sync_func=_sync)
    refill = _assignment(tmp_path, "slot01")
    assert refill["assignment_id"] not in original_ids
    assert [item["assignment_id"] for item in snapshot["assignments"]] == original_ids

    overlapping = snapshot_task(tmp_path, "task01", now=NOW + timedelta(hours=1))
    assert overlapping["status"] == "busy"
    events = [
        json.loads(line)
        for line in (tmp_path / "data" / "news_work" / "task_runs" / "task01" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["event"] == "busy_skip"
    assert events[-1]["queue_id"] == _assignment(tmp_path, "slot01")["queue_id"]
    release_task(tmp_path, "task01", snapshot["run_token"], success_count=1, now=NOW + timedelta(minutes=10))
    next_run = snapshot_task(tmp_path, "task01", now=NOW + timedelta(hours=2, minutes=1))
    assert refill["assignment_id"] in {item["assignment_id"] for item in next_run["assignments"]}


def test_expired_eight_by_four_task_guard_is_recovered_and_auditable(tmp_path):
    _setup(tmp_path, count=200, batch_size=4)
    first = snapshot_task(tmp_path, "task01", now=NOW)

    recovered = snapshot_task(tmp_path, "task01", now=NOW + timedelta(hours=2, minutes=1))

    assert recovered["status"] == "snapshot_created"
    assert recovered["run_id"] != first["run_id"]
    stale = tmp_path / "data" / "news_work" / "task_runs" / "task01" / "history" / f"{first['run_id']}.stale.json"
    assert stale.exists()
    events = [
        json.loads(line)
        for line in (tmp_path / "data" / "news_work" / "task_runs" / "task01" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["event"] == "stale_guard_recovered"
    assert events[-1]["recovered_run_id"] == first["run_id"]


def test_partial_task_snapshots_only_ready_existing_slots(tmp_path):
    _, _, _, _ = _setup(tmp_path)
    _bridge(tmp_path, "slot08").assignment.unlink()
    snapshot = snapshot_task(tmp_path, "task03", now=NOW)
    assert [item["slot_id"] for item in snapshot["assignments"]] == ["slot07", "slot09"]


def test_eight_by_four_partial_snapshot_only_contains_ready_existing_slots(tmp_path):
    _, _, _, _ = _setup(tmp_path, count=200, batch_size=4)
    _bridge(tmp_path, "slot02").assignment.unlink()
    _bridge(tmp_path, "slot04").assignment.unlink()
    snapshot = snapshot_task(tmp_path, "task01", now=NOW)
    assert [item["slot_id"] for item in snapshot["assignments"]] == ["slot01", "slot03"]


def test_one_operational_failure_does_not_discard_two_successes(tmp_path):
    queue, _, worker, _ = _setup(tmp_path)
    success10 = _save_success(tmp_path, "slot10")
    failed11 = _save_failure(tmp_path, "slot11")
    success12 = _save_success(tmp_path, "slot12")
    result = run_once(worker, sync_func=_sync)
    assert result["completed"] == 2
    assert _assignment(tmp_path, "slot11")["queue_attempt"] == 2
    assert _assignment(tmp_path, "slot11")["assignment_id"] != failed11["assignment_id"]
    entries = _entries(queue)
    assert {entry["assignment_id"] for entry in entries if entry["status"] == "completed"} == {
        success10["assignment_id"], success12["assignment_id"],
    }


def test_four_company_operational_failure_is_isolated_from_three_successes(tmp_path):
    queue, _, worker, _ = _setup(tmp_path, count=200, batch_size=4)
    success = [_save_success(tmp_path, slot_id) for slot_id in ("slot01", "slot02", "slot04")]
    failed = _save_failure(tmp_path, "slot03")

    result = run_once(worker, sync_func=_sync)

    assert result["completed"] == 3
    retry = _assignment(tmp_path, "slot03")
    assert retry["queue_attempt"] == 2
    assert retry["assignment_id"] != failed["assignment_id"]
    assert {entry["assignment_id"] for entry in _entries(queue) if entry["status"] == "completed"} == {
        assignment["assignment_id"] for assignment in success
    }


def test_one_schema_validation_failure_retries_while_batch_peers_complete(tmp_path):
    queue, bridge, worker, _ = _setup(tmp_path)
    failed10 = _assignment(tmp_path, "slot10")
    invalid = _payload(failed10)
    invalid["items"] = [{"source_url": "https://example.com/missing-headline"}]
    _write_json(expected_output_path(_bridge(tmp_path, "slot10"), failed10), invalid)
    success11 = _save_success(tmp_path, "slot11")
    success12 = _save_success(tmp_path, "slot12")

    result = run_once(worker, sync_func=_sync)

    assert result["completed"] == 2
    assert queue_status(queue, bridge)["metrics"]["validation_failure_count"] == 1
    retry10 = _assignment(tmp_path, "slot10")
    assert retry10["ticker"] == failed10["ticker"]
    assert retry10["queue_attempt"] == 2
    assert {entry["assignment_id"] for entry in _entries(queue) if entry["status"] == "completed"} == {
        success11["assignment_id"], success12["assignment_id"],
    }


def test_three_no_news_payloads_complete_normally(tmp_path):
    queue, _, worker, _ = _setup(tmp_path)
    for slot_id in ("slot01", "slot02", "slot03"):
        _save_success(tmp_path, slot_id)
    run_once(worker, sync_func=_sync)
    completed = [entry for entry in _entries(queue) if entry["status"] == "completed"]
    assert len(completed) == 3
    assert all(entry["news_item_count"] == 0 for entry in completed)


def test_two_tasks_complete_six_unique_companies_and_get_six_refills(tmp_path):
    queue, _, worker, _ = _setup(tmp_path)
    old_ids = {_save_success(tmp_path, f"slot{index:02d}")["assignment_id"] for index in range(1, 7)}
    run_once(worker, sync_func=_sync)
    new_assignments = [_assignment(tmp_path, f"slot{index:02d}") for index in range(1, 7)]
    assert len({item["assignment_id"] for item in new_assignments}) == 6
    assert not old_ids.intersection(item["assignment_id"] for item in new_assignments)
    assert sum(entry["status"] == "completed" for entry in _entries(queue)) == 6


def test_eight_tasks_complete_thirty_two_unique_companies_and_get_unique_refills(tmp_path):
    queue, bridge, worker, _ = _setup(tmp_path, count=200, batch_size=4)
    old_ids = {
        _save_success(tmp_path, f"slot{index:02d}")["assignment_id"]
        for index in range(1, 33)
    }

    result = run_once(worker, sync_func=_sync)

    assert result["completed"] == 32
    status = queue_status(queue, bridge)
    current = [_assignment(tmp_path, f"slot{index:02d}") for index in range(1, 33)]
    assert status["completed"] == 32
    assert status["active"] == 32
    assert status["pending"] == 136
    assert len({item["ticker"] for item in current}) == 32
    assert len({item["queue_position"] for item in current}) == 32
    assert len({item["assignment_id"] for item in current}) == 32
    assert not old_ids.intersection(item["assignment_id"] for item in current)


def test_stale_and_duplicate_payloads_do_not_advance_current_assignment(tmp_path):
    queue, _, worker, _ = _setup(tmp_path)
    old = _save_success(tmp_path, "slot14")
    old_path = expected_output_path(_bridge(tmp_path, "slot14"), old)
    run_once(worker, sync_func=_sync)
    current = _assignment(tmp_path, "slot14")
    processed = worker.inbox / "processed" / old_path.name
    shutil.copy2(processed, old_path)
    run_once(worker, sync_func=lambda *_: (_ for _ in ()).throw(AssertionError("must not sync")))
    assert _assignment(tmp_path, "slot14") == current
    assert sum(entry["status"] == "completed" for entry in _entries(queue)) == 1


def test_hundred_company_fixture_completes_out_of_order_with_all_slots_idle(tmp_path):
    queue, bridge, worker, _ = _setup(tmp_path)
    for _ in range(5):
        status = queue_status(queue, bridge)
        active = [slot_id for slot_id, value in status["slots"].items() if value["assignment_id"]]
        for slot_id in reversed(active):
            _save_success(tmp_path, slot_id)
        run_once(worker, sync_func=_sync)
        if queue_status(queue, bridge)["queue_status"] == "completed":
            break
    status = queue_status(queue, bridge)
    assert status["queue_status"] == "completed"
    assert status["completed"] == 100
    assert status["failed"] == 0
    assert status["pending"] == 0
    assert status["active"] == 0
    assert all(value["assignment_id"] is None for value in status["slots"].values())


def test_two_hundred_company_eight_by_four_fixture_completes_out_of_order(tmp_path):
    queue, bridge, worker, _ = _setup(tmp_path, count=200, batch_size=4)
    for _ in range(7):
        status = queue_status(queue, bridge)
        active = [slot_id for slot_id, value in status["slots"].items() if value["assignment_id"]]
        for slot_id in reversed(active):
            _save_success(tmp_path, slot_id)
        run_once(worker, sync_func=_sync)
        if queue_status(queue, bridge)["queue_status"] == "completed":
            break
    status = queue_status(queue, bridge)
    assert status["queue_status"] == "completed"
    assert status["completed"] == 200
    assert status["failed"] == 0
    assert status["pending"] == 0
    assert status["active"] == 0
    assert all(value["assignment_id"] is None for value in status["slots"].values())


def test_soak_selection_prefers_never_scanned_companies_and_spreads_sectors(tmp_path):
    master = tmp_path / "master.db"
    db = tmp_path / "news.db"
    _master(master)
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE canonical_news_scan_runs(ticker TEXT,status TEXT,checked_at TEXT)")
    connection.executemany(
        "INSERT INTO canonical_news_scan_runs VALUES(?,?,?)",
        [(f"{2000 + index:04d}", "completed", "2026-08-29T00:00:00+09:00") for index in range(1, 16)],
    )
    connection.commit()
    connection.close()
    queue = QueuePaths.from_values(tmp_path)
    result = initialize_pilot(
        queue,
        BridgePaths.from_root(tmp_path),
        master,
        db,
        count=100,
        task_count=8,
        batch_size=3,
        stratified=True,
        now=NOW,
    )
    tickers = {company["ticker"] for company in result["companies"]}
    assert not tickers.intersection({f"{2000 + index:04d}" for index in range(1, 16)})
    assert len({company["sector"] for company in result["companies"]}) == 33


def test_eight_thin_prompts_and_setup_files_define_mapping_and_schedule():
    work = Path(__file__).resolve().parents[1] / "data" / "news_work"
    minutes = ("00", "08", "15", "23", "30", "38", "45", "53")
    for index, minute in enumerate(minutes, start=1):
        task_id = f"task{index:02d}"
        prompt = (work / f"SCHEDULED_TASK_TASK{index:02d}_PROMPT.txt").read_text(encoding="utf-8")
        setup = (work / f"SCHEDULED_TASK_SETUP_TASK{index:02d}.txt").read_text(encoding="utf-8")
        assert f"snapshot --task-id {task_id}" in prompt
        expected_slots = [f"slot{slot:02d}" for slot in range((index - 1) * 4 + 1, index * 4 + 1)]
        assert "、".join(expected_slots) in prompt
        assert "最大4 assignment" in prompt
        assert "5社目を処理しない" in prompt
        assert f"Company News Monitor Task{index:02d}" in setup
        assert f"毎時{minute}分" in setup
        assert "最大4社" in setup
