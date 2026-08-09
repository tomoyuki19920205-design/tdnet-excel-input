from __future__ import annotations

from unittest.mock import patch

from lib.pipeline.financial_recovery_enqueue import _terminal_retry_exists


def test_failed_financial_recovery_job_is_a_terminal_enqueue_guard():
    row = {"disclosure_id": "a" * 64}
    with patch(
        "lib.pipeline.financial_recovery_enqueue.supabase_select",
        return_value=[{"id": 7, "attempts": 3, "status": "failed"}],
    ) as select:
        assert _terminal_retry_exists(row) is True
    params = select.call_args.kwargs["params"]
    assert params["job_type"] == "eq.tdnet_financial_recovery"
    assert params["target_id"] == f"eq.{row['disclosure_id']}"
    assert params["status"] == "eq.failed"


def test_missing_disclosure_identity_does_not_query_terminal_jobs():
    with patch(
        "lib.pipeline.financial_recovery_enqueue.supabase_select"
    ) as select:
        assert _terminal_retry_exists({}) is False
    select.assert_not_called()
