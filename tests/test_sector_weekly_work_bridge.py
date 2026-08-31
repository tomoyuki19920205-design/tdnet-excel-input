import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from lib.sector_weekly import CANONICAL_SQLITE_SCHEMA, connect_sector_db, weekly_window
from lib.sector_weekly_work import (
    claim_next,
    enqueue_assignment,
    enqueue_retry_candidate,
    get_assignment,
    recover_expired_leases,
)
from tools.sector_weekly_work_bridge import (
    RESULT_SCHEMA,
    SectorBridgeError,
    claim_one,
    start_one,
    submit_one,
)
from tools.apply_sector_weekly_work_sqlite_migration import apply_sqlite_migration

AT = datetime.fromisoformat("2026-09-05T06:05:00+09:00")
OWNER = "sector-weekly-worker"


def _migrate_fixture(db: Path) -> None:
    if db.exists():
        return
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(CANONICAL_SQLITE_SCHEMA)
    conn.close()
    apply_sqlite_migration(
        db, expected_db_path=db, backup_dir=db.parent / "migration_backups",
    )


def _report() -> dict:
    return {
        "importance": "A",
        "direction": "mixed",
        "summary_bullets": ["海外需給が変化", "国内利益へ波及", "反証条件を監視"],
        "watchlist_companies": [{"code": "5713", "name": "住友金属鉱山", "direction": "mixed"}],
        "next_week_watchpoints": ["取引所在庫と現物プレミアム"],
        "missed_candidates": ["マイナー金属群は確認したが重要変動なし"],
        "full_report_md": "# 【東証33業種週次】鉱業\n\n## 今週の要旨\nFactとEstimateを区別した本文。\n\n## 重要材料\nTransmission、Magnitude、Pricing-in、Counterevidenceを確認。\n\n" + "定量的な利益波及と反証条件を簡潔に記述する。" * 8,
        "sources": [{
            "title": "Primary market data", "url": "https://example.com/market",
            "source_name": "Exchange", "source_type": "market_data", "published_at": "2026-09-04",
        }],
    }


def _enqueue(db: Path, code: int = 2) -> dict:
    _migrate_fixture(db)
    conn = connect_sector_db(db)
    try:
        row, _ = enqueue_assignment(conn, code, weekly_window(AT), now=AT)
        return row
    finally:
        conn.close()


def _envelope(assignment: dict, report: dict | None = None) -> dict:
    return {
        "schema_version": RESULT_SCHEMA,
        "assignment_id": assignment["assignment_id"],
        "claim_owner": OWNER,
        "sector_code": assignment["sector_code"],
        "sector_name": assignment["sector_name"],
        "period_start": assignment["period_start"],
        "period_end": assignment["period_end"],
        "report": report or _report(),
    }


