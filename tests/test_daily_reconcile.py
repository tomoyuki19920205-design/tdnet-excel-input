from __future__ import annotations

from datetime import datetime, timedelta, timezone

import tools.daily_reconcile as reconcile


PREVIOUS = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
CURRENT = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)


def _job(job_id: int, finished_at, *, attempts: int = 4) -> dict:
    if isinstance(finished_at, datetime):
        finished_at = finished_at.isoformat()
    return {
        "id": job_id,
        "job_type": "tdnet_realtime_process",
        "target_id": str(job_id),
        "error_message": "permanent test failure",
        "attempts": attempts,
        "finished_at": finished_at,
    }


def _classify(
    monkeypatch,
    jobs: list[dict],
    *,
    open_issue_rows: list[dict] | None = None,
    fail_page_offset: int | None = None,
    fail_issue_lookup: bool = False,
    upsert_ok: bool = True,
):
    writes = []

    def fake_select(table, *, params, config):
        if table == "job_queue":
            offset = int(params["offset"])
            if fail_page_offset == offset:
                raise reconcile.ReconcileReadError("page failed")
            limit = int(params["limit"])
            return jobs[offset : offset + limit]
        if table == "data_quality_issues":
            if fail_issue_lookup:
                raise reconcile.ReconcileReadError("issue lookup failed")
            rows = list(open_issue_rows or [])
            offset = int(params["offset"])
            limit = int(params["limit"])
            return rows[offset : offset + limit]
        raise AssertionError(f"unexpected table: {table}")

    def fake_upsert(table, payload, **kwargs):
        writes.append((table, payload, kwargs))
        return {"ok": upsert_ok, "status": 201 if upsert_ok else 500, "error": None}

    monkeypatch.setattr(reconcile, "_required_select", fake_select)
    monkeypatch.setattr(reconcile, "supabase_upsert", fake_upsert)
    monkeypatch.setattr(reconcile, "supabase_update", lambda *args, **kwargs: True)
    result = reconcile.check_failed_jobs(
        {"rest_url": "https://invalid.test", "headers": {}},
        False,
        previous_cutoff=PREVIOUS,
        current_cutoff=CURRENT,
    )
    return result, writes


def test_existing_100_new_zero(monkeypatch):
    jobs = [_job(i, PREVIOUS - timedelta(minutes=1)) for i in range(100)]
    result, writes = _classify(monkeypatch, jobs)
    assert result["status"] == "success"
    assert result["existing_backlog"] == 100
    assert result["new_critical"] == 0
    assert result["issue_rows_created"] == 0
    assert writes == []


def test_existing_100_new_one_creates_one_issue(monkeypatch):
    jobs = [_job(i, PREVIOUS) for i in range(100)]
    jobs.append(_job(100, PREVIOUS + timedelta(seconds=1)))
    result, writes = _classify(monkeypatch, jobs)
    assert result["status"] == "success"
    assert result["existing_backlog"] == 100
    assert result["new_critical"] == 1
    assert result["issue_rows_created"] == 1
    assert len(writes) == 1


def test_existing_open_issue_is_reused(monkeypatch):
    result, writes = _classify(
        monkeypatch,
        [_job(1, CURRENT)],
        open_issue_rows=[{"id": 10}],
    )
    assert result["issue_rows_created"] == 0
    assert result["issue_rows_reused"] == 1
    assert writes == []


def test_duplicate_open_issues_are_reported_without_insert(monkeypatch):
    result, writes = _classify(
        monkeypatch,
        [_job(1, CURRENT)],
        open_issue_rows=[{"id": 10}, {"id": 11}, {"id": 12}],
    )
    assert result["status"] == "success"
    assert result["issue_rows_reused"] == 1
    assert result["duplicate_open_issues"] == 2
    assert writes == []


def test_duplicate_open_issue_count_is_paged(monkeypatch):
    existing = [{"id": i} for i in range(1001)]
    result, writes = _classify(
        monkeypatch,
        [_job(1, CURRENT)],
        open_issue_rows=existing,
    )
    assert result["issue_rows_reused"] == 1
    assert result["duplicate_open_issues"] == 1000
    assert writes == []


