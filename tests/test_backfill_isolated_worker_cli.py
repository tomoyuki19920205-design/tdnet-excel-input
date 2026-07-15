from types import SimpleNamespace
from pathlib import Path
import json
import sqlite3
import stat
import subprocess
import sys
import tempfile

import pytest

import tools.backfill_segments_tdnet as cli
from lib.backfill.state_store import BackfillStateStore


class _Metrics:
    def __init__(self):
        self.stats = None
        self.upserted_count = 0

    def record_upsert(self, stats):
        self.stats = stats

    def summary_dict(self):
        return {"upserted": self.upserted_count, "upsert_failed_batches": 0}


class _Store:
    def __init__(self):
        self.upserted = []
        self.failed = []

    def mark_upserted(self, filing_id):
        self.upserted.append(filing_id)

    def mark_failed(self, filing_id, *, error="", stage="unknown"):
        self.failed.append((filing_id, error, stage))


class _RunLogger:
    def __init__(self):
        self.events = []

    def log_upsert(self, filing_id, detail):
        self.events.append((filing_id, detail))


def _upsert_stats(**overrides):
    values = {
        "inserted": 0,
        "updated": 0,
        "no_change": 0,
        "rejected_lower_priority": 0,
        "rejected_filing_conflict": 0,
        "rejected_filing_identity_unresolved": 0,
        "failed_batches": 0,
        "canonical_sync_ids": [],
        "validation_rejected_record_count": 0,
        "validation_rejected_filing_count": 0,
        "validation_rejected_filing_ids": [],
        "validation_reasons_by_filing": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _mock_flush_batch(monkeypatch, stats_or_callable):
    class DB:
        def __init__(self, path):
            self.path = path

        def close(self):
            pass

    monkeypatch.setattr("src.migration.migration_db.MigrationDB", DB)
    if callable(stats_or_callable):
        batch = stats_or_callable
    else:
        batch = lambda records, db, batch_size: stats_or_callable
    monkeypatch.setattr(cli, "batch_upsert_segments", batch)


def _isolated_paths(tmp_path):
    root = tmp_path / "run"
    for directory in ("input", "state", "logs"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    filing_list = root / "input" / "filings.json"
    filing_list.write_text("[]", encoding="utf-8")
    return {
        "isolated_run_root": str(root),
        "decision_db_path": str(root / "state" / "decision.db"),
        "state_db_path": str(root / "state" / "state.db"),
        "log_jsonl_path": str(root / "logs" / "run.jsonl"),
        "filing_list_path": str(filing_list),
    }


def _invoke(monkeypatch, args, run_backfill=None):
    monkeypatch.setattr("sys.argv", ["backfill_segments_tdnet.py", *args])
    if run_backfill is not None:
        monkeypatch.setattr(cli, "run_backfill", run_backfill)
    cli.main()


def _insert_state(
    store,
    filing_id,
    *,
    ticker="1000",
    status="queued",
    stage="listing",
    attempt_count=0,
    error=None,
):
    store.conn.execute(
        "INSERT INTO filing_state "
        "(filing_id,ticker,disclosure_date,status,stage,attempt_count,last_error,last_error_stage,"
        "review_hint,started_at,finished_at,duration_ms,last_attempt_at,via,segment_count,result_fingerprint) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            filing_id, ticker, "2026-07-13", status, stage, attempt_count, error,
            stage if error else None, error, "2026-07-13T00:00:00+09:00",
            "2026-07-13T00:01:00+09:00", 60, "2026-07-13T00:00:00+09:00",
            "xbrl", 8, "fingerprint",
        ),
    )


def _manifest_record(index, *, requested_id=None, filing_id=None, ticker=None):
    return {
        "filing_id": filing_id or f"manifest-{index:03d}",
        "requested_disclosure_no": requested_id or f"20260713{index:06d}",
        "expected_period": "2026-05-31",
        "expected_quarter": "FY",
        "ticker": ticker or f"{1000 + index:04d}",
        "title": "決算短信",
        "disclosure_date": "2026-07-13",
        "doc_url": f"https://example.invalid/{index}.pdf",
        "xbrl_url": f"https://example.invalid/{index}.zip",
        "doc_type": "financial_statement",
        "listing_source": "manifest",
        "has_xbrl": True,
    }


def _write_manifest(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records), encoding="utf-8")


def _seed_cli_args(run_root, filing_list, *extra):
    return [
        "--isolated-worker-dry-run",
        "--run-root", str(run_root),
        "--filing-list", str(filing_list),
        "--workers", "1",
        *extra,
    ]


def _create_seed_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE seed_rows (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO seed_rows(value) VALUES ('source')")


def _create_seed_cache(root, requested_id, *, zip_file=True, sidecar=True):
    filing_root = root / requested_id
    filing_root.mkdir(parents=True, exist_ok=True)
    if zip_file:
        (filing_root / "xbrl.zip").write_bytes(b"verified-xbrl")
    if sidecar:
        (filing_root / "xbrl.zip.provenance.json").write_text(
            '{"verified": true}', encoding="utf-8"
        )
    return filing_root


@pytest.mark.parametrize("isolated,apply", [(False, False), (True, True)])
def test_isolated_seed_rejects_invalid_modes(
    monkeypatch, tmp_path, capsys, isolated, apply
):
    source_db = tmp_path / "source.db"
    _create_seed_db(source_db)
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [_manifest_record(1)])
    args = ["--isolated-seed-decision-db", str(source_db)]
    if isolated:
        args = _seed_cli_args(run_root, filing_list, *args)
    if apply:
        args.append("--apply")

    with pytest.raises(SystemExit):
        _invoke(monkeypatch, args)

    assert "STOP_BACKFILL_ISOLATED_SEED_INVALID_MODE" in capsys.readouterr().err


def test_isolated_seed_db_uses_readonly_backup_and_preserves_source(
    monkeypatch, tmp_path
):
    source_db = tmp_path / "source.db"
    _create_seed_db(source_db)
    source_before = cli._file_snapshot(source_db)
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [_manifest_record(1)])
    captured = {}
    connect_calls = []
    real_connect = cli.sqlite3.connect

    def connect(database, *args, **kwargs):
        connect_calls.append((str(database), kwargs.get("uri", False)))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(cli.sqlite3, "connect", connect)

    def run_backfill(**kwargs):
        captured.update(kwargs)
        return {"summary": {}}

    _invoke(
        monkeypatch,
        _seed_cli_args(
            run_root, filing_list, "--isolated-seed-decision-db", str(source_db)
        ),
        run_backfill,
    )

    destination = run_root / "state" / "decision.db"
    assert destination.is_file()
    assert source_before == cli._file_snapshot(source_db)
    assert any("?mode=ro" in database and uri for database, uri in connect_calls)
    summary = captured["isolated_seed_summary"]
    assert summary["isolated_seed_decision_db_used"] is True
    assert summary["isolated_seed_decision_db_source_sha256"] == source_before[2]
    assert summary["isolated_seed_decision_db_destination_sha256"] == cli._sha256_file(destination)
    assert summary["isolated_seed_copy_verified"] is True
    assert str(source_db) not in json.dumps(summary)

    with sqlite3.connect(destination) as conn:
        conn.execute("UPDATE seed_rows SET value='destination'")
    with sqlite3.connect(source_db) as conn:
        assert conn.execute("SELECT value FROM seed_rows").fetchone() == ("source",)


@pytest.mark.parametrize(
    "source_factory,expected_code",
    [
        (lambda path: None, "STOP_BACKFILL_ISOLATED_SEED_DB_INVALID"),
        (
            lambda path: path.write_text("not sqlite", encoding="utf-8"),
            "STOP_BACKFILL_ISOLATED_SEED_DB_INVALID",
        ),
    ],
)
def test_isolated_seed_db_rejects_missing_or_invalid_source(
    monkeypatch, tmp_path, source_factory, expected_code
):
    source_db = tmp_path / "source.db"
    source_factory(source_db)
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [_manifest_record(1)])
    called = []

    with pytest.raises(SystemExit) as exc_info:
        _invoke(
            monkeypatch,
            _seed_cli_args(
                run_root, filing_list,
                "--isolated-seed-decision-db", str(source_db),
            ),
            lambda **kwargs: called.append(kwargs),
        )

    assert exc_info.value.code != 0
    assert called == []


def test_isolated_seed_rejects_relative_and_reparse_sources(monkeypatch, tmp_path):
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [_manifest_record(1)])
    with pytest.raises(SystemExit):
        _invoke(
            monkeypatch,
            _seed_cli_args(
                run_root, filing_list,
                "--isolated-seed-decision-db", "relative.db",
            ),
            lambda **kwargs: pytest.fail("worker must not start"),
        )

    real_db = tmp_path / "real.db"
    link_db = tmp_path / "link.db"
    _create_seed_db(real_db)
    try:
        link_db.symlink_to(real_db)
    except OSError:
        link_db = real_db
        original_reparse_check = cli._path_has_reparse_component
        monkeypatch.setattr(
            cli,
            "_path_has_reparse_component",
            lambda path: Path(path).resolve() == real_db.resolve()
            or original_reparse_check(path),
        )
    fresh_root = tmp_path / "run-link"
    fresh_manifest = fresh_root / "input" / "filings.json"
    _write_manifest(fresh_manifest, [_manifest_record(1)])
    with pytest.raises(SystemExit):
        _invoke(
            monkeypatch,
            _seed_cli_args(
                fresh_root, fresh_manifest,
                "--isolated-seed-decision-db", str(link_db),
            ),
            lambda **kwargs: pytest.fail("worker must not start"),
        )


def test_isolated_seed_cache_copies_manifest_whitelist_only(monkeypatch, tmp_path):
    requested_ids = ["20260713000001", "20260713000002"]
    seed_cache = tmp_path / "seed-cache"
    source_files = []
    for requested_id in requested_ids:
        source_root = _create_seed_cache(seed_cache, requested_id)
        (source_root / "document.pdf").write_bytes(b"do-not-copy")
        (source_root / "logs.jsonl").write_text("do-not-copy", encoding="utf-8")
        source_files.extend(
            [source_root / "xbrl.zip", source_root / "xbrl.zip.provenance.json"]
        )
    _create_seed_cache(seed_cache, "manifest-outside")
    before = {path: cli._file_snapshot(path) for path in source_files}
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(
        filing_list,
        [
            _manifest_record(index, requested_id=requested_id)
            for index, requested_id in enumerate(requested_ids, 1)
        ],
    )
    captured = {}

    _invoke(
        monkeypatch,
        _seed_cli_args(
            run_root, filing_list,
            "--isolated-seed-cache-root", str(seed_cache),
        ),
        lambda **kwargs: (captured.update(kwargs) or {"summary": {}}),
    )

    assert {path: cli._file_snapshot(path) for path in source_files} == before
    assert sorted(path.relative_to(run_root / "cache").as_posix() for path in (run_root / "cache").rglob("*") if path.is_file()) == [
        f"{requested_ids[0]}/xbrl.zip",
        f"{requested_ids[0]}/xbrl.zip.provenance.json",
        f"{requested_ids[1]}/xbrl.zip",
        f"{requested_ids[1]}/xbrl.zip.provenance.json",
    ]
    for source_file, snapshot in before.items():
        destination = run_root / "cache" / source_file.relative_to(seed_cache)
        assert cli._sha256_file(destination) == snapshot[2]
    summary = captured["isolated_seed_summary"]
    assert summary["isolated_seed_cache_used"] is True
    assert summary["isolated_seed_cache_filing_count"] == 2
    assert summary["isolated_seed_cache_requested_ids"] == requested_ids
    assert summary["isolated_seed_copy_verified"] is True
    assert str(seed_cache) not in json.dumps(summary)


