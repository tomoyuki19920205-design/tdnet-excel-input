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


def _run_summary(
    monkeypatch,
    *,
    existing: int,
    new: int,
    stuck: int = 0,
    rebuild: int = 0,
    duplicates: int = 0,
    created: int = 0,
    reused: int = 0,
    duplicate_open: int = 0,
    classification_status: str = "complete",
):
    failed_status = "success" if classification_status == "complete" else "failed"
    failed = {
        "status": failed_status,
        "failed_jobs_total": existing + new,
        "reconcile_scanned": existing + new,
        "existing_backlog": existing,
        "new_critical": new,
        "unclassified_failed": 0 if classification_status == "complete" else 1,
        "issue_rows_created": created,
        "issue_rows_reused": reused,
        "duplicate_open_issues": duplicate_open,
        "classification_status": classification_status,
        "issue_write_status": "success",
        "has_more": False,
        "oldest_failed_at": PREVIOUS.isoformat(),
        "latest_failed_at": CURRENT.isoformat(),
        "reason_counts": {},
    }
    monkeypatch.setattr(reconcile, "_utc_now", lambda: CURRENT)
    monkeypatch.setattr(reconcile, "load_env", lambda *_: None)
    monkeypatch.setattr(reconcile, "get_supabase_config", lambda: {})
    monkeypatch.setattr(reconcile, "_get_previous_reconcile_cutoff", lambda *_: PREVIOUS)
    monkeypatch.setattr(reconcile, "check_stuck_jobs", lambda *_: stuck)
    monkeypatch.setattr(reconcile, "check_failed_jobs", lambda *args, **kwargs: failed)
    monkeypatch.setattr(reconcile, "check_rebuild_backlog", lambda *_: rebuild)
    monkeypatch.setattr(reconcile, "check_financials_duplicates", lambda *_: duplicates)
    monkeypatch.setattr(reconcile, "check_quarantine_spike", lambda *_: 999)
    return reconcile.run(dry_run=True)


def test_issue_total_excludes_existing_100(monkeypatch):
    result = _run_summary(monkeypatch, existing=100, new=0)
    assert result["existing_backlog"] == 100
    assert result["new_critical"] == 0
    assert result["non_failed_issues_total"] == 0
    assert result["failed_issue_contribution"] == 0
    assert result["issues_total"] == 0


def test_issue_total_excludes_existing_201(monkeypatch):
    result = _run_summary(monkeypatch, existing=201, new=0)
    assert result["issues_total"] == 0


def test_one_new_critical_contributes_one(monkeypatch):
    result = _run_summary(monkeypatch, existing=100, new=1)
    assert result["failed_issue_contribution"] == 1
    assert result["issues_total"] == 1


def test_new_three_plus_other_two_totals_five(monkeypatch):
    result = _run_summary(monkeypatch, existing=100, new=3, stuck=1, rebuild=1)
    assert result["non_failed_issues_total"] == 2
    assert result["issues_total"] == 5


def test_other_nine_remains_nine(monkeypatch):
    result = _run_summary(monkeypatch, existing=100, new=0, stuck=9)
    assert result["issues_total"] == 9


def test_other_ten_remains_ten(monkeypatch):
    result = _run_summary(monkeypatch, existing=100, new=0, rebuild=10)
    assert result["issues_total"] == 10


def test_created_issue_rows_do_not_affect_total(monkeypatch):
    result = _run_summary(monkeypatch, existing=100, new=1, created=7)
    assert result["issue_rows_created"] == 7
    assert result["issues_total"] == 1


def test_reused_issue_rows_do_not_affect_total(monkeypatch):
    result = _run_summary(monkeypatch, existing=100, new=1, reused=8)
    assert result["issue_rows_reused"] == 8
    assert result["issues_total"] == 1


def test_duplicate_open_issues_do_not_affect_total(monkeypatch):
    result = _run_summary(monkeypatch, existing=100, new=1, duplicate_open=12)
    assert result["duplicate_open_issues"] == 12
    assert result["issues_total"] == 1


def test_unknown_classification_keeps_failed_status(monkeypatch):
    result = _run_summary(
        monkeypatch,
        existing=100,
        new=0,
        classification_status="unknown",
    )
    assert result["classification_status"] == "unknown"
    assert result["status"] == "failed"


def test_each_existing_non_failed_component_is_included(monkeypatch):
    result = _run_summary(
        monkeypatch,
        existing=100,
        new=0,
        stuck=2,
        rebuild=3,
        duplicates=4,
    )
    assert result["non_failed_issues_total"] == 9
    assert result["issues_total"] == 9
    assert result["checks"] == {
        "stuck_jobs": 2,
        "failed_jobs": 100,
        "rebuild_backlog": 3,
        "financials_duplicates": 4,
        "quarantine_today": 999,
    }
