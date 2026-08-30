import json
import os
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest

import tools.company_news_inbox_worker as worker_module
from tools.company_news_inbox_worker import WorkerPaths, run_once
from tools.company_news_work_bridge import BridgePaths, create_assignment, expected_output_path


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _master(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE companies(ticker_code TEXT, name_ja TEXT)")
    connection.execute("INSERT INTO companies VALUES('7203','トヨタ自動車')")
    connection.commit()
    connection.close()


def _payload(assignment_id: str = "worker-smoke-001") -> dict:
    return {
        "schema_version": "company_news_v1",
        "run_id": assignment_id,
        "ticker": "7203",
        "checked_at": "2026-08-29T11:00:00+09:00",
        "collector_type": "chatgpt_work",
        "task_id": assignment_id,
        "sources_checked_count": 1,
        "items": [{
            "headline": "Worker fixture headline",
            "published_at": "2026-08-28T09:00:00+09:00",
            "source_name": "Fixture Source",
            "source_url": "https://example.com/worker/1",
            "category": "demand",
            "direction": "neutral",
            "importance": "low",
            "earnings_relevance": "general",
            "summary": "Worker fixture summary.",
            "why_it_matters": "Worker fixture rationale.",
            "evidence_excerpt": "Short worker fixture excerpt.",
            "temporal_scope": "current",
            "tags": ["worker_fixture"],
        }],
    }


def _setup(tmp_path: Path):
    bridge = BridgePaths.from_root(tmp_path)
    master = tmp_path / "master.db"
    _master(master)
    assignment = create_assignment(
        bridge,
        master,
        assignment_id="worker-smoke-001",
        ticker="7203",
        search_from=date(2026, 8, 22),
        search_to=date(2026, 8, 29),
    )
    worker = WorkerPaths.from_values(tmp_path, db=tmp_path / "news.db")
    return worker, bridge, assignment


def test_detect_ingest_sync_complete_and_log_stages(tmp_path):
    worker, bridge, assignment = _setup(tmp_path)
    output = expected_output_path(bridge, assignment)
    _write_json(output, _payload())
    calls = []

    def sync(db_path, dry_run):
        calls.append((db_path, dry_run))
        return {"canonical_news_events": 1, "canonical_news_scan_runs": 1}

    result = run_once(worker, sync_func=sync, trigger="task_scheduler")
    assert result["status"] == "completed"
    assert result["completed"] == 1
    assert result["unattended_local_write_candidate"] == "UNATTENDED_LOCAL_WRITE_PASS"
    assert len(calls) == 1
    assert not output.exists()
    assert (worker.inbox / "processed" / output.name).exists()
    current = json.loads(bridge.assignment.read_text(encoding="utf-8"))
    assert current["status"] == "completed"
    connection = sqlite3.connect(worker.db)
    assert connection.execute("SELECT count(*) FROM canonical_news_events").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM canonical_news_scan_runs").fetchone()[0] == 1
    events = [json.loads(line)["event"] for line in bridge.log.read_text(encoding="utf-8").splitlines()]
    assert {"validated", "ingested", "synced", "completed"}.issubset(events)
    worker_events = [json.loads(line)["event"] for line in worker.log.read_text(encoding="utf-8").splitlines()]
    assert "detected" in worker_events


def test_duplicate_processed_payload_is_ignored_without_sync(tmp_path):
    worker, bridge, assignment = _setup(tmp_path)
    output = expected_output_path(bridge, assignment)
    _write_json(output, _payload())
    run_once(worker, sync_func=lambda *_: {"canonical_news_events": 1, "canonical_news_scan_runs": 1})
    shutil.copy2(worker.inbox / "processed" / output.name, output)
    calls = []
    result = run_once(worker, sync_func=lambda *_: calls.append(True) or {})
    assert result["completed"] == 1
    assert calls == []
    assert not output.exists()
    connection = sqlite3.connect(worker.db)
    assert connection.execute("SELECT count(*) FROM canonical_news_events").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM canonical_news_scan_runs").fetchone()[0] == 1


def test_bad_payload_is_quarantined_without_blocking_valid_assignment(tmp_path):
    worker, bridge, assignment = _setup(tmp_path)
    stale = worker.inbox / "work_slot01_stale-assignment.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("{broken", encoding="utf-8")
    _write_json(expected_output_path(bridge, assignment), _payload())
    result = run_once(worker, sync_func=lambda *_: {"canonical_news_events": 1, "canonical_news_scan_runs": 1})
    assert result["completed"] == 1
    assert result["quarantined"] == 1
    assert (worker.inbox / "quarantine" / stale.name).exists()
    assert json.loads(bridge.assignment.read_text(encoding="utf-8"))["status"] == "completed"


def test_sync_failure_resumes_without_second_ingest_or_auto_advance(tmp_path):
    worker, bridge, assignment = _setup(tmp_path)
    output = expected_output_path(bridge, assignment)
    _write_json(output, _payload())
    sync_calls = []

    def fail_sync(*_):
        sync_calls.append("failed")
        raise RuntimeError("temporary sync failure")

    first = run_once(worker, sync_func=fail_sync)
    assert first["failed"] == 1
    assert not output.exists()
    assert json.loads(bridge.assignment.read_text(encoding="utf-8"))["status"] == "failed"

    def pass_sync(*_):
        sync_calls.append("passed")
        return {"canonical_news_events": 1, "canonical_news_scan_runs": 1}

    second = run_once(worker, sync_func=pass_sync)
    assert second["completed"] == 1
    assert sync_calls == ["failed", "passed"]
    connection = sqlite3.connect(worker.db)
    assert connection.execute("SELECT count(*) FROM canonical_news_events").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM canonical_news_scan_runs").fetchone()[0] == 1
    assert json.loads(bridge.assignment.read_text(encoding="utf-8"))["assignment_id"] == "worker-smoke-001"


def test_concurrent_worker_is_ignored(tmp_path, monkeypatch):
    worker, bridge, assignment = _setup(tmp_path)
    _write_json(expected_output_path(bridge, assignment), _payload())
    worker.lock.parent.mkdir(parents=True, exist_ok=True)
    other_worker_pid = 42424242
    monkeypatch.setattr(worker_module, "_pid_is_alive", lambda pid: pid == other_worker_pid)
    worker.lock.write_text(f"pid={other_worker_pid}\n", encoding="utf-8")
    try:
        result = run_once(worker, sync_func=lambda *_: (_ for _ in ()).throw(AssertionError("must not sync")))
        assert result["status"] == "busy"
        assert expected_output_path(bridge, assignment).exists()
    finally:
        worker.lock.unlink(missing_ok=True)


def test_stale_worker_lock_is_recovered_and_cleaned_up(tmp_path, monkeypatch):
    worker, bridge, assignment = _setup(tmp_path)
    _write_json(expected_output_path(bridge, assignment), _payload())
    worker.lock.parent.mkdir(parents=True, exist_ok=True)
    worker.lock.write_text("pid=42424243\n", encoding="utf-8")
    monkeypatch.setattr(worker_module, "_pid_is_alive", lambda _pid: False)

    result = run_once(
        worker,
        sync_func=lambda *_: {"canonical_news_events": 1, "canonical_news_scan_runs": 1},
    )

    assert result["status"] == "completed"
    assert not worker.lock.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows PID probing regression")
def test_windows_pid_probe_is_non_destructive_and_reports_liveness():
    current_pid = os.getpid()
    assert worker_module._pid_is_alive(current_pid) is True
    assert os.getpid() == current_pid
    assert worker_module._pid_is_alive(0xFFFFFFFF) is False


def test_optional_console_output_accepts_pythonw_streams():
    worker_module._write_console(None, "pythonw has no console stream")


def test_main_writes_invocation_audit_log(tmp_path, monkeypatch):
    result = {
        "status": "completed",
        "trigger": "task_scheduler",
        "detected": 2,
        "completed": 1,
        "quarantined": 1,
        "failed": 0,
        "results": [],
    }
    monkeypatch.setattr(worker_module, "run_once", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(
        worker_module.sys,
        "argv",
        ["company_news_inbox_worker.py", "--once", "--root", str(tmp_path), "--trigger", "task_scheduler"],
    )

    assert worker_module.main() == 0

    paths = WorkerPaths.from_values(tmp_path)
    records = [json.loads(line) for line in paths.log.read_text(encoding="utf-8").splitlines()]
    assert records[0]["event"] == "worker_started"
    assert records[0]["trigger"] == "task_scheduler"
    assert records[-1]["event"] == "worker_finished"
    assert records[-1]["exit_status"] == 0
    assert records[-1]["processed_count"] == 2


def test_generic_payload_sync_resume_and_processed_ignore(tmp_path):
    worker = WorkerPaths.from_values(tmp_path, db=tmp_path / "news.db")
    generic = _payload("generic-run-001")
    generic["collector_type"] = "manual_fixture"
    path = worker.inbox / "generic.json"
    _write_json(path, generic)

    first = run_once(worker, sync_func=lambda *_: (_ for _ in ()).throw(RuntimeError("sync down")))
    assert first["failed"] == 1
    assert not path.exists()
    assert (worker.inbox / "processed" / path.name).exists()

    calls = []
    second = run_once(worker, sync_func=lambda *_: calls.append(True) or {"canonical_news_events": 1, "canonical_news_scan_runs": 1})
    assert second["failed"] == 0
    assert calls == [True]
    third = run_once(worker, sync_func=lambda *_: (_ for _ in ()).throw(AssertionError("must not sync")))
    assert third["detected"] == 0
    assert third["failed"] == 0
