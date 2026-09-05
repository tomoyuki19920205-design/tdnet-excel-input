import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from lib.sector_weekly import CANONICAL_SQLITE_SCHEMA, sector_name, weekly_window
from lib.sector_weekly_sqlite import connect_sector_db
from lib.sector_weekly_work import (
    complete_assignment,
    completion_status,
    enqueue_assignment,
    get_assignment,
    recover_expired_leases,
)
from tools.apply_sector_weekly_work_sqlite_migration import apply_sqlite_migration
from tools.sector_weekly_scheduler import run_scheduled
from tools.sector_weekly_inbox_worker import WorkerPaths, process_one
from tools.sector_weekly_work_bridge import (
    RESULT_SCHEMA,
    abandon_one,
    claim_one,
    heartbeat_one,
    start_one,
    stage_one,
)

SAT = datetime.fromisoformat("2026-09-05T06:00:00+09:00")
OWNER = "sector-weekly-soak-worker"
CADENCE = timedelta(hours=1)
ALL_SLOTS = [SAT + index * CADENCE for index in range(51)]
OUTAGE_SLOT_COUNT = 12
TEMPORARY_FAILURES = 6


def _migrate(db: Path, root: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(CANONICAL_SQLITE_SCHEMA)
    conn.execute("CREATE TABLE company_news_soak_sentinel (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO company_news_soak_sentinel(value) VALUES('unchanged')")
    conn.commit()
    conn.close()
    apply_sqlite_migration(db, expected_db_path=db, backup_dir=root / "backups")


def _report(code: int) -> dict:
    name = sector_name(code)
    materials = "\n\n\n\n".join(
        f"### 材料{i}：fixture需給{i}\n\n"
        "**確認できた事実**\n\nfixture上の需給変化を検証した。\n\n"
        "**日本企業への波及**\n\n日本上場企業の対象事業へ波及する。\n\n"
        "**利益への影響**\n\n感応度を確認した。\n\n"
        "**株価への織り込み**\n\n会社計画との差を確認した。\n\n"
        "**反対材料・注意点**\n\n価格反転時に仮説が崩れる。" +
        ("\n\n**試算**\n\n感応度は10〜20億円。\n\n**仮説**\n\n需給変化は継続する。" if i == 1 else "")
        for i in range(1, 4)
    )
    return {
        "importance": "A" if code % 4 else "B",
        "direction": "mixed",
        "summary_bullets": [
            f"{name}の世界需給を確認", "日本企業への利益波及を工程別に評価", "反証条件と翌週指標を特定",
        ],
        "watchlist_companies": [],
        "next_week_watchpoints": ["海外価格・在庫・企業計画の更新"],
        "missed_candidates": ["横断候補を確認したが重要度不足の材料は不採用"],
        "full_report_md": f"# 【東証33業種週次】{name}\n\n## 今週の要旨\nfixture要旨。\n\n{materials}",
        "sources": [{
            "title": f"Fixture primary source {code}",
            "url": f"https://example.com/sector-weekly-soak/{code}",
            "source_name": "Fixture Authority",
            "source_type": "government",
            "published_at": "2026-09-04",
        }],
    }


def _envelope(claimed: dict) -> dict:
    return {
        "schema_version": RESULT_SCHEMA,
        "assignment_id": claimed["assignment_id"],
        "claim_owner": OWNER,
        "sector_code": claimed["sector_code"],
        "sector_name": claimed["sector_name"],
        "period_start": claimed["period_start"],
        "period_end": claimed["period_end"],
        "attempt_count": claimed["attempt_count"],
        "contract_hash": claimed["contract_hash"],
        "report": _report(int(claimed["sector_code"])),
    }


def _available_slots(outage_start: int) -> list[datetime]:
    return ALL_SLOTS[:outage_start] + ALL_SLOTS[outage_start + OUTAGE_SLOT_COUNT:]


def _heartbeat_to_minute_40(db: Path, assignment_id: str, worker_at: datetime) -> None:
    for minute in (10, 20, 30, 40):
        renewed = heartbeat_one(db, assignment_id, OWNER, at=worker_at + timedelta(minutes=minute))
        assert renewed["status"] == "lease_renewed"


def test_accelerated_51_slot_33_sector_nonproduction_soak(tmp_path: Path):
    db = tmp_path / "isolated-soak.db"
    work = tmp_path / "work"
    _migrate(db, tmp_path)
    # Prior-period stale work is deliberately retained.
    conn = connect_sector_db(db)
    try:
        old_window = weekly_window(SAT - timedelta(days=7))
        old, _ = enqueue_assignment(conn, 1, old_window, now=SAT - timedelta(days=7))
    finally:
        conn.close()

    scheduler_log = tmp_path / "scheduler.jsonl"
    scheduler_lock = tmp_path / "scheduler.lock"
    # A twelve-hour stop from the beginning removes exactly twelve of 51 slots.
    slots = _available_slots(0)
    assert len(ALL_SLOTS) == 51 and len(slots) == 39

    sync_calls: list[int] = []

    def fake_sync(_db: Path, _dedupe_key: str, _run_id: str, _dry_run: bool) -> dict[str, int]:
        sync_calls.append(1)
        return {"canonical_sector_reports": 1, "canonical_sector_report_runs": 1}

    completed_codes: set[int] = set()
    failed_attempts = 0
    last_completed_at: datetime | None = None
    last_processed: Path | None = None
    last_claimed: dict | None = None
    for slot in slots:
        scheduled = run_scheduled(
            slot, db_path=db, log_path=scheduler_log, lock_path=scheduler_lock,
        )
        assert scheduled["created"] is True or scheduled["status"] in {"retry_queued", "retry_complete"}
        worker_at = slot + timedelta(minutes=5)
        claimed = claim_one(db, OWNER, work_root=work, at=worker_at)["assignment"]
        code = int(claimed["sector_code"])
        start_one(db, claimed["assignment_id"], OWNER, at=worker_at)
        _heartbeat_to_minute_40(db, claimed["assignment_id"], worker_at)
        if failed_attempts < TEMPORARY_FAILURES:
            released = abandon_one(
                db, claimed["assignment_id"], OWNER,
                at=worker_at + timedelta(minutes=49), reason="fixture temporary timeout",
                work_root=work,
            )
            assert released["status"] == "retry_pending"
            failed_attempts += 1
            continue
        payload = tmp_path / f"result-{code:02d}.json"
        payload.write_text(json.dumps(_envelope(claimed), ensure_ascii=False), encoding="utf-8")
        submit_at = worker_at + timedelta(minutes=45)
        staged = stage_one(
            db, claimed["assignment_id"], OWNER, payload, work_root=work,
            at=submit_at,
        )
        assert staged["status"] == "handoff_pending"
        submitted = process_one(
            WorkerPaths.from_values(tmp_path, db, work),
            work / "inbox" / f"{claimed['assignment_id']}.json", sync_func=fake_sync,
        )
        assert submitted["status"] == "success"
        completed_codes.add(code)
        last_completed_at = submit_at
        last_processed = Path(submitted["processed_path"])
        last_claimed = claimed

    assert last_processed is not None and last_claimed is not None
    repeated_submit = process_one(
        WorkerPaths.from_values(tmp_path, db, work), last_processed,
        sync_func=fake_sync,
    )
    assert repeated_submit["status"] == "already_success"

    conn = connect_sector_db(db)
    try:
        status = completion_status(conn, weekly_window(SAT))
        status_again = completion_status(conn, weekly_window(SAT))
        assert get_assignment(conn, old["assignment_id"])["status"] == "ready"
        sentinel = conn.execute("SELECT value FROM company_news_soak_sentinel").fetchone()[0]
        report_count = conn.execute("SELECT count(*) FROM canonical_sector_reports").fetchone()[0]
        queue_count = conn.execute(
            "SELECT count(*) FROM sector_weekly_work_assignments WHERE period_end=?",
            ("2026-09-04T20:59:59Z",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert status["state"] == "COMPLETE_33_OF_33"
    assert status["success"] == 33 and status["failed"] == 0
    assert status["missing_count"] == status["duplicate_count"] == 0
    assert status["stale_count"] == 1
    assert failed_attempts == TEMPORARY_FAILURES
    assert status["attempts"] == 33 + TEMPORARY_FAILURES
    assert last_completed_at is not None
    assert last_completed_at <= datetime.fromisoformat("2026-09-07T08:55:00+09:00")
    completion_events = {status["completion_event"]["event_key"], status_again["completion_event"]["event_key"]}
    assert len(completion_events) == 1
    assert len(sync_calls) == report_count == queue_count == 33
    assert sentinel == "unchanged"
    rerun = run_scheduled(
        datetime.fromisoformat("2026-09-07T09:00:00+09:00"), db_path=db,
        log_path=scheduler_log, lock_path=scheduler_lock,
    )
    assert rerun["created"] is False


@pytest.mark.parametrize("outage_start", range(40))
def test_every_12_hour_outage_position_retains_capacity_for_six_retries(tmp_path: Path, outage_start: int):
    db = tmp_path / f"capacity-{outage_start:02d}.db"
    work = tmp_path / f"work-{outage_start:02d}"
    _migrate(db, tmp_path / f"migration-{outage_start:02d}")
    conn = connect_sector_db(db)
    try:
        old, _ = enqueue_assignment(
            conn, 1, weekly_window(SAT - timedelta(days=7)), now=SAT - timedelta(days=7),
        )
    finally:
        conn.close()
    slots = _available_slots(outage_start)
    assert len(slots) == 39
    failures = 0
    completion_keys: set[str] = set()
    last_finished_at: datetime | None = None
    for slot in slots:
        run_scheduled(
            slot, db_path=db, log_path=tmp_path / f"scheduler-{outage_start:02d}.jsonl",
            lock_path=tmp_path / f"scheduler-{outage_start:02d}.lock",
        )
        worker_at = slot + timedelta(minutes=5)
        claimed = claim_one(db, OWNER, work_root=work, at=worker_at)["assignment"]
        start_one(db, claimed["assignment_id"], OWNER, at=worker_at)
        _heartbeat_to_minute_40(db, claimed["assignment_id"], worker_at)
        if failures < TEMPORARY_FAILURES:
            abandon_one(
                db, claimed["assignment_id"], OWNER,
                at=worker_at + timedelta(minutes=49), reason="capacity fixture retry",
                work_root=work,
            )
            failures += 1
            last_finished_at = worker_at + timedelta(minutes=49)
            continue
        conn = connect_sector_db(db)
        try:
            complete_assignment(
                conn, claimed["assignment_id"], OWNER,
                f"{int(claimed['sector_code']):064x}",
                now=worker_at + timedelta(minutes=45),
            )
        finally:
            conn.close()
        last_finished_at = worker_at + timedelta(minutes=45)
    conn = connect_sector_db(db)
    try:
        status = completion_status(conn, weekly_window(SAT))
        repeated = completion_status(conn, weekly_window(SAT))
        assert get_assignment(conn, old["assignment_id"])["status"] == "ready"
        sentinel = conn.execute("SELECT value FROM company_news_soak_sentinel").fetchone()[0]
    finally:
        conn.close()
    completion_keys.add(status["completion_event"]["event_key"])
    completion_keys.add(repeated["completion_event"]["event_key"])
    assert failures == 6 and status["attempts"] == 39
    assert status["success"] == 33 and status["failed"] == 0
    assert status["missing_count"] == status["duplicate_count"] == 0
    assert status["stale_count"] == 1 and len(completion_keys) == 1
    assert sentinel == "unchanged"
    assert last_finished_at is not None
    assert last_finished_at <= datetime.fromisoformat("2026-09-07T08:55:00+09:00")
