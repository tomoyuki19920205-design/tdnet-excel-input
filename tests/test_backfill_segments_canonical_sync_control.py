from types import SimpleNamespace
import sys

import pytest

import tools.backfill_segments_tdnet as cli


class Metrics:
    def __init__(self):
        self.upserted_count = 0
        self.stats = None

    def record_upsert(self, stats):
        self.stats = stats

    def summary_dict(self):
        return {"upserted": self.upserted_count, "upsert_failed_batches": 0}


class Store:
    def __init__(self):
        self.upserted = []
        self.failed = []

    def mark_upserted(self, filing_id):
        self.upserted.append(filing_id)

    def mark_failed(self, filing_id, *, error="", stage="unknown"):
        self.failed.append((filing_id, error, stage))


class RunLogger:
    def __init__(self):
        self.events = []

    def log_upsert(self, filing_id, detail):
        self.events.append((filing_id, detail))


def stats(*, ids=(), failed_batches=0):
    return SimpleNamespace(
        inserted=len(ids),
        updated=0,
        no_change=0,
        rejected_lower_priority=0,
        rejected_filing_conflict=0,
        rejected_filing_identity_unresolved=0,
        failed_batches=failed_batches,
        canonical_sync_ids=list(ids),
        validation_rejected_record_count=0,
        validation_rejected_filing_count=0,
        validation_rejected_filing_ids=[],
        validation_reasons_by_filing={},
    )


def install_flush_mocks(monkeypatch, result):
    class DB:
        def __init__(self, path):
            self.path = path

        def close(self):
            pass

    monkeypatch.setattr("src.migration.migration_db.MigrationDB", DB)
    monkeypatch.setattr(cli, "batch_upsert_segments", lambda *args, **kwargs: result)


def invoke(monkeypatch, args, run_backfill=None):
    monkeypatch.setattr(sys, "argv", ["backfill_segments_tdnet.py", *args])
    if run_backfill is not None:
        monkeypatch.setattr(cli, "run_backfill", run_backfill)
    cli.main()


def test_default_mode_is_auto():
    assert cli._resolve_canonical_sync_mode(
        "auto", None, production_apply=False
    ) == "auto"


@pytest.mark.parametrize(
    ("mode", "confirm", "production_apply"),
    [
        ("disabled", "disabled", True),
        ("disabled", None, False),
        ("auto", None, True),
        ("auto", "auto", True),
    ],
)
def test_valid_mode_contracts(mode, confirm, production_apply):
    assert cli._resolve_canonical_sync_mode(
        mode, confirm, production_apply=production_apply
    ) == mode


@pytest.mark.parametrize(
    ("mode", "confirm", "production_apply"),
    [
        ("disabled", None, True),
        ("disabled", "auto", True),
        ("auto", "disabled", False),
        ("typo", None, False),
    ],
)
def test_invalid_mode_contracts_fail_closed(mode, confirm, production_apply):
    with pytest.raises(ValueError):
        cli._resolve_canonical_sync_mode(
            mode, confirm, production_apply=production_apply
        )


def test_cli_default_and_explicit_auto_are_forwarded(monkeypatch):
    modes = []

    def run_backfill(**kwargs):
        modes.append(kwargs["canonical_sync_mode"])
        return {"summary": {}}

    invoke(monkeypatch, [], run_backfill)
    invoke(monkeypatch, ["--canonical-sync-mode", "auto"], run_backfill)
    assert modes == ["auto", "auto"]


def test_cli_disabled_apply_requires_and_forwards_confirmation(monkeypatch):
    captured = {}
    monkeypatch.setenv("ALLOW_BACKFILL_XBRL_WRITE", "1")

    def run_backfill(**kwargs):
        captured.update(kwargs)
        return {"summary": {}}

    invoke(
        monkeypatch,
        [
            "--apply",
            "--canonical-sync-mode", "disabled",
            "--confirm-canonical-sync-mode", "disabled",
        ],
        run_backfill,
    )
    assert captured["canonical_sync_mode"] == "disabled"
    assert captured["dry_run_only"] is False


@pytest.mark.parametrize(
    "args",
    [
        ["--apply", "--canonical-sync-mode", "disabled"],
        [
            "--apply", "--canonical-sync-mode", "disabled",
            "--confirm-canonical-sync-mode", "auto",
        ],
        ["--canonical-sync-mode", "auto", "--confirm-canonical-sync-mode", "disabled"],
        ["--canonical-sync-mode", "invalid"],
    ],
)
def test_cli_mode_errors_stop_before_run_backfill(monkeypatch, tmp_path, args):
    called = []
    state_db = tmp_path / "must-not-exist.db"
    monkeypatch.setenv("ALLOW_BACKFILL_XBRL_WRITE", "1")
    with pytest.raises(SystemExit):
        invoke(
            monkeypatch,
            ["--state-db", str(state_db), *args],
            lambda **kwargs: called.append(kwargs),
        )
    assert called == []
    assert not state_db.exists()


