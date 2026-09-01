import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import tools.sector_weekly_inbox_worker as inbox_worker
from lib.sector_weekly import CANONICAL_SQLITE_SCHEMA, connect_sector_db, weekly_window
from lib.sector_weekly_work import enqueue_assignment, get_assignment
from tools.apply_sector_weekly_work_sqlite_migration import apply_sqlite_migration
from tools.sector_weekly_inbox_worker import WorkerPaths, process_one, run_once
from tools.sector_weekly_work_bridge import RESULT_SCHEMA, claim_one, stage_one

AT = datetime.fromisoformat("2026-09-05T06:05:00+09:00")
OWNER = "sector-weekly-worker"


def _read_log(paths: WorkerPaths) -> list[dict]:
    return [json.loads(line) for line in paths.log.read_text(encoding="utf-8").splitlines()]


def _run_main(
    monkeypatch,
    paths: WorkerPaths,
    *,
    trigger: str,
    sync_func=lambda *_: {},
) -> int:
    original_run_once = inbox_worker.run_once

    def fixture_run_once(worker_paths, *, dry_run_sync=False, trigger="manual"):
        return original_run_once(
            worker_paths,
            sync_func=sync_func,
            dry_run_sync=dry_run_sync,
            trigger=trigger,
        )

    monkeypatch.setattr(inbox_worker, "run_once", fixture_run_once)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sector_weekly_inbox_worker.py",
            "--once",
            "--root",
            str(paths.root),
            "--db",
            str(paths.db),
            "--work-root",
            str(paths.work_root),
            "--trigger",
            trigger,
        ],
    )
    return inbox_worker.main()


def _fixture(tmp_path: Path, code: int = 4) -> tuple[Path, Path, dict, Path]:
    db = tmp_path / "db.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(CANONICAL_SQLITE_SCHEMA)
    conn.close()
    apply_sqlite_migration(db, expected_db_path=db, backup_dir=tmp_path / "backups")
    conn = connect_sector_db(db)
    try:
        enqueue_assignment(conn, code, weekly_window(AT), now=AT)
    finally:
        conn.close()
    work = tmp_path / "work"
    claimed = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    envelope = {
        "schema_version": RESULT_SCHEMA,
        "assignment_id": claimed["assignment_id"],
        "claim_owner": OWNER,
        "sector_code": claimed["sector_code"],
        "sector_name": claimed["sector_name"],
        "period_start": claimed["period_start"],
        "period_end": claimed["period_end"],
        "report": {
            "importance": "A", "direction": "mixed",
            "summary_bullets": ["海外需給", "国内波及", "反証条件"],
            "watchlist_companies": [], "next_week_watchpoints": ["価格"],
            "missed_candidates": ["重要変動なしも確認"],
            "full_report_md": (
                f"# 【東証33業種週次】{claimed['sector_name']}\n\n## 今週の要旨\n"
                "**Fact** 需給変化。\n\n## 重要材料\n**Transmission** 国内波及。\n\n"
                "**Magnitude** Estimate。 **Pricing-in** 未織込み。 "
                "**Counterevidence** 反証。\n\n" + "fixture本文。" * 30
            ),
            "sources": [{
                "title": "Primary", "url": "https://example.com/primary",
                "source_name": "Authority", "source_type": "government",
                "published_at": "2026-09-04",
            }],
        },
    }
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    stage_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT)
    inbox = work / "inbox" / f"{claimed['assignment_id']}.json"
    return db, work, claimed, inbox


def test_worker_happy_path_upserts_syncs_completes_and_processes(tmp_path: Path):
    db, work, claimed, inbox = _fixture(tmp_path)
    calls = []

    def sync(path: Path, dry: bool):
        calls.append((path, dry))
        return {"canonical_sector_reports": 1, "canonical_sector_report_runs": 33}

    result = process_one(WorkerPaths.from_values(tmp_path, db, work), inbox, sync_func=sync)
    assert result["status"] == "success" and result["attempt_count"] == 1
    assert len(calls) == 1 and not inbox.exists()
    assert Path(result["processed_path"]).exists()
    conn = connect_sector_db(db)
    try:
        row = get_assignment(conn, claimed["assignment_id"])
        assert row["status"] == "success" and row["attempt_count"] == 1
        assert conn.execute("SELECT count(*) FROM canonical_sector_reports").fetchone()[0] == 1
    finally:
        conn.close()


