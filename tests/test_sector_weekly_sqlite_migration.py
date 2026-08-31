import hashlib
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from lib.sector_weekly import CANONICAL_SQLITE_SCHEMA, connect_sector_db, weekly_window
from lib.sector_weekly_sqlite import (
    EXPECTED_COLUMNS,
    EXPECTED_STATUS_VALUES,
    MAX_ATTEMPTS,
    MIGRATION_ID,
    MigrationRequiredError,
    SchemaMismatchError,
    inspect_sector_schema,
    validate_work_schema,
)
from lib.sector_weekly_work import claim_next, enqueue_assignment, recover_expired_leases
from tools.apply_sector_weekly_work_sqlite_migration import (
    SectorSQLiteMigrationError,
    apply_sqlite_migration,
    dry_run,
)
from tools.sector_weekly_work_bridge import claim_one


ROOT = Path(__file__).parents[1]
SQLITE_MIGRATION = ROOT / "migrations" / "sqlite" / "018_sector_weekly_work_assignments.sql"
POSTGRES_MIGRATION = ROOT / "migrations" / "018_sector_weekly_work_assignments.sql"
AT = datetime.fromisoformat("2026-09-05T06:05:00+09:00")


def _apply(db: Path, *, canonical: bool = False) -> dict:
    if canonical:
        conn = sqlite3.connect(db)
        conn.executescript(CANONICAL_SQLITE_SCHEMA)
        conn.close()
    return apply_sqlite_migration(
        db, expected_db_path=db, backup_dir=db.parent / "backups", allow_create_empty=not db.exists(),
    )


def _base_assignment(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "assignment_id": "a-1",
        "schema_version": "sector_weekly_assignment_v1",
        "stable_key": "sector_weekly:2026-09-05:1",
        "sector_code": 1,
        "sector_name": "水産・農林業",
        "period_start": "2026-08-28T21:00:00Z",
        "period_end": "2026-09-04T20:59:59Z",
        "status": "pending",
        "attempt_count": 0,
        "available_at": "2026-09-04T21:05:00Z",
        "created_at": "2026-09-04T21:05:00Z",
        "updated_at": "2026-09-04T21:05:00Z",
    }
    row.update(overrides)
    return row


def _insert(conn: sqlite3.Connection, values: dict[str, object]) -> None:
    columns = tuple(values)
    conn.execute(
        f"INSERT INTO sector_weekly_work_assignments ({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})",
        tuple(values[name] for name in columns),
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_empty_db_first_migration_creates_expected_schema_and_checks(tmp_path: Path):
    db = tmp_path / "empty.db"
    result = _apply(db)
    assert result["status"] == "applied"
    assert result["assignment_count"] == 0
    assert result["integrity_check"] == "ok"
    assert result["foreign_key_check"] == []
    conn = sqlite3.connect(db)
    validate_work_schema(conn)
    columns = tuple(row[1] for row in conn.execute("PRAGMA table_info(sector_weekly_work_assignments)"))
    indexes = {row[1]: row for row in conn.execute("PRAGMA index_list(sector_weekly_work_assignments)")}
    assert columns == EXPECTED_COLUMNS
    assert "ix_sector_weekly_work_ready" in indexes
    assert indexes["ix_sector_weekly_work_lease"][4] == 1
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_migration_reapply_is_noop_and_history_is_unique(tmp_path: Path):
    db = tmp_path / "repeat.db"
    first = _apply(db)
    second = _apply(db)
    assert first["status"] == "applied"
    assert second["status"] == "already_applied"
    assert second["backup"] is None
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT count(*) FROM sector_weekly_sqlite_migrations WHERE migration_id=?", (MIGRATION_ID,),
    ).fetchone()[0] == 1
    conn.close()


def test_existing_mismatched_queue_fails_closed(tmp_path: Path):
    db = tmp_path / "mismatch.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sector_weekly_work_assignments (assignment_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaMismatchError, match="without a matching migration history"):
        _apply(db)
    assert not (tmp_path / "backups").exists()


def test_failure_mid_migration_rolls_back_every_ddl_statement(tmp_path: Path):
    db = tmp_path / "rollback.db"

    def fail_after_first(number: int, _conn: sqlite3.Connection) -> None:
        if number == 1:
            raise RuntimeError("injected migration failure")

    with pytest.raises(RuntimeError, match="injected migration failure"):
        apply_sqlite_migration(
            db, expected_db_path=db, backup_dir=tmp_path / "backups",
            allow_create_empty=True, after_statement=fail_after_first,
        )
    conn = sqlite3.connect(db)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "sector_weekly_sqlite_migrations" not in names
    assert "sector_weekly_work_assignments" not in names