def test_isolated_seed_combined_fixture_is_verified_before_worker(monkeypatch, tmp_path):
    requested_id = "20260713591788"
    source_db = tmp_path / "source.db"
    _create_seed_db(source_db)
    source_db_before = cli._file_snapshot(source_db)
    seed_cache = tmp_path / "seed-cache"
    source_cache = _create_seed_cache(seed_cache, requested_id)
    source_cache_before = {
        path.name: cli._file_snapshot(path)
        for path in source_cache.iterdir()
    }
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [_manifest_record(1, requested_id=requested_id)])
    calls = []

    def run_backfill(**kwargs):
        calls.append(kwargs)
        destination_db = run_root / "state" / "decision.db"
        assert destination_db.is_file()
        for name, snapshot in source_cache_before.items():
            destination = run_root / "cache" / requested_id / name
            assert cli._sha256_file(destination) == snapshot[2]
        assert kwargs["isolated_seed_summary"]["isolated_seed_copy_verified"] is True
        return {"summary": {}}

    _invoke(
        monkeypatch,
        _seed_cli_args(
            run_root, filing_list,
            "--isolated-seed-decision-db", str(source_db),
            "--isolated-seed-cache-root", str(seed_cache),
        ),
        run_backfill,
    )

    assert len(calls) == 1
    assert cli._file_snapshot(source_db) == source_db_before
    assert {
        path.name: cli._file_snapshot(path)
        for path in source_cache.iterdir()
    } == source_cache_before
    summary = calls[0]["isolated_seed_summary"]
    assert summary["isolated_seed_decision_db_used"] is True
    assert summary["isolated_seed_cache_used"] is True
    assert summary["isolated_seed_cache_requested_ids"] == [requested_id]


@pytest.mark.parametrize("missing_name", ["xbrl.zip", "xbrl.zip.provenance.json"])
def test_isolated_seed_cache_requires_both_verified_files(
    monkeypatch, tmp_path, missing_name
):
    requested_id = "20260713000001"
    seed_cache = tmp_path / "seed-cache"
    source_root = _create_seed_cache(seed_cache, requested_id)
    (source_root / missing_name).unlink()
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [_manifest_record(1, requested_id=requested_id)])
    called = []

    with pytest.raises(SystemExit):
        _invoke(
            monkeypatch,
            _seed_cli_args(
                run_root, filing_list,
                "--isolated-seed-cache-root", str(seed_cache),
            ),
            lambda **kwargs: called.append(kwargs),
        )

    assert called == []


def test_isolated_seed_cache_rejects_unresolved_manifest_id(monkeypatch, tmp_path):
    seed_cache = tmp_path / "seed-cache"
    seed_cache.mkdir()
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    record = _manifest_record(1)
    record["requested_disclosure_no"] = ""
    _write_manifest(filing_list, [record])

    with pytest.raises(SystemExit):
        _invoke(
            monkeypatch,
            _seed_cli_args(
                run_root, filing_list,
                "--isolated-seed-cache-root", str(seed_cache),
            ),
            lambda **kwargs: pytest.fail("worker must not start"),
        )


def test_isolated_seed_validates_all_sources_before_copy(monkeypatch, tmp_path):
    requested_id = "20260713000001"
    source_db = tmp_path / "source.db"
    _create_seed_db(source_db)
    seed_cache = tmp_path / "seed-cache"
    _create_seed_cache(seed_cache, requested_id, sidecar=False)
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [_manifest_record(1, requested_id=requested_id)])

    with pytest.raises(SystemExit):
        _invoke(
            monkeypatch,
            _seed_cli_args(
                run_root, filing_list,
                "--isolated-seed-decision-db", str(source_db),
                "--isolated-seed-cache-root", str(seed_cache),
            ),
            lambda **kwargs: pytest.fail("worker must not start"),
        )

    assert not (run_root / "state" / "decision.db").exists()
    assert list((run_root / "cache").iterdir()) == []


def test_isolated_seed_cache_reparse_path_is_unsafe(monkeypatch, tmp_path):
    requested_id = "20260713000001"
    seed_cache = tmp_path / "seed-cache"
    source_root = _create_seed_cache(seed_cache, requested_id)
    original_reparse_check = cli._path_has_reparse_component
    monkeypatch.setattr(
        cli,
        "_path_has_reparse_component",
        lambda path: Path(path) == source_root or original_reparse_check(path),
    )
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [_manifest_record(1, requested_id=requested_id)])

    with pytest.raises(SystemExit):
        _invoke(
            monkeypatch,
            _seed_cli_args(
                run_root, filing_list,
                "--isolated-seed-cache-root", str(seed_cache),
            ),
            lambda **kwargs: pytest.fail("worker must not start"),
        )


def test_isolated_seed_copy_mismatch_stops_before_worker(monkeypatch, tmp_path):
    requested_id = "20260713000001"
    seed_cache = tmp_path / "seed-cache"
    _create_seed_cache(seed_cache, requested_id)
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [_manifest_record(1, requested_id=requested_id)])
    called = []

    def corrupt_copy(source, destination):
        Path(destination).write_bytes(b"corrupt")

    monkeypatch.setattr(cli.shutil, "copyfile", corrupt_copy)
    with pytest.raises(SystemExit):
        _invoke(
            monkeypatch,
            _seed_cli_args(
                run_root, filing_list,
                "--isolated-seed-cache-root", str(seed_cache),
            ),
            lambda **kwargs: called.append(kwargs),
        )

    assert called == []


def test_isolated_run_root_validation_precedes_seed_access(monkeypatch, tmp_path):
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [_manifest_record(1)])
    (run_root / "unexpected").write_text("occupied", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _invoke(
            monkeypatch,
            _seed_cli_args(
                run_root, filing_list,
                "--isolated-seed-decision-db", str(tmp_path / "missing.db"),
            ),
            lambda **kwargs: pytest.fail("worker must not start"),
        )

    assert exc_info.value.code == 2


def test_isolated_seed_defaults_are_additive_and_do_not_change_existing_mode(
    monkeypatch, tmp_path
):
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [])
    captured = {}

    _invoke(
        monkeypatch,
        _seed_cli_args(run_root, filing_list),
        lambda **kwargs: (captured.update(kwargs) or {"summary": {}}),
    )

    assert captured["isolated_seed_summary"] == {
        "isolated_seed_decision_db_used": False,
        "isolated_seed_decision_db_source_sha256": None,
        "isolated_seed_decision_db_destination_sha256": None,
        "isolated_seed_cache_used": False,
        "isolated_seed_cache_filing_count": 0,
        "isolated_seed_cache_requested_ids": [],
        "isolated_seed_copy_verified": False,
    }
    assert captured["dry_run_only"] is True
    assert captured["isolated_worker_dry_run"] is True


def test_isolated_seed_summary_is_included_in_run_summary():
    metrics = _Metrics()
    metrics._isolated_seed_summary = {
        "isolated_seed_decision_db_used": True,
        "isolated_seed_copy_verified": True,
    }

    summary = cli._summary_with_validation_rejections(metrics)

    assert summary["isolated_seed_decision_db_used"] is True
    assert summary["isolated_seed_copy_verified"] is True


def _create_seed_db_snapshot_files(path):
    _create_seed_db(path)
    wal = Path(f"{path}-wal")
    shm = Path(f"{path}-shm")
    wal.write_bytes(b"test-wal-snapshot")
    shm.write_bytes(b"test-shm-snapshot")
    return [path, wal, shm]


def _install_db_failure(monkeypatch, source_db, stage, *, mutate_source=False):
    class Cursor:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

    class Connection:
        def __init__(self, source):
            self.source = source

        def execute(self, sql):
            if self.source and stage == "schema" and "schema_version" in sql:
                raise sqlite3.DatabaseError("schema failure")
            if not self.source and stage == "integrity" and "integrity_check" in sql:
                raise sqlite3.DatabaseError("integrity failure")
            return Cursor((1,) if self.source else ("ok",))

        def backup(self, destination):
            if stage == "backup":
                if mutate_source:
                    source_db.write_bytes(source_db.read_bytes() + b"mutated")
                raise sqlite3.OperationalError("backup failure")

        def close(self):
            pass

    monkeypatch.setattr(
        cli.sqlite3,
        "connect",
        lambda database, *args, **kwargs: Connection(bool(kwargs.get("uri"))),
    )


@pytest.mark.parametrize("stage", ["schema", "backup", "integrity"])
def test_isolated_seed_db_exception_revalidates_db_wal_shm_and_preserves_stop(
    monkeypatch, tmp_path, stage
):
    source_db = tmp_path / "source.db"
    source_files = _create_seed_db_snapshot_files(source_db)
    before = {path: cli._file_snapshot(path) for path in source_files}
    real_snapshot = cli._file_snapshot
    snapshot_calls = {path: 0 for path in source_files}

    def tracked_snapshot(path):
        path = Path(path)
        if path in snapshot_calls:
            snapshot_calls[path] += 1
        return real_snapshot(path)

    monkeypatch.setattr(cli, "_file_snapshot", tracked_snapshot)
    _install_db_failure(monkeypatch, source_db, stage)
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [_manifest_record(1)])
    workers = []

    with pytest.raises(SystemExit):
        _invoke(
            monkeypatch,
            _seed_cli_args(
                run_root, filing_list,
                "--isolated-seed-decision-db", str(source_db),
            ),
            lambda **kwargs: workers.append(kwargs),
        )

    assert workers == []
    assert {path: real_snapshot(path) for path in source_files} == before
    assert all(count >= 2 for count in snapshot_calls.values())


def test_isolated_seed_db_exception_prioritizes_source_mutated(monkeypatch, tmp_path, capsys):
    source_db = tmp_path / "source.db"
    source_files = _create_seed_db_snapshot_files(source_db)
    wal_shm_before = {
        path: cli._file_snapshot(path)
        for path in source_files[1:]
    }
    _install_db_failure(
        monkeypatch, source_db, "backup", mutate_source=True
    )
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [_manifest_record(1)])
    workers = []

    with pytest.raises(SystemExit) as exc_info:
        _invoke(
            monkeypatch,
            _seed_cli_args(
                run_root, filing_list,
                "--isolated-seed-decision-db", str(source_db),
            ),
            lambda **kwargs: workers.append(kwargs),
        )

    payload, captured = _read_isolated_seed_stop(capsys)
    assert exc_info.value.code != 0
    assert payload["stop_code"] == "STOP_BACKFILL_ISOLATED_SEED_SOURCE_MUTATED"
    assert payload["detail"] == {
        "source_kind": "decision_db",
        "safe_identifier": "database",
        "affected_count": 1,
    }
    assert str(tmp_path) not in captured.err
    assert "backup failure" not in captured.err
    assert workers == []
    assert {
        path: cli._file_snapshot(path)
        for path in source_files[1:]
    } == wal_shm_before


