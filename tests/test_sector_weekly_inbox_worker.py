import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

import tools.sector_weekly_inbox_worker as inbox_worker
import tools.sector_weekly_work_bridge as work_bridge
from lib.sector_weekly import CANONICAL_SQLITE_SCHEMA, connect_sector_db, weekly_window
from lib.sector_weekly_work import enqueue_assignment, get_assignment, payload_hash
from tools.apply_sector_weekly_work_sqlite_migration import apply_sqlite_migration
from tools.sector_weekly_inbox_worker import WorkerPaths, process_one, run_once
from tools.sector_weekly_work_bridge import (
    QUALITY_REOPEN_CONFIRMATION,
    RESULT_SCHEMA,
    SectorBridgeError,
    claim_one,
    reopen_quality_one,
    stage_one,
)

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


def _fixture(
    tmp_path: Path, code: int = 4, *, published_at: str = "2026-09-04",
) -> tuple[Path, Path, dict, Path]:
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
        "attempt_count": claimed["attempt_count"],
        "contract_hash": claimed["contract_hash"],
        "report": {
            "importance": "A", "direction": "mixed",
            "summary_bullets": ["海外需給", "国内波及", "反証条件"],
            "watchlist_companies": [], "next_week_watchpoints": ["価格"],
            "missed_candidates": ["重要変動なしも確認"],
            "full_report_md": (
                f"# 【東証33業種週次】{claimed['sector_name']}\n\n## 今週の要旨\n要旨。\n\n" +
                "\n\n\n\n".join(
                    f"### 材料{i}：需給{i}\n\n"
                    "**確認できた事実**\n\n需給変化。\n\n"
                    "**日本企業への波及**\n\n国内波及。\n\n"
                    "**利益への影響**\n\n利益感応。\n\n"
                    "**株価への織り込み**\n\n未織込み。\n\n"
                    "**反対材料・注意点**\n\n反証。" +
                    ("\n\n**試算**\n\n10〜20億円。\n\n**仮説**\n\n継続する。" if i == 1 else "")
                    for i in range(1, 4)
                )
            ),
            "sources": [{
                "title": "Primary", "url": "https://example.com/primary",
                "source_name": "Authority", "source_type": "government",
                "published_at": published_at,
            }],
        },
    }
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    stage_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT)
    inbox = work / "inbox" / f"{claimed['assignment_id']}.json"
    return db, work, claimed, inbox


def _completed_fixture(tmp_path: Path) -> tuple[Path, Path, dict, WorkerPaths, dict, str]:
    db, work, claimed, inbox = _fixture(tmp_path)
    paths = WorkerPaths.from_values(tmp_path, db, work)
    completed = process_one(paths, inbox, sync_func=lambda *_: {"canonical_sector_reports": 1})
    conn = connect_sector_db(db)
    try:
        old_report = dict(conn.execute(
            "SELECT * FROM canonical_sector_reports WHERE dedupe_key=?", (claimed["stable_key"],),
        ).fetchone())
    finally:
        conn.close()
    return db, work, claimed, paths, old_report, completed["payload_hash"]


def _bind_current_contract(envelope: dict, claimed: dict) -> dict:
    result = dict(envelope)
    for field in (
        "assignment_id", "sector_code", "sector_name", "period_start", "period_end",
        "attempt_count", "contract_hash",
    ):
        result[field] = claimed[field]
    result["claim_owner"] = OWNER
    return result


