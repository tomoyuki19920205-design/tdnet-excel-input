import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tools.company_news_queue as queue_module
from tools.company_news_inbox_worker import WorkerPaths, run_once
from tools.company_news_queue import (
    QueuePaths,
    initialize_pilot,
    pause_queue,
    queue_status,
    reconcile_queue,
    resume_queue,
)
from tools.company_news_work_bridge import BridgePaths, expected_output_path

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 8, 29, 21, 0, tzinfo=JST)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _master(path: Path, count: int = 15) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE companies(ticker_code TEXT, name_ja TEXT, sector TEXT)")
    connection.executemany(
        "INSERT INTO companies VALUES(?,?,?)",
        [(f"{1300 + index:04d}", f"Company {index:02d}", f"Sector {index % 3}") for index in range(1, count + 1)],
    )
    connection.commit()
    connection.close()


def _setup(tmp_path: Path, *, slots: int = 5, count: int = 15, activate: bool = True):
    master = tmp_path / "master.db"
    db = tmp_path / "news.db"
    _master(master, count)
    queue = QueuePaths.from_values(tmp_path)
    base_bridge = BridgePaths.from_root(tmp_path)
    result = initialize_pilot(
        queue,
        base_bridge,
        master,
        db,
        activate=activate,
        now=NOW,
        slot_count=slots,
        count=count,
    )
    worker = WorkerPaths.from_values(tmp_path, db=db)
    return queue, base_bridge, worker, result


def _bridge(root: Path, slot_id: str) -> BridgePaths:
    return BridgePaths.from_root(root, slot_id=slot_id)


def _assignment(root: Path, slot_id: str) -> dict:
    return json.loads(_bridge(root, slot_id).assignment.read_text(encoding="utf-8"))


def _entries(queue: QueuePaths) -> list[dict]:
    return [json.loads(line) for line in queue.entries.read_text(encoding="utf-8").splitlines() if line]


def _payload(assignment: dict, *, valid: bool = True) -> dict:
    payload = {
        "schema_version": "company_news_v1",
        "run_id": assignment["assignment_id"],
        "ticker": assignment["ticker"],
        "checked_at": "2026-08-29T21:00:00+09:00",
        "collector_type": "chatgpt_work",
        "task_id": assignment["assignment_id"],
        "sources_checked_count": 1,
        "items": [],
    }
    if not valid:
        payload["items"] = [{"source_url": "https://example.com/missing-headline"}]
    return payload


def _complete(root: Path, slot_ids: list[str], *, valid: bool = True) -> list[dict]:
    assignments = []
    for slot_id in slot_ids:
        bridge = _bridge(root, slot_id)
        assignment = _assignment(root, slot_id)
        _write_json(expected_output_path(bridge, assignment), _payload(assignment, valid=valid))
        assignments.append(assignment)
    return assignments


def _sync(*_):
    return {"canonical_news_events": 0, "canonical_news_scan_runs": 1}


def test_initial_five_slot_fill_uses_one_global_queue_without_duplicates(tmp_path):
    queue, _, _, result = _setup(tmp_path)

    assert result["queue_status"] == "active"
    assert [item["slot_id"] for item in result["assignments"]] == [f"slot{i:02d}" for i in range(1, 6)]
    assert [item["queue_position"] for item in result["assignments"]] == [1, 2, 3, 4, 5]
    entries = _entries(queue)
    assert [entry["assigned_slot"] for entry in entries[:5]] == [f"slot{i:02d}" for i in range(1, 6)]
    assert len({entry["ticker"] for entry in entries if entry["status"] == "assigned"}) == 5
    assert len({entry["assignment_id"] for entry in entries if entry["status"] == "assigned"}) == 5


def test_out_of_order_completion_refills_only_the_completed_slot(tmp_path):
    _, _, worker, _ = _setup(tmp_path)
    slot01_before = _assignment(tmp_path, "slot01")
    slot03_before = _assignment(tmp_path, "slot03")

    _complete(tmp_path, ["slot03"])
    result03 = run_once(worker, sync_func=_sync, trigger="task_scheduler")
    slot03_after = _assignment(tmp_path, "slot03")
    assert result03["failed"] == 0
    assert slot03_after["queue_position"] == 6
    assert slot03_after["assignment_id"] != slot03_before["assignment_id"]
    assert _assignment(tmp_path, "slot01") == slot01_before

    _complete(tmp_path, ["slot01"])
    run_once(worker, sync_func=_sync, trigger="task_scheduler")
    assert _assignment(tmp_path, "slot01")["queue_position"] == 7
    assert _assignment(tmp_path, "slot03") == slot03_after