def test_isolated_seed_db_revalidation_failure_stops_before_worker(
    monkeypatch, tmp_path
):
    source_db = tmp_path / "source.db"
    _create_seed_db_snapshot_files(source_db)
    real_snapshot = cli._file_snapshot
    database_snapshot_calls = 0

    def fail_database_revalidation(path):
        nonlocal database_snapshot_calls
        path = Path(path)
        if path == source_db:
            database_snapshot_calls += 1
            if database_snapshot_calls > 1:
                raise OSError("revalidation unavailable")
        return real_snapshot(path)

    monkeypatch.setattr(cli, "_file_snapshot", fail_database_revalidation)
    _install_db_failure(monkeypatch, source_db, "none")
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [_manifest_record(1)])
    workers = []

    with pytest.raises(SystemExit):
        _invoke(
            monkeypatch,
            _seed_cli_args(
                run_root, filing_list,
                "--isolated-seed-decision-db", str(source_db),
            ),
            lambda **kwargs: workers.append(kwargs),
        )

    assert workers == []
    assert database_snapshot_calls == 2


def _cache_exception_fixture(tmp_path):
    requested_ids = ["20260713000101", "20260713000102"]
    seed_cache = tmp_path / "seed-cache"
    source_files = []
    for requested_id in requested_ids:
        source_root = _create_seed_cache(seed_cache, requested_id)
        (source_root / "document.pdf").write_bytes(b"untouched-pdf")
        (source_root / "logs.jsonl").write_bytes(b"untouched-log")
        source_files.extend(
            [source_root / "xbrl.zip", source_root / "xbrl.zip.provenance.json"]
        )
    outside = _create_seed_cache(seed_cache, "manifest-outside")
    extras = [
        seed_cache / requested_ids[0] / "document.pdf",
        seed_cache / requested_ids[0] / "logs.jsonl",
        outside / "xbrl.zip",
        outside / "xbrl.zip.provenance.json",
    ]
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(
        filing_list,
        [
            _manifest_record(index, requested_id=requested_id)
            for index, requested_id in enumerate(requested_ids, 1)
        ],
    )
    return requested_ids, seed_cache, source_files, extras, run_root, filing_list


@pytest.mark.parametrize("failure_name", ["xbrl.zip", "xbrl.zip.provenance.json"])
def test_isolated_seed_cache_copy_exception_revalidates_all_sources(
    monkeypatch, tmp_path, failure_name
):
    requested_ids, seed_cache, source_files, extras, run_root, filing_list = (
        _cache_exception_fixture(tmp_path)
    )
    real_snapshot = cli._file_snapshot
    before = {path: real_snapshot(path) for path in source_files + extras}
    snapshot_calls = {path: 0 for path in source_files + extras}

    def tracked_snapshot(path):
        path = Path(path)
        if path in snapshot_calls:
            snapshot_calls[path] += 1
        return real_snapshot(path)

    real_copy = cli.shutil.copyfile

    def failing_copy(source, destination):
        source = Path(source)
        if source.parent.name == requested_ids[0] and source.name == failure_name:
            raise OSError("copy failure")
        return real_copy(source, destination)

    monkeypatch.setattr(cli, "_file_snapshot", tracked_snapshot)
    monkeypatch.setattr(cli.shutil, "copyfile", failing_copy)
    workers = []

    with pytest.raises(SystemExit):
        _invoke(
            monkeypatch,
            _seed_cli_args(
                run_root, filing_list,
                "--isolated-seed-cache-root", str(seed_cache),
            ),
            lambda **kwargs: workers.append(kwargs),
        )

    assert workers == []
    assert {path: real_snapshot(path) for path in source_files + extras} == before
    assert all(snapshot_calls[path] >= 2 for path in source_files)
    assert all(snapshot_calls[path] == 0 for path in extras)


@pytest.mark.parametrize("failure_name", ["xbrl.zip", "xbrl.zip.provenance.json"])
def test_isolated_seed_cache_hash_mismatch_revalidates_all_sources(
    monkeypatch, tmp_path, failure_name
):
    requested_ids, seed_cache, source_files, extras, run_root, filing_list = (
        _cache_exception_fixture(tmp_path)
    )
    real_snapshot = cli._file_snapshot
    before = {path: real_snapshot(path) for path in source_files + extras}
    snapshot_calls = {path: 0 for path in source_files}
    real_copy = cli.shutil.copyfile

    def tracked_snapshot(path):
        path = Path(path)
        if path in snapshot_calls:
            snapshot_calls[path] += 1
        return real_snapshot(path)

    def corrupt_destination(source, destination):
        source = Path(source)
        if source.parent.name == requested_ids[0] and source.name == failure_name:
            Path(destination).write_bytes(b"corrupt-destination")
            return destination
        return real_copy(source, destination)

    monkeypatch.setattr(cli, "_file_snapshot", tracked_snapshot)
    monkeypatch.setattr(cli.shutil, "copyfile", corrupt_destination)
    workers = []

    with pytest.raises(SystemExit):
        _invoke(
            monkeypatch,
            _seed_cli_args(
                run_root, filing_list,
                "--isolated-seed-cache-root", str(seed_cache),
            ),
            lambda **kwargs: workers.append(kwargs),
        )

    assert workers == []
    assert {path: real_snapshot(path) for path in source_files + extras} == before
    assert all(count >= 2 for count in snapshot_calls.values())


def test_isolated_seed_cache_multi_filing_failure_revalidates_every_source(
    monkeypatch, tmp_path
):
    requested_ids, seed_cache, source_files, extras, run_root, filing_list = (
        _cache_exception_fixture(tmp_path)
    )
    real_snapshot = cli._file_snapshot
    before = {path: real_snapshot(path) for path in source_files + extras}
    snapshot_calls = {path: 0 for path in source_files}
    real_copy = cli.shutil.copyfile

    def tracked_snapshot(path):
        path = Path(path)
        if path in snapshot_calls:
            snapshot_calls[path] += 1
        return real_snapshot(path)

    def fail_second_filing(source, destination):
        source = Path(source)
        if source.parent.name == requested_ids[1] and source.name == "xbrl.zip":
            raise OSError("second filing failure")
        return real_copy(source, destination)

    monkeypatch.setattr(cli, "_file_snapshot", tracked_snapshot)
    monkeypatch.setattr(cli.shutil, "copyfile", fail_second_filing)
    workers = []

    with pytest.raises(SystemExit):
        _invoke(
            monkeypatch,
            _seed_cli_args(
                run_root, filing_list,
                "--isolated-seed-cache-root", str(seed_cache),
            ),
            lambda **kwargs: workers.append(kwargs),
        )

    assert workers == []
    assert {path: real_snapshot(path) for path in source_files + extras} == before
    assert all(count >= 2 for count in snapshot_calls.values())


def test_isolated_seed_cache_exception_prioritizes_source_mutated(
    monkeypatch, tmp_path, capsys
):
    requested_ids, seed_cache, source_files, _extras, run_root, filing_list = (
        _cache_exception_fixture(tmp_path)
    )
    real_copy = cli.shutil.copyfile

    def mutate_then_fail(source, destination):
        source = Path(source)
        if source.parent.name == requested_ids[0] and source.name == "xbrl.zip":
            source.write_bytes(source.read_bytes() + b"mutated")
            raise OSError("copy failure")
        return real_copy(source, destination)

    monkeypatch.setattr(cli.shutil, "copyfile", mutate_then_fail)
    workers = []

    with pytest.raises(SystemExit) as exc_info:
        _invoke(
            monkeypatch,
            _seed_cli_args(
                run_root, filing_list,
                "--isolated-seed-cache-root", str(seed_cache),
            ),
            lambda **kwargs: workers.append(kwargs),
        )

    payload, captured = _read_isolated_seed_stop(capsys)
    assert exc_info.value.code != 0
    assert payload["stop_code"] == "STOP_BACKFILL_ISOLATED_SEED_SOURCE_MUTATED"
    assert payload["detail"] == {
        "source_kind": "cache",
        "safe_identifier": f"{requested_ids[0]}/xbrl.zip",
        "affected_count": 1,
    }
    assert str(tmp_path) not in captured.err
    assert "copy failure" not in captured.err
    assert workers == []


@pytest.mark.parametrize("extra", [
    [],
    ["--apply"],
    ["--dry-run"],
    ["--worker-version", "v2"],
    ["--workers", "2"],
])
def test_isolated_cli_rejects_invalid_mode_combinations(monkeypatch, tmp_path, extra):
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    filing_list.parent.mkdir(parents=True); filing_list.write_text("[]", encoding="utf-8")
    args = ["--isolated-worker-dry-run", "--run-root", str(run_root), "--filing-list", str(filing_list), "--workers", "1", *extra]
    if not extra:
        args = ["--isolated-worker-dry-run", "--run-root", str(run_root)]

    with pytest.raises(SystemExit):
        _invoke(monkeypatch, args)


def test_isolated_cli_rejects_production_or_nonempty_run_root(monkeypatch, tmp_path):
    production_args = ["--isolated-worker-dry-run", "--run-root", "logs", "--filing-list", "logs/input/filings.json", "--workers", "1"]
    with pytest.raises(SystemExit):
        _invoke(monkeypatch, production_args)

    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    filing_list.parent.mkdir(parents=True); filing_list.write_text("[]", encoding="utf-8")
    (run_root / "unexpected").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        _invoke(monkeypatch, ["--isolated-worker-dry-run", "--run-root", str(run_root), "--filing-list", str(filing_list), "--workers", "1"])


def test_isolated_cli_routes_all_mutable_paths_under_run_root(monkeypatch, tmp_path):
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    filing_list.parent.mkdir(parents=True); filing_list.write_text("[]", encoding="utf-8")
    captured = {}
    def run_backfill(**kwargs):
        captured.update(kwargs)
        return {"summary": {}}

    _invoke(monkeypatch, ["--isolated-worker-dry-run", "--run-root", str(run_root), "--filing-list", str(filing_list), "--workers", "1"], run_backfill)

    assert captured["worker_version"] == "v4"
    assert captured["workers"] == 1
    assert captured["dry_run_only"] is True
    assert captured["isolated_worker_dry_run"] is True
    assert captured["state_db"] == str(run_root / "state" / "state.db")
    assert captured["decision_db_path"] == str(run_root / "state" / "decision.db")
    assert captured["cache_root"] == str(run_root / "cache")
    assert captured["log_jsonl_path"] == str(run_root / "logs" / "run.jsonl")
    assert captured["manifest_dir"] == str(run_root / "manifest")


def _mark_reparse_component(monkeypatch, target):
    target = Path(target)
    real_lstat = cli.os.lstat

    def lstat(path):
        if Path(path) == target:
            return SimpleNamespace(
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            )
        return real_lstat(path)

    monkeypatch.setattr(cli.os, "lstat", lstat)


@pytest.mark.parametrize("component", ["run_root", "run_root_parent"])
def test_isolated_raw_run_root_rejects_reparse_before_resolve(
    monkeypatch, tmp_path, component
):
    parent = tmp_path / "parent"
    parent.mkdir()
    run_root = parent / "run"
    target = run_root if component == "run_root" else parent
    if target == run_root:
        run_root.mkdir()
    _mark_reparse_component(monkeypatch, target)

    with pytest.raises(RuntimeError, match="STOP_BACKFILL_ISOLATED_SEED_UNSAFE_PATH"):
        cli._assert_isolated_path_safe(run_root, path_role="run_root")


