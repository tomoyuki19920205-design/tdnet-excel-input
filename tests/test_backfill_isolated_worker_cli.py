from types import SimpleNamespace
from pathlib import Path
import json
import sqlite3
import tempfile

import pytest

import tools.backfill_segments_tdnet as cli
from lib.backfill.state_store import BackfillStateStore


class _Metrics:
    def __init__(self):
        self.stats = None

    def record_upsert(self, stats):
        self.stats = stats


class _Store:
    def __init__(self):
        self.upserted = []

    def mark_upserted(self, filing_id):
        self.upserted.append(filing_id)


class _RunLogger:
    def __init__(self):
        self.events = []

    def log_upsert(self, filing_id, detail):
        self.events.append((filing_id, detail))


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
    assert run_logger.events[0][1]["canonical_sync_ids"] == [41]


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