def test_three_completions_in_one_worker_run_get_distinct_next_companies(tmp_path):
    queue, _, worker, _ = _setup(tmp_path)
    _complete(tmp_path, ["slot01", "slot02", "slot03"])

    result = run_once(worker, sync_func=_sync, trigger="task_scheduler")

    assert result["failed"] == 0
    current = [_assignment(tmp_path, slot_id) for slot_id in ("slot01", "slot02", "slot03")]
    assert {item["queue_position"] for item in current} == {6, 7, 8}
    assert len({item["ticker"] for item in current}) == 3
    assert len({item["assignment_id"] for item in current}) == 3
    assert sum(entry["status"] == "completed" for entry in _entries(queue)) == 3


def test_late_payload_is_quarantined_without_completing_current_assignment(tmp_path):
    queue, _, worker, _ = _setup(tmp_path)
    old = _complete(tmp_path, ["slot02"])[0]
    old_path = expected_output_path(_bridge(tmp_path, "slot02"), old)
    run_once(worker, sync_func=_sync)
    current = _assignment(tmp_path, "slot02")
    shutil.copy2(worker.inbox / "processed" / old_path.name, old_path)

    late = run_once(worker, sync_func=lambda *_: (_ for _ in ()).throw(AssertionError("must not sync")))

    assert late["quarantined"] == 1
    assert _assignment(tmp_path, "slot02") == current
    assert sum(entry["status"] == "completed" for entry in _entries(queue)) == 1


def test_slot_retry_exhaustion_does_not_stop_other_slots(tmp_path):
    queue, _, worker, _ = _setup(tmp_path)
    other_assignments = {slot_id: _assignment(tmp_path, slot_id) for slot_id in ("slot01", "slot02", "slot03", "slot05")}
    _complete(tmp_path, ["slot04"], valid=False)
    first = run_once(worker, sync_func=_sync)
    retry = _assignment(tmp_path, "slot04")
    assert first["queue"]["status"] == "retry_assigned"
    assert retry["queue_attempt"] == 2

    _complete(tmp_path, ["slot04"], valid=False)
    second = run_once(worker, sync_func=_sync)
    assert second["queue"]["status"] == "assigned"
    assert _entries(queue)[3]["status"] == "failed"
    assert _assignment(tmp_path, "slot04")["queue_position"] == 6
    for slot_id, assignment in other_assignments.items():
        assert _assignment(tmp_path, slot_id) == assignment


def test_sync_failure_resumes_same_slot_before_refill(tmp_path):
    _, _, worker, _ = _setup(tmp_path)
    original = _complete(tmp_path, ["slot05"])[0]
    first = run_once(worker, sync_func=lambda *_: (_ for _ in ()).throw(RuntimeError("sync down")))
    assert first["queue"]["status"] == "blocked_sync_retry"
    assert _assignment(tmp_path, "slot05")["assignment_id"] == original["assignment_id"]

    second = run_once(worker, sync_func=_sync)
    assert second["failed"] == 0
    assert _assignment(tmp_path, "slot05")["queue_position"] == 6


def test_pause_allows_completion_but_defers_refill_until_resume(tmp_path):
    queue, bridge, worker, _ = _setup(tmp_path)
    pause_queue(queue)
    _complete(tmp_path, ["slot01"])
    paused_result = run_once(worker, sync_func=_sync)

    assert paused_result["queue"]["status"] == "paused"
    status = queue_status(queue, bridge)
    assert status["slots"]["slot01"]["assignment_id"] is None
    assert status["queue_status"] == "paused"

    resumed = resume_queue(queue, bridge, worker.db)
    assert resumed["status"] == "assigned"
    assert _assignment(tmp_path, "slot01")["queue_position"] == 6


