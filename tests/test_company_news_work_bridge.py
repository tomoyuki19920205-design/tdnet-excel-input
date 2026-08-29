import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from tools.company_news_work_bridge import (
    BridgeError,
    BridgePaths,
    bridge_status,
    create_assignment,
    expected_output_path,
    process_assignment,
)


def write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def master_db(path: Path):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE companies(ticker_code TEXT, name_ja TEXT)")
    connection.execute("INSERT INTO companies VALUES('7203','トヨタ自動車')")
    connection.commit(); connection.close()


def output_payload(assignment_id="bridge-smoke-001", ticker="7203"):
    return {
        "schema_version": "company_news_v1",
        "run_id": assignment_id,
        "ticker": ticker,
        "checked_at": "2026-08-29T11:00:00+09:00",
        "collector_type": "chatgpt_work",
        "task_id": assignment_id,
        "sources_checked_count": 1,
        "items": [{
            "headline": "Bridge fixture headline",
            "published_at": "2026-08-28T09:00:00+09:00",
            "source_name": "Fixture Source",
            "source_url": "https://example.com/bridge/1",
            "category": "demand",
            "direction": "neutral",
            "importance": "low",
            "earnings_relevance": "general",
            "summary": "Bridge fixture summary.",
            "why_it_matters": "Bridge fixture rationale.",
            "evidence_excerpt": "Short bridge fixture excerpt.",
            "temporal_scope": "current",
            "tags": ["bridge_fixture"],
        }],
    }


@pytest.fixture
def bridge(tmp_path):
    paths = BridgePaths.from_root(tmp_path)
    master = tmp_path / "master.db"; master_db(master)
    assignment = create_assignment(
        paths, master, assignment_id="bridge-smoke-001", ticker="7203",
        search_from=date(2026, 8, 22), search_to=date(2026, 8, 29),
    )
    return paths, tmp_path / "news.db", assignment


def test_create_and_waiting_status(bridge):
    paths, db_path, assignment = bridge
    assert assignment["status"] == "ready"
    assert assignment["company_name"] == "トヨタ自動車"
    result = process_assignment(paths, db_path, sync_func=lambda *_: {})
    assert result["status"] == "waiting"
    assert bridge_status(paths, db_path)["output_present"] is False


def test_assignment_to_inbox_ingest_sync_and_duplicate_safe(bridge):
    paths, db_path, assignment = bridge
    write_json(expected_output_path(paths, assignment), output_payload())
    sync_calls = []

    def fake_sync(path, dry_run):
        sync_calls.append((path, dry_run))
        return {"canonical_news_events": 1, "canonical_news_scan_runs": 1}

    first = process_assignment(paths, db_path, sync_func=fake_sync)
    second = process_assignment(paths, db_path, sync_func=fake_sync)
    assert first["status"] == "completed"
    assert second["status"] == "already_completed"
    assert len(sync_calls) == 1
    connection = sqlite3.connect(db_path)
    assert connection.execute("SELECT count(*) FROM canonical_news_events").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM canonical_news_scan_runs").fetchone()[0] == 1
    assert json.loads(paths.assignment.read_text(encoding="utf-8"))["status"] == "completed"


def test_empty_items_is_a_successful_completion(bridge):
    paths, db_path, assignment = bridge
    payload = output_payload(); payload["items"] = []
    write_json(expected_output_path(paths, assignment), payload)
    result = process_assignment(paths, db_path, sync_func=lambda *_: {"canonical_news_events": 0, "canonical_news_scan_runs": 1})
    assert result["status"] == "completed"
    connection = sqlite3.connect(db_path)
    assert connection.execute("SELECT count(*) FROM canonical_news_events").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM canonical_news_scan_runs").fetchone()[0] == 1


def test_sync_failure_resumes_without_second_ingest(bridge):
    paths, db_path, assignment = bridge
    write_json(expected_output_path(paths, assignment), output_payload())

    def fail_sync(*_):
        raise RuntimeError("temporary sync failure")

    with pytest.raises(RuntimeError, match="temporary sync failure"):
        process_assignment(paths, db_path, sync_func=fail_sync)
    assert json.loads(paths.assignment.read_text(encoding="utf-8"))["status"] == "failed"
    resumed = process_assignment(paths, db_path, sync_func=lambda *_: {"canonical_news_events": 1, "canonical_news_scan_runs": 1})
    assert resumed["status"] == "completed"
    connection = sqlite3.connect(db_path)
    assert connection.execute("SELECT count(*) FROM canonical_news_events").fetchone()[0] == 1


def test_wrong_ticker_is_quarantined_without_database_write(bridge):
    paths, db_path, assignment = bridge
    write_json(expected_output_path(paths, assignment), output_payload(ticker="6758"))
    with pytest.raises(BridgeError, match="ticker does not match"):
        process_assignment(paths, db_path, sync_func=lambda *_: {})
    assert not db_path.exists()
    assert (paths.inbox / "quarantine" / expected_output_path(paths, assignment).name).exists()
    assert json.loads(paths.assignment.read_text(encoding="utf-8"))["status"] == "failed"


def test_published_date_outside_assignment_is_rejected(bridge):
    paths, db_path, assignment = bridge
    payload = output_payload(); payload["items"][0]["published_at"] = "2026-08-01T09:00:00+09:00"
    write_json(expected_output_path(paths, assignment), payload)
    with pytest.raises(BridgeError, match="outside assignment search range"):
        process_assignment(paths, db_path, sync_func=lambda *_: {})


def test_v1_refuses_automatic_next_assignment(bridge, tmp_path):
    paths, _, _ = bridge
    master = tmp_path / "master2.db"; master_db(master)
    with pytest.raises(BridgeError, match="unfinished assignment"):
        create_assignment(paths, master, assignment_id="bridge-smoke-002", ticker="7203", search_from=date(2026, 8, 22), search_to=date(2026, 8, 29))


def test_stale_process_lock_is_recovered(bridge):
    paths, db_path, assignment = bridge
    paths.lock.parent.mkdir(parents=True, exist_ok=True)
    paths.lock.write_text("pid=99999999 at=2026-08-29T00:00:00+09:00\n", encoding="utf-8")
    write_json(expected_output_path(paths, assignment), output_payload())
    result = process_assignment(paths, db_path, sync_func=lambda *_: {"canonical_news_events": 1, "canonical_news_scan_runs": 1})
    assert result["status"] == "completed"
    assert not paths.lock.exists()
