#!/usr/bin/env python3
"""pipeline_run.py のテスト (mock ベース)"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch, MagicMock
import importlib


# ============================================================
# Helper: pipeline_run を fresh import + mock で実行
# ============================================================

def _run_pipeline_mocked(
    *,
    ingest_return=None,
    ingest_error=None,
    process_return=None,
    process_error=None,
    serving_return=None,
    serving_error=None,
    notify_return=None,
    notify_error=None,
    dry_run=True,
    skip_notify=False,
    skip_jquants=True,
):
    """各ステップの mock を設定して run_pipeline を呼ぶ"""
    # Create mock modules
    mock_ingest_mod = MagicMock()
    mock_process_mod = MagicMock()
    mock_serving_mod = MagicMock()
    mock_notify_mod = MagicMock()

    if ingest_error:
        mock_ingest_mod.run.side_effect = ingest_error
    else:
        mock_ingest_mod.run.return_value = ingest_return or {
            "total": 3, "results": [], "summary": {"succeeded": 3}
        }

    if process_error:
        mock_process_mod.run.side_effect = process_error
    else:
        mock_process_mod.run.return_value = process_return or {
            "push": {"errors": 0}, "jquants": None
        }

    if serving_error:
        mock_serving_mod.run.side_effect = serving_error
    else:
        mock_serving_mod.run.return_value = serving_return or {"status": "skipped"}

    if notify_error:
        mock_notify_mod.run.side_effect = notify_error
    else:
        mock_notify_mod.run.return_value = notify_return or {"status": "success"}

    with patch.dict("sys.modules", {
        "tools.filings_ingest": mock_ingest_mod,
        "tools.filings_process": mock_process_mod,
        "tools.rebuild_serving_views": mock_serving_mod,
        "tools.notify_updates": mock_notify_mod,
    }):
        # Force reimport to pick up mocked modules
        if "tools.pipeline_run" in sys.modules:
            del sys.modules["tools.pipeline_run"]
        from tools.pipeline_run import run_pipeline
        result = run_pipeline(
            dry_run=dry_run,
            skip_notify=skip_notify,
            skip_jquants=skip_jquants,
        )

    return result, mock_ingest_mod, mock_process_mod, mock_serving_mod, mock_notify_mod


# ============================================================
# pipeline_run.py: 全ステップ成功
# ============================================================

class TestPipelineRunAllSuccess:
    def test_all_success(self):
        result, *_ = _run_pipeline_mocked()
        assert result["overall"] == "success"
        assert result["steps"]["ingest"] == "success"
        assert result["steps"]["process"] == "success"

    def test_skip_notify(self):
        result, _, _, _, mock_notify = _run_pipeline_mocked(skip_notify=True)
        assert result["steps"]["notify"] == "skipped"
        mock_notify.run.assert_not_called()

    def test_elapsed_positive(self):
        result, *_ = _run_pipeline_mocked()
        assert result["elapsed"] >= 0


# ============================================================
# pipeline_run.py: 失敗ケース
# ============================================================

class TestPipelineRunFailures:
    def test_ingest_failure_exits_early(self):
        result, *_ = _run_pipeline_mocked(
            ingest_error=RuntimeError("API down"),
        )
        assert result["overall"] == "failed"
        assert result["steps"]["ingest"] == "failed"
        assert "process" not in result["steps"]

    def test_process_failure_exits_early(self):
        result, *_ = _run_pipeline_mocked(
            process_error=RuntimeError("DB error"),
        )
        assert result["overall"] == "failed"
        assert result["steps"]["process"] == "failed"

    def test_notify_failure_is_warning(self):
        result, *_ = _run_pipeline_mocked(
            notify_return={"status": "error", "error": "webhook down"},
            skip_notify=False,
        )
        assert result["steps"]["notify"] == "error"
        assert result["overall"] != "failed"

    def test_serving_failure_is_warning(self):
        result, *_ = _run_pipeline_mocked(
            serving_error=RuntimeError("View error"),
            skip_notify=True,
        )
        assert result["steps"]["rebuild"] == "warning"
        assert result["overall"] == "partial_success"


# ============================================================
# StepResult
# ============================================================

class TestStepResult:
    def test_defaults(self):
        from tools.pipeline_run import StepResult
        s = StepResult("test")
        assert s.name == "test"
        assert s.status == "pending"

    def test_repr(self):
        from tools.pipeline_run import StepResult
        s = StepResult("test")
        s.status = "success"
        s.elapsed = 1.234
        assert "success" in repr(s)


# ============================================================
# _determine_overall
# ============================================================

class TestDetermineOverall:
    def test_all_success(self):
        from tools.pipeline_run import _determine_overall
        assert _determine_overall({"a": "success", "b": "success"}) == "success"

    def test_with_skipped(self):
        from tools.pipeline_run import _determine_overall
        assert _determine_overall({"a": "success", "b": "skipped"}) == "success"

    def test_with_failed(self):
        from tools.pipeline_run import _determine_overall
        assert _determine_overall({"a": "success", "b": "failed"}) == "failed"

    def test_with_warning(self):
        from tools.pipeline_run import _determine_overall
        assert _determine_overall({"a": "success", "b": "warning"}) == "partial_success"


# ============================================================
# rebuild_serving_views.py
# ============================================================

class TestRebuildServingViews:
    def test_noop_returns_skipped(self):
        from tools.rebuild_serving_views import run
        result = run()
        assert result["status"] == "skipped"
        assert "reason" in result


# ============================================================
# quarantine_review.py
# ============================================================

class TestQuarantineReview:
    def test_run_returns_total(self):
        from tools.quarantine_review import run
        result = run(limit=10)
        assert "total" in result

    def test_load_jsonl_review_empty(self, tmp_path):
        from tools.quarantine_review import _load_jsonl_review
        result = _load_jsonl_review(str(tmp_path))
        assert result == []


# ============================================================
# backfill_filings.py
# ============================================================

class TestBackfillDates:
    def test_single_day(self):
        from tools.backfill_filings import backfill_dates
        dates = backfill_dates("2025-01-01", "2025-01-01")
        assert dates == ["2025-01-01"]

    def test_range(self):
        from tools.backfill_filings import backfill_dates
        dates = backfill_dates("2025-01-01", "2025-01-03")
        assert len(dates) == 3
        assert dates[0] == "2025-01-01"
        assert dates[-1] == "2025-01-03"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
