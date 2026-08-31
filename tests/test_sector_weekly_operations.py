import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from lib.sector_weekly import CANONICAL_SQLITE_SCHEMA, weekly_window
from lib.sector_weekly_sqlite import connect_sector_db
from lib.sector_weekly_work import (
    DEFAULT_LEASE_SECONDS,
    MAX_LEASE_LIFETIME_SECONDS,
    MAX_ATTEMPTS,
    SectorWorkError,
    abandon_assignment,
    claim_next,
    complete_assignment,
    completion_status,
    enqueue_assignment,
    get_assignment,
    heartbeat_assignment,
    payload_hash,
    recover_expired_leases,
)
from tools.apply_sector_weekly_work_sqlite_migration import apply_sqlite_migration
from tools.sector_weekly_scheduler import (
    SCHEDULER_CADENCE_MINUTES,
    in_catchup_window,
    in_worker_window,
    run_scheduled,
    scheduler_slots,
)
from tools.sector_weekly_work_bridge import abandon_one, claim_one, heartbeat_one, start_one, status_one

SAT = datetime.fromisoformat("2026-09-05T06:00:00+09:00")
OWNER = "sector-weekly-worker"
CADENCE = timedelta(minutes=SCHEDULER_CADENCE_MINUTES)
SLOTS = list(scheduler_slots(SAT))


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "isolated-sector-weekly.db"
    conn = sqlite3.connect(db)
    conn.executescript(CANONICAL_SQLITE_SCHEMA)
    conn.close()
    apply_sqlite_migration(db, expected_db_path=db, backup_dir=tmp_path / "backups")
    return db


def _schedule(db: Path, tmp_path: Path, at: datetime) -> dict:
    return run_scheduled(
        at, db_path=db, log_path=tmp_path / "scheduler.jsonl",
        lock_path=tmp_path / "scheduler.lock",
    )


def test_scheduler_has_51_hourly_slots_and_stops_after_monday_0800(tmp_path: Path):
    db = _db(tmp_path)
    assert CADENCE == timedelta(hours=1)
    assert len(SLOTS) == 51
    assert SLOTS[0].isoformat() == "2026-09-05T06:00:00+09:00"
    assert SLOTS[-1].isoformat() == "2026-09-07T08:00:00+09:00"
    assert sum(slot.weekday() == 5 for slot in SLOTS) == 18
    assert sum(slot.weekday() == 6 for slot in SLOTS) == 24
    assert sum(slot.weekday() == 0 for slot in SLOTS) == 9
    assert in_catchup_window(SLOTS[-1]) is True
    assert in_catchup_window(SLOTS[-1] + CADENCE) is False
    for index, slot in enumerate(SLOTS):
        result = _schedule(db, tmp_path, slot)
        if index < 33:
            assert result["created"] is True
            assert result["sector_code"] == index + 1
        else:
            assert result["created"] is False
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM sector_weekly_work_assignments").fetchone()[0] == 33
    assert conn.execute("SELECT count(DISTINCT stable_key) FROM sector_weekly_work_assignments").fetchone()[0] == 33


def test_scheduler_duplicate_same_instant_and_dry_run_are_read_only(tmp_path: Path):
    db = _db(tmp_path)
    first = _schedule(db, tmp_path, SAT)
    second = _schedule(db, tmp_path, SAT)
    before = db.stat().st_mtime_ns
    planned = run_scheduled(
        SAT + CADENCE, db_path=db, log_path=tmp_path / "unused.jsonl",
        lock_path=tmp_path / "unused.lock", dry_run=True,
    )
    assert first["created"] is True
    assert second["created"] is False
    assert second["status"] == "slot_already_processed"
    assert planned["status"] == "dry_run" and planned["sector_code"] == 2
    assert not (tmp_path / "unused.jsonl").exists()
    assert not (tmp_path / "unused.lock").exists()
    assert db.stat().st_mtime_ns == before


