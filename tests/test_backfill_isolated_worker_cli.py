from types import SimpleNamespace
from pathlib import Path

import pytest

import tools.backfill_segments_tdnet as cli


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