def test_isolated_raw_run_root_rejects_real_symlink_parent(monkeypatch, tmp_path):
    target_parent = tmp_path / "target"
    target_parent.mkdir()
    link_parent = tmp_path / "link-parent"
    try:
        link_parent.symlink_to(target_parent, target_is_directory=True)
    except OSError:
        _mark_reparse_component(monkeypatch, target_parent)
        link_parent = target_parent

    with pytest.raises(RuntimeError, match="STOP_BACKFILL_ISOLATED_SEED_UNSAFE_PATH"):
        cli._assert_isolated_path_safe(link_parent / "new-run", path_role="run_root")


def test_isolated_reparse_attributes_unavailable_fails_closed(monkeypatch, tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()
    real_lstat = cli.os.lstat

    def lstat(path):
        if Path(path) == run_root:
            return SimpleNamespace()
        return real_lstat(path)

    monkeypatch.setattr(cli.os, "lstat", lstat)
    with pytest.raises(RuntimeError, match="STOP_BACKFILL_ISOLATED_SEED_UNSAFE_PATH"):
        cli._assert_isolated_path_safe(run_root, path_role="run_root")


@pytest.mark.parametrize(
    ("path_role", "destination_name"),
    [
        ("decision_db", "state/decision.db"),
        ("state_db", "state/state.db"),
        ("log_jsonl", "logs/run.jsonl"),
        ("filing_cache", "cache/20260713000001"),
        ("xbrl_zip", "cache/20260713000001/xbrl.zip"),
        ("sidecar", "cache/20260713000001/xbrl.zip.provenance.json"),
    ],
)
def test_isolated_destination_guard_rejects_reparse_before_write(
    monkeypatch, tmp_path, path_role, destination_name
):
    run_root = tmp_path / "run"
    destination = run_root / destination_name
    destination.parent.mkdir(parents=True)
    _mark_reparse_component(monkeypatch, destination.parent)

    with pytest.raises(RuntimeError, match="STOP_BACKFILL_ISOLATED_SEED_UNSAFE_PATH"):
        cli._assert_isolated_destination_safe(
            destination,
            run_root=run_root,
            path_role=path_role,
        )
    assert not destination.exists()


def test_isolated_layout_recheck_stops_before_seed_source_access(monkeypatch, tmp_path):
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [_manifest_record(1)])
    source_db = tmp_path / "source.db"
    _create_seed_db(source_db)
    source_accesses = []

    def reject_layout(*_args, **_kwargs):
        raise RuntimeError("STOP_BACKFILL_ISOLATED_SEED_UNSAFE_PATH: path_role=state_db; reason=reparse_component")

    monkeypatch.setattr(cli, "_validate_isolated_write_paths", reject_layout)
    monkeypatch.setattr(
        cli,
        "_validate_isolated_decision_db_seed",
        lambda *_args, **_kwargs: source_accesses.append(True),
    )

    with pytest.raises(SystemExit):
        _invoke(
            monkeypatch,
            _seed_cli_args(
                run_root, filing_list,
                "--isolated-seed-decision-db", str(source_db),
            ),
            lambda **_kwargs: pytest.fail("worker must not start"),
        )
    assert source_accesses == []


_ISOLATED_SEED_STOP_CODES = (
    "STOP_BACKFILL_ISOLATED_SEED_INVALID_MODE",
    "STOP_BACKFILL_ISOLATED_SEED_DB_INVALID",
    "STOP_BACKFILL_ISOLATED_SEED_CACHE_MISSING",
    "STOP_BACKFILL_ISOLATED_SEED_UNSAFE_PATH",
    "STOP_BACKFILL_ISOLATED_SEED_COPY_MISMATCH",
    "STOP_BACKFILL_ISOLATED_SEED_MANIFEST_ID_UNRESOLVED",
    "STOP_BACKFILL_ISOLATED_SEED_SOURCE_MUTATED",
)


def _read_isolated_seed_stop(capsys):
    captured = capsys.readouterr()
    payloads = [
        json.loads(line)
        for line in captured.err.splitlines()
        if line.startswith("{") and line.endswith("}")
    ]
    assert len(payloads) == 1
    return payloads[0], captured


@pytest.mark.parametrize("stop_code", _ISOLATED_SEED_STOP_CODES)
def test_isolated_seed_stops_emit_structured_nonzero_json(
    monkeypatch, tmp_path, capsys, stop_code
):
    worker_calls = []
    if stop_code == "STOP_BACKFILL_ISOLATED_SEED_INVALID_MODE":
        args = ["--isolated-seed-decision-db", str(tmp_path / "source.db")]
    else:
        run_root = tmp_path / "run"
        filing_list = run_root / "input" / "filings.json"
        _write_manifest(filing_list, [_manifest_record(1)])
        monkeypatch.setattr(
            cli,
            "_prepare_isolated_seeds",
            lambda **_kwargs: (_ for _ in ()).throw(
                RuntimeError(f"{stop_code}: C:/Users/private/secret.db raw failure")
            ),
        )
        args = _seed_cli_args(run_root, filing_list)

    with pytest.raises(SystemExit) as exc_info:
        _invoke(monkeypatch, args, lambda **_kwargs: worker_calls.append(True))

    payload, captured = _read_isolated_seed_stop(capsys)
    assert exc_info.value.code != 0
    assert payload == {
        "status": "stopped",
        "stop_code": stop_code,
        "stage": "isolated_seed_mode" if stop_code.endswith("INVALID_MODE") else "isolated_seed_prepare",
        "detail": {"reason": "invalid_mode"} if stop_code.endswith("INVALID_MODE") else {},
        "worker_started": False,
        "canonical_sync_enabled": False,
    }
    assert "Traceback" not in captured.err
    assert "C:/Users/private" not in captured.err
    assert "raw failure" not in captured.err
    assert worker_calls == []


def test_isolated_seed_stop_preserves_allowlisted_safe_detail(monkeypatch, tmp_path, capsys):
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [_manifest_record(1)])
    monkeypatch.setattr(
        cli,
        "_prepare_isolated_seeds",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "STOP_BACKFILL_ISOLATED_SEED_UNSAFE_PATH: "
                "path_role=decision_db; reason=reparse_component; C:/Users/private"
            )
        ),
    )

    with pytest.raises(SystemExit):
        _invoke(monkeypatch, _seed_cli_args(run_root, filing_list))

    payload, captured = _read_isolated_seed_stop(capsys)
    assert payload["detail"] == {
        "path_role": "decision_db",
        "reason": "reparse_component",
    }
    assert str(tmp_path) not in captured.err


def test_unexpected_seed_prepare_runtime_error_is_not_converted(monkeypatch, tmp_path, capsys):
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [_manifest_record(1)])
    monkeypatch.setattr(
        cli,
        "_prepare_isolated_seeds",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("unexpected seed failure")),
    )

    with pytest.raises(RuntimeError, match="unexpected seed failure"):
        _invoke(monkeypatch, _seed_cli_args(run_root, filing_list))

    assert capsys.readouterr().err == ""


def _run_isolated_cli_subprocess(*args):
    return subprocess.run(
        [sys.executable, str(Path(cli.__file__)), *args],
        cwd=Path(cli.__file__).parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("kind", "expected_code"),
    [
        ("invalid", "STOP_BACKFILL_ISOLATED_SEED_INVALID_MODE"),
        ("unsafe", "STOP_BACKFILL_ISOLATED_SEED_UNSAFE_PATH"),
        ("db_invalid", "STOP_BACKFILL_ISOLATED_SEED_DB_INVALID"),
    ],
)
def test_isolated_seed_stops_are_machine_readable_in_subprocess(
    tmp_path, kind, expected_code
):
    if kind == "invalid":
        result = _run_isolated_cli_subprocess(
            "--isolated-seed-decision-db", str(tmp_path / "source.db")
        )
    else:
        run_root = tmp_path / f"run-{kind}"
        filing_list = run_root / "input" / "filings.json"
        _write_manifest(filing_list, [_manifest_record(1)])
        source = tmp_path / ("relative.db" if kind == "unsafe" else "invalid.db")
        if kind == "db_invalid":
            source.write_text("not sqlite", encoding="utf-8")
        result = _run_isolated_cli_subprocess(
            "--isolated-worker-dry-run", "--run-root", str(run_root),
            "--filing-list", str(filing_list), "--workers", "1",
            "--isolated-seed-decision-db", "relative.db" if kind == "unsafe" else str(source),
        )

    payloads = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert result.returncode != 0
    assert len(payloads) == 1
    assert payloads[0]["stop_code"] == expected_code
    assert payloads[0]["worker_started"] is False
    assert payloads[0]["canonical_sync_enabled"] is False
    assert "Traceback" not in result.stderr


def test_isolated_cli_subprocess_normal_path_keeps_summary_contract(tmp_path):
    run_root = tmp_path / "run-normal"
    filing_list = run_root / "input" / "filings.json"
    _write_manifest(filing_list, [])

    result = _run_isolated_cli_subprocess(
        "--isolated-worker-dry-run", "--run-root", str(run_root),
        "--filing-list", str(filing_list), "--workers", "1",
    )

    assert result.returncode == 0
    assert "stop_code" not in result.stderr
    assert "Traceback" not in result.stderr


def test_isolated_cli_passes_skip_pdf_to_run_backfill(monkeypatch, tmp_path):
    run_root = tmp_path / "run"
    filing_list = run_root / "input" / "filings.json"
    filing_list.parent.mkdir(parents=True); filing_list.write_text("[]", encoding="utf-8")
    captured = {}

    def run_backfill(**kwargs):
        captured.update(kwargs)
        return {"summary": {}}

    _invoke(monkeypatch, ["--isolated-worker-dry-run", "--run-root", str(run_root), "--filing-list", str(filing_list), "--workers", "1", "--skip-pdf"], run_backfill)

    assert captured["skip_pdf"] is True


def test_isolated_flush_uses_real_upsert_and_never_canonical_sync(monkeypatch, tmp_path):
    paths = _isolated_paths(tmp_path)
    calls = {"batch": 0, "dry": 0, "sync": 0}
    stats = SimpleNamespace(
        inserted=1, updated=0, no_change=0,
        rejected_lower_priority=0, rejected_filing_conflict=0,
        rejected_filing_identity_unresolved=0, failed_batches=0,
        canonical_sync_ids=[41],
    )

    class DB:
        def __init__(self, path):
            assert Path(path).resolve() == Path(paths["decision_db_path"]).resolve()
        def close(self):
            pass

    def batch(records, db, batch_size):
        calls["batch"] += 1
        return stats

    def dry(*args, **kwargs):
        calls["dry"] += 1
        raise AssertionError("isolated mode must not use report-only upsert")

    def sync(*args, **kwargs):
        calls["sync"] += 1
        raise AssertionError("isolated mode must not canonical-sync")

    monkeypatch.setattr("src.migration.migration_db.MigrationDB", DB)
    monkeypatch.setattr("lib.backfill.batch_upsert.dry_run_upsert_segments", dry)
    monkeypatch.setattr(cli, "batch_upsert_segments", batch)
    monkeypatch.setattr("tools.sync_segments.sync_sqlite_segment_ids", sync)
    metrics, store, run_logger = _Metrics(), _Store(), _RunLogger()

    cli._flush_buffer(
        [{"segment_name": "Core"}], ["filing-1"],
        batch_size=100, metrics=metrics, store=store, run_logger=run_logger,
        dry_run_only=True, isolated_worker_dry_run=True, **paths,
    )

    assert calls == {"batch": 1, "dry": 0, "sync": 0}
    assert metrics.stats is stats
    assert store.upserted == ["filing-1"]
    assert store.failed == []
    assert run_logger.events[0][1]["canonical_sync_ids"] == [41]
    assert cli._validation_rejection_summary(metrics) == {
        "validation_rejected_record_count": 0,
        "validation_rejected_filing_count": 0,
        "validation_rejected_filing_ids": [],
        "validation_reasons_by_filing": {},
        "validation_rejection_filing_unresolved": False,
    }