@pytest.mark.parametrize("gap", [3, 12])
def test_scheduler_catches_up_oldest_missing_after_gap(tmp_path: Path, gap: int):
    db = _db(tmp_path)
    assert _schedule(db, tmp_path, SAT)["sector_code"] == 1
    after_gap = _schedule(db, tmp_path, SAT + timedelta(hours=gap) + CADENCE)
    assert after_gap["sector_code"] == 2
    assert after_gap["created"] is True


def test_scheduler_outside_window_week_rollover_and_stale_do_not_collide(tmp_path: Path):
    db = _db(tmp_path)
    outside = _schedule(db, tmp_path, datetime.fromisoformat("2026-09-04T20:00:00+09:00"))
    assert outside["status"] == "not_scheduled"
    first = _schedule(db, tmp_path, SAT)
    next_week = _schedule(db, tmp_path, SAT + timedelta(days=7))
    assert first["sector_code"] == next_week["sector_code"] == 1
    assert first["stable_key"] != next_week["stable_key"]


def test_monday_recovery_boundaries_and_worker_noop_outside_window(tmp_path: Path):
    db = _db(tmp_path)
    monday_scheduler = datetime.fromisoformat("2026-09-07T08:00:00+09:00")
    monday_worker = datetime.fromisoformat("2026-09-07T08:05:00+09:00")
    assert _schedule(db, tmp_path, monday_scheduler)["sector_code"] == 1
    assert in_worker_window(monday_worker) is True
    claimed = claim_one(db, OWNER, at=monday_worker, work_root=tmp_path / "work")
    assert claimed["status"] == "claimed"
    assert _schedule(db, tmp_path, monday_scheduler + timedelta(hours=1))["status"] == "not_scheduled"
    outside = claim_one(db, OWNER, at=monday_worker + timedelta(hours=1), work_root=tmp_path / "work")
    assert outside == {"status": "no_work", "claim_owner": OWNER, "reason": "outside_worker_window"}


def test_scheduler_uses_jst_not_host_dst(tmp_path: Path):
    db = _db(tmp_path)
    utc_instant = datetime.fromisoformat("2026-09-04T21:00:00+00:00")
    result = _schedule(db, tmp_path, utc_instant)
    assert result["sector_code"] == 1
    assert result["period_end"] == "2026-09-04T20:59:59Z"


def _enqueue_two_weeks(db: Path) -> tuple[dict, dict]:
    current = weekly_window(SAT)
    previous_at = SAT - timedelta(days=7)
    previous = weekly_window(previous_at)
    conn = connect_sector_db(db)
    try:
        old, _ = enqueue_assignment(conn, 1, previous, now=previous_at)
        fresh, _ = enqueue_assignment(conn, 2, current, now=SAT)
        return old, fresh
    finally:
        conn.close()


def test_claim_current_period_excludes_previous_retryable_job(tmp_path: Path):
    db = _db(tmp_path)
    old, fresh = _enqueue_two_weeks(db)
    conn = connect_sector_db(db)
    try:
        claimed = claim_next(conn, OWNER, now=SAT, window=weekly_window(SAT))
        assert claimed["assignment_id"] == fresh["assignment_id"]
        assert get_assignment(conn, old["assignment_id"])["status"] == "ready"
    finally:
        conn.close()


def test_heartbeat_owner_expiry_and_maximum_lifetime(tmp_path: Path):
    db = _db(tmp_path)
    conn = connect_sector_db(db)
    try:
        assignment, _ = enqueue_assignment(conn, 1, weekly_window(SAT), now=SAT)
        claim_next(conn, OWNER, now=SAT, lease_seconds=600, window=weekly_window(SAT))
        renewed = heartbeat_assignment(
            conn, assignment["assignment_id"], OWNER, now=SAT + timedelta(minutes=5),
            lease_seconds=600, max_lifetime_seconds=1200,
        )
        assert renewed["lease_expires_at"] == "2026-09-04T21:15:00Z"
        capped = heartbeat_assignment(
            conn, assignment["assignment_id"], OWNER, now=SAT + timedelta(minutes=11),
            lease_seconds=600, max_lifetime_seconds=1200,
        )
        assert capped["lease_expires_at"] == "2026-09-04T21:20:00Z"
        with pytest.raises(SectorWorkError, match="owner"):
            heartbeat_assignment(conn, assignment["assignment_id"], "other-worker", now=SAT + timedelta(minutes=12))
        with pytest.raises(SectorWorkError, match="maximum lease lifetime"):
            heartbeat_assignment(
                conn, assignment["assignment_id"], OWNER, now=SAT + timedelta(minutes=19),
                lease_seconds=600, max_lifetime_seconds=1200,
            )
    finally:
        conn.close()


