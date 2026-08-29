import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tools.company_news_queue as queue_module
from tools.company_news_inbox_worker import WorkerPaths, run_once
from tools.company_news_queue import (
    QueueError,
    QueuePaths,
    initialize_pilot,
    pause_queue,
    queue_status,
    reconcile_queue,
    reset_pilot,
    resume_queue,
)
from tools.company_news_work_bridge import BridgePaths, expected_output_path

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 8, 29, 16, 0, tzinfo=JST)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _master(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE companies(ticker_code TEXT, name_ja TEXT, sector TEXT)")
    connection.executemany(
        "INSERT INTO companies VALUES(?,?,?)",
        [
            ("1301", "Company 1", "Sector A"),
            ("1332", "Company 2", "Sector A"),
            ("1605", "Company 3", "Sector B"),
            ("1721", "Company 4", "Sector C"),
            ("1801", "Company 5", "Sector C"),
            ("9999", "Company 6", "Sector Z"),
        ],
    )
    connection.commit()
    connection.close()


def _setup(tmp_path: Path, *, activate: bool):
    master = tmp_path / "master.db"
    db = tmp_path / "news.db"
    _master(master)
    queue = QueuePaths.from_values(tmp_path)
    bridge = BridgePaths.from_root(tmp_path)
    result = initialize_pilot(queue, bridge, master, db, activate=activate, now=NOW)
    worker = WorkerPaths.from_values(tmp_path, db=db)
    return queue, bridge, worker, result


def _payload(assignment: dict, *, items: list | None = None) -> dict:
    return {
        "schema_version": "company_news_v1",
        "run_id": assignment["assignment_id"],
        "ticker": assignment["ticker"],
        "checked_at": "2026-08-29T16:00:00+09:00",
        "collector_type": "chatgpt_work",
        "task_id": assignment["assignment_id"],
        "sources_checked_count": 1,
        "items": [] if items is None else items,
    }


def _assignment(bridge: BridgePaths) -> dict:
    return json.loads(bridge.assignment.read_text(encoding="utf-8"))


def _legacy_assignment(status: str) -> dict:
    return {
        "schema_version": "company_news_assignment_v1",
        "slot_id": "slot01",
        "assignment_id": f"legacy-{status}-assignment",
        "ticker": "7203",
        "company_name": "Legacy Company",
        "search_from": "2026-08-23",
        "search_to": "2026-08-29",
        "status": status,
        "output_directory": "data/news_inbox",
        "created_at": "2026-08-29T14:00:00+09:00",
    }


def _entries(queue: QueuePaths) -> list[dict]:
    return [json.loads(line) for line in queue.entries.read_text(encoding="utf-8").splitlines() if line]


def _sync(*_):
    return {"canonical_news_events": 0, "canonical_news_scan_runs": 1}


def test_queue_initialization_is_deterministic_and_inert_by_default(tmp_path):
    queue, bridge, _, result = _setup(tmp_path, activate=False)
    assert result["queue_status"] == "fixture_ready"
    assert [item["ticker"] for item in result["companies"]] == ["1301", "1332", "1605", "1721", "1801"]
    assert [item["queue_position"] for item in _entries(queue)] == [1, 2, 3, 4, 5]
    assert not bridge.assignment.exists()
    status = queue_status(queue, bridge)
    assert status["completed"] == 0
    assert status["pending"] == 5
    assert status["next_assignment"] is None


def test_active_queue_generates_unique_first_assignment_with_seven_day_window(tmp_path):
    queue, bridge, _, result = _setup(tmp_path, activate=True)
    assignment = result["assignment"]
    assert assignment["assignment_id"] == "slot01-20260829T160000000000-000001"
    assert assignment["ticker"] == "1301"
    assert assignment["search_from"] == "2026-08-23"
    assert assignment["search_to"] == "2026-08-29"
    assert assignment["queue_position"] == 1
    assert _entries(queue)[0]["attempt_count"] == 1
    assert _assignment(bridge)["status"] == "ready"


def test_five_company_no_news_fixture_advances_and_finishes_queue(tmp_path):
    queue, bridge, worker, _ = _setup(tmp_path, activate=True)
    seen_ids = []
    seen_tickers = []
    for position in range(1, 6):
        assignment = _assignment(bridge)
        seen_ids.append(assignment["assignment_id"])
        seen_tickers.append(assignment["ticker"])
        _write_json(expected_output_path(bridge, assignment), _payload(assignment))
        result = run_once(worker, sync_func=_sync, trigger="task_scheduler")
        assert result["failed"] == 0
        assert result["completed"] == 1
        if position < 5:
            assert result["queue"]["status"] == "assigned"
            assert _assignment(bridge)["status"] == "ready"
        else:
            assert result["queue"]["status"] == "queue_completed"

    assert len(set(seen_ids)) == 5
    assert seen_tickers == ["1301", "1332", "1605", "1721", "1801"]
    assert [entry["status"] for entry in _entries(queue)] == ["completed"] * 5
    status = queue_status(queue, bridge)
    assert status["queue_status"] == "completed"
    assert status["completed"] == 5
    assert status["pending"] == 0
    assert json.loads(bridge.state.read_text(encoding="utf-8"))["phase"] == "idle"


def test_failed_output_retries_once_then_moves_to_next_company(tmp_path):
    queue, bridge, worker, _ = _setup(tmp_path, activate=True)
    first = _assignment(bridge)
    bad = _payload(first)
    bad["items"] = [{"source_url": "https://example.com/missing-headline"}]
    _write_json(expected_output_path(bridge, first), bad)
    first_result = run_once(worker, sync_func=_sync)
    assert first_result["failed"] == 1
    assert first_result["queue"]["status"] == "retry_assigned"
    retry = _assignment(bridge)
    assert retry["ticker"] == first["ticker"]
    assert retry["assignment_id"] != first["assignment_id"]
    assert retry["queue_attempt"] == 2

    bad_retry = _payload(retry)
    bad_retry["items"] = [{"source_url": "https://example.com/still-missing-headline"}]
    _write_json(expected_output_path(bridge, retry), bad_retry)
    second_result = run_once(worker, sync_func=_sync)
    assert second_result["failed"] == 1
    assert second_result["queue"]["status"] == "assigned"
    assert _entries(queue)[0]["status"] == "failed"
    assert _entries(queue)[0]["attempt_count"] == 2
    assert _entries(queue)[0]["last_error"]
    assert _assignment(bridge)["ticker"] == "1332"


def test_sync_failure_holds_assignment_until_resume_then_advances(tmp_path):
    queue, bridge, worker, _ = _setup(tmp_path, activate=True)
    assignment = _assignment(bridge)
    _write_json(expected_output_path(bridge, assignment), _payload(assignment))

    first = run_once(worker, sync_func=lambda *_: (_ for _ in ()).throw(RuntimeError("sync down")))
    assert first["failed"] == 1
    assert first["queue"]["status"] == "blocked_sync_retry"
    assert _assignment(bridge)["assignment_id"] == assignment["assignment_id"]
    assert _entries(queue)[0]["status"] == "assigned"

    second = run_once(worker, sync_func=_sync)
    assert second["failed"] == 0
    assert second["queue"]["status"] == "assigned"
    assert _assignment(bridge)["ticker"] == "1332"


def test_duplicate_worker_run_does_not_advance_twice(tmp_path):
    _, bridge, worker, _ = _setup(tmp_path, activate=True)
    first = _assignment(bridge)
    _write_json(expected_output_path(bridge, first), _payload(first))
    run_once(worker, sync_func=_sync)
    second = _assignment(bridge)
    duplicate = run_once(worker, sync_func=lambda *_: (_ for _ in ()).throw(AssertionError("must not sync")))
    assert duplicate["failed"] == 0
    assert duplicate["queue"]["status"] == "waiting"
    assert _assignment(bridge)["assignment_id"] == second["assignment_id"]


def test_persisted_assignment_transition_is_recovered_after_restart(tmp_path):
    queue, bridge, worker, _ = _setup(tmp_path, activate=False)
    entries = _entries(queue)
    state = json.loads(queue.state.read_text(encoding="utf-8"))
    assignment = {
        "schema_version": "company_news_assignment_v1",
        "slot_id": "slot01",
        "assignment_id": "slot01-20260829T160000000000-000001",
        "ticker": entries[0]["ticker"],
        "company_name": entries[0]["company_name"],
        "search_from": "2026-08-23",
        "search_to": "2026-08-29",
        "status": "ready",
        "output_directory": "data/news_inbox",
        "created_at": "2026-08-29T16:00:00+09:00",
        "queue_id": state["queue_id"],
        "queue_position": 1,
        "queue_attempt": 1,
    }
    state.update({
        "queue_status": "active",
        "fixture_mode": False,
        "transition": {"kind": "assign", "queue_position": 1, "assignment_sequence": 1, "assignment": assignment},
    })
    _write_json(queue.state, state)

    result = reconcile_queue(queue, bridge, worker.db, now=NOW)
    assert result["status"] == "transition_recovered"
    assert _assignment(bridge)["assignment_id"] == assignment["assignment_id"]
    assert _entries(queue)[0]["status"] == "assigned"
    assert "transition" not in json.loads(queue.state.read_text(encoding="utf-8"))


def test_pause_resume_status_and_fixture_reset(tmp_path):
    queue, bridge, worker, _ = _setup(tmp_path, activate=False)
    paused = pause_queue(queue)
    assert paused["queue_status"] == "paused"
    assert {entry["status"] for entry in _entries(queue)} == {"paused"}
    with pytest.raises(QueueError, match="requires resume --activate"):
        resume_queue(queue, bridge, worker.db)
    resumed = resume_queue(queue, bridge, worker.db, activate=True)
    assert resumed["status"] == "assigned"
    assert json.loads(queue.state.read_text(encoding="utf-8"))["queue_status"] == "active"

    # A separate inert fixture can be safely reset, but an activated queue cannot.
    other = tmp_path / "other"
    fixture, _, _, _ = _setup(other, activate=False)
    assert reset_pilot(fixture, confirmation="RESET-PILOT")["status"] == "reset"
    assert not fixture.entries.exists()
    assert not fixture.state.exists()


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
def test_terminal_unmanaged_assignment_is_preserved_and_activation_continues(tmp_path, terminal_status):
    queue, bridge, worker, _ = _setup(tmp_path, activate=False)
    legacy = _legacy_assignment(terminal_status)
    _write_json(bridge.assignment, legacy)

    result = resume_queue(queue, bridge, worker.db, activate=True)

    assert result["status"] == "assigned"
    assert result["assignment"]["ticker"] == "1301"
    assert _assignment(bridge)["queue_id"] == json.loads(queue.state.read_text(encoding="utf-8"))["queue_id"]
    history = bridge.assignment.parent / "history" / f"{legacy['assignment_id']}.json"
    assert json.loads(history.read_text(encoding="utf-8")) == legacy
    state = json.loads(queue.state.read_text(encoding="utf-8"))
    assert state["queue_status"] == "active"
    assert state["current_queue_position"] == 1


@pytest.mark.parametrize("nonterminal_status", ["ready", "running", "processing"])
def test_nonterminal_unmanaged_assignment_blocks_activation_without_mutation(tmp_path, nonterminal_status):
    queue, bridge, worker, _ = _setup(tmp_path, activate=False)
    legacy = _legacy_assignment(nonterminal_status)
    _write_json(bridge.assignment, legacy)
    entries_before = queue.entries.read_bytes()
    state_before = queue.state.read_bytes()

    result = resume_queue(queue, bridge, worker.db, activate=True)

    assert result == {"status": "unmanaged_assignment", "assignment_id": legacy["assignment_id"]}
    assert queue.entries.read_bytes() == entries_before
    assert queue.state.read_bytes() == state_before
    assert _assignment(bridge) == legacy
    assert not (bridge.assignment.parent / "history").exists()


def test_assignment_write_failure_does_not_mark_queue_active(tmp_path, monkeypatch):
    queue, bridge, worker, _ = _setup(tmp_path, activate=False)
    original_atomic_json = queue_module._atomic_json

    def fail_assignment_write(path, value):
        if path == bridge.assignment:
            raise OSError("simulated assignment write failure")
        return original_atomic_json(path, value)

    monkeypatch.setattr(queue_module, "_atomic_json", fail_assignment_write)
    with pytest.raises(OSError, match="simulated assignment write failure"):
        resume_queue(queue, bridge, worker.db, activate=True)

    state = json.loads(queue.state.read_text(encoding="utf-8"))
    assert state["queue_status"] == "fixture_ready"
    assert state["current_queue_position"] is None
    assert state["next_assignment"] is None
    assert state["transition"]["activate_queue"] is True
    assert not bridge.assignment.exists()
    assert {entry["status"] for entry in _entries(queue)} == {"pending"}


def test_active_partial_state_recovers_on_resume(tmp_path):
    queue, bridge, worker, _ = _setup(tmp_path, activate=False)
    state = json.loads(queue.state.read_text(encoding="utf-8"))
    state.update({"queue_status": "active", "fixture_mode": False})
    _write_json(queue.state, state)

    result = resume_queue(queue, bridge, worker.db, activate=True)

    assert result["status"] == "assigned"
    assignment = _assignment(bridge)
    assert assignment["ticker"] == "1301"
    recovered = json.loads(queue.state.read_text(encoding="utf-8"))
    assert recovered["queue_status"] == "active"
    assert recovered["current_queue_position"] == 1
    assert recovered["next_assignment"] == assignment["assignment_id"]


def test_resume_is_idempotent_after_assignment_activation(tmp_path):
    queue, bridge, worker, _ = _setup(tmp_path, activate=False)
    _write_json(bridge.assignment, _legacy_assignment("completed"))

    first = resume_queue(queue, bridge, worker.db, activate=True)
    first_assignment = _assignment(bridge)
    second = resume_queue(queue, bridge, worker.db, activate=True)

    assert first["status"] == "assigned"
    assert second == {"status": "waiting", "assignment_id": first_assignment["assignment_id"]}
    assert _assignment(bridge) == first_assignment
    assert json.loads(queue.state.read_text(encoding="utf-8"))["assignment_sequence"] == 1
    assert _entries(queue)[0]["attempt_count"] == 1


def test_live_queue_lock_prevents_operator_mutation(tmp_path, monkeypatch):
    queue, _, _, _ = _setup(tmp_path, activate=False)
    queue.lock.write_text("pid=42424245\n", encoding="utf-8")
    monkeypatch.setattr(queue_module, "_pid_is_alive", lambda pid: pid == 42424245)
    try:
        with pytest.raises(QueueError, match="already being processed"):
            pause_queue(queue)
    finally:
        queue.lock.unlink(missing_ok=True)


def test_slot_prompt_is_generic_and_stops_without_ready_assignment():
    work = Path(__file__).resolve().parents[1] / "data" / "news_work"
    prompt = (work / "SCHEDULED_TASK_SLOT01_PROMPT.txt").read_text(encoding="utf-8")
    common = (work / "SCHEDULED_TASK_COMMON_PROMPT.txt").read_text(encoding="utf-8")
    combined = prompt + common
    assert "statusがreadyでない場合" in prompt
    assert "assignmentが存在しない" in prompt
    assert "<assignment_id>" in combined
    assert "<current assignmentのtickerと完全一致>" in combined
    assert "7203" not in combined
    assert "schema-retry-003" not in combined


@pytest.mark.skipif(os.name != "nt", reason="Windows PID probing regression")
def test_queue_uses_windows_safe_pid_probe():
    current_pid = os.getpid()
    assert queue_module._pid_is_alive(current_pid) is True
    assert os.getpid() == current_pid
    assert queue_module._pid_is_alive(0xFFFFFFFF) is False