def test_claim_returns_one_assignment_and_prevents_double_claim(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    _enqueue(db, 2)
    _enqueue(db, 6)
    conn = connect_sector_db(db)
    try:
        first = claim_next(conn, OWNER, now=AT)
        assert first is not None and first["sector_code"] == 2
        assert claim_next(conn, OWNER, now=AT) is None
        assert claim_next(conn, "second-worker", now=AT) is None
    finally:
        conn.close()


def test_bridge_claim_contract_contains_prompt_but_no_api_controls(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    _enqueue(db)
    result = claim_one(db, OWNER, work_root=tmp_path / "work", at=AT)
    assert result["status"] == "claimed"
    assignment = result["assignment"]
    assert "日本企業名や国内ニュースから検索を始めず" in assignment["research_prompt"]
    assert assignment["result_schema_version"] == RESULT_SCHEMA
    assert "max_tool_calls" not in assignment["research_prompt"]
    assert "OPENAI_API_KEY" not in assignment["research_prompt"]


def test_lease_expiry_recovers_assignment_for_retry(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    assignment = _enqueue(db)
    conn = connect_sector_db(db)
    try:
        claim_next(conn, OWNER, now=AT, lease_seconds=60)
        assert recover_expired_leases(conn, now=AT + timedelta(seconds=61)) == 1
        recovered = get_assignment(conn, assignment["assignment_id"])
        assert recovered["status"] == "retry_pending"
        assert recovered["claim_owner"] is None
    finally:
        conn.close()


def test_valid_submit_upserts_canonical_syncs_and_is_idempotent(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db)
    claimed = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    start_one(db, claimed["assignment_id"], OWNER, at=AT)
    draft = tmp_path / "result.json"
    draft.write_text(json.dumps(_envelope(claimed), ensure_ascii=False), encoding="utf-8")
    sync_calls: list[tuple[Path, bool]] = []

    def fake_sync(path: Path, dry_run: bool) -> dict[str, int]:
        sync_calls.append((path, dry_run))
        return {"canonical_sector_reports": 1, "canonical_sector_report_runs": 33}

    first = submit_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT, sync_func=fake_sync)
    second = submit_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT, sync_func=fake_sync)
    assert first["status"] == "success"
    assert second["status"] == "already_success"
    assert len(sync_calls) == 1
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM canonical_sector_reports").fetchone()[0] == 1
    row = conn.execute(
        "SELECT status,submitted_payload_hash FROM sector_weekly_work_assignments WHERE assignment_id=?",
        (claimed["assignment_id"],),
    ).fetchone()
    assert row[0] == "success" and len(row[1]) == 64


def test_sync_failure_keeps_assignment_retryable_and_never_marks_it_success(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db)
    claimed = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    draft = tmp_path / "result.json"
    draft.write_text(json.dumps(_envelope(claimed), ensure_ascii=False), encoding="utf-8")

    def failing_sync(path: Path, _dry_run: bool) -> dict[str, int]:
        probe = sqlite3.connect(path)
        status = probe.execute(
            "SELECT status FROM canonical_sector_report_runs WHERE run_id=?", (claimed["stable_key"],),
        ).fetchone()[0]
        probe.close()
        assert status == "success"
        raise RuntimeError("fixture sync failure")

    with pytest.raises(RuntimeError, match="fixture sync failure"):
        submit_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT, sync_func=failing_sync)
    conn = connect_sector_db(db)
    try:
        assignment = get_assignment(conn, claimed["assignment_id"])
        run = conn.execute(
            "SELECT status FROM canonical_sector_report_runs WHERE run_id=?", (claimed["stable_key"],),
        ).fetchone()
        assert assignment["status"] == "retry_pending"
        assert run["status"] == "retry_pending"
        assert conn.execute("SELECT count(*) FROM canonical_sector_reports").fetchone()[0] == 1
    finally:
        conn.close()


def test_malformed_payload_is_quarantined_and_retried(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db)
    claimed = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    draft = tmp_path / "bad.json"
    draft.write_text("{not-json", encoding="utf-8")
    with pytest.raises(SectorBridgeError, match="valid UTF-8 JSON"):
        submit_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT, sync_func=lambda *_: {})
    conn = connect_sector_db(db)
    try:
        row = get_assignment(conn, claimed["assignment_id"])
        assert row["status"] == "retry_pending"
        assert row["last_error_type"] == "SectorBridgeError"
        assert conn.execute("SELECT count(*) FROM canonical_sector_reports").fetchone()[0] == 0
    finally:
        conn.close()
    assert (work / "quarantine" / f"{claimed['assignment_id']}.json").exists()


def test_failed_sector_does_not_block_a_fresh_sector(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db, 2)
    first = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    with pytest.raises(SectorBridgeError):
        submit_one(db, first["assignment_id"], OWNER, bad, work_root=work, at=AT, sync_func=lambda *_: {})
    _enqueue(db, 3)
    second = claim_one(db, OWNER, work_root=work, at=AT + timedelta(hours=1))["assignment"]
    assert second["sector_code"] == 3
    assert second["assignment_id"] != first["assignment_id"]


@pytest.mark.parametrize("field,value", [
    ("claim_owner", "wrong-worker"),
    ("sector_code", 4),
    ("period_end", "2026-09-06T05:59:59+09:00"),
])
def test_submit_rejects_owner_sector_or_period_mismatch(tmp_path: Path, field: str, value: object):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db)
    claimed = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    envelope = _envelope(claimed)
    envelope[field] = value
    draft = tmp_path / "wrong.json"
    draft.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SectorBridgeError, match="does not match"):
        submit_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT, sync_func=lambda *_: {})


def test_sunday_retry_enqueues_one_missing_or_failed_sector(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    _migrate_fixture(db)
    window = weekly_window(AT)
    conn = connect_sector_db(db)
    try:
        first, created = enqueue_retry_candidate(
            conn, window, now=datetime.fromisoformat("2026-09-06T15:00:00+09:00"),
        )
        assert created is True
        assert first["sector_code"] == 1
        assert conn.execute("SELECT count(*) FROM sector_weekly_work_assignments").fetchone()[0] == 1
    finally:
        conn.close()


def test_sector_queue_does_not_create_or_mutate_company_news_tables(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    _enqueue(db)
    conn = sqlite3.connect(db)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sector_weekly_work_assignments" in names
    assert not any(name.startswith("company_news") for name in names)