def _staged_revision_fixture(tmp_path: Path) -> tuple[Path, Path, dict, WorkerPaths, Path, dict, str, str]:
    db, work, claimed, paths, old_report, old_hash = _completed_fixture(tmp_path)
    reopen_quality_one(
        db, claimed["assignment_id"], claimed["stable_key"], old_hash, "fixture revision",
        QUALITY_REOPEN_CONFIRMATION, work_root=work, at=AT,
        supabase_reader=lambda _key: [dict(old_report)],
    )
    revised_claim = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    envelope = json.loads(
        (work / "processed" / f"{claimed['assignment_id']}.json").read_text(encoding="utf-8")
    )
    envelope = _bind_current_contract(envelope, revised_claim)
    envelope["report"]["full_report_md"] = envelope["report"]["full_report_md"].replace(
        "**確認できた事実**\n\n需給変化。", "**確認できた事実**\n\ncrash recovery後の需給変化。", 1,
    )
    draft = tmp_path / "revision.json"
    draft.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    staged = stage_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT)
    inbox = work / "inbox" / f"{claimed['assignment_id']}.json"
    return db, work, revised_claim, paths, inbox, old_report, old_hash, staged["payload_hash"]


def test_worker_happy_path_upserts_syncs_completes_and_processes(tmp_path: Path):
    db, work, claimed, inbox = _fixture(tmp_path)
    calls = []

    def sync(path: Path, dedupe_key: str, run_id: str, dry: bool):
        calls.append((path, dedupe_key, run_id, dry))
        return {"canonical_sector_reports": 1, "canonical_sector_report_runs": 1}

    result = process_one(WorkerPaths.from_values(tmp_path, db, work), inbox, sync_func=sync)
    assert result["status"] == "success" and result["attempt_count"] == 1
    assert calls == [(db, claimed["stable_key"], claimed["stable_key"], False)]
    assert not inbox.exists()
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


def test_malformed_payload_research_retry_syncs_only_current_keys(tmp_path: Path, monkeypatch):
    db, work, claimed, inbox = _fixture(tmp_path)
    paths = WorkerPaths.from_values(tmp_path, db, work)
    valid_payload = json.loads(inbox.read_text(encoding="utf-8"))
    inbox.write_text("{", encoding="utf-8")
    original_reject = inbox_worker.reject_staged_payload
    monkeypatch.setattr(
        inbox_worker, "reject_staged_payload",
        lambda conn, assignment_id, error: original_reject(conn, assignment_id, error, now=AT),
    )
    assert process_one(paths, inbox, sync_func=lambda *_: pytest.fail("transport called"))["status"] == "quarantined"

    retried = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    draft = tmp_path / "retry.json"
    draft.write_text(
        json.dumps(_bind_current_contract(valid_payload, retried), ensure_ascii=False), encoding="utf-8",
    )
    stage_one(db, retried["assignment_id"], OWNER, draft, work_root=work, at=AT)
    calls = []

    def sync(path: Path, dedupe_key: str, run_id: str, dry: bool):
        calls.append((path, dedupe_key, run_id, dry))
        return {"canonical_sector_reports": 1, "canonical_sector_report_runs": 1}

    result = process_one(paths, work / "inbox" / inbox.name, sync_func=sync)
    assert result["status"] == "success"
    assert calls == [(db, claimed["stable_key"], claimed["stable_key"], False)]


