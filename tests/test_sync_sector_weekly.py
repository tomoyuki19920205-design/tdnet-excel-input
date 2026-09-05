import json
import sqlite3
from pathlib import Path

import pytest

import tools.sync_sector_weekly as sync_module
from lib.sector_weekly import CANONICAL_SQLITE_SCHEMA
from tools.apply_sector_weekly_work_sqlite_migration import apply_sqlite_migration


CURRENT_KEY = "sector_weekly:2026-09-05:01"


def _report(key: str, code: int, start: str, end: str) -> dict:
    return {
        "id": f"report-{key}", "schema_version": "sector_weekly_v1",
        "report_type": "sector_weekly", "sector_code": code,
        "sector_name": f"sector-{code}", "period_start": start, "period_end": end,
        "generated_at": end, "importance": "B", "direction": "mixed",
        "summary_bullets": json.dumps(["a", "b", "c"]), "full_report_md": "body",
        "watchlist_companies": "[]", "next_week_watchpoints": "[]",
        "missed_candidates": "[]", "sources": "[]", "run_id": key,
        "dedupe_key": key, "created_at": end, "updated_at": end,
    }


def _run(key: str, code: int, start: str, end: str) -> dict:
    return {
        "run_id": key, "report_type": "sector_weekly", "sector_code": code,
        "sector_name": f"sector-{code}", "period_start": start, "period_end": end,
        "dedupe_key": key, "status": "success", "attempt_count": 1,
        "last_error_type": None, "last_error_message": None, "started_at": start,
        "completed_at": end, "created_at": start, "updated_at": end,
    }


def _insert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    columns = list(row)
    conn.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        [row[column] for column in columns],
    )


def _database(tmp_path: Path) -> Path:
    db = tmp_path / "sector.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(CANONICAL_SQLITE_SCHEMA)
    previous_start, previous_end = "2026-08-22T06:00:00+09:00", "2026-08-29T05:59:59+09:00"
    current_start, current_end = "2026-08-29T06:00:00+09:00", "2026-09-05T05:59:59+09:00"
    for code in range(1, 34):
        key = f"sector_weekly:2026-08-29:{code:02d}"
        _insert(conn, "canonical_sector_reports", _report(key, code, previous_start, previous_end))
        _insert(conn, "canonical_sector_report_runs", _run(key, code, previous_start, previous_end))
    for code in range(1, 34):
        key = f"sector_weekly:2026-09-05:{code:02d}"
        _insert(conn, "canonical_sector_report_runs", _run(key, code, current_start, current_end))
    _insert(conn, "canonical_sector_reports", _report(CURRENT_KEY, 1, current_start, current_end))
    conn.commit()
    conn.close()
    apply_sqlite_migration(db, expected_db_path=db, backup_dir=tmp_path / "backups")
    return db


def _capture_transport(monkeypatch):
    calls = []
    monkeypatch.setattr(sync_module, "load_env", lambda: None)
    monkeypatch.setattr(sync_module, "get_supabase_write_config", lambda: {"test": "only"})

    def upsert(table, rows, *, on_conflict, config):
        calls.append((table, rows, on_conflict))
        return {"ok": True, "count": len(rows)}

    monkeypatch.setattr(sync_module, "supabase_upsert", upsert)
    return calls


def test_sync_one_sends_only_current_report_and_run(tmp_path: Path, monkeypatch):
    db = _database(tmp_path)
    calls = _capture_transport(monkeypatch)

    result = sync_module.sync_one(db, CURRENT_KEY, CURRENT_KEY)

    assert result == {"canonical_sector_reports": 1, "canonical_sector_report_runs": 1}
    assert [table for table, _rows, _conflict in calls] == [
        "canonical_sector_reports", "canonical_sector_report_runs",
    ]
    report_rows, run_rows = calls[0][1], calls[1][1]
    assert len(report_rows) == len(run_rows) == 1
    assert report_rows[0]["dedupe_key"] == CURRENT_KEY
    assert run_rows[0]["run_id"] == CURRENT_KEY
    assert all("2026-08-29:" not in row.get("dedupe_key", "") for _table, rows, _key in calls for row in rows)


@pytest.mark.parametrize("dedupe_key,run_id", [("", CURRENT_KEY), (CURRENT_KEY, "")])
def test_sync_one_rejects_missing_keys_before_transport(tmp_path: Path, monkeypatch, dedupe_key, run_id):
    db = _database(tmp_path)
    monkeypatch.setattr(sync_module, "supabase_upsert", lambda *_a, **_kw: pytest.fail("transport called"))
    with pytest.raises(ValueError, match="required"):
        sync_module.sync_one(db, dedupe_key, run_id)