def test_existing_apply_environment_confirmation_is_still_required(monkeypatch):
    called = []
    monkeypatch.delenv("ALLOW_BACKFILL_XBRL_WRITE", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        invoke(monkeypatch, ["--apply"], lambda **kwargs: called.append(kwargs))
    assert exc_info.value.code == 1
    assert called == []


def test_disabled_flush_writes_sqlite_and_state_without_supabase(monkeypatch, tmp_path):
    install_flush_mocks(monkeypatch, stats(ids=[41, 42]))
    calls = {"load_env": 0, "config": 0, "sync": 0, "map": 0}

    def forbidden(name):
        def call(*args, **kwargs):
            calls[name] += 1
            raise AssertionError(f"disabled mode called {name}")
        return call

    monkeypatch.setattr("lib.pipeline.db.load_env", forbidden("load_env"))
    monkeypatch.setattr("lib.pipeline.db.get_supabase_write_config", forbidden("config"))
    monkeypatch.setattr("tools.sync_segments.sync_sqlite_segment_ids", forbidden("sync"))
    monkeypatch.setattr(cli, "_canonical_sync_ids_by_filing", forbidden("map"))
    metrics, store, logger = Metrics(), Store(), RunLogger()

    cli._flush_buffer(
        [{"segment_name": "A"}],
        ["filing-A"],
        str(tmp_path / "decision.db"),
        100,
        metrics,
        store,
        logger,
        dry_run_only=False,
        canonical_sync_mode="disabled",
    )

    assert calls == {"load_env": 0, "config": 0, "sync": 0, "map": 0}
    assert metrics.stats.canonical_sync_ids == [41, 42]
    assert store.upserted == ["filing-A"]
    assert store.failed == []
    assert cli._canonical_sync_control_summary(metrics) == {
        "canonical_sync_mode": "disabled",
        "canonical_sync_requested": False,
        "canonical_sync_attempted": False,
        "canonical_sync_ids_count": 2,
        "canonical_rows_written": 0,
        "supabase_config_loaded": False,
    }
    assert logger.events[0][1]["canonical_sync_mode"] == "disabled"


def test_disabled_flush_with_no_sync_ids_has_complete_summary(monkeypatch, tmp_path):
    install_flush_mocks(monkeypatch, stats())
    metrics = Metrics()
    cli._flush_buffer(
        [{}], ["filing-A"], str(tmp_path / "decision.db"), 100,
        metrics, Store(), RunLogger(), dry_run_only=False,
        canonical_sync_mode="disabled",
    )
    assert cli._canonical_sync_control_summary(metrics) == {
        "canonical_sync_mode": "disabled",
        "canonical_sync_requested": False,
        "canonical_sync_attempted": False,
        "canonical_sync_ids_count": 0,
        "canonical_rows_written": 0,
        "supabase_config_loaded": False,
    }


def test_auto_flush_keeps_existing_supabase_and_sync_path(monkeypatch, tmp_path):
    install_flush_mocks(monkeypatch, stats(ids=[51]))
    calls = {"load_env": 0, "config": 0, "sync": 0}
    monkeypatch.setattr(
        cli, "_canonical_sync_ids_by_filing",
        lambda *args, **kwargs: {"filing-A": [51]},
    )
    monkeypatch.setattr(
        "lib.pipeline.db.load_env",
        lambda: calls.__setitem__("load_env", calls["load_env"] + 1),
    )

    def config():
        calls["config"] += 1
        return {"rest_url": "https://example.invalid", "headers": {}}

    def sync(db_path, ids, rest_url, headers, dry_run):
        calls["sync"] += 1
        return {"synced_segment_ids": list(ids)}

    monkeypatch.setattr("lib.pipeline.db.get_supabase_write_config", config)
    monkeypatch.setattr("tools.sync_segments.sync_sqlite_segment_ids", sync)
    metrics, store = Metrics(), Store()

    cli._flush_buffer(
        [{}], ["filing-A"], str(tmp_path / "decision.db"), 100,
        metrics, store, RunLogger(), dry_run_only=False,
        canonical_sync_mode="auto",
    )

    assert calls == {"load_env": 1, "config": 1, "sync": 1}
    assert store.upserted == ["filing-A"]
    assert cli._canonical_sync_control_summary(metrics) == {
        "canonical_sync_mode": "auto",
        "canonical_sync_requested": True,
        "canonical_sync_attempted": True,
        "canonical_sync_ids_count": 1,
        "canonical_rows_written": 1,
        "supabase_config_loaded": True,
    }


def test_failed_batch_never_reaches_canonical_branch(monkeypatch, tmp_path):
    install_flush_mocks(monkeypatch, stats(ids=[61], failed_batches=1))
    monkeypatch.setattr(
        cli, "_canonical_sync_ids_by_filing",
        lambda *args, **kwargs: pytest.fail("failed worker/upsert must not map sync ids"),
    )
    metrics, store = Metrics(), Store()
    cli._flush_buffer(
        [{}], ["filing-A"], str(tmp_path / "decision.db"), 100,
        metrics, store, RunLogger(), dry_run_only=False,
        canonical_sync_mode="auto",
    )
    summary = cli._canonical_sync_control_summary(metrics)
    assert summary["canonical_sync_requested"] is True
    assert summary["canonical_sync_attempted"] is False
    assert store.upserted == []


def test_run_backfill_rejects_invalid_mode_before_state_store_creation(tmp_path):
    state_db = tmp_path / "state.db"
    with pytest.raises(ValueError):
        cli.run_backfill(
            start_date="2026-01-01",
            end_date="2026-01-02",
            state_db=str(state_db),
            canonical_sync_mode="typo",
        )
    assert not state_db.exists()

