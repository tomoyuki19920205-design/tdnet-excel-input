"""test_pipeline_integration.py -- pipeline_runs / rebuild_queue 連携テスト

テスト:
1. rebuild サブコマンドで pipeline_runs が書き込まれること
2. pipeline_runs が例外時に failed になること
3. rebuild_queue pending が処理時に done に更新されること
4. rebuild_queue 処理失敗時に failed に更新されること
5. queue 更新失敗時に warning が出ても rebuild 本体は落ちないこと
6. --ticker 直指定時は rebuild_queue を消化しないこと
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)


# ============================================================
# テスト1: rebuild サブコマンドで pipeline_runs に記録される
# ============================================================
class TestPipelineRunsWrittenOnRebuildSubcommand:
    """pipeline_run.py rebuild サブコマンド実行時に
    pipeline_runs テーブルへ running → done が記録されることを検証。
    """

    @patch("lib.pipeline.logging_utils.supabase_select")
    @patch("lib.pipeline.logging_utils.supabase_update")
    @patch("lib.pipeline.logging_utils.supabase_insert")
    def test_pipeline_runs_recorded(
        self, mock_insert, mock_update, mock_select
    ):
        """PipelineRun context が __enter__ で INSERT、__exit__ で UPDATE する。"""
        # supabase_insert → ok, 直接 id を返す
        mock_insert.return_value = {"ok": True, "rows": [{"id": 42}]}
        # supabase_update → ok
        mock_update.return_value = True

        from lib.pipeline.logging_utils import PipelineRun

        with PipelineRun("rebuild", trigger_type="manual") as pl:
            pl.update(processed=1, success=1)

        # __enter__: INSERT
        assert mock_insert.call_count == 1
        enter_call = mock_insert.call_args_list[0]
        assert enter_call[0][0] == "pipeline_runs"
        row = enter_call[0][1]
        assert row["status"] == "running"
        assert row["job_type"] == "rebuild"
        assert row["trigger_type"] == "manual"

        # __exit__: UPDATE with done
        assert mock_update.call_count == 1
        exit_call = mock_update.call_args_list[0]
        assert exit_call[0][0] == "pipeline_runs"
        update_data = exit_call[0][1]
        assert update_data["status"] == "done"
        assert "finished_at" in update_data
        assert "duration_sec" in update_data
        assert update_data["processed_count"] == 1
        assert update_data["success_count"] == 1

        # supabase_select は呼ばれない (直接 id 取得のため)
        mock_select.assert_not_called()

    @patch("lib.pipeline.logging_utils.supabase_select")
    @patch("lib.pipeline.logging_utils.supabase_update")
    @patch("lib.pipeline.logging_utils.supabase_insert")
    def test_pipeline_runs_failed_on_exception(
        self, mock_insert, mock_update, mock_select
    ):
        """例外発生時に pipeline_runs が status=failed で更新される。"""
        mock_insert.return_value = {"ok": True, "rows": [{"id": 99}]}
        mock_update.return_value = True

        from lib.pipeline.logging_utils import PipelineRun

        with pytest.raises(ValueError):
            with PipelineRun("rebuild", trigger_type="scheduler") as pl:
                raise ValueError("test error")

        exit_call = mock_update.call_args_list[0]
        update_data = exit_call[0][1]
        assert update_data["status"] == "failed"
        assert "test error" in (update_data.get("message") or "")

    @patch("lib.pipeline.logging_utils.supabase_select")
    @patch("lib.pipeline.logging_utils.supabase_update")
    @patch("lib.pipeline.logging_utils.supabase_insert")
    def test_pipeline_runs_warning_on_insert_failure(
        self, mock_insert, mock_update, mock_select
    ):
        """INSERT 失敗時に warning が出るが処理は続行する。"""
        mock_insert.return_value = {"ok": False, "rows": [], "error": "RLS denied"}
        mock_update.return_value = True

        from lib.pipeline.logging_utils import PipelineRun

        # 例外は発生しない
        with PipelineRun("rebuild") as pl:
            pl.update(processed=1)
            assert pl.run_id is None  # ID 取得失敗

        # UPDATE はスキップされる (run_id がないため)
        mock_update.assert_not_called()


# ============================================================
# テスト2: rebuild_queue status が processing 時に更新される
# ============================================================
class TestRebuildQueueStatusUpdated:
    """rebuild_serving_views が rebuild_queue pending を処理し、
    完了時に done/failed に更新することを検証。
    """

    @patch("tools.rebuild_serving_views.complete_rebuild")
    @patch("tools.rebuild_serving_views.take_pending_rebuilds")
    @patch("tools.rebuild_serving_views.supabase_select")
    @patch("tools.rebuild_serving_views.load_env")
    @patch("tools.rebuild_serving_views.get_supabase_config")
    def test_queue_done_on_success(
        self, mock_config, mock_env, mock_select, mock_take, mock_complete
    ):
        """pending 行が正常処理後に done に更新される。"""
        mock_config.return_value = {"url": "http://test", "key": "test"}
        mock_take.return_value = [
            {"id": 1, "ticker": "6750", "status": "running"},
        ]
        # 1回目: financials, 2回目: segment_canonical
        mock_select.side_effect = [
            [{"ticker": "6750", "period": "2025-03-31", "quarter": "2Q"}],
            [{"ticker": "6750", "period": "2025-03-31", "quarter": "2Q", "segment_name": "Seg1"}],
        ]

        from tools.rebuild_serving_views import run
        result = run(dry_run=False)

        assert result["status"] == "success"
        assert result["total"] == 1
        mock_complete.assert_called_once_with(1, "done", config=mock_config.return_value)

    @patch("tools.rebuild_serving_views.complete_rebuild")
    @patch("tools.rebuild_serving_views.take_pending_rebuilds")
    @patch("tools.rebuild_serving_views.supabase_select")
    @patch("tools.rebuild_serving_views.load_env")
    @patch("tools.rebuild_serving_views.get_supabase_config")
    def test_queue_failed_on_error(
        self, mock_config, mock_env, mock_select, mock_take, mock_complete
    ):
        """処理失敗時に queue が failed に更新される。"""
        mock_config.return_value = {"url": "http://test", "key": "test"}
        mock_take.return_value = [
            {"id": 2, "ticker": "9999", "status": "running"},
        ]
        mock_select.side_effect = Exception("supabase down")

        from tools.rebuild_serving_views import run
        result = run(dry_run=False)

        assert result["failed"] == 1
        mock_complete.assert_called_once_with(2, "failed", config=mock_config.return_value)

    @patch("tools.rebuild_serving_views.complete_rebuild")
    @patch("tools.rebuild_serving_views.take_pending_rebuilds")
    @patch("tools.rebuild_serving_views.supabase_select")
    @patch("tools.rebuild_serving_views.load_env")
    @patch("tools.rebuild_serving_views.get_supabase_config")
    def test_queue_update_failure_logged_not_raised(
        self, mock_config, mock_env, mock_select, mock_take, mock_complete
    ):
        """queue 更新失敗時に warning ログが出るが例外は握りつぶす。"""
        mock_config.return_value = {"url": "http://test", "key": "test"}
        mock_take.return_value = [
            {"id": 3, "ticker": "1234", "status": "running"},
        ]
        # 1回目: financials, 2回目: segment_canonical
        mock_select.side_effect = [
            [{"ticker": "1234", "period": "2025-03-31", "quarter": "2Q"}],
            [{"ticker": "1234", "period": "2025-03-31", "quarter": "2Q", "segment_name": "Seg1"}],
        ]
        # complete_rebuild が例外を投げる
        mock_complete.side_effect = Exception("network error")

        from tools.rebuild_serving_views import run
        result = run(dry_run=False)
        assert result["success"] == 1
        mock_complete.assert_called_once()


# ============================================================
# テスト3: --ticker 直指定時は rebuild_queue を消化しない
# ============================================================
class TestRebuildTickerDirectDoesNotConsumeQueue:
    """--ticker 直指定時に take_pending_rebuilds が呼ばれないことを検証。"""

    @patch("tools.rebuild_serving_views.complete_rebuild")
    @patch("tools.rebuild_serving_views.take_pending_rebuilds")
    @patch("tools.rebuild_serving_views.supabase_select")
    @patch("tools.rebuild_serving_views.load_env")
    @patch("tools.rebuild_serving_views.get_supabase_config")
    def test_ticker_direct_skips_queue(
        self, mock_config, mock_env, mock_select, mock_take, mock_complete
    ):
        """ticker 直指定時に queue は触らない。"""
        mock_config.return_value = {"url": "http://test", "key": "test"}
        # 1回目: financials, 2回目: segment_canonical
        mock_select.side_effect = [
            [{"ticker": "6750", "period": "2025-03-31", "quarter": "2Q"}],
            [{"ticker": "6750", "period": "2025-03-31", "quarter": "2Q", "segment_name": "Seg1"}],
        ]

        from tools.rebuild_serving_views import run
        result = run(dry_run=False, ticker="6750")

        assert result["total"] == 1
        assert result["success"] == 1
        mock_take.assert_not_called()
        mock_complete.assert_not_called()

    @patch("tools.rebuild_serving_views.complete_rebuild")
    @patch("tools.rebuild_serving_views.take_pending_rebuilds")
    @patch("tools.rebuild_serving_views.supabase_select")
    @patch("tools.rebuild_serving_views.load_env")
    @patch("tools.rebuild_serving_views.get_supabase_config")
    def test_ticker_direct_does_not_change_existing_pending(
        self, mock_config, mock_env, mock_select, mock_take, mock_complete
    ):
        """--ticker 直指定でも既存 pending queue は残ったまま。"""
        mock_config.return_value = {"url": "http://test", "key": "test"}
        mock_select.side_effect = [
            [{"ticker": "6750", "period": "2025-03-31", "quarter": "2Q"}],
            [{"ticker": "6750", "period": "2025-03-31", "quarter": "2Q", "segment_name": "Seg1"}],
        ]

        from tools.rebuild_serving_views import run
        result = run(dry_run=False, ticker="6750")

        # queue 操作は一切行わない
        mock_take.assert_not_called()
        mock_complete.assert_not_called()
        assert result["status"] == "success"


# ============================================================
# テスト4: queue update の warning ログ
# ============================================================
class TestWarningLoggedWhenQueueUpdateFails:
    """queue update 失敗時に warning が出て、rebuild 本体は落ちないこと。"""

    @patch("lib.pipeline.queue.supabase_update")
    @patch("lib.pipeline.queue.supabase_select")
    def test_complete_rebuild_warns_on_failure(
        self, mock_select, mock_update
    ):
        """complete_rebuild が update 失敗時に warning を出して False を返す。"""
        mock_update.return_value = False

        from lib.pipeline.queue import complete_rebuild
        result = complete_rebuild(999, "done")
        assert result is False
        mock_update.assert_called_once()