def test_quality_revision_archives_old_payload_and_preserves_canonical_until_sync_success(tmp_path: Path):
    db, work, claimed, inbox = _fixture(tmp_path)
    paths = WorkerPaths.from_values(tmp_path, db, work)
    original = process_one(paths, inbox, sync_func=lambda *_: {"canonical_sector_reports": 1})
    old_hash = original["payload_hash"]
    conn = connect_sector_db(db)
    try:
        old_report = dict(conn.execute(
            "SELECT * FROM canonical_sector_reports WHERE dedupe_key=?", (claimed["stable_key"],),
        ).fetchone())
    finally:
        conn.close()
    reopened = reopen_quality_one(
        db, claimed["assignment_id"], claimed["stable_key"], old_hash, "missing material structure",
        QUALITY_REOPEN_CONFIRMATION, work_root=work, at=AT,
        supabase_reader=lambda _key: [dict(old_report)],
    )
    archive = Path(reopened["archive_path"])
    assert {
        reopened["assignment_payload_hash"], reopened["processed_payload_hash"],
        reopened["local_canonical_payload_hash"], reopened["supabase_canonical_payload_hash"],
    } == {old_hash}
    assert archive.exists()
    archived = json.loads(archive.read_text(encoding="utf-8"))
    assert archived["old_payload_hash"] == old_hash
    assert archived["original_payload"]["assignment_id"] == claimed["assignment_id"]
    conn = connect_sector_db(db)
    try:
        row = get_assignment(conn, claimed["assignment_id"])
        assert row["status"] == "retry_pending" and row["attempt_count"] == 1
        assert row["last_error_type"] == "quality_revision"
        assert conn.execute(
            "SELECT full_report_md FROM canonical_sector_reports WHERE dedupe_key=?", (claimed["stable_key"],),
        ).fetchone()[0] == old_report["full_report_md"]
        assert conn.execute(
            "SELECT status FROM canonical_sector_report_runs WHERE dedupe_key=?", (claimed["stable_key"],),
        ).fetchone()[0] == "success"
    finally:
        conn.close()

    revised_claim = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    assert revised_claim["attempt_count"] == 2
    old_envelope = json.loads((work / "processed" / f"{claimed['assignment_id']}.json").read_text(encoding="utf-8"))
    old_envelope = _bind_current_contract(old_envelope, revised_claim)
    old_envelope["report"]["full_report_md"] = old_envelope["report"]["full_report_md"].replace(
        "**確認できた事実**\n\n需給変化。", "**確認できた事実**\n\n改訂後の需給変化。", 1,
    )
    draft = tmp_path / "revision.json"
    draft.write_text(json.dumps(old_envelope, ensure_ascii=False), encoding="utf-8")
    stage_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT)
    revision_inbox = work / "inbox" / f"{claimed['assignment_id']}.json"
    failed = process_one(paths, revision_inbox, sync_func=lambda *_: (_ for _ in ()).throw(RuntimeError("offline")))
    assert failed["status"] == "sync_error" and revision_inbox.exists()
    conn = connect_sector_db(db)
    try:
        assert conn.execute(
            "SELECT full_report_md FROM canonical_sector_reports WHERE dedupe_key=?", (claimed["stable_key"],),
        ).fetchone()[0] == old_report["full_report_md"]
        assert get_assignment(conn, claimed["assignment_id"])["last_error_type"] == "quality_revision_sync_error"
    finally:
        conn.close()
    revision_sync_calls = []

    def revision_sync(path: Path, dedupe_key: str, run_id: str, dry: bool):
        revision_sync_calls.append((path, dedupe_key, run_id, dry))
        return {"canonical_sector_reports": 1, "canonical_sector_report_runs": 1}

    completed = process_one(paths, revision_inbox, sync_func=revision_sync)
    assert completed["status"] == "success" and completed["attempt_count"] == 2
    assert len(revision_sync_calls) == 1
    assert revision_sync_calls[0][1:] == (claimed["stable_key"], claimed["stable_key"], False)
    conn = connect_sector_db(db)
    try:
        revised = conn.execute(
            "SELECT full_report_md FROM canonical_sector_reports WHERE dedupe_key=?", (claimed["stable_key"],),
        ).fetchone()[0]
        assert "改訂後の需給変化" in revised
        assert conn.execute("SELECT count(*) FROM canonical_sector_reports WHERE dedupe_key=?", (claimed["stable_key"],)).fetchone()[0] == 1
    finally:
        conn.close()


def test_quality_revision_rejects_wrong_hash_and_double_reopen(tmp_path: Path):
    db, work, claimed, inbox = _fixture(tmp_path)
    paths = WorkerPaths.from_values(tmp_path, db, work)
    original = process_one(paths, inbox, sync_func=lambda *_: {})
    conn = connect_sector_db(db)
    try:
        report = dict(conn.execute("SELECT * FROM canonical_sector_reports").fetchone())
    finally:
        conn.close()
    with pytest.raises((SectorBridgeError, RuntimeError), match="hash"):
        reopen_quality_one(
            db, claimed["assignment_id"], claimed["stable_key"], "0" * 64, "reason",
            QUALITY_REOPEN_CONFIRMATION, work_root=work, supabase_reader=lambda _key: [report],
        )
    reopen_quality_one(
        db, claimed["assignment_id"], claimed["stable_key"], original["payload_hash"], "reason",
        QUALITY_REOPEN_CONFIRMATION, work_root=work, at=AT, supabase_reader=lambda _key: [report],
    )
    with pytest.raises((SectorBridgeError, RuntimeError), match="successful assignment"):
        reopen_quality_one(
            db, claimed["assignment_id"], claimed["stable_key"], original["payload_hash"], "reason",
            QUALITY_REOPEN_CONFIRMATION, work_root=work, at=AT, supabase_reader=lambda _key: [report],
        )


