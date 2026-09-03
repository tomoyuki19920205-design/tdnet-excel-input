from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.production_environment import (
    ProductionEnvironmentError,
    SUPABASE_WRITE_ENV_NAMES,
    bootstrap_production_write_environment,
)
from tests.ny_market_quality_fixture import payload as quality_payload
from tools import company_news_inbox_worker as worker
from tools import migrate_ny_market_20260903_after_hours_v3 as migration
from tools import sync_ny_market


def _clear_write_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SUPABASE_WRITE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _write_env(root: Path, *, url: str = "https://production.invalid", key: str = "secret-key") -> None:
    (root / ".env.local").write_text(
        f"SUPABASE_URL={url}\nSUPABASE_SERVICE_ROLE_KEY={key}\n",
        encoding="utf-8",
    )


def test_bootstrap_uses_explicit_root_independent_of_cwd(monkeypatch, tmp_path):
    _clear_write_environment(monkeypatch)
    production_root = tmp_path / "production"
    production_root.mkdir()
    _write_env(production_root)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _write_env(unrelated, url="https://wrong.invalid", key="wrong-secret")
    monkeypatch.chdir(unrelated)

    environment = bootstrap_production_write_environment(production_root)

    assert environment.production_root == production_root.resolve()
    assert environment.env_files == ((production_root / ".env.local").resolve(),)
    assert environment.safe_metadata()["required_env_names"] == list(SUPABASE_WRITE_ENV_NAMES)
    assert "secret-key" not in json.dumps(environment.safe_metadata())
    assert "wrong-secret" not in json.dumps(environment.safe_metadata())