def test_default_lease_cadence_and_atomic_abandon(tmp_path: Path):
    assert DEFAULT_LEASE_SECONDS == 15 * 60
    assert MAX_LEASE_LIFETIME_SECONDS == 55 * 60
    db = _db(tmp_path)
    work = tmp_path / "work"
    conn = connect_sector_db(db)
    try:
        assignment, _ = enqueue_assignment(conn, 1, weekly_window(SAT), now=SAT)
    finally:
        conn.close()
    worker_at = SAT + timedelta(minutes=5)
    claimed = claim_one(db, OWNER, at=worker_at, work_root=work)["assignment"]
    start_one(db, assignment["assignment_id"], OWNER, at=worker_at)
    for minute in (10, 20, 30, 40):
        heartbeat_one(db, assignment["assignment_id"], OWNER, at=worker_at + timedelta(minutes=minute))
    released = abandon_one(
        db, assignment["assignment_id"], OWNER, at=worker_at + timedelta(minutes=49),
        reason="fixture hard time budget", work_root=work,
    )
    assert claimed["lease_expires_at"] == "2026-09-04T21:20:00Z"
    assert released["status"] == "retry_pending" and released["released"] is True
    conn = connect_sector_db(db)
    try:
        row = get_assignment(conn, assignment["assignment_id"])
    finally:
        conn.close()
    assert row["claim_owner"] is None and row["lease_expires_at"] is None
    assert row["started_at"] is None and row["last_error_type"] == "WorkerAbandoned"
    conn = connect_sector_db(db)
    try:
        with pytest.raises(SectorWorkError, match="active claim"):
            abandon_assignment(
                conn, assignment["assignment_id"], OWNER,
                now=worker_at + timedelta(minutes=50),
            )
    finally:
        conn.close()
    recovered = claim_one(db, OWNER, at=worker_at + timedelta(hours=1), work_root=work)["assignment"]
    assert recovered["assignment_id"] == assignment["assignment_id"]
    assert recovered["attempt_count"] == 2


def test_abandon_rejects_wrong_owner_expired_claim_and_late_submit(tmp_path: Path):
    db = _db(tmp_path)
    conn = connect_sector_db(db)
    try:
        assignment, _ = enqueue_assignment(conn, 1, weekly_window(SAT), now=SAT)
        claim_next(conn, OWNER, now=SAT, lease_seconds=60, window=weekly_window(SAT))
        with pytest.raises(SectorWorkError, match="owner"):
            abandon_assignment(conn, assignment["assignment_id"], "other-worker", now=SAT + timedelta(seconds=30))
        with pytest.raises(SectorWorkError, match="expired"):
            abandon_assignment(conn, assignment["assignment_id"], OWNER, now=SAT + timedelta(seconds=61))
        recover_expired_leases(conn, now=SAT + timedelta(seconds=61))
        claimed = claim_next(conn, OWNER, now=SAT + timedelta(seconds=62), window=weekly_window(SAT))
        abandon_assignment(conn, assignment["assignment_id"], OWNER, now=SAT + timedelta(seconds=63))
        assert claimed and claimed["attempt_count"] == 2
        with pytest.raises(SectorWorkError, match="active claim"):
            complete_assignment(
                conn, assignment["assignment_id"], OWNER, payload_hash({"late": True}),
                now=SAT + timedelta(seconds=64),
            )
    finally:
        conn.close()