def test_quality_revision_preflight_accepts_equivalent_source_timestamp_spelling(tmp_path: Path):
    db, work, claimed, inbox = _fixture(
        tmp_path, published_at="2026-09-04T00:00:00Z",
    )
    paths = WorkerPaths.from_values(tmp_path, db, work)
    original = process_one(paths, inbox, sync_func=lambda *_: {})
    conn = connect_sector_db(db)
    try:
        report = dict(conn.execute("SELECT * FROM canonical_sector_reports").fetchone())
    finally:
        conn.close()
    remote = dict(report)
    remote_sources = json.loads(remote["sources"])
    remote_sources[0]["published_at"] = "2026-09-04T09:00:00+09:00"
    remote["sources"] = remote_sources
    reopened = reopen_quality_one(
        db, claimed["assignment_id"], claimed["stable_key"], original["payload_hash"], "reason",
        QUALITY_REOPEN_CONFIRMATION, work_root=work, at=AT,
        supabase_reader=lambda _key: [remote],
    )
    assert reopened["processed_payload_hash"] == original["payload_hash"]


def test_quality_revision_preflight_rejects_actual_source_timestamp_difference(tmp_path: Path):
    db, work, claimed, inbox = _fixture(
        tmp_path, published_at="2026-09-04T00:00:00Z",
    )
    paths = WorkerPaths.from_values(tmp_path, db, work)
    original = process_one(paths, inbox, sync_func=lambda *_: {})
    conn = connect_sector_db(db)
    try:
        report = dict(conn.execute("SELECT * FROM canonical_sector_reports").fetchone())
    finally:
        conn.close()
    remote = dict(report)
    remote_sources = json.loads(remote["sources"])
    remote_sources[0]["published_at"] = "2026-09-04T00:00:01Z"
    remote["sources"] = remote_sources
    with pytest.raises(SectorBridgeError, match="Supabase canonical report"):
        reopen_quality_one(
            db, claimed["assignment_id"], claimed["stable_key"], original["payload_hash"], "reason",
            QUALITY_REOPEN_CONFIRMATION, work_root=work, at=AT,
            supabase_reader=lambda _key: [remote],
        )
    conn = connect_sector_db(db)
    try:
        row = get_assignment(conn, claimed["assignment_id"])
        assert row["status"] == "success" and row["attempt_count"] == 1
    finally:
        conn.close()
    assert list((work / "revisions" / claimed["assignment_id"]).glob("*.json")) == []


@pytest.mark.parametrize("unsafe_state", ["active_lease", "attempt_limit"])
def test_quality_revision_rejects_active_lease_and_attempt_limit(tmp_path: Path, unsafe_state: str):
    case = tmp_path / unsafe_state
    case.mkdir()
    db, work, claimed, inbox = _fixture(case)
    paths = WorkerPaths.from_values(case, db, work)
    original = process_one(paths, inbox, sync_func=lambda *_: {})
    conn = connect_sector_db(db)
    try:
        report = dict(conn.execute("SELECT * FROM canonical_sector_reports").fetchone())
        if unsafe_state == "active_lease":
            conn.execute(
                "UPDATE sector_weekly_work_assignments SET claim_owner='unexpected-owner',"
                "claimed_at='2026-09-04T21:05:00Z',lease_expires_at='2026-09-04T21:20:00Z' "
                "WHERE assignment_id=?", (claimed["assignment_id"],),
            )
        else:
            conn.execute(
                "UPDATE sector_weekly_work_assignments SET attempt_count=3 WHERE assignment_id=?",
                (claimed["assignment_id"],),
            )
        conn.commit()
    finally:
        conn.close()
    expected = "claim owner or lease" if unsafe_state == "active_lease" else "attempt limit"
    with pytest.raises(SectorBridgeError, match=expected):
        reopen_quality_one(
            db, claimed["assignment_id"], claimed["stable_key"], original["payload_hash"], "reason",
            QUALITY_REOPEN_CONFIRMATION, work_root=work, at=AT, supabase_reader=lambda _key: [report],
        )