def test_isolated_sqlite_persists_verified_internal_id(monkeypatch, tmp_path):
    import sqlite3

    paths = _isolated_paths(tmp_path)
    requested_id = "20260713591788"
    internal_id = "20260713340570"
    record = {
        "ticker": "4057", "period": "2026-05-31", "quarter": "FY",
        "segment_name": "Core", "segment_order": 1,
        "segment_sales": 100.0, "segment_profit": 20.0,
        "raw_profit_label": "operating profit", "source": "backfill_xbrl",
        "segment_name_norm": "core", "extractor_route": "xbrl",
        "source_doc_type": "earnings_summary", "disclosure_date": "2026-07-13",
        "tdnet_doc_id": internal_id, "row_type": "segment",
        "_identity_verified": True,
        "_identity_verdict": "official_linked_xbrl_match",
        "_requested_disclosure_no": requested_id,
        "_internal_document_id": internal_id,
        "_canonical_expected_period": "2026-05-31",
        "_canonical_expected_quarter": "FY",
        "_resolved_zip_sha256": "a" * 64,
        "_verified_xbrl_same_zip": True,
        "_worker_version": "v4", "_segment_period_role": "current",
    }
    metrics, run_logger = _Metrics(), _RunLogger()
    monkeypatch.setattr(
        "tools.sync_segments.sync_sqlite_segment_ids",
        lambda *a, **k: pytest.fail("isolated mode must not canonical-sync"),
    )

    cli._flush_buffer(
        [record], ["manifest-filing-id"], batch_size=100, metrics=metrics,
        store=_Store(), run_logger=run_logger, dry_run_only=True,
        isolated_worker_dry_run=True, **paths,
    )

    with sqlite3.connect(paths["decision_db_path"]) as conn:
        saved = conn.execute(
            "SELECT tdnet_doc_id FROM segment_financials WHERE company_code='4057'"
        ).fetchone()
    assert saved == (internal_id,)
    assert record["_requested_disclosure_no"] == requested_id
    assert metrics.stats.inserted == 1
    assert len(metrics.stats.canonical_sync_ids) == 1


def test_normal_dry_run_remains_report_only(monkeypatch, tmp_path):
    calls = {"batch": 0, "dry": 0}

    class DB:
        def __init__(self, path):
            self.path = path
        def close(self):
            pass

    def dry(records, db):
        calls["dry"] += 1

    monkeypatch.setattr("src.migration.migration_db.MigrationDB", DB)
    monkeypatch.setattr("lib.backfill.batch_upsert.dry_run_upsert_segments", dry)
    monkeypatch.setattr(cli, "batch_upsert_segments", lambda *a, **k: calls.__setitem__("batch", calls["batch"] + 1))

    cli._flush_buffer(
        [{"segment_name": "Core"}], ["filing-1"], str(tmp_path / "dry.db"), 100,
        _Metrics(), _Store(), _RunLogger(), dry_run_only=True,
        isolated_worker_dry_run=False,
    )

    assert calls == {"batch": 0, "dry": 1}


def test_production_apply_keeps_sqlite_and_canonical_sync(monkeypatch, tmp_path):
    calls = {"batch": 0, "sync": 0}
    stats = SimpleNamespace(
        inserted=1, updated=0, no_change=0,
        rejected_lower_priority=0, rejected_filing_conflict=0,
        rejected_filing_identity_unresolved=0, failed_batches=0,
        canonical_sync_ids=[51],
    )

    class DB:
        def __init__(self, path):
            self.path = path
        def close(self):
            pass

    monkeypatch.setattr("src.migration.migration_db.MigrationDB", DB)
    monkeypatch.setattr(cli, "batch_upsert_segments", lambda *a, **k: (calls.__setitem__("batch", calls["batch"] + 1) or stats))
    monkeypatch.setattr("lib.pipeline.db.load_env", lambda: None)
    monkeypatch.setattr("lib.pipeline.db.get_supabase_write_config", lambda: {"rest_url": "https://example.invalid", "headers": {}})

    def sync(db_path, ids, rest_url, headers, dry_run):
        calls["sync"] += 1
        return {"synced_segment_ids": ids}
    monkeypatch.setattr("tools.sync_segments.sync_sqlite_segment_ids", sync)

    cli._flush_buffer(
        [{"segment_name": "Core"}], ["filing-1"], str(tmp_path / "apply.db"), 100,
        _Metrics(), _Store(), _RunLogger(), dry_run_only=False,
        isolated_worker_dry_run=False,
    )

    assert calls == {"batch": 1, "sync": 1}


def test_validation_rejected_filing_is_failed_and_not_upserted(monkeypatch, tmp_path):
    paths = _isolated_paths(tmp_path)
    stats = _upsert_stats(
        validation_rejected_record_count=2,
        validation_rejected_filing_count=1,
        validation_rejected_filing_ids=["requested-B"],
        validation_reasons_by_filing={
            "requested-B": {"invalid_quarter:INVALID": 2},
        },
    )
    _mock_flush_batch(monkeypatch, stats)
    metrics, store, run_logger = _Metrics(), _Store(), _RunLogger()

    cli._flush_buffer(
        [{"segment_name": "Rejected 1"}, {"segment_name": "Rejected 2"}],
        ["filing-B"],
        batch_size=100,
        metrics=metrics,
        store=store,
        run_logger=run_logger,
        dry_run_only=True,
        isolated_worker_dry_run=True,
        validation_filing_id_map={"requested-B": "filing-B"},
        **paths,
    )

    assert store.upserted == []
    assert store.failed == [
        ("filing-B", '{"invalid_quarter:INVALID": 2}', "validation_rejected")
    ]
    assert run_logger.events[0][1]["validation_rejected_filing_ids"] == ["filing-B"]
    assert cli._validation_rejection_summary(metrics)["validation_rejected_record_count"] == 2


def test_multi_filing_rejection_preserves_normal_success_and_canonical_sync(monkeypatch, tmp_path):
    stats = _upsert_stats(
        inserted=1,
        canonical_sync_ids=[51],
        validation_rejected_record_count=1,
        validation_rejected_filing_count=1,
        validation_rejected_filing_ids=["requested-B"],
        validation_reasons_by_filing={
            "requested-B": {"verified_current_period_contract_mismatch": 1},
        },
    )
    _mock_flush_batch(monkeypatch, stats)
    monkeypatch.setattr("lib.pipeline.db.load_env", lambda: None)
    monkeypatch.setattr(
        "lib.pipeline.db.get_supabase_write_config",
        lambda: {"rest_url": "https://example.invalid", "headers": {}},
    )
    synced = []

    def sync(db_path, ids, rest_url, headers, dry_run):
        synced.extend(ids)
        return {"synced_segment_ids": ids}

    monkeypatch.setattr("tools.sync_segments.sync_sqlite_segment_ids", sync)
    monkeypatch.setattr(
        cli,
        "_canonical_sync_ids_by_filing",
        lambda *args, **kwargs: {"filing-A": [51]},
    )
    metrics, store = _Metrics(), _Store()

    cli._flush_buffer(
        [{"segment_name": "Accepted"}, {"segment_name": "Rejected"}],
        ["filing-A", "filing-B"],
        str(tmp_path / "apply.db"),
        100,
        metrics,
        store,
        _RunLogger(),
        dry_run_only=False,
        isolated_worker_dry_run=False,
        validation_filing_id_map={"requested-B": "filing-B"},
    )

    assert synced == [51]
    assert store.upserted == ["filing-A"]
    assert store.failed == [
        (
            "filing-B",
            '{"verified_current_period_contract_mismatch": 1}',
            "validation_rejected",
        )
    ]


def test_partial_rejection_syncs_accepted_row_but_does_not_upsert_filing(monkeypatch, tmp_path):
    stats = _upsert_stats(
        inserted=1,
        canonical_sync_ids=[52],
        validation_rejected_record_count=1,
        validation_rejected_filing_count=1,
        validation_rejected_filing_ids=["requested-A"],
        validation_reasons_by_filing={
            "requested-A": {"verified_previous_period_contract_mismatch": 1},
        },
    )
    _mock_flush_batch(monkeypatch, stats)
    monkeypatch.setattr("lib.pipeline.db.load_env", lambda: None)
    monkeypatch.setattr(
        "lib.pipeline.db.get_supabase_write_config",
        lambda: {"rest_url": "https://example.invalid", "headers": {}},
    )
    synced = []
    monkeypatch.setattr(
        "tools.sync_segments.sync_sqlite_segment_ids",
        lambda db_path, ids, rest_url, headers, dry_run: (
            synced.extend(ids) or {"synced_segment_ids": ids}
        ),
    )
    metrics, store = _Metrics(), _Store()

    cli._flush_buffer(
        [{"segment_name": "Accepted"}, {"segment_name": "Rejected"}],
        ["filing-A"],
        str(tmp_path / "partial.db"),
        100,
        metrics,
        store,
        _RunLogger(),
        dry_run_only=False,
        isolated_worker_dry_run=False,
        validation_filing_id_map={"requested-A": "filing-A"},
    )

    assert synced == [52]
    assert store.upserted == []
    assert store.failed[0][0] == "filing-A"


def test_validation_rejections_are_aggregated_across_flushes(monkeypatch, tmp_path):
    paths = _isolated_paths(tmp_path)
    pending_stats = [
        _upsert_stats(
            validation_rejected_record_count=1,
            validation_rejected_filing_count=1,
            validation_rejected_filing_ids=["requested-A"],
            validation_reasons_by_filing={
                "requested-A": {"invalid_quarter:INVALID": 1},
            },
        ),
        _upsert_stats(
            validation_rejected_record_count=2,
            validation_rejected_filing_count=2,
            validation_rejected_filing_ids=["requested-A", "requested-B"],
            validation_reasons_by_filing={
                "requested-A": {"invalid_quarter:INVALID": 1},
                "requested-B": {"verified_unknown_period_role": 1},
            },
        ),
    ]

    def batch(records, db, batch_size):
        return pending_stats.pop(0)

    _mock_flush_batch(monkeypatch, batch)
    metrics, store = _Metrics(), _Store()
    id_map = {"requested-A": "filing-A", "requested-B": "filing-B"}

    cli._flush_buffer(
        [{"segment_name": "A1"}], ["filing-A"], batch_size=100,
        metrics=metrics, store=store, run_logger=_RunLogger(),
        dry_run_only=True, isolated_worker_dry_run=True,
        validation_filing_id_map=id_map, **paths,
    )
    cli._flush_buffer(
        [{"segment_name": "A2"}, {"segment_name": "B"}],
        ["filing-A", "filing-B"], batch_size=100,
        metrics=metrics, store=store, run_logger=_RunLogger(),
        dry_run_only=True, isolated_worker_dry_run=True,
        validation_filing_id_map=id_map, **paths,
    )

    summary = cli._validation_rejection_summary(metrics)
    assert summary["validation_rejected_record_count"] == 3
    assert summary["validation_rejected_filing_count"] == 2
    assert summary["validation_rejected_filing_ids"] == ["filing-A", "filing-B"]
    assert summary["validation_reasons_by_filing"] == {
        "filing-A": {"invalid_quarter:INVALID": 2},
        "filing-B": {"verified_unknown_period_role": 1},
    }