def test_crashed_worker_lease_expires_before_next_hourly_claim(tmp_path: Path):
    db = _db(tmp_path)
    work = tmp_path / "work"
    worker_at = SAT + timedelta(minutes=5)
    conn = connect_sector_db(db)
    try:
        assignment, _ = enqueue_assignment(conn, 1, weekly_window(SAT), now=SAT)
    finally:
        conn.close()
    claim_one(db, OWNER, at=worker_at, work_root=work)
    conn = connect_sector_db(db)
    try:
        assert recover_expired_leases(conn, now=worker_at + timedelta(minutes=16)) == 1
    finally:
        conn.close()
    reclaimed = claim_one(db, OWNER, at=worker_at + timedelta(hours=1), work_root=work)["assignment"]
    assert reclaimed["assignment_id"] == assignment["assignment_id"]
    assert reclaimed["attempt_count"] == 2


def test_abandon_third_attempt_becomes_final_failed(tmp_path: Path):
    db = _db(tmp_path)
    conn = connect_sector_db(db)
    try:
        assignment, _ = enqueue_assignment(conn, 1, weekly_window(SAT), now=SAT)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            claimed = claim_next(conn, OWNER, now=SAT + timedelta(minutes=attempt), window=weekly_window(SAT))
            assert claimed and claimed["attempt_count"] == attempt
            released = abandon_assignment(
                conn, assignment["assignment_id"], OWNER,
                now=SAT + timedelta(minutes=attempt, seconds=1),
            )
        assert released["status"] == "failed"
        assert claim_next(conn, OWNER, now=SAT + timedelta(minutes=10), window=weekly_window(SAT)) is None
    finally:
        conn.close()


def test_expired_lease_retries_then_stops_at_max_attempts_and_rejects_late_submit(tmp_path: Path):
    db = _db(tmp_path)
    digest = payload_hash({"report": "fixture"})
    conn = connect_sector_db(db)
    try:
        assignment, _ = enqueue_assignment(conn, 1, weekly_window(SAT), now=SAT)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            claimed = claim_next(
                conn, OWNER, now=SAT + timedelta(minutes=attempt * 2), lease_seconds=60,
                window=weekly_window(SAT),
            )
            assert claimed and claimed["attempt_count"] == attempt
            expired_at = SAT + timedelta(minutes=attempt * 2, seconds=61)
            with pytest.raises(SectorWorkError, match="expired"):
                complete_assignment(conn, assignment["assignment_id"], OWNER, digest, now=expired_at)
            assert recover_expired_leases(conn, now=expired_at) == 1
        assert get_assignment(conn, assignment["assignment_id"])["status"] == "failed"
        assert claim_next(conn, OWNER, now=SAT + timedelta(hours=1), window=weekly_window(SAT)) is None
    finally:
        conn.close()


def test_completion_states_json_contract_and_deterministic_event(tmp_path: Path):
    db = _db(tmp_path)
    window = weekly_window(SAT)
    empty = status_one(db, at=SAT)
    assert empty["state"] == "INCOMPLETE_RETRYABLE" and empty["exit_code"] == 11
    conn = connect_sector_db(db)
    try:
        for code in range(1, 34):
            row, _ = enqueue_assignment(conn, code, window, now=SAT + timedelta(minutes=code))
            claimed = claim_next(
                conn, OWNER, now=SAT + timedelta(minutes=code), window=window,
            )
            assert claimed and claimed["assignment_id"] == row["assignment_id"]
            complete_assignment(conn, row["assignment_id"], OWNER, payload_hash({"code": code}), now=SAT + timedelta(minutes=code, seconds=1))
        complete = completion_status(conn, window)
        again = completion_status(conn, window)
    finally:
        conn.close()
    assert complete["state"] == "COMPLETE_33_OF_33"
    assert complete["success"] == 33 and complete["missing_count"] == 0
    assert complete["completion_event"] == again["completion_event"]
    json.dumps(complete, ensure_ascii=False)