@pytest.mark.parametrize(
    "dedupe_key,run_id",
    [("sector_weekly:missing", CURRENT_KEY), (CURRENT_KEY, "sector_weekly:missing")],
)
def test_sync_one_rejects_missing_rows_before_transport(
    tmp_path: Path, monkeypatch, dedupe_key, run_id,
):
    db = _database(tmp_path)
    monkeypatch.setattr(sync_module, "supabase_upsert", lambda *_a, **_kw: pytest.fail("transport called"))
    with pytest.raises(RuntimeError, match="found 0"):
        sync_module.sync_one(db, dedupe_key, run_id)


@pytest.mark.parametrize("multiple_table", ["canonical_sector_reports", "canonical_sector_report_runs"])
def test_sync_one_rejects_multiple_rows_before_transport(tmp_path: Path, monkeypatch, multiple_table):
    db = _database(tmp_path)
    report = _report(CURRENT_KEY, 1, "start", "end")
    run = _run(CURRENT_KEY, 1, "start", "end")
    monkeypatch.setattr(
        sync_module, "rows_for_sync",
        lambda _conn, table, *, key: (
            [report, dict(report)] if table == multiple_table == "canonical_sector_reports"
            else [run, dict(run)] if table == multiple_table == "canonical_sector_report_runs"
            else [report] if table == "canonical_sector_reports"
            else [run]
        ),
    )
    monkeypatch.setattr(sync_module, "supabase_upsert", lambda *_a, **_kw: pytest.fail("transport called"))
    with pytest.raises(RuntimeError, match="found 2"):
        sync_module.sync_one(db, CURRENT_KEY, CURRENT_KEY)


@pytest.mark.parametrize("field,value", [("sector_code", 2), ("period_end", "different")])
def test_sync_one_rejects_report_run_identity_mismatch(tmp_path: Path, monkeypatch, field, value):
    db = _database(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(f"UPDATE canonical_sector_report_runs SET {field}=? WHERE run_id=?", (value, CURRENT_KEY))
    conn.commit()
    conn.close()
    monkeypatch.setattr(sync_module, "supabase_upsert", lambda *_a, **_kw: pytest.fail("transport called"))
    with pytest.raises(RuntimeError, match="identity|period"):
        sync_module.sync_one(db, CURRENT_KEY, CURRENT_KEY)


def test_sync_one_rejects_rows_that_do_not_match_request(tmp_path: Path, monkeypatch):
    db = _database(tmp_path)
    wrong = "sector_weekly:2026-09-05:02"
    report = _report(wrong, 2, "start", "end")
    run = _run(wrong, 2, "start", "end")
    monkeypatch.setattr(
        sync_module, "rows_for_sync",
        lambda _conn, table, *, key: [report] if table == "canonical_sector_reports" else [run],
    )
    monkeypatch.setattr(sync_module, "supabase_upsert", lambda *_a, **_kw: pytest.fail("transport called"))
    with pytest.raises(RuntimeError, match="requested sync keys"):
        sync_module.sync_one(db, CURRENT_KEY, CURRENT_KEY)


def test_sync_one_is_idempotent_by_conflict_key(tmp_path: Path, monkeypatch):
    db = _database(tmp_path)
    remote = {"canonical_sector_reports": {}, "canonical_sector_report_runs": {}}
    monkeypatch.setattr(sync_module, "load_env", lambda: None)
    monkeypatch.setattr(sync_module, "get_supabase_write_config", lambda: {"test": "only"})

    def upsert(table, rows, *, on_conflict, config):
        for row in rows:
            remote[table][row[on_conflict]] = row
        return {"ok": True, "count": len(rows)}

    monkeypatch.setattr(sync_module, "supabase_upsert", upsert)
    sync_module.sync_one(db, CURRENT_KEY, CURRENT_KEY)
    sync_module.sync_one(db, CURRENT_KEY, CURRENT_KEY)
    assert len(remote["canonical_sector_reports"]) == 1
    assert len(remote["canonical_sector_report_runs"]) == 1


def test_sync_one_stops_after_supabase_failure(tmp_path: Path, monkeypatch):
    db = _database(tmp_path)
    monkeypatch.setattr(sync_module, "load_env", lambda: None)
    monkeypatch.setattr(sync_module, "get_supabase_write_config", lambda: {"test": "only"})
    monkeypatch.setattr(
        sync_module, "supabase_upsert",
        lambda *_a, **_kw: {"ok": False, "count": 0, "error": "offline"},
    )
    with pytest.raises(RuntimeError, match="sync failed"):
        sync_module.sync_one(db, CURRENT_KEY, CURRENT_KEY)


def test_sync_one_rejects_transport_count_mismatch(tmp_path: Path, monkeypatch):
    db = _database(tmp_path)
    monkeypatch.setattr(sync_module, "load_env", lambda: None)
    monkeypatch.setattr(sync_module, "get_supabase_write_config", lambda: {"test": "only"})
    monkeypatch.setattr(
        sync_module, "supabase_upsert",
        lambda *_a, **_kw: {"ok": True, "count": 0, "error": None},
    )
    with pytest.raises(RuntimeError, match="count mismatch"):
        sync_module.sync_one(db, CURRENT_KEY, CURRENT_KEY)