def test_unresolved_validation_filing_fails_every_buffer_filing(monkeypatch, tmp_path):
    paths = _isolated_paths(tmp_path)
    stats = _upsert_stats(
        validation_rejected_record_count=1,
        validation_rejected_filing_count=0,
        validation_rejected_filing_ids=[],
        validation_reasons_by_filing={},
    )
    _mock_flush_batch(monkeypatch, stats)
    metrics, store = _Metrics(), _Store()

    cli._flush_buffer(
        [{"segment_name": "Unknown rejection"}], ["filing-A"],
        batch_size=100, metrics=metrics, store=store,
        run_logger=_RunLogger(), dry_run_only=True,
        isolated_worker_dry_run=True, **paths,
    )

    assert store.upserted == []
    assert store.failed == [
        (
            "filing-A",
            "validation_rejection_filing_unresolved",
            "validation_rejected",
        )
    ]
    summary = cli._validation_rejection_summary(metrics)
    assert summary["validation_rejected_record_count"] == 1
    assert summary["validation_rejection_filing_unresolved"] is True


def test_existing_failed_batch_still_prevents_all_upserted_updates(monkeypatch, tmp_path):
    paths = _isolated_paths(tmp_path)
    _mock_flush_batch(monkeypatch, _upsert_stats(failed_batches=1))
    metrics, store = _Metrics(), _Store()

    cli._flush_buffer(
        [{"segment_name": "Batch failure"}], ["filing-A"],
        batch_size=100, metrics=metrics, store=store,
        run_logger=_RunLogger(), dry_run_only=True,
        isolated_worker_dry_run=True, **paths,
    )

    assert store.upserted == []
    assert store.failed == []


def test_duplicate_requested_id_mapping_is_rejected_without_ticker_fallback():
    filings = [
        SimpleNamespace(
            filing_id="filing-A",
            requested_disclosure_no="requested-same",
        ),
        SimpleNamespace(
            filing_id="filing-B",
            requested_disclosure_no="requested-same",
        ),
    ]

    with pytest.raises(
        RuntimeError,
        match="STOP_BACKFILL_REJECTION_CALLER_ID_MAPPING_UNRESOLVED",
    ):
        cli._build_validation_filing_id_map(filings)


@pytest.mark.parametrize("escaped", ["decision_db", "state_db", "log_jsonl", "filing_list"])
def test_isolated_path_guard_rejects_every_escape_before_upsert(tmp_path, escaped):
    paths = _isolated_paths(tmp_path)
    paths[escaped + "_path" if escaped != "filing_list" else "filing_list_path"] = str(tmp_path / "outside" / f"{escaped}.db")
    with pytest.raises(RuntimeError, match="STOP_ISOLATED_SQLITE_PATH_ESCAPE"):
        cli._validate_isolated_write_paths(
            run_root=paths["isolated_run_root"],
            decision_db_path=paths["decision_db_path"],
            state_db_path=paths["state_db_path"],
            log_jsonl_path=paths["log_jsonl_path"],
            filing_list_path=paths["filing_list_path"],
        )


def test_isolated_path_guard_rejects_parent_traversal(tmp_path):
    paths = _isolated_paths(tmp_path)
    paths["decision_db_path"] = str(Path(paths["isolated_run_root"]) / "state" / ".." / ".." / "escaped.db")
    with pytest.raises(RuntimeError, match="STOP_ISOLATED_SQLITE_PATH_ESCAPE"):
        cli._validate_isolated_write_paths(
            run_root=paths["isolated_run_root"],
            decision_db_path=paths["decision_db_path"],
            state_db_path=paths["state_db_path"],
            log_jsonl_path=paths["log_jsonl_path"],
            filing_list_path=paths["filing_list_path"],
        )


def test_scoped_pending_filters_in_sql_and_preserves_non_scoped_contract():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = BackfillStateStore(str(Path(temp_dir) / "state.db"))
        try:
            manifest_queued = [f"manifest-q-{index}" for index in range(3)]
            for filing_id in manifest_queued:
                _insert_state(store, filing_id, status="queued")
            _insert_state(store, "manifest-quarantined", status="quarantined")
            for index in range(10):
                _insert_state(store, f"outside-q-{index}", status="queued")
            for index in range(5):
                _insert_state(store, f"outside-x-{index}", status="quarantined")

            requested_ids = [
                *manifest_queued,
                "manifest-quarantined",
                manifest_queued[0],
            ]
            scoped = store.get_pending_for_filing_ids(requested_ids)
            assert {row["filing_id"] for row in scoped} == set(manifest_queued)
            assert len(scoped) == len(manifest_queued)
            assert len(store.get_pending()) == 13
            assert store.get_pending_for_filing_ids([]) == []
            assert [
                row["filing_id"]
                for row in store.get_pending_for_filing_ids(
                    requested_ids, statuses=["quarantined"]
                )
            ] == ["manifest-quarantined"]
        finally:
            store.close()


def test_scoped_pending_supports_more_than_one_thousand_ids_without_duplicates():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = BackfillStateStore(str(Path(temp_dir) / "state.db"))
        try:
            filing_ids = [f"bulk-{index:04d}" for index in range(1005)]
            for filing_id in filing_ids:
                _insert_state(store, filing_id, status="queued")
            rows = store.get_pending_for_filing_ids([*filing_ids, *filing_ids[:20]])
            assert len(rows) == 1005
            assert len({row["filing_id"] for row in rows}) == 1005
        finally:
            store.close()


def test_single_requeue_is_atomic_and_preserves_audit_fields():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = BackfillStateStore(str(Path(temp_dir) / "state.db"))
        try:
            _insert_state(
                store,
                "target",
                status="quarantined",
                stage="segment_extraction_v4",
                attempt_count=7,
                error="missing_expected_period",
            )
            _insert_state(store, "outside", status="quarantined", error="outside")
            result = store.requeue_single_filing(
                "target",
                expected_stage="segment_extraction_v4",
                expected_error="missing_expected_period",
            )

            assert result["before"]["status"] == "quarantined"
            assert result["after"]["status"] == "queued"
            assert result["after"]["stage"] == "listing"
            assert result["after"]["attempt_count"] == 7
            assert result["after"]["last_attempt_at"] == "2026-07-13T00:00:00+09:00"
            assert result["after"]["via"] == "xbrl"
            assert result["after"]["segment_count"] == 8
            assert result["after"]["result_fingerprint"] == "fingerprint"
            for field in (
                "last_error", "last_error_stage", "review_hint",
                "started_at", "finished_at", "duration_ms",
            ):
                assert result["after"][field] is None
            outside = store.conn.execute(
                "SELECT status,last_error FROM filing_state WHERE filing_id='outside'"
            ).fetchone()
            assert tuple(outside) == ("quarantined", "outside")
        finally:
            store.close()


@pytest.mark.parametrize("status", ["running", "completed", "done", "upserted"])
def test_single_requeue_rejects_disallowed_source_status(status):
    with tempfile.TemporaryDirectory() as temp_dir:
        store = BackfillStateStore(str(Path(temp_dir) / "state.db"))
        try:
            _insert_state(store, "target", status=status)
            with pytest.raises(RuntimeError, match="STOP_REQUEUE_SOURCE_STATE_CHANGED"):
                store.requeue_single_filing("target")
            saved = store.conn.execute(
                "SELECT status FROM filing_state WHERE filing_id='target'"
            ).fetchone()[0]
            assert saved == status
        finally:
            store.close()


def test_single_requeue_rejects_missing_row_without_write():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = BackfillStateStore(str(Path(temp_dir) / "state.db"))
        try:
            with pytest.raises(RuntimeError, match="STOP_REQUEUE_SOURCE_STATE_CHANGED"):
                store.requeue_single_filing("missing")
            assert store.conn.execute("SELECT COUNT(*) FROM filing_state").fetchone()[0] == 0
        finally:
            store.close()


def test_single_requeue_rowcount_mismatch_rolls_back():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = BackfillStateStore(str(Path(temp_dir) / "state.db"))
        try:
            _insert_state(
                store, "target", status="quarantined",
                stage="segment_extraction_v4", error="missing_expected_period",
            )
            store.conn.execute(
                "CREATE TRIGGER ignore_target_requeue BEFORE UPDATE ON filing_state "
                "WHEN OLD.filing_id='target' BEGIN SELECT RAISE(IGNORE); END"
            )
            with pytest.raises(RuntimeError, match="expected one updated row"):
                store.requeue_single_filing(
                    "target",
                    expected_stage="segment_extraction_v4",
                    expected_error="missing_expected_period",
                )
            saved = store.conn.execute(
                "SELECT status,last_error FROM filing_state WHERE filing_id='target'"
            ).fetchone()
            assert tuple(saved) == ("quarantined", "missing_expected_period")
        finally:
            store.close()


def test_requeue_only_changes_one_existing_manifest_row_and_starts_no_worker(monkeypatch, tmp_path):
    state_db = tmp_path / "state.db"
    store = BackfillStateStore(str(state_db))
    _insert_state(
        store,
        "target-filing",
        ticker="1377",
        status="quarantined",
        stage="segment_extraction_v4",
        attempt_count=3,
        error="missing_expected_period",
    )
    _insert_state(store, "outside-quarantine", status="quarantined", error="outside")
    store.close()

    records = [
        _manifest_record(
            0,
            requested_id="20260609566520",
            filing_id="target-filing",
            ticker="1377",
        ),
        *[_manifest_record(index) for index in range(1, 30)],
    ]
    manifest = tmp_path / "chunk.json"
    _write_manifest(manifest, records)
    log_path = tmp_path / "must-not-exist.jsonl"

    def worker_forbidden(**kwargs):
        pytest.fail("requeue-only must not start run_backfill")

    _invoke(
        monkeypatch,
        [
            "--requeue-only",
            "--requeue-requested-id", "20260609566520",
            "--requeue-expected-stage", "segment_extraction_v4",
            "--requeue-expected-error", "missing_expected_period",
            "--filing-list", str(manifest),
            "--state-db", str(state_db),
            "--decision-db", str(tmp_path / "must-not-exist.db"),
            "--log-jsonl", str(log_path),
        ],
        worker_forbidden,
    )

    with sqlite3.connect(state_db) as conn:
        rows = conn.execute(
            "SELECT filing_id,status,attempt_count FROM filing_state ORDER BY filing_id"
        ).fetchall()
    assert rows == [
        ("outside-quarantine", "quarantined", 0),
        ("target-filing", "queued", 3),
    ]
    assert not (tmp_path / "must-not-exist.db").exists()
    assert not log_path.exists()


