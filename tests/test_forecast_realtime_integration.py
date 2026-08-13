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


def test_process_realtime_writes_actual_and_forecast_and_completes_both_queues(monkeypatch):
    import lib.pipeline.forecast_sync as forecast_sync
    import lib.pipeline.queue as queue

    jobs = {
        "tdnet_realtime_process": [{"id": 1, "target_id": "earnings-doc"}],
        "tdnet_realtime_forecast": [{"id": 2, "target_id": "revision-doc"}],
    }
    completed = []
    monkeypatch.setattr(queue, "take_pending_jobs", lambda job_type, **kwargs: jobs[job_type])
    monkeypatch.setattr(
        queue, "complete_job",
        lambda job_id, status="done", **kwargs: completed.append((job_id, status)),
    )
    _patch_actual_pipeline(monkeypatch)
    calls = []

    def fake_forecast(**kwargs):
        calls.append(kwargs)
        return {
            "forecast_rows": 4, "written": 4, "errors": 0, "quarantined": 0,
            "earnings_quarantine_ids": [], "revision_quarantine_ids": [],
            "revision_disclosure_ids": ["revision-doc"],
        }

    monkeypatch.setattr(forecast_sync, "sync_realtime_forecasts", fake_forecast)
    result = filings_process.run_realtime(dry_run=True, db_path="unused.db")
    assert calls[0]["earnings_disclosure_ids"] == ["earnings-doc"]
    assert calls[0]["revision_disclosure_ids"] == ["revision-doc"]
    assert completed == [(1, "done"), (2, "done")]
    assert result["queue_success"] == 2
    assert result["forecast"]["forecast_rows"] == 4


def test_bad_revision_does_not_fail_unrelated_actual_job(monkeypatch):
    import lib.pipeline.forecast_sync as forecast_sync
    import lib.pipeline.queue as queue

    jobs = {
        "tdnet_realtime_process": [{"id": 1, "target_id": "earnings-doc"}],
        "tdnet_realtime_forecast": [{"id": 2, "target_id": "bad-revision"}],
    }
    completed = []
    monkeypatch.setattr(queue, "take_pending_jobs", lambda job_type, **kwargs: jobs[job_type])
    monkeypatch.setattr(
        queue, "complete_job",
        lambda job_id, status="done", **kwargs: completed.append((job_id, status)),
    )
    _patch_actual_pipeline(monkeypatch)
    monkeypatch.setattr(
        forecast_sync,
        "sync_realtime_forecasts",
        lambda **kwargs: {
            "forecast_rows": 0, "written": 0, "errors": 0, "quarantined": 1,
            "earnings_quarantine_ids": [],
            "revision_quarantine_ids": ["bad-revision"],
            "revision_disclosure_ids": [],
        },
    )
    result = filings_process.run_realtime(dry_run=True, db_path="unused.db")
    assert completed == [(1, "done"), (2, "failed")]
    assert result["queue_success"] == 1
    assert result["queue_failed"] == 1