def test_quality_revision_crash_before_archive_leaves_success_unchanged(tmp_path: Path, monkeypatch):
    db, work, claimed, _paths, report, old_hash = _completed_fixture(tmp_path)
    monkeypatch.setattr(work_bridge, "atomic_write_json", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError, match="crash"):
        reopen_quality_one(
            db, claimed["assignment_id"], claimed["stable_key"], old_hash, "reason",
            QUALITY_REOPEN_CONFIRMATION, work_root=work, at=AT, supabase_reader=lambda _key: [report],
        )
    conn = connect_sector_db(db)
    try:
        assert get_assignment(conn, claimed["assignment_id"])["status"] == "success"
    finally:
        conn.close()
    assert list((work / "revisions" / claimed["assignment_id"]).glob("*.json")) == []


def test_quality_revision_crash_after_archive_before_reopen_removes_orphan(tmp_path: Path, monkeypatch):
    db, work, claimed, _paths, report, old_hash = _completed_fixture(tmp_path)
    monkeypatch.setattr(
        work_bridge, "reopen_quality_revision",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash after archive")),
    )
    with pytest.raises(RuntimeError, match="after archive"):
        reopen_quality_one(
            db, claimed["assignment_id"], claimed["stable_key"], old_hash, "reason",
            QUALITY_REOPEN_CONFIRMATION, work_root=work, at=AT, supabase_reader=lambda _key: [report],
        )
    assert list((work / "revisions" / claimed["assignment_id"]).glob("*.json")) == []
    conn = connect_sector_db(db)
    try:
        assert get_assignment(conn, claimed["assignment_id"])["status"] == "success"
    finally:
        conn.close()


def test_revision_stage_state_preserves_old_canonical_and_archive(tmp_path: Path):
    db, work, claimed, _paths, inbox, old_report, old_hash, new_hash = _staged_revision_fixture(tmp_path)
    conn = connect_sector_db(db)
    try:
        row = get_assignment(conn, claimed["assignment_id"])
        assert row["status"] == "retry_pending"
        assert row["last_error_type"] == "quality_revision_sync_pending"
        assert row["attempt_count"] == 2 and row["submitted_payload_hash"] == new_hash
        assert conn.execute("SELECT full_report_md FROM canonical_sector_reports").fetchone()[0] == old_report["full_report_md"]
    finally:
        conn.close()
    assert inbox.exists()
    archive = list((work / "revisions" / claimed["assignment_id"]).glob("*.json"))
    assert len(archive) == 1 and json.loads(archive[0].read_text(encoding="utf-8"))["old_payload_hash"] == old_hash


