import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from lib.sector_weekly import CANONICAL_SQLITE_SCHEMA, sector_name, weekly_window
from lib.sector_weekly_sqlite import connect_sector_db
from lib.sector_weekly_work import (
    completion_status,
    enqueue_assignment,
    get_assignment,
    recover_expired_leases,
)
from tools.apply_sector_weekly_work_sqlite_migration import apply_sqlite_migration
from tools.sector_weekly_scheduler import run_scheduled
from tools.sector_weekly_work_bridge import (
    RESULT_SCHEMA,
    SectorBridgeError,
    claim_one,
    heartbeat_one,
    start_one,
    submit_one,
)

SAT = datetime.fromisoformat("2026-09-05T06:00:00+09:00")
OWNER = "sector-weekly-soak-worker"


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
    return {
        "importance": "A" if code % 4 else "B",
        "direction": "mixed",
        "summary_bullets": [
            f"{name}の世界需給を確認", "日本企業への利益波及を工程別に評価", "反証条件と翌週指標を特定",
        ],
        "watchlist_companies": [],
        "next_week_watchpoints": ["海外価格・在庫・企業計画の更新"],
        "missed_candidates": ["横断候補を確認したが重要度不足の材料は不採用"],
        "full_report_md": (
            f"# 【東証33業種週次】{name}\n\n## 今週の要旨\n"
            "**Fact** fixture上の需給変化を検証した。\n\n## 重要材料\n"
            "**Transmission** 日本上場企業の対象事業へ波及する。\n\n"
            "**Magnitude** Estimateとして感応度を確認した。\n\n"
            "**Pricing-in** 会社計画との差を確認した。\n\n"
            "**Counterevidence** 価格反転時に仮説が崩れる。\n\n"
            + "システム完走性を確認する秘密情報のないfixture本文。" * 10
        ),
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
        "report": _report(int(claimed["sector_code"])),
    }


def test_accelerated_33_sector_nonproduction_soak(tmp_path: Path):
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
    first = run_scheduled(SAT, db_path=db, log_path=scheduler_log, lock_path=scheduler_lock)
    assert first["sector_code"] == 1
    # Simulate a 12-hour PC stop, then accelerate distinct invocations without sleep.
    resumed = SAT + timedelta(hours=13)
    for index in range(32):
        result = run_scheduled(
            resumed + timedelta(minutes=index + 1), db_path=db,
            log_path=scheduler_log, lock_path=scheduler_lock,
        )
        assert result["created"] is True and result["sector_code"] == index + 2

    sync_calls: list[int] = []

    def fake_sync(_db: Path, _dry_run: bool) -> dict[str, int]:
        sync_calls.append(1)
        return {"canonical_sector_reports": len(sync_calls)}

    cursor = resumed + timedelta(hours=1)
    completed_codes: set[int] = set()
    malformed_done = False
    lease_expiry_done = False
    while len(completed_codes) < 33:
        submit_at = cursor + timedelta(minutes=11)
        claimed = claim_one(db, OWNER, work_root=work, at=cursor)["assignment"]
        code = int(claimed["sector_code"])
        start_one(db, claimed["assignment_id"], OWNER, at=cursor)
        if code == 1 and not malformed_done:
            malformed = tmp_path / "malformed.json"
            malformed.write_text("{not-json", encoding="utf-8")
            with pytest.raises(SectorBridgeError):
                submit_one(
                    db, claimed["assignment_id"], OWNER, malformed,
                    work_root=work, at=cursor + timedelta(minutes=1), sync_func=fake_sync,
                )
            malformed_done = True
            cursor += timedelta(hours=1)
            continue
        if code == 1 and not lease_expiry_done:
            # Worker disappears; lease recovery and a fresh claim complete it.
            recovery_conn = connect_sector_db(db)
            try:
                assert recover_expired_leases(recovery_conn, now=cursor + timedelta(minutes=56)) == 1
            finally:
                recovery_conn.close()
            lease_expiry_done = True
            cursor += timedelta(hours=1)
            continue
        heartbeat = heartbeat_one(
            db, claimed["assignment_id"], OWNER, at=cursor + timedelta(minutes=10),
        )
        assert heartbeat["status"] == "lease_renewed"
        payload = tmp_path / f"result-{code:02d}.json"
        payload.write_text(json.dumps(_envelope(claimed), ensure_ascii=False), encoding="utf-8")
        submitted = submit_one(
            db, claimed["assignment_id"], OWNER, payload, work_root=work,
            at=submit_at, sync_func=fake_sync,
        )
        assert submitted["status"] == "success"
        completed_codes.add(code)
        cursor += timedelta(hours=1)

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
    assert malformed_done and lease_expiry_done
    completion_events = {status["completion_event"]["event_key"], status_again["completion_event"]["event_key"]}
    assert len(completion_events) == 1
    assert len(sync_calls) == report_count == queue_count == 33
    assert sentinel == "unchanged"
    rerun = run_scheduled(
        datetime.fromisoformat("2026-09-06T23:30:00+09:00"), db_path=db,
        log_path=scheduler_log, lock_path=scheduler_lock,
    )
    assert rerun["created"] is False
