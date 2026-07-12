from types import SimpleNamespace

import pytest

import tools.backfill_segments_tdnet as cli


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