def test_revision_crash_after_remote_sync_before_local_replace_converges_idempotently(tmp_path: Path, monkeypatch):
    db, work, claimed, paths, inbox, old_report, old_hash, new_hash = _staged_revision_fixture(tmp_path)
    original_upsert = inbox_worker.upsert_report
    calls = {"upsert": 0, "sync": 0, "remote_hash": None}

    def crash_on_real_upsert(conn, validated):
        calls["upsert"] += 1
        if calls["upsert"] == 2:
            raise RuntimeError("crash after remote sync")
        return original_upsert(conn, validated)

    def idempotent_sync(_db: Path, _dedupe_key: str, _run_id: str, _dry: bool):
        calls["sync"] += 1
        if calls["remote_hash"] is None:
            calls["remote_hash"] = new_hash
        assert calls["remote_hash"] == new_hash
        return {"canonical_sector_reports": 1}

    monkeypatch.setattr(inbox_worker, "upsert_report", crash_on_real_upsert)
    with pytest.raises(RuntimeError, match="after remote sync"):
        process_one(paths, inbox, sync_func=idempotent_sync)
    conn = connect_sector_db(db)
    try:
        assert conn.execute("SELECT full_report_md FROM canonical_sector_reports").fetchone()[0] == old_report["full_report_md"]
        row = get_assignment(conn, claimed["assignment_id"])
        assert row["attempt_count"] == 2 and row["status"] == "retry_pending"
    finally:
        conn.close()
    assert (work / "processed" / inbox.name).exists() and inbox.exists()
    monkeypatch.setattr(inbox_worker, "upsert_report", original_upsert)
    result = process_one(paths, inbox, sync_func=idempotent_sync)
    assert result["status"] == "success" and result["attempt_count"] == 2
    assert calls["sync"] == 2
    assert len(list((work / "revisions" / claimed["assignment_id"]).glob("*.json"))) == 1


def test_revision_crash_after_local_replace_before_assignment_success_recovers_sync_only(tmp_path: Path, monkeypatch):
    db, work, claimed, paths, inbox, _old_report, _old_hash, _new_hash = _staged_revision_fixture(tmp_path)
    original_complete = inbox_worker.complete_staged_assignment
    monkeypatch.setattr(
        inbox_worker, "complete_staged_assignment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash before assignment success")),
    )
    with pytest.raises(RuntimeError, match="before assignment success"):
        process_one(paths, inbox, sync_func=lambda *_: {})
    conn = connect_sector_db(db)
    try:
        row = get_assignment(conn, claimed["assignment_id"])
        assert row["status"] == "retry_pending" and row["attempt_count"] == 2
        assert "crash recovery" in conn.execute("SELECT full_report_md FROM canonical_sector_reports").fetchone()[0]
    finally:
        conn.close()
    monkeypatch.setattr(inbox_worker, "complete_staged_assignment", original_complete)
    assert process_one(paths, inbox, sync_func=lambda *_: {})["status"] == "success"


def test_revision_crash_before_processed_replace_recovers_without_research_attempt(tmp_path: Path, monkeypatch):
    db, work, claimed, paths, inbox, _old_report, _old_hash, new_hash = _staged_revision_fixture(tmp_path)
    original_replace = inbox_worker._replace_processed
    monkeypatch.setattr(
        inbox_worker, "_replace_processed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash before processed replace")),
    )
    with pytest.raises(RuntimeError, match="processed replace"):
        process_one(paths, inbox, sync_func=lambda *_: {})
    conn = connect_sector_db(db)
    try:
        row = get_assignment(conn, claimed["assignment_id"])
        assert row["status"] == "success" and row["attempt_count"] == 2
    finally:
        conn.close()
    monkeypatch.setattr(inbox_worker, "_replace_processed", original_replace)
    result = process_one(paths, inbox, sync_func=lambda *_: (_ for _ in ()).throw(AssertionError("must not resync")))
    assert result["status"] == "already_success"
    assert payload_hash(json.loads(Path(result["processed_path"]).read_text(encoding="utf-8"))) == new_hash


def test_revision_crash_after_processed_replace_before_log_leaves_no_work(tmp_path: Path, monkeypatch):
    db, work, _claimed, paths, _inbox, _old_report, _old_hash, _new_hash = _staged_revision_fixture(tmp_path)
    original_log = inbox_worker._append_log

    def crash_on_finished(worker_paths, event, **details):
        if event == "payload_finished":
            raise RuntimeError("crash before finish log")
        return original_log(worker_paths, event, **details)

    monkeypatch.setattr(inbox_worker, "_append_log", crash_on_finished)
    with pytest.raises(RuntimeError, match="finish log"):
        run_once(paths, sync_func=lambda *_: {})
    monkeypatch.setattr(inbox_worker, "_append_log", original_log)
    result = run_once(paths, sync_func=lambda *_: (_ for _ in ()).throw(AssertionError("must not sync")))
    assert result["status"] == "no_work" and result["detected"] == 0