def test_backup_is_consistent_and_restorable(tmp_path: Path):
    db = tmp_path / "backup.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    conn.execute("INSERT INTO marker VALUES ('before')")
    conn.commit()
    conn.close()
    result = _apply(db)
    backup = Path(result["backup"]["path"])
    assert backup.is_file() and backup.stat().st_size == result["backup"]["size"]
    restored = sqlite3.connect(backup)
    assert restored.execute("SELECT value FROM marker").fetchone()[0] == "before"
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert restored.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='sector_weekly_work_assignments'"
    ).fetchone()[0] == 0
    restored.close()


def test_apply_path_guard_rejects_unexpected_database(tmp_path: Path):
    db = tmp_path / "actual.db"
    sqlite3.connect(db).close()
    with pytest.raises(SectorSQLiteMigrationError, match="unsafe DB path"):
        apply_sqlite_migration(
            db, expected_db_path=tmp_path / "expected.db", backup_dir=tmp_path / "backups",
        )


def test_cli_apply_requires_explicit_confirmation_and_does_not_write(tmp_path: Path):
    db = tmp_path / "cli.db"
    sqlite3.connect(db).close()
    before = _sha256(db)
    result = subprocess.run(
        [
            sys.executable, str(ROOT / "tools" / "apply_sector_weekly_work_sqlite_migration.py"),
            "--db", str(db), "--apply",
        ],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert f"--confirm {MIGRATION_ID} is required" in result.stderr
    assert _sha256(db) == before


def test_normal_connect_validates_without_mutating_database(tmp_path: Path):
    db = tmp_path / "connect.db"
    _apply(db)
    before = _sha256(db)
    conn = connect_sector_db(db)
    assert conn.execute("SELECT count(*) FROM sector_weekly_work_assignments").fetchone()[0] == 0
    conn.close()
    assert _sha256(db) == before


def test_missing_schema_connect_fails_without_creating_or_repairing(tmp_path: Path):
    missing = tmp_path / "missing.db"
    with pytest.raises(MigrationRequiredError):
        connect_sector_db(missing)
    assert not missing.exists()
    empty = tmp_path / "empty-existing.db"
    sqlite3.connect(empty).close()
    before = _sha256(empty)
    with pytest.raises(MigrationRequiredError):
        connect_sector_db(empty)
    assert _sha256(empty) == before


def test_bridge_no_work_is_read_only_and_creates_no_assignment(tmp_path: Path):
    db = tmp_path / "no-work.db"
    _apply(db)
    before = _sha256(db)
    work = tmp_path / "work"
    result = claim_one(db, "sector-weekly-worker", work_root=work, at=AT)
    assert result == {"status": "no_work", "claim_owner": "sector-weekly-worker"}
    assert _sha256(db) == before
    assert not work.exists()
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM sector_weekly_work_assignments").fetchone()[0] == 0
    conn.close()


def test_assignment_timestamps_are_fixed_utc_and_claim_is_atomic(tmp_path: Path):
    db = tmp_path / "utc.db"
    _apply(db, canonical=True)
    conn = connect_sector_db(db)
    window = weekly_window(AT)
    first, _ = enqueue_assignment(conn, 1, window, now=AT)
    enqueue_assignment(conn, 2, window, now=AT)
    for field in ("period_start", "period_end", "available_at", "created_at", "updated_at"):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", first[field])
    claimed = claim_next(conn, "worker-one", now=AT, lease_seconds=60)
    assert claimed and claimed["sector_code"] == 1
    assert claim_next(conn, "worker-two", now=AT, lease_seconds=60) is None
    assert recover_expired_leases(conn, now=datetime.fromisoformat("2026-09-05T06:06:01+09:00")) == 1
    conn.close()


@pytest.mark.parametrize("overrides", [
    {"sector_code": 0},
    {"sector_code": 34},
    {"sector_name": "  "},
    {"period_end": "2026-08-28T21:00:00Z"},
    {"status": "unknown"},
    {"attempt_count": -1},
    {"attempt_count": MAX_ATTEMPTS + 1},
    {"status": "claimed"},
    {"submitted_payload_hash": "A" * 64},
    {"submitted_payload_hash": "a" * 63},
])
def test_queue_constraints_reject_invalid_rows(tmp_path: Path, overrides: dict[str, object]):
    db = tmp_path / "constraints.db"
    _apply(db)
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, _base_assignment(**overrides))
    conn.close()
    assert MAX_ATTEMPTS >= 1


