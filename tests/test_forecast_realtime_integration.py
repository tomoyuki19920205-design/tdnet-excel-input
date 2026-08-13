from tools import filings_process


def _patch_actual_pipeline(monkeypatch):
    import lib.pipeline.canonical_sync as canonical_sync
    import tools.sqlite_to_supabase as sqlite_to_supabase

    monkeypatch.setattr(
        sqlite_to_supabase,
        "push_sqlite_to_supabase_targeted",
        lambda **kwargs: {"errors": 0, "target_keys": [["3032", "2027-03-31", "1Q"]]},
    )
    monkeypatch.setattr(
        canonical_sync,
        "sync_canonical",
        lambda **kwargs: {"status": "ok", "financials": {"written": 2}, "segments": {"written": 0}},
    )


def test_process_realtime_writes_actual_without_tdnet_forecast_canonical_sync(monkeypatch):
    import lib.pipeline.queue as queue

    jobs = {"tdnet_realtime_process": [{"id": 1, "target_id": "earnings-doc"}]}
    completed = []
    requested_job_types = []

    def take_jobs(job_type, **kwargs):
        requested_job_types.append(job_type)
        return jobs[job_type]

    monkeypatch.setattr(queue, "take_pending_jobs", take_jobs)
    monkeypatch.setattr(
        queue, "complete_job",
        lambda job_id, status="done", **kwargs: completed.append((job_id, status)),
    )
    _patch_actual_pipeline(monkeypatch)
    result = filings_process.run_realtime(dry_run=True, db_path="unused.db")
    assert requested_job_types == ["tdnet_realtime_process"]
    assert completed == [(1, "done")]
    assert result["queue_success"] == 1
    assert "forecast" not in result


def test_repair_cli_has_no_apply_capability(monkeypatch):
    import pytest
    from tools import repair_forecast_canonical

    monkeypatch.setattr("sys.argv", ["repair_forecast_canonical.py", "--apply"])
    with pytest.raises(SystemExit) as exc_info:
        repair_forecast_canonical.main()
    assert exc_info.value.code == 2


def test_tdnet_canonical_writer_and_queue_are_not_reachable():
    import inspect
    import lib.pipeline.forecast_sync as forecast_sync
    import src.events.event_pipeline as event_pipeline

    assert not hasattr(forecast_sync, "sync_realtime_forecasts")
    assert "tdnet_realtime_forecast" not in inspect.getsource(event_pipeline)
    assert "tdnet_realtime_forecast" not in inspect.getsource(filings_process.run_realtime)


def test_jquants_nightly_canonical_row_expansion_is_preserved():
    from lib.pipeline.forecast_sync import ForecastDTO, expand_forecast_rows

    rows = expand_forecast_rows([ForecastDTO(
        ticker="3032",
        forecast_period_end="2027-03-31",
        metric="sales",
        value=7000,
        disclosure_datetime="2026-05-13",
        filing_id="",
        source="jquants_nxf",
        correction_flag=False,
        forecast_horizon="next_fy",
        accounting_standard="UNKNOWN",
        document_type="jquants_forecast",
    )])
    assert len(rows) == 1
    assert rows[0]["source"] == "jquants_nxf"
    assert rows[0]["source_priority"] == 10
    assert rows[0]["source_row_key"].startswith("cf|3032|2027-03-31|FY|sales|jquants_nxf|")