@pytest.mark.parametrize(
    ("records", "requested_id", "judgment"),
    [
        ([_manifest_record(1)], "missing", "STOP_REQUEUE_REQUESTED_ID_NOT_IN_MANIFEST"),
        (
            [
                _manifest_record(1, requested_id="duplicate", filing_id="one"),
                _manifest_record(2, requested_id="duplicate", filing_id="two"),
            ],
            "duplicate",
            "STOP_REQUEUE_REQUESTED_ID_NOT_UNIQUE",
        ),
    ],
)
def test_requeue_only_rejects_missing_or_duplicate_requested_id_without_state_write(
    records, requested_id, judgment, tmp_path
):
    manifest = tmp_path / "chunk.json"
    state_db = tmp_path / "state.db"
    _write_manifest(manifest, records)
    with pytest.raises(RuntimeError, match=judgment):
        cli._run_requeue_only(
            filing_list_path=str(manifest),
            state_db_path=str(state_db),
            requested_disclosure_no=requested_id,
        )
    assert not state_db.exists()


@pytest.mark.parametrize(
    "incompatible",
    ["--apply", "--dry-run", "--retry-quarantine", "--reset-target"],
)
def test_requeue_only_rejects_worker_and_global_retry_modes(
    monkeypatch, tmp_path, incompatible
):
    manifest = tmp_path / "chunk.json"
    _write_manifest(manifest, [_manifest_record(1)])
    args = [
        "--requeue-only", "--requeue-requested-id", "20260713000001",
        "--filing-list", str(manifest), "--state-db", str(tmp_path / "state.db"),
        incompatible,
    ]
    with pytest.raises(SystemExit):
        _invoke(monkeypatch, args)
    assert not (tmp_path / "state.db").exists()


def test_cli_passes_manifest_scope_and_require_all_flags(monkeypatch, tmp_path):
    manifest = tmp_path / "chunk.json"
    _write_manifest(manifest, [_manifest_record(1)])
    captured = {}

    def run_backfill(**kwargs):
        captured.update(kwargs)
        return {"summary": {}}

    _invoke(
        monkeypatch,
        [
            "--filing-list", str(manifest),
            "--scope-pending-to-manifest",
            "--require-all-manifest-pending",
        ],
        run_backfill,
    )
    assert captured["scope_pending_to_manifest"] is True
    assert captured["require_all_manifest_pending"] is True


def test_cli_exits_nonzero_when_validation_rejection_is_reported(monkeypatch, tmp_path):
    manifest = tmp_path / "chunk.json"
    _write_manifest(manifest, [_manifest_record(1)])

    def run_backfill(**kwargs):
        return {
            "summary": {
                "validation_rejected_record_count": 1,
                "validation_rejected_filing_count": 1,
                "validation_rejected_filing_ids": ["manifest-001"],
                "validation_reasons_by_filing": {
                    "manifest-001": {"invalid_quarter:INVALID": 1},
                },
            }
        }

    with pytest.raises(SystemExit) as exc_info:
        _invoke(monkeypatch, ["--filing-list", str(manifest)], run_backfill)

    assert exc_info.value.code == 1


def test_canonical_sync_exception_fails_filing_after_sqlite_commit(monkeypatch, tmp_path):
    stats = _upsert_stats(inserted=2, canonical_sync_ids=[61, 62])
    _mock_flush_batch(monkeypatch, stats)
    monkeypatch.setattr(
        cli,
        "_canonical_sync_ids_by_filing",
        lambda *args, **kwargs: {"filing-A": [61, 62]},
    )
    monkeypatch.setattr("lib.pipeline.db.load_env", lambda: None)
    monkeypatch.setattr(
        "lib.pipeline.db.get_supabase_write_config",
        lambda: {"rest_url": "https://example.invalid", "headers": {}},
    )

    def fail_sync(*args, **kwargs):
        raise RuntimeError("secret remote detail must not enter summary")

    monkeypatch.setattr("tools.sync_segments.sync_sqlite_segment_ids", fail_sync)
    metrics, store = _Metrics(), _Store()

    cli._flush_buffer(
        [{"_requested_disclosure_no": "requested-A"}],
        ["filing-A"],
        str(tmp_path / "apply.db"),
        100,
        metrics,
        store,
        _RunLogger(),
        dry_run_only=False,
        validation_filing_id_map={"requested-A": "filing-A"},
    )

    assert metrics.stats is stats
    assert store.upserted == []
    assert store.failed == [
        ("filing-A", "canonical_sync_exception", "canonical_sync_failed")
    ]
    assert cli._canonical_sync_failure_summary(metrics) == {
        "canonical_sync_failed_record_count": 2,
        "canonical_sync_failed_filing_count": 1,
        "canonical_sync_failed_filing_ids": ["filing-A"],
        "canonical_sync_failures_by_filing": {
            "filing-A": {"canonical_sync_exception": 2}
        },
        "canonical_sync_filing_unresolved": False,
    }


def test_canonical_sync_exception_preserves_real_sqlite_accepted_row(
    monkeypatch, tmp_path
):
    requested_id = "20260713591788"
    internal_id = "20260713340570"
    record = {
        "ticker": "4057", "period": "2026-05-31", "quarter": "FY",
        "segment_name": "Core", "segment_order": 1,
        "segment_sales": 100.0, "segment_profit": 20.0,
        "raw_profit_label": "operating profit", "source": "backfill_xbrl",
        "segment_name_norm": "core", "extractor_route": "xbrl",
        "source_doc_type": "earnings_summary", "disclosure_date": "2026-07-13",
        "tdnet_doc_id": internal_id, "row_type": "segment",
        "_identity_verified": True,
        "_identity_verdict": "official_linked_xbrl_match",
        "_requested_disclosure_no": requested_id,
        "_internal_document_id": internal_id,
        "_canonical_expected_period": "2026-05-31",
        "_canonical_expected_quarter": "FY",
        "_resolved_zip_sha256": "a" * 64,
        "_verified_xbrl_same_zip": True,
        "_worker_version": "v4", "_segment_period_role": "current",
    }
    db_path = tmp_path / "apply.db"
    monkeypatch.setattr("lib.pipeline.db.load_env", lambda: None)
    monkeypatch.setattr(
        "lib.pipeline.db.get_supabase_write_config",
        lambda: {"rest_url": "https://example.invalid", "headers": {}},
    )
    monkeypatch.setattr(
        "tools.sync_segments.sync_sqlite_segment_ids",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("remote failed")),
    )
    metrics, store = _Metrics(), _Store()

    cli._flush_buffer(
        [record], ["manifest-A"], str(db_path), 100,
        metrics, store, _RunLogger(), dry_run_only=False,
        validation_filing_id_map={requested_id: "manifest-A"},
    )

    with sqlite3.connect(db_path) as conn:
        saved = conn.execute(
            "SELECT id, tdnet_doc_id FROM segment_financials "
            "WHERE company_code='4057'"
        ).fetchall()
    assert len(saved) == 1
    assert saved[0][1] == internal_id
    assert metrics.stats.canonical_sync_ids == [saved[0][0]]
    assert store.upserted == []
    assert store.failed == [
        ("manifest-A", "canonical_sync_exception", "canonical_sync_failed")
    ]


def test_canonical_sync_separates_success_and_failure_by_filing(monkeypatch, tmp_path):
    stats = _upsert_stats(inserted=2, canonical_sync_ids=[71, 72])
    _mock_flush_batch(monkeypatch, stats)
    monkeypatch.setattr(
        cli,
        "_canonical_sync_ids_by_filing",
        lambda *args, **kwargs: {"filing-A": [71], "filing-B": [72]},
    )
    monkeypatch.setattr("lib.pipeline.db.load_env", lambda: None)
    monkeypatch.setattr(
        "lib.pipeline.db.get_supabase_write_config",
        lambda: {"rest_url": "https://example.invalid", "headers": {}},
    )
    calls = []

    def sync(db_path, ids, rest_url, headers, dry_run):
        calls.append(list(ids))
        if ids == [72]:
            return {"sync_error": "remote detail", "synced_segment_ids": []}
        return {"synced_segment_ids": ids}

    monkeypatch.setattr("tools.sync_segments.sync_sqlite_segment_ids", sync)
    metrics, store = _Metrics(), _Store()

    cli._flush_buffer(
        [{"segment_name": "A"}, {"segment_name": "B"}],
        ["filing-A", "filing-B"],
        str(tmp_path / "apply.db"),
        100,
        metrics,
        store,
        _RunLogger(),
        dry_run_only=False,
    )

    assert calls == [[71], [72]]
    assert store.upserted == ["filing-A"]
    assert store.failed == [
        ("filing-B", "canonical_sync_error", "canonical_sync_failed")
    ]
    assert cli._canonical_sync_failure_summary(metrics)[
        "canonical_sync_failed_filing_ids"
    ] == ["filing-B"]


@pytest.mark.parametrize(
    ("sync_result", "expected_reason"),
    [
        ({"sync_error": "remote detail", "synced_segment_ids": []}, "canonical_sync_error"),
        ({"synced_segment_ids": []}, "canonical_sync_readback_mismatch"),
    ],
)
def test_canonical_sync_formal_failure_signals_are_propagated(
    monkeypatch, tmp_path, sync_result, expected_reason
):
    _mock_flush_batch(monkeypatch, _upsert_stats(inserted=1, canonical_sync_ids=[81]))
    monkeypatch.setattr(
        cli,
        "_canonical_sync_ids_by_filing",
        lambda *args, **kwargs: {"filing-A": [81]},
    )
    monkeypatch.setattr("lib.pipeline.db.load_env", lambda: None)
    monkeypatch.setattr(
        "lib.pipeline.db.get_supabase_write_config",
        lambda: {"rest_url": "https://example.invalid", "headers": {}},
    )
    monkeypatch.setattr(
        "tools.sync_segments.sync_sqlite_segment_ids",
        lambda *args, **kwargs: sync_result,
    )
    metrics, store = _Metrics(), _Store()

    cli._flush_buffer(
        [{}], ["filing-A"], str(tmp_path / "apply.db"), 100,
        metrics, store, _RunLogger(), dry_run_only=False,
    )

    assert store.upserted == []
    assert store.failed == [("filing-A", expected_reason, "canonical_sync_failed")]


def test_canonical_sync_failures_aggregate_across_flushes(monkeypatch, tmp_path):
    pending_stats = [
        _upsert_stats(inserted=1, canonical_sync_ids=[91]),
        _upsert_stats(inserted=2, canonical_sync_ids=[92, 93]),
    ]
    _mock_flush_batch(monkeypatch, lambda *args, **kwargs: pending_stats.pop(0))
    monkeypatch.setattr(
        cli,
        "_canonical_sync_ids_by_filing",
        lambda db, ids, *args, **kwargs: {
            "filing-A" if ids == [91] else "filing-B": list(ids)
        },
    )
    monkeypatch.setattr("lib.pipeline.db.load_env", lambda: None)
    monkeypatch.setattr(
        "lib.pipeline.db.get_supabase_write_config",
        lambda: {"rest_url": "https://example.invalid", "headers": {}},
    )
    monkeypatch.setattr(
        "tools.sync_segments.sync_sqlite_segment_ids",
        lambda *args, **kwargs: {"sync_error": "remote detail"},
    )
    metrics, store = _Metrics(), _Store()

    for filing_id in ("filing-A", "filing-B"):
        cli._flush_buffer(
            [{}], [filing_id], str(tmp_path / "apply.db"), 100,
            metrics, store, _RunLogger(), dry_run_only=False,
        )

    summary = cli._canonical_sync_failure_summary(metrics)
    assert summary["canonical_sync_failed_record_count"] == 3
    assert summary["canonical_sync_failed_filing_count"] == 2
    assert summary["canonical_sync_failed_filing_ids"] == ["filing-A", "filing-B"]
    assert summary["canonical_sync_failures_by_filing"] == {
        "filing-A": {"canonical_sync_error": 1},
        "filing-B": {"canonical_sync_error": 2},
    }