def test_claimed_row_with_owner_and_lease_is_accepted(tmp_path: Path):
    db = tmp_path / "claimed.db"
    _apply(db)
    conn = sqlite3.connect(db)
    _insert(conn, _base_assignment(
        status="claimed", claim_owner="worker", claimed_at="2026-09-04T21:05:00Z",
        lease_expires_at="2026-09-04T22:00:00Z",
    ))
    conn.commit()
    assert conn.execute("SELECT count(*) FROM sector_weekly_work_assignments").fetchone()[0] == 1
    conn.close()


def test_protected_canonical_and_company_news_tables_are_unchanged(tmp_path: Path):
    db = tmp_path / "protected.db"
    conn = sqlite3.connect(db)
    for table in (
        "canonical_sector_reports", "canonical_sector_report_runs",
        "canonical_news_events", "canonical_news_scan_runs",
    ):
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute(f"INSERT INTO {table}(value) VALUES('unchanged')")
    before = {
        table: (conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
                conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0])
        for table in (
            "canonical_sector_reports", "canonical_sector_report_runs",
            "canonical_news_events", "canonical_news_scan_runs",
        )
    }
    conn.commit()
    conn.close()
    _apply(db)
    conn = sqlite3.connect(db)
    after = {
        table: (conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
                conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0])
        for table in before
    }
    conn.close()
    assert after == before


def test_dry_run_uses_copy_and_leaves_source_unchanged(tmp_path: Path):
    db = tmp_path / "dry.db"
    sqlite3.connect(db).close()
    before = _sha256(db)
    result = dry_run(db)
    assert result["status"] == "dry_run_ok"
    assert _sha256(db) == before
    assert inspect_sector_schema(db)["status"] == "migration_required"


def _column_names(sql: str) -> tuple[str, ...]:
    body = sql.split("CREATE TABLE IF NOT EXISTS sector_weekly_work_assignments", 1)[1]
    body = body.split("CREATE INDEX", 1)[0]
    names = []
    for line in body.splitlines():
        match = re.match(r"\s{4}([a-z_]+)\s+(?:UUID|TEXT|INTEGER|TIMESTAMPTZ)\b", line)
        if match:
            names.append(match.group(1))
    return tuple(names)


def _required_columns(sql: str) -> set[str]:
    body = sql.split("CREATE TABLE IF NOT EXISTS sector_weekly_work_assignments", 1)[1]
    body = body.split("CREATE INDEX", 1)[0]
    required = set()
    for line in body.splitlines():
        match = re.match(r"\s{4}([a-z_]+)\s+(?:UUID|TEXT|INTEGER|TIMESTAMPTZ)\b", line)
        if match and ("NOT NULL" in line or "PRIMARY KEY" in line):
            required.add(match.group(1))
    return required


def test_postgres_and_sqlite_migrations_have_logical_schema_parity():
    postgres = POSTGRES_MIGRATION.read_text(encoding="utf-8")
    sqlite = SQLITE_MIGRATION.read_text(encoding="utf-8")
    assert _column_names(postgres) == EXPECTED_COLUMNS
    assert _column_names(sqlite) == EXPECTED_COLUMNS
    assert _required_columns(postgres) == _required_columns(sqlite)
    for status in EXPECTED_STATUS_VALUES:
        assert f"'{status}'" in postgres and f"'{status}'" in sqlite
    for term in (
        "stable_key TEXT NOT NULL UNIQUE", "sector_code", "period_end > period_start",
        "attempt_count", "submitted_payload_hash", "claim_owner IS NOT NULL",
        "ix_sector_weekly_work_ready", "ix_sector_weekly_work_lease",
    ):
        assert term in postgres and term in sqlite
    assert "WHERE status IN ('claimed', 'running')" in postgres
    assert "WHERE status IN ('claimed', 'running')" in sqlite
    sqlite_statements = "\n".join(
        line for line in sqlite.splitlines() if not line.lstrip().startswith("--")
    )
    for postgres_only in ("UUID", "TIMESTAMPTZ", "NOW()", "ENABLE ROW LEVEL SECURITY"):
        assert postgres_only not in sqlite_statements


def test_migration_runtime_has_no_supabase_or_openai_dependency():
    sources = "\n".join(path.read_text(encoding="utf-8") for path in (
        ROOT / "lib" / "sector_weekly_sqlite.py",
        ROOT / "tools" / "apply_sector_weekly_work_sqlite_migration.py",
        ROOT / "tools" / "sector_weekly_scheduler.py",
        ROOT / "tools" / "sector_weekly_work_bridge.py",
        ROOT / "lib" / "sector_weekly_work.py",
    ))
    for forbidden in (
        "OPENAI_API_KEY", "OpenAI(", "responses.create", "client.responses",
        "SUPABASE_POSTGRES_URL", "psycopg2", "supabase_upsert(",
    ):
        assert forbidden not in sources