def test_revision_permanent_validation_failure_preserves_old_canonical(tmp_path: Path):
    db, work, claimed, paths, old_report, old_hash = _completed_fixture(tmp_path)
    reopen_quality_one(
        db, claimed["assignment_id"], claimed["stable_key"], old_hash, "fixture revision",
        QUALITY_REOPEN_CONFIRMATION, work_root=work, at=AT, supabase_reader=lambda _key: [old_report],
    )
    revised = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    envelope = json.loads((work / "processed" / f"{claimed['assignment_id']}.json").read_text(encoding="utf-8"))
    envelope = _bind_current_contract(envelope, revised)
    envelope["report"]["full_report_md"] = envelope["report"]["full_report_md"].replace(
        "**確認できた事実**", "確認できた事実", 1,
    )
    draft = tmp_path / "invalid-revision.json"
    draft.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(Exception, match="確認できた事実"):
        stage_one(db, revised["assignment_id"], OWNER, draft, work_root=work, at=AT)
    conn = connect_sector_db(db)
    try:
        assert conn.execute("SELECT full_report_md FROM canonical_sector_reports").fetchone()[0] == old_report["full_report_md"]
        assert get_assignment(conn, claimed["assignment_id"])["attempt_count"] == 2
    finally:
        conn.close()


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


def test_prechange_english_label_payload_recovers_without_new_stage_validation(tmp_path: Path):
    db, work, claimed, inbox = _fixture(tmp_path, 4)
    envelope = json.loads(inbox.read_text(encoding="utf-8"))
    replacements = {
        "**確認できた事実**\n\n": "**Fact**: ",
        "**日本企業への波及**\n\n": "**Transmission**: ",
        "**利益への影響**\n\n": "**Magnitude**: ",
        "**株価への織り込み**\n\n": "**Pricing-in**: ",
        "**反対材料・注意点**\n\n": "**Counterevidence**: ",
        "**試算**\n\n": "**Estimate**: ",
        "**仮説**\n\n": "**Hypothesis**: ",
    }
    for current, legacy in replacements.items():
        envelope["report"]["full_report_md"] = envelope["report"]["full_report_md"].replace(
            current, legacy,
        )
    legacy_hash = payload_hash(envelope)
    inbox.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    conn = connect_sector_db(db)
    try:
        conn.execute(
            "UPDATE sector_weekly_work_assignments SET submitted_payload_hash=? WHERE assignment_id=?",
            (legacy_hash, claimed["assignment_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    result = process_one(
        WorkerPaths.from_values(tmp_path, db, work), inbox,
        sync_func=lambda *_: {"canonical_sector_reports": 1},
    )
    assert result["status"] == "success"
    assert result["payload_hash"] == legacy_hash
    assert result["attempt_count"] == 1


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

    def sync(path: Path, dedupe_key: str, run_id: str, dry: bool):
        calls.append((path, dedupe_key, run_id, dry))
        return {"canonical_sector_reports": 1, "canonical_sector_report_runs": 1}

    exit_code = _run_main(
        monkeypatch,
        paths,
        trigger="task_scheduler",
        sync_func=sync,
    )

    assert exit_code == 0
    assert calls == [(db, claimed["stable_key"], claimed["stable_key"], False)]
    assert not inbox.exists()
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
    payload_finished = next(item for item in records if item["event"] == "payload_finished")
    assert payload_finished["sync_result"] == {
        "canonical_sector_reports": 1, "canonical_sector_report_runs": 1,
    }
    assert payload_finished["sector_name"] == claimed["sector_name"]
    assert payload_finished["dedupe_key"] == claimed["stable_key"]
    assert payload_finished["run_id"] == claimed["stable_key"]
    assert payload_finished["period_start"] and payload_finished["period_end"]


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