def test_slot_assignment_write_failure_is_recovered_without_duplicate_company(tmp_path, monkeypatch):
    master = tmp_path / "master.db"
    _master(master)
    queue = QueuePaths.from_values(tmp_path)
    bridge = BridgePaths.from_root(tmp_path)
    original_atomic = queue_module._atomic_json
    failed = {"done": False}

    def fail_slot03_once(path, value):
        if path == _bridge(tmp_path, "slot03").assignment and not failed["done"]:
            failed["done"] = True
            raise OSError("slot03 write interrupted")
        return original_atomic(path, value)

    monkeypatch.setattr(queue_module, "_atomic_json", fail_slot03_once)
    with pytest.raises(OSError, match="slot03 write interrupted"):
        initialize_pilot(
            queue,
            bridge,
            master,
            tmp_path / "news.db",
            activate=True,
            now=NOW,
            slot_count=5,
            count=15,
        )

    state = json.loads(queue.state.read_text(encoding="utf-8"))
    assert state["queue_status"] == "active"
    assert "slot03" in state["transitions"]
    monkeypatch.setattr(queue_module, "_atomic_json", original_atomic)
    reconcile_queue(queue, bridge, tmp_path / "news.db", now=NOW)
    entries = _entries(queue)
    assigned = [entry for entry in entries if entry["status"] == "assigned"]
    assert len(assigned) == 5
    assert len({entry["ticker"] for entry in assigned}) == 5
    assert len({entry["assignment_id"] for entry in assigned}) == 5


def test_fifteen_company_queue_completes_with_all_slots_idle(tmp_path):
    queue, bridge, worker, _ = _setup(tmp_path)
    for _round in range(3):
        active_slots = [
            slot_id
            for slot_id, value in queue_status(queue, bridge)["slots"].items()
            if value["assignment_id"] is not None
        ]
        _complete(tmp_path, active_slots)
        run_once(worker, sync_func=_sync, trigger="task_scheduler")

    status = queue_status(queue, bridge)
    assert status["queue_status"] == "completed"
    assert status["completed"] == 15
    assert status["failed"] == 0
    assert status["pending"] == 0
    assert status["active"] == 0
    assert all(value["assignment_id"] is None for value in status["slots"].values())

    old_entries = queue.entries.read_text(encoding="utf-8")
    old_state = queue.state.read_text(encoding="utf-8")
    replacement = initialize_pilot(
        queue,
        bridge,
        tmp_path / "master.db",
        worker.db,
        activate=False,
        now=NOW + timedelta(days=1),
        slot_count=5,
        count=15,
    )
    archive = Path(replacement["archived_queue"])
    assert (archive / "company_queue.jsonl").read_text(encoding="utf-8") == old_entries
    assert (archive / "queue_state.json").read_text(encoding="utf-8") == old_state
    assert replacement["queue_id"] != status["queue_id"]
    assert replacement["queue_status"] == "fixture_ready"


def test_one_slot_mode_remains_default_compatible(tmp_path):
    queue, bridge, _, result = _setup(tmp_path, slots=1, count=5)
    assert result["assignment"]["slot_id"] == "slot01"
    assert result["queue_status"] == "active"
    status = queue_status(queue, bridge)
    assert status["slot_count"] == 1
    assert set(status["slots"]) == {"slot01"}


def test_five_thin_prompts_and_setup_templates_reference_only_their_slot():
    work = Path(__file__).resolve().parents[1] / "data" / "news_work"
    common = (work / "SCHEDULED_TASK_COMMON_PROMPT.txt").read_text(encoding="utf-8")
    assert '"schema_version": "company_news_v1"' in common
    for index, minute in enumerate(("00", "10", "20", "30", "40"), start=1):
        slot_id = f"slot{index:02d}"
        prompt = (work / f"SCHEDULED_TASK_SLOT{index:02d}_PROMPT.txt").read_text(encoding="utf-8")
        setup = (work / f"SCHEDULED_TASK_SETUP_SLOT{index:02d}.txt").read_text(encoding="utf-8")
        assert f"slots\\{slot_id}\\assignment.json" in prompt
        assert f"work_{slot_id}_<assignment_id>.json" in prompt
        assert "SCHEDULED_TASK_COMMON_PROMPT.txt" in prompt
        assert f"Company News Monitor Slot{index:02d}" in setup
        assert f"毎時{minute}分" in setup