def test_same_filing_failure_in_one_flush_blocks_later_success(monkeypatch, tmp_path):
    pending_stats = [
        _upsert_stats(inserted=1, canonical_sync_ids=[94]),
        _upsert_stats(no_change=1, canonical_sync_ids=[95]),
    ]
    _mock_flush_batch(monkeypatch, lambda *args, **kwargs: pending_stats.pop(0))
    monkeypatch.setattr(
        cli,
        "_canonical_sync_ids_by_filing",
        lambda db, ids, *args, **kwargs: {"filing-A": list(ids)},
    )
    monkeypatch.setattr("lib.pipeline.db.load_env", lambda: None)
    monkeypatch.setattr(
        "lib.pipeline.db.get_supabase_write_config",
        lambda: {"rest_url": "https://example.invalid", "headers": {}},
    )
    results = [
        {"sync_error": "first flush failed", "synced_segment_ids": []},
        {"synced_segment_ids": [95]},
    ]
    monkeypatch.setattr(
        "tools.sync_segments.sync_sqlite_segment_ids",
        lambda *args, **kwargs: results.pop(0),
    )
    metrics, store = _Metrics(), _Store()

    for _ in range(2):
        cli._flush_buffer(
            [{}], ["filing-A"], str(tmp_path / "apply.db"), 100,
            metrics, store, _RunLogger(), dry_run_only=False,
        )

    assert store.upserted == []
    assert store.failed == [
        ("filing-A", "canonical_sync_error", "canonical_sync_failed")
    ]
    assert cli._canonical_sync_failure_summary(metrics)[
        "canonical_sync_failed_filing_count"
    ] == 1


def test_later_filing_failure_does_not_rollback_prior_success(monkeypatch, tmp_path):
    pending_stats = [
        _upsert_stats(inserted=1, canonical_sync_ids=[96]),
        _upsert_stats(inserted=1, canonical_sync_ids=[97]),
    ]
    _mock_flush_batch(monkeypatch, lambda *args, **kwargs: pending_stats.pop(0))
    monkeypatch.setattr(
        cli,
        "_canonical_sync_ids_by_filing",
        lambda db, ids, records, mapping, fids: {fids[0]: list(ids)},
    )
    monkeypatch.setattr("lib.pipeline.db.load_env", lambda: None)
    monkeypatch.setattr(
        "lib.pipeline.db.get_supabase_write_config",
        lambda: {"rest_url": "https://example.invalid", "headers": {}},
    )

    def sync(db_path, ids, rest_url, headers, dry_run):
        if ids == [97]:
            raise RuntimeError("failure B")
        return {"synced_segment_ids": ids}

    monkeypatch.setattr("tools.sync_segments.sync_sqlite_segment_ids", sync)
    metrics, store = _Metrics(), _Store()

    for filing_id in ("filing-A", "filing-B"):
        cli._flush_buffer(
            [{}], [filing_id], str(tmp_path / "apply.db"), 100,
            metrics, store, _RunLogger(), dry_run_only=False,
        )

    assert store.upserted == ["filing-A"]
    assert store.failed == [
        ("filing-B", "canonical_sync_exception", "canonical_sync_failed")
    ]


def test_unresolved_canonical_sync_filing_fails_all_buffer_filings(monkeypatch, tmp_path):
    _mock_flush_batch(monkeypatch, _upsert_stats(inserted=1, canonical_sync_ids=[101]))
    monkeypatch.setattr(
        cli,
        "_canonical_sync_ids_by_filing",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("canonical_sync_filing_unresolved")
        ),
    )
    sync_calls = []
    monkeypatch.setattr(
        "tools.sync_segments.sync_sqlite_segment_ids",
        lambda *args, **kwargs: sync_calls.append(args),
    )
    metrics, store = _Metrics(), _Store()

    cli._flush_buffer(
        [{}, {}], ["filing-A", "filing-B"], str(tmp_path / "apply.db"), 100,
        metrics, store, _RunLogger(), dry_run_only=False,
    )

    assert sync_calls == []
    assert store.upserted == []
    assert store.failed == [
        ("filing-A", "canonical_sync_filing_unresolved", "canonical_sync_failed"),
        ("filing-B", "canonical_sync_filing_unresolved", "canonical_sync_failed"),
    ]
    summary = cli._canonical_sync_failure_summary(metrics)
    assert summary["canonical_sync_failed_record_count"] == 1
    assert summary["canonical_sync_failed_filing_count"] == 2
    assert summary["canonical_sync_filing_unresolved"] is True


def test_canonical_sync_row_ids_resolve_only_through_formal_identifiers():
    class Cursor:
        def fetchall(self):
            return [(111, "internal-A"), (112, "internal-B")]

    class Conn:
        def execute(self, sql, params):
            assert "tdnet_doc_id" in sql
            assert params == [111, 112]
            return Cursor()

    db = SimpleNamespace(_conn=Conn())
    records = [
        {
            "ticker": "same",
            "period": "2026-05-31",
            "_requested_disclosure_no": "requested-A",
            "_internal_document_id": "internal-A",
            "tdnet_doc_id": "internal-A",
        },
        {
            "ticker": "same",
            "period": "2026-05-31",
            "_requested_disclosure_no": "requested-B",
            "_internal_document_id": "internal-B",
            "tdnet_doc_id": "internal-B",
        },
    ]

    assert cli._canonical_sync_ids_by_filing(
        db,
        [111, 112],
        records,
        {"requested-A": "filing-A", "requested-B": "filing-B"},
        ["filing-A", "filing-B"],
    ) == {"filing-A": [111], "filing-B": [112]}


def test_cli_exits_nonzero_when_canonical_sync_failure_is_reported(
    monkeypatch, tmp_path
):
    manifest = tmp_path / "chunk.json"
    _write_manifest(manifest, [_manifest_record(1)])

    def run_backfill(**kwargs):
        return {
            "summary": {
                "canonical_sync_failed_record_count": 1,
                "canonical_sync_failed_filing_count": 1,
                "canonical_sync_failed_filing_ids": ["manifest-001"],
                "canonical_sync_failures_by_filing": {
                    "manifest-001": {"canonical_sync_exception": 1}
                },
                "canonical_sync_filing_unresolved": False,
            }
        }

    with pytest.raises(SystemExit) as exc_info:
        _invoke(monkeypatch, ["--filing-list", str(manifest)], run_backfill)

    assert exc_info.value.code == 1


def test_cli_rejects_require_all_without_manifest_scope(monkeypatch, tmp_path):
    manifest = tmp_path / "chunk.json"
    _write_manifest(manifest, [_manifest_record(1)])
    with pytest.raises(SystemExit):
        _invoke(
            monkeypatch,
            ["--filing-list", str(manifest), "--require-all-manifest-pending"],
        )


def test_cli_rejects_limit_with_require_all_manifest_pending(monkeypatch, tmp_path):
    manifest = tmp_path / "chunk.json"
    _write_manifest(manifest, [_manifest_record(1)])
    with pytest.raises(SystemExit):
        _invoke(
            monkeypatch,
            [
                "--filing-list", str(manifest),
                "--scope-pending-to-manifest",
                "--require-all-manifest-pending",
                "--limit", "1",
            ],
        )


def _run_scoped_fixture(monkeypatch, tmp_path, *, blocked_status=None, inject_outside=False):
    records = [_manifest_record(index) for index in range(30)]
    manifest = tmp_path / "chunk.json"
    state_db = tmp_path / "state.db"
    _write_manifest(manifest, records)
    store = BackfillStateStore(str(state_db))
    if blocked_status is None:
        _insert_state(
            store,
            records[0]["filing_id"],
            status="quarantined",
            stage="segment_extraction_v4",
            error="missing_expected_period",
        )
        store.requeue_single_filing(
            records[0]["filing_id"],
            expected_stage="segment_extraction_v4",
            expected_error="missing_expected_period",
        )
    else:
        _insert_state(store, records[0]["filing_id"], status=blocked_status)
    _insert_state(store, "outside-queued", status="queued")
    store.close()

    monkeypatch.setattr(
        "lib.backfill.canonical_filing_metadata.load_canonical_filing_metadata_index",
        lambda: {},
    )
    captured = {"worker_calls": 0, "pending": []}

    def runner(pending, filing_map, **kwargs):
        captured["worker_calls"] += 1
        captured["pending"] = [row["filing_id"] for row in pending]

    monkeypatch.setattr("lib.backfill.phase2_runner.run_phase2_v4", runner)
    if inject_outside:
        original = BackfillStateStore.get_pending_for_filing_ids

        def polluted(self, filing_ids, **kwargs):
            rows = original(self, filing_ids, **kwargs)
            rows.append({"filing_id": "outside-queued", "ticker": "9999"})
            return rows

        monkeypatch.setattr(BackfillStateStore, "get_pending_for_filing_ids", polluted)

    def execute():
        return cli.run_backfill(
            start_date="2026-07-13",
            end_date="2026-07-13",
            workers=1,
            state_db=str(state_db),
            decision_db_path=str(tmp_path / "decision.db"),
            log_jsonl_path=str(tmp_path / "run.jsonl"),
            manifest_dir=str(tmp_path / "manifests"),
            filing_list_path=str(manifest),
            worker_version="v4",
            dry_run_only=True,
            scope_pending_to_manifest=True,
            require_all_manifest_pending=True,
        )

    return records, state_db, captured, execute


def test_manifest_scoped_apply_registers_and_passes_only_thirty_manifest_rows(
    monkeypatch, tmp_path
):
    records, state_db, captured, execute = _run_scoped_fixture(monkeypatch, tmp_path)
    execute()
    assert captured["worker_calls"] == 1
    assert len(captured["pending"]) == 30
    assert set(captured["pending"]) == {record["filing_id"] for record in records}
    assert "outside-queued" not in captured["pending"]
    with sqlite3.connect(state_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM filing_state").fetchone()[0] == 31
        assert conn.execute(
            "SELECT status FROM filing_state WHERE filing_id='outside-queued'"
        ).fetchone()[0] == "queued"


@pytest.mark.parametrize("blocked_status", ["completed", "quarantined"])
def test_manifest_pending_guard_stops_before_worker_for_nonpending_manifest_row(
    monkeypatch, tmp_path, blocked_status
):
    _, _, captured, execute = _run_scoped_fixture(
        monkeypatch, tmp_path, blocked_status=blocked_status
    )
    with pytest.raises(RuntimeError, match="STOP_MANIFEST_PENDING_SET_MISMATCH"):
        execute()
    assert captured["worker_calls"] == 0


def test_manifest_pending_guard_stops_before_worker_on_outside_injection(
    monkeypatch, tmp_path
):
    _, _, captured, execute = _run_scoped_fixture(
        monkeypatch, tmp_path, inject_outside=True
    )
    with pytest.raises(RuntimeError, match="STOP_MANIFEST_PENDING_SET_MISMATCH"):
        execute()
    assert captured["worker_calls"] == 0