def test_all_201_failed_jobs_are_scanned(monkeypatch):
    jobs = [_job(i, PREVIOUS) for i in range(201)]
    result, _ = _classify(monkeypatch, jobs)
    assert result["failed_jobs_total"] == 201
    assert result["reconcile_scanned"] == 201
    assert result["has_more"] is False


def test_paging_over_1000_fetches_every_unique_job(monkeypatch):
    jobs = [_job(i, PREVIOUS) for i in range(1001)]
    result, _ = _classify(monkeypatch, jobs)
    assert result["status"] == "success"
    assert result["reconcile_scanned"] == 1001
    assert result["existing_backlog"] == 1001


def test_previous_cutoff_missing_fails_closed(monkeypatch):
    monkeypatch.setattr(reconcile, "_utc_now", lambda: CURRENT)
    monkeypatch.setattr(reconcile, "load_env", lambda *_: None)
    monkeypatch.setattr(reconcile, "get_supabase_config", lambda: {})
    monkeypatch.setattr(reconcile, "_get_previous_reconcile_cutoff", lambda *_: None)
    result = reconcile.run(dry_run=True)
    assert result["status"] == "failed"
    assert result["classification_status"] == "unknown"
    assert result["new_critical"] is None
    assert result["existing_backlog"] is None


def test_missing_finished_at_is_unclassified(monkeypatch):
    result, _ = _classify(monkeypatch, [_job(1, None)])
    assert result["status"] == "failed"
    assert result["classification_status"] == "unknown"
    assert result["unclassified_failed"] == 1


def test_future_finished_at_fails_closed(monkeypatch):
    result, _ = _classify(monkeypatch, [_job(1, CURRENT + timedelta(microseconds=1))])
    assert result["status"] == "failed"
    assert result["unclassified_failed"] == 1


def test_finished_at_equal_previous_is_existing(monkeypatch):
    result, _ = _classify(monkeypatch, [_job(1, PREVIOUS)])
    assert result["existing_backlog"] == 1
    assert result["new_critical"] == 0


def test_finished_at_equal_current_is_new(monkeypatch):
    result, _ = _classify(monkeypatch, [_job(1, CURRENT)])
    assert result["new_critical"] == 1
    assert result["status"] == "success"


def test_retried_existing_job_with_new_finished_at_is_new(monkeypatch):
    result, _ = _classify(
        monkeypatch,
        [_job(99, PREVIOUS + timedelta(minutes=10), attempts=5)],
    )
    assert result["new_critical"] == 1


def test_paging_failure_fails_closed(monkeypatch):
    jobs = [_job(i, PREVIOUS) for i in range(1001)]
    result, _ = _classify(monkeypatch, jobs, fail_page_offset=1000)
    assert result["status"] == "failed"
    assert result["classification_status"] == "unknown"
    assert result["has_more"] is True


def test_issue_lookup_failure_fails_closed(monkeypatch):
    result, _ = _classify(
        monkeypatch,
        [_job(1, CURRENT)],
        fail_issue_lookup=True,
    )
    assert result["status"] == "failed"
    assert result["issue_write_status"] == "failed"


def test_issue_save_failure_fails_closed(monkeypatch):
    result, writes = _classify(monkeypatch, [_job(1, CURRENT)], upsert_ok=False)
    assert len(writes) == 1
    assert result["status"] == "failed"
    assert result["issue_write_status"] == "failed"


def test_reauditing_same_existing_jobs_never_writes_issues(monkeypatch):
    jobs = [_job(i, PREVIOUS - timedelta(seconds=1)) for i in range(100)]
    first, first_writes = _classify(monkeypatch, jobs)
    second, second_writes = _classify(monkeypatch, jobs)
    assert first["new_critical"] == second["new_critical"] == 0
    assert first_writes == second_writes == []


def test_current_cutoff_is_captured_once(monkeypatch):
    calls = []

    def now():
        calls.append(1)
        return CURRENT

    monkeypatch.setattr(reconcile, "_utc_now", now)
    monkeypatch.setattr(reconcile, "load_env", lambda *_: None)
    monkeypatch.setattr(reconcile, "get_supabase_config", lambda: {})
    monkeypatch.setattr(reconcile, "_get_previous_reconcile_cutoff", lambda *_: None)
    reconcile.run(dry_run=True)
    assert len(calls) == 1