def test_process_environment_wins_over_files(monkeypatch, tmp_path):
    production_root = tmp_path / "production"
    production_root.mkdir()
    _write_env(production_root, url="https://file.invalid", key="file-secret")
    monkeypatch.setenv("SUPABASE_URL", "https://process.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "process-secret")

    bootstrap_production_write_environment(production_root)

    assert worker.os.environ["SUPABASE_URL"] == "https://process.invalid"
    assert worker.os.environ["SUPABASE_SERVICE_ROLE_KEY"] == "process-secret"


def test_missing_environment_fails_before_worker_sqlite_write(monkeypatch, tmp_path):
    _clear_write_environment(monkeypatch)
    inbox = tmp_path / "data" / "news_inbox"
    inbox.mkdir(parents=True)
    payload_path = inbox / "ny_market_daily_20260902.json"
    payload_path.write_text(json.dumps(quality_payload(), ensure_ascii=False), encoding="utf-8")
    db_path = tmp_path / "decision.db"

    result = worker.run_once(
        worker.WorkerPaths.from_values(root=tmp_path, db=db_path),
        sync_func=lambda *_args: {},
    )

    assert result["status"] == "completed_with_errors"
    assert result["failed"] == 1
    assert payload_path.exists()
    assert not db_path.exists()
    log_text = (tmp_path / "data" / "news_work" / "logs" / "inbox_worker.jsonl").read_text(encoding="utf-8")
    assert "production_environment_failed" in log_text
    assert "SUPABASE_SERVICE_ROLE_KEY" in log_text


def test_worker_and_migration_share_the_same_bootstrap():
    assert worker.bootstrap_production_write_environment is bootstrap_production_write_environment
    assert migration.bootstrap_production_write_environment is bootstrap_production_write_environment


def test_successful_worker_log_never_contains_secret_value(monkeypatch, tmp_path):
    _clear_write_environment(monkeypatch)
    _write_env(tmp_path, key="never-log-this-secret")
    inbox = tmp_path / "data" / "news_inbox"
    inbox.mkdir(parents=True)
    (inbox / "ny_market_daily_20260902.json").write_text(
        json.dumps(quality_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        worker,
        "sync_ny_market",
        lambda *_args, **_kwargs: {
            "canonical_ny_market_reports": 1,
            "canonical_ny_market_report_runs": 1,
        },
    )

    result = worker.run_once(
        worker.WorkerPaths.from_values(root=tmp_path, db=tmp_path / "decision.db"),
        sync_func=lambda *_args: {},
    )

    assert result["status"] == "completed"
    log_text = (tmp_path / "data" / "news_work" / "logs" / "inbox_worker.jsonl").read_text(encoding="utf-8")
    assert "production_environment_ready" in log_text
    assert "never-log-this-secret" not in log_text


def test_missing_environment_error_contains_names_not_values(monkeypatch, tmp_path):
    _clear_write_environment(monkeypatch)
    with pytest.raises(ProductionEnvironmentError) as captured:
        bootstrap_production_write_environment(tmp_path)
    message = str(captured.value)
    assert "SUPABASE_URL" in message
    assert "SUPABASE_SERVICE_ROLE_KEY" in message
    assert "secret" not in message.lower()


class _Connection:
    def close(self) -> None:
        return None


def _configure_sync(monkeypatch, *, snapshots, upsert_results):
    monkeypatch.setattr(sync_ny_market, "bootstrap_production_write_environment", lambda _root: None)
    monkeypatch.setattr(sync_ny_market, "connect_db", lambda _path: _Connection())
    monkeypatch.setattr(sync_ny_market, "get_supabase_write_config", lambda: {"write": True})
    monkeypatch.setattr(sync_ny_market, "get_supabase_read_config", lambda: {"read": True})
    monkeypatch.setattr(
        sync_ny_market,
        "rows_for_sync",
        lambda _conn, table: [{"stable_key": "ny_market_daily:2026-09-03", "table": table}],
    )
    monkeypatch.setattr(
        sync_ny_market,
        "supabase_select",
        lambda table, *, params, config: snapshots[(table, params["stable_key"])],
    )
    calls = []

    def upsert(table, rows, **kwargs):
        calls.append((table, rows, kwargs))
        return upsert_results.pop(0)

    monkeypatch.setattr(sync_ny_market, "supabase_upsert", upsert)
    return calls


def test_targeted_sync_restores_existing_rows_after_partial_failure(monkeypatch, tmp_path):
    stable_key = "ny_market_daily:2026-09-03"
    tables = ("canonical_ny_market_reports", "canonical_ny_market_report_runs")
    snapshots = {
        (table, f"eq.{stable_key}"): [{"stable_key": stable_key, "old": table}]
        for table in tables
    }
    calls = _configure_sync(
        monkeypatch,
        snapshots=snapshots,
        upsert_results=[
            {"ok": True, "count": 1},
            {"ok": False, "error": "forced"},
            {"ok": True, "count": 1},
            {"ok": True, "count": 1},
        ],
    )
    deleted = []
    monkeypatch.setattr(sync_ny_market, "_delete_remote_rows", lambda *args: deleted.append(args) or True)

    with pytest.raises(RuntimeError, match="snapshot restored"):
        sync_ny_market.sync(
            tmp_path / "db.sqlite",
            stable_keys=[stable_key],
            production_root=tmp_path,
        )

    assert [call[0] for call in calls] == [tables[0], tables[1], tables[1], tables[0]]
    assert calls[-1][1][0]["old"] == tables[0]
    assert deleted == []


def test_targeted_sync_deletes_new_rows_after_partial_failure(monkeypatch, tmp_path):
    stable_key = "ny_market_daily:2026-09-03"
    tables = ("canonical_ny_market_reports", "canonical_ny_market_report_runs")
    snapshots = {(table, f"eq.{stable_key}"): [] for table in tables}
    _configure_sync(
        monkeypatch,
        snapshots=snapshots,
        upsert_results=[
            {"ok": True, "count": 1},
            {"ok": False, "error": "forced"},
        ],
    )
    deleted = []
    monkeypatch.setattr(
        sync_ny_market,
        "_delete_remote_rows",
        lambda table, key, config: deleted.append((table, key)) or True,
    )

    with pytest.raises(RuntimeError, match="snapshot restored"):
        sync_ny_market.sync(
            tmp_path / "db.sqlite",
            stable_keys=[stable_key],
            production_root=tmp_path,
        )

    assert deleted == [(tables[1], stable_key), (tables[0], stable_key)]