def test_sync_failure_preserves_payload_attempt_and_resumes_next_poll(tmp_path: Path):
    db, work, claimed, inbox = _fixture(tmp_path)
    paths = WorkerPaths.from_values(tmp_path, db, work)
    failed = process_one(paths, inbox, sync_func=lambda *_: (_ for _ in ()).throw(RuntimeError("secret=hidden")))
    assert failed["status"] == "sync_error" and inbox.exists()
    assert "hidden" not in failed["error"]
    conn = connect_sector_db(db)
    try:
        row = get_assignment(conn, claimed["assignment_id"])
        assert row["status"] == "retry_pending"
        assert row["last_error_type"] == "sync_error" and row["attempt_count"] == 1
    finally:
        conn.close()
    resumed = process_one(paths, inbox, sync_func=lambda *_: {"canonical_sector_reports": 1})
    assert resumed["status"] == "success" and resumed["attempt_count"] == 1


def test_legacy_sector4_partial_and_identical_quarantine_recover_without_attempt(tmp_path: Path):
    db, work, claimed, inbox = _fixture(tmp_path, 4)
    quarantine = work / "quarantine" / inbox.name
    quarantine.parent.mkdir(parents=True)
    quarantine.write_bytes(inbox.read_bytes())
    conn = connect_sector_db(db)
    try:
        conn.execute(
            "UPDATE sector_weekly_work_assignments SET attempt_count=2,last_error_type='RuntimeError',"
            "last_error_message='legacy sync failure' WHERE assignment_id=?",
            (claimed["assignment_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    result = process_one(
        WorkerPaths.from_values(tmp_path, db, work), inbox,
        sync_func=lambda *_: {"canonical_sector_reports": 1},
    )
    assert result["status"] == "success" and result["attempt_count"] == 2
    assert result["identical_quarantine_removed"] is True and not quarantine.exists()


def test_corrupt_json_is_quarantined_and_returns_to_research_retry(tmp_path: Path):
    db, work, claimed, inbox = _fixture(tmp_path)
    inbox.write_text("{broken", encoding="utf-8")
    result = process_one(WorkerPaths.from_values(tmp_path, db, work), inbox, sync_func=lambda *_: {})
    assert result["status"] == "quarantined"
    conn = connect_sector_db(db)
    try:
        row = get_assignment(conn, claimed["assignment_id"])
        assert row["status"] == "retry_pending" and row["attempt_count"] == 1
        assert row["submitted_payload_hash"] is None
        assert row["last_error_type"].startswith("validation_")
    finally:
        conn.close()


def test_worker_ignores_tmp_and_isolates_one_failure(tmp_path: Path):
    db, work, _, inbox = _fixture(tmp_path)
    (work / "inbox" / ".partial.json.tmp").write_text("partial", encoding="utf-8")
    (work / "inbox" / "not-an-assignment.json").write_text("{}", encoding="utf-8")
    result = run_once(
        WorkerPaths.from_values(tmp_path, db, work),
        sync_func=lambda *_: {"canonical_sector_reports": 1}, trigger="task_scheduler",
    )
    assert result["detected"] == 2
    assert result["success"] == 1 and result["failed"] == 1
    assert not inbox.exists()
    assert (work / "inbox" / ".partial.json.tmp").exists()
    assert (work / "logs" / "inbox_worker.jsonl").exists()


def test_worker_lock_returns_busy(tmp_path: Path):
    db, work, _, _ = _fixture(tmp_path)
    paths = WorkerPaths.from_values(tmp_path, db, work)
    paths.lock.parent.mkdir(parents=True, exist_ok=True)
    paths.lock.write_text(f"pid={__import__('os').getpid()}\n", encoding="utf-8")
    result = run_once(paths, sync_func=lambda *_: {}, trigger="task_scheduler")
    assert result["status"] == "busy" and result["detected"] == 0
    assert result["trigger"] == "task_scheduler"


def test_main_no_work_finishes_once_with_clean_exit(tmp_path: Path, monkeypatch):
    paths = WorkerPaths.from_values(tmp_path, tmp_path / "unused.sqlite", tmp_path / "work")
    assert not paths.db.exists()

    exit_code = _run_main(monkeypatch, paths, trigger="task_scheduler")

    assert exit_code == 0
    assert not paths.db.exists()
    records = _read_log(paths)
    assert [item["event"] for item in records] == ["worker_started", "worker_finished"]
    finished = records[-1]
    assert finished["status"] == "no_work"
    assert finished["trigger"] == "task_scheduler"
    assert finished["detected"] == finished["success"] == finished["failed"] == 0
    assert finished["exit_status"] == 0
    assert finished["duration_seconds"] >= 0
    assert sum(item["event"] == "worker_finished" for item in records) == 1
    assert sum(item["event"] == "worker_error" for item in records) == 0
    assert all(line.count('"trigger"') == 1 for line in paths.log.read_text(encoding="utf-8").splitlines())


def test_main_processed_finishes_after_successful_sync(tmp_path: Path, monkeypatch):
    db, work, claimed, inbox = _fixture(tmp_path)
    paths = WorkerPaths.from_values(tmp_path, db, work)
    calls = []

    def sync(path: Path, dry: bool):
        calls.append((path, dry))
        return {"canonical_sector_reports": 1, "canonical_sector_report_runs": 33}

    exit_code = _run_main(
        monkeypatch,
        paths,
        trigger="task_scheduler",
        sync_func=sync,
    )

    assert exit_code == 0
    assert len(calls) == 1 and not inbox.exists()
    conn = connect_sector_db(db)
    try:
        row = get_assignment(conn, claimed["assignment_id"])
        assert row["status"] == "success" and row["attempt_count"] == 1
        assert conn.execute("SELECT count(*) FROM canonical_sector_reports").fetchone()[0] == 1
    finally:
        conn.close()
    records = _read_log(paths)
    assert sum(item["event"] == "worker_started" for item in records) == 1
    assert sum(item["event"] == "worker_finished" for item in records) == 1
    assert sum(item["event"] == "worker_error" for item in records) == 0
    finished = records[-1]
    assert finished["status"] == "completed" and finished["success"] == 1
    assert finished["failed"] == 0 and finished["exit_status"] == 0
    assert finished["trigger"] == "task_scheduler"
    assert finished["duration_seconds"] >= 0
    assert all(line.count('"trigger"') == 1 for line in paths.log.read_text(encoding="utf-8").splitlines())


def test_main_sync_failure_is_nonzero_and_preserves_payload(tmp_path: Path, monkeypatch):
    db, work, claimed, inbox = _fixture(tmp_path)
    paths = WorkerPaths.from_values(tmp_path, db, work)

    exit_code = _run_main(
        monkeypatch,
        paths,
        trigger="manual",
        sync_func=lambda *_: (_ for _ in ()).throw(RuntimeError("transport unavailable")),
    )

    assert exit_code == 1 and inbox.exists()
    conn = connect_sector_db(db)
    try:
        row = get_assignment(conn, claimed["assignment_id"])
        assert row["status"] == "retry_pending" and row["attempt_count"] == 1
        assert row["submitted_payload_hash"]
    finally:
        conn.close()
    records = _read_log(paths)
    assert sum(item["event"] == "worker_finished" for item in records) == 1
    assert sum(item["event"] == "worker_error" for item in records) == 0
    finished = records[-1]
    assert finished["status"] == "completed_with_errors"
    assert finished["trigger"] == "manual" and finished["exit_status"] == 1
    assert finished["duration_seconds"] >= 0


def test_main_unhandled_exception_logs_error_and_finished(tmp_path: Path, monkeypatch):
    paths = WorkerPaths.from_values(tmp_path, tmp_path / "unused.sqlite", tmp_path / "work")

    def fail_run_once(*_args, **_kwargs):
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(inbox_worker, "run_once", fail_run_once)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sector_weekly_inbox_worker.py",
            "--once",
            "--root",
            str(paths.root),
            "--db",
            str(paths.db),
            "--work-root",
            str(paths.work_root),
            "--trigger",
            "manual",
        ],
    )

    assert inbox_worker.main() == 1
    records = _read_log(paths)
    assert [item["event"] for item in records] == [
        "worker_started",
        "worker_error",
        "worker_finished",
    ]
    assert records[1]["trigger"] == records[2]["trigger"] == "manual"
    assert records[2]["exit_status"] == 1
    assert records[2]["duration_seconds"] >= 0


def test_main_busy_result_has_trigger_and_clean_exit(tmp_path: Path, monkeypatch):
    paths = WorkerPaths.from_values(tmp_path, tmp_path / "unused.sqlite", tmp_path / "work")
    paths.lock.parent.mkdir(parents=True, exist_ok=True)
    paths.lock.write_text(f"pid={__import__('os').getpid()}\n", encoding="utf-8")

    assert _run_main(monkeypatch, paths, trigger="task_scheduler") == 0

    records = _read_log(paths)
    assert [item["event"] for item in records] == [
        "worker_started",
        "concurrent_run_ignored",
        "worker_finished",
    ]
    assert records[-1]["status"] == "busy"
    assert records[-1]["trigger"] == "task_scheduler"
    assert records[-1]["exit_status"] == 0