def test_stale_is_reported_but_does_not_prevent_current_completion(tmp_path: Path):
    db = _db(tmp_path)
    old, _ = _enqueue_two_weeks(db)
    conn = connect_sector_db(db)
    current = weekly_window(SAT)
    try:
        for code in range(1, 34):
            row, _ = enqueue_assignment(conn, code, current, now=SAT)
            if code == 2:
                continue
            claimed = claim_next(conn, OWNER, now=SAT + timedelta(minutes=code), window=current)
            complete_assignment(conn, claimed["assignment_id"], OWNER, payload_hash({"code": code}), now=SAT + timedelta(minutes=code, seconds=1))
        # sector 2 was created by _enqueue_two_weeks and is completed last.
        claimed = claim_next(conn, OWNER, now=SAT + timedelta(hours=1), window=current)
        complete_assignment(conn, claimed["assignment_id"], OWNER, payload_hash({"code": 2}), now=SAT + timedelta(hours=1, seconds=1))
        result = completion_status(conn, current)
        assert get_assignment(conn, old["assignment_id"])["status"] == "ready"
    finally:
        conn.close()
    assert result["state"] == "COMPLETE_33_OF_33"
    assert "STALE_PREVIOUS_PERIOD" in result["conditions"]
    assert result["stale_count"] == 1


def test_bridge_claim_defaults_to_current_week(tmp_path: Path):
    db = _db(tmp_path)
    old, fresh = _enqueue_two_weeks(db)
    result = claim_one(db, OWNER, at=SAT + timedelta(minutes=5), work_root=tmp_path / "work")
    assert result["assignment"]["assignment_id"] == fresh["assignment_id"]
    conn = connect_sector_db(db)
    try:
        assert get_assignment(conn, old["assignment_id"])["status"] == "ready"
    finally:
        conn.close()


def test_completion_reports_32_failed_active_stale_and_inconsistent_states(tmp_path: Path):
    db = _db(tmp_path)
    window = weekly_window(SAT)
    conn = connect_sector_db(db)
    try:
        for code in range(1, 33):
            enqueue_assignment(conn, code, window, now=SAT + timedelta(seconds=code))
        result_32 = completion_status(conn, window)
        assert result_32["state"] == "INCOMPLETE_RETRYABLE"
        assert result_32["assignments"] == 32 and result_32["missing_count"] == 1
        claimed = claim_next(conn, OWNER, now=SAT + timedelta(minutes=1), window=window)
        active = completion_status(conn, window)
        assert active["state"] == "IN_PROGRESS" and active["claimed"] == 1
        conn.execute(
            "UPDATE sector_weekly_work_assignments SET status='failed',attempt_count=?,claim_owner=NULL,"
            "claimed_at=NULL,lease_expires_at=NULL,last_error_type='FixtureFailure' WHERE assignment_id=?",
            (MAX_ATTEMPTS, claimed["assignment_id"]),
        )
        conn.commit()
        failed = completion_status(conn, window)
        assert failed["state"] == "FAILED_FINAL" and failed["failed"] == 1
        exemplar = conn.execute(
            "SELECT * FROM sector_weekly_work_assignments WHERE sector_code=2 AND period_start=?",
            ("2026-08-28T21:00:00Z",),
        ).fetchone()
        values = dict(exemplar)
        values["assignment_id"] = "00000000-0000-0000-0000-000000000099"
        values["stable_key"] = "sector_weekly:2026-09-04:duplicate-02"
        columns = list(values)
        conn.execute(
            f"INSERT INTO sector_weekly_work_assignments ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            [values[column] for column in columns],
        )
        conn.commit()
        inconsistent = completion_status(conn, window)
        assert inconsistent["state"] == "DATA_INCONSISTENT"
        assert inconsistent["duplicate_count"] == 1
    finally:
        conn.close()


def test_status_with_only_previous_period_work_uses_stale_state(tmp_path: Path):
    db = _db(tmp_path)
    previous_at = SAT - timedelta(days=7)
    conn = connect_sector_db(db)
    try:
        enqueue_assignment(conn, 1, weekly_window(previous_at), now=previous_at)
        result = completion_status(conn, weekly_window(SAT))
    finally:
        conn.close()
    assert result["state"] == "STALE_PREVIOUS_PERIOD"
    assert result["exit_code"] == 13 and result["stale_count"] == 1
