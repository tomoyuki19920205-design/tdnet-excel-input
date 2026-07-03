"""
tests/test_scheduler_realtime_deadline.py
==========================================
scheduler_realtime.py の deadline budget / skip reason / ingest timeout
後続step継続の動作テスト。

subprocess を呼ばずに run_step をモックして動作を検証する。
"""
from __future__ import annotations

import logging
import time
import pytest
from unittest.mock import MagicMock, patch

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.scheduler_realtime import (
    INGEST_TIMEOUT_SEC,
    PROCESS_TIMEOUT_SEC,
    NOTIFY_TIMEOUT_SEC,
    DEADLINE_MINUTES,
    PROCESS_MIN_BUDGET_SEC,
    NOTIFY_MIN_BUDGET_SEC,
    StepResult,
)


# ============================================================
# ヘルパー: StepResult 生成
# ============================================================

def _make_step(name: str, status: str, duration: float = 1.0, rc: int = 0) -> StepResult:
    s = StepResult(name)
    s.status = status
    s.duration = duration
    s.rc = rc
    return s


def _run_main_mocked(steps_seq: list[StepResult], extra_patches=None):
    """main() を最小モックで実行。steps_seq の順に StepResult を返す。"""
    import tools.scheduler_realtime as sr

    call_count = [0]
    def fake_run_step(name, cmd, *, timeout_sec=120, cwd=""):
        idx = call_count[0]
        call_count[0] += 1
        return steps_seq[idx] if idx < len(steps_seq) else _make_step(name, "success")

    ctx_patches = [
        patch.object(sr, "run_step", side_effect=fake_run_step),
        patch.object(sr, "acquire_dual_lock", return_value=(MagicMock(), MagicMock())),
        patch.object(sr, "release_dual_lock"),
        patch("sys.argv", ["scheduler_realtime.py", "--skip-delay"]),
    ]
    if extra_patches:
        ctx_patches.extend(extra_patches)

    with _nested(*ctx_patches):
        return sr.main()


def _nested(*ctxs):
    """複数のcontextmanagerをネストして使うためのヘルパー。"""
    from contextlib import ExitStack
    class _CM:
        def __enter__(self):
            self.stack = ExitStack()
            for ctx in ctxs:
                self.stack.enter_context(ctx)
            return self
        def __exit__(self, *args):
            return self.stack.__exit__(*args)
    return _CM()


# ============================================================
# Phase 1: タイムアウト定数の設計検証
# ============================================================

class TestDeadlineConstants:

    def test_ingest_timeout_less_than_deadline(self):
        """ingest timeout が全体 deadline より必ず小さいこと。"""
        deadline_sec = DEADLINE_MINUTES * 60
        assert INGEST_TIMEOUT_SEC < deadline_sec, (
            f"ingest timeout {INGEST_TIMEOUT_SEC}s >= deadline {deadline_sec}s: "
            "後段 step の予算が残らない"
        )

    def test_ingest_timeout_leaves_process_budget(self):
        """ingest timeout 後に process + notify の最低予算が残ること。"""
        deadline_sec = DEADLINE_MINUTES * 60
        remaining_after_ingest = deadline_sec - INGEST_TIMEOUT_SEC
        min_required = PROCESS_MIN_BUDGET_SEC + NOTIFY_MIN_BUDGET_SEC
        assert remaining_after_ingest >= min_required, (
            f"ingest timeout後の残り {remaining_after_ingest}s < "
            f"最低必要予算 {min_required}s"
        )

    def test_process_min_budget_less_than_timeout(self):
        """process の最低予算が process timeout 未満であること。"""
        assert PROCESS_MIN_BUDGET_SEC <= PROCESS_TIMEOUT_SEC

    def test_notify_min_budget_less_than_timeout(self):
        """notify の最低予算が notify timeout 未満であること。"""
        assert NOTIFY_MIN_BUDGET_SEC <= NOTIFY_TIMEOUT_SEC


# ============================================================
# Phase 2: deadline budget ログ出力テスト
# ============================================================

class TestDeadlineBudgetLogging:
    """各 step 前に REALTIME_DEADLINE_BUDGET ログが出ることを確認。"""

    def test_deadline_budget_logged_for_each_step(self, caplog):
        """ingest / process-realtime / notify それぞれの前に REALTIME_DEADLINE_BUDGET が出る。"""
        steps_seq = [
            _make_step("ingest", "success", 5.0),
            _make_step("process-realtime", "success", 2.0),
            _make_step("notify", "success", 1.0),
        ]
        with caplog.at_level(logging.INFO, logger="scheduler.realtime"):
            _run_main_mocked(steps_seq)

        msgs = [r.message for r in caplog.records]
        budget_logs = [m for m in msgs if "REALTIME_DEADLINE_BUDGET" in m]
        step_names = set()
        for m in budget_logs:
            if "step=ingest" in m:
                step_names.add("ingest")
            if "step=process-realtime" in m:
                step_names.add("process-realtime")
            if "step=notify" in m:
                step_names.add("notify")

        assert "ingest" in step_names, "ingest の REALTIME_DEADLINE_BUDGET ログがない"
        assert "process-realtime" in step_names, "process-realtime の REALTIME_DEADLINE_BUDGET ログがない"
        assert "notify" in step_names, "notify の REALTIME_DEADLINE_BUDGET ログがない"

    def test_budget_log_contains_remaining_and_timeout(self, caplog):
        """REALTIME_DEADLINE_BUDGET ログに remaining_sec と timeout_sec が含まれる。"""
        steps_seq = [
            _make_step("ingest", "success", 5.0),
            _make_step("process-realtime", "success", 2.0),
            _make_step("notify", "success", 1.0),
        ]
        with caplog.at_level(logging.INFO, logger="scheduler.realtime"):
            _run_main_mocked(steps_seq)

        msgs = [r.message for r in caplog.records]
        budget_logs = [m for m in msgs if "REALTIME_DEADLINE_BUDGET" in m]
        assert budget_logs
        for log in budget_logs:
            assert "remaining_sec=" in log, f"remaining_sec がない: {log}"
            assert "timeout_sec=" in log, f"timeout_sec がない: {log}"

    def test_summary_includes_process_ran_and_notify_ran(self, caplog):
        """正常実行時のsummaryに process_ran=True / notify_ran=True が含まれる。"""
        steps_seq = [
            _make_step("ingest", "success", 5.0),
            _make_step("process-realtime", "success", 2.0),
            _make_step("notify", "success", 1.0),
        ]
        with caplog.at_level(logging.INFO, logger="scheduler.realtime"):
            _run_main_mocked(steps_seq)

        msgs = [r.message for r in caplog.records]
        summary_logs = [m for m in msgs if "run_id=summary" in m]
        assert summary_logs, f"summary ログが出なかった: {msgs[-5:]}"
        summary = summary_logs[0]
        assert "process_ran=True" in summary, f"process_ran=True がない: {summary}"
        assert "notify_ran=True" in summary, f"notify_ran=True がない: {summary}"


# ============================================================
# Phase 3: deadline 不足時の skip ログテスト
#
# DEADLINE_MINUTES をモジュールレベルで 0 にすることで
# 全ての step が deadline 超過状態になる。
# ============================================================

class TestDeadlineSkipLogging:

    def test_skip_process_with_explicit_reason_when_budget_low(self, caplog):
        """残り予算不足で REALTIME_STEP_SKIPPED_DEADLINE が出ること。"""
        import tools.scheduler_realtime as sr

        # DEADLINE_MINUTES=0 → deadline が過去になる → ingest 後に全 step が skip される
        with patch.object(sr, "DEADLINE_MINUTES", 0), \
             patch.object(sr, "run_step", return_value=_make_step("ingest", "success", 1.0)), \
             patch.object(sr, "acquire_dual_lock", return_value=(MagicMock(), MagicMock())), \
             patch.object(sr, "release_dual_lock"), \
             patch("sys.argv", ["scheduler_realtime.py", "--skip-delay"]), \
             caplog.at_level(logging.WARNING, logger="scheduler.realtime"):
            sr.main()

        warn_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        skip_logs = [m for m in warn_msgs if "REALTIME_STEP_SKIPPED_DEADLINE" in m]
        assert skip_logs, f"REALTIME_STEP_SKIPPED_DEADLINE が出なかった: {warn_msgs}"

    def test_summary_shows_process_ran_false_when_skipped(self, caplog):
        """process-realtime がskipされた場合、summary に process_ran=False が含まれる。"""
        import tools.scheduler_realtime as sr

        with patch.object(sr, "DEADLINE_MINUTES", 0), \
             patch.object(sr, "run_step", return_value=_make_step("ingest", "success", 1.0)), \
             patch.object(sr, "acquire_dual_lock", return_value=(MagicMock(), MagicMock())), \
             patch.object(sr, "release_dual_lock"), \
             patch("sys.argv", ["scheduler_realtime.py", "--skip-delay"]), \
             caplog.at_level(logging.INFO, logger="scheduler.realtime"):
            sr.main()

        msgs = [r.message for r in caplog.records]
        summary_logs = [m for m in msgs if "run_id=summary" in m]
        assert summary_logs
        summary = summary_logs[0]
        assert "process_ran=False" in summary or "notify_ran=False" in summary, \
            f"skipされたはずなのに全て True: {summary}"


# ============================================================
# Phase 4: ingest timeout 後の後段継続テスト
# ============================================================

class TestIngestTimeoutBehavior:

    def test_continues_to_process_after_ingest_timeout_if_budget_allows(self, caplog):
        """ingest が timeout しても残りdeadlineがあれば process-realtime を実行する。"""
        import tools.scheduler_realtime as sr

        run_step_calls = []
        def fake_run_step(name, cmd, *, timeout_sec=120, cwd=""):
            run_step_calls.append(name)
            if name == "ingest":
                return _make_step("ingest", "timeout", float(INGEST_TIMEOUT_SEC))
            return _make_step(name, "success", 1.0)

        with patch.object(sr, "run_step", side_effect=fake_run_step), \
             patch.object(sr, "acquire_dual_lock", return_value=(MagicMock(), MagicMock())), \
             patch.object(sr, "release_dual_lock"), \
             patch("sys.argv", ["scheduler_realtime.py", "--skip-delay"]), \
             caplog.at_level(logging.INFO, logger="scheduler.realtime"):
            sr.main()

        assert "process-realtime" in run_step_calls, \
            f"ingest timeout 後に process-realtime が呼ばれなかった: {run_step_calls}"

        info_msgs = [r.message for r in caplog.records]
        timeout_logs = [m for m in info_msgs if "REALTIME_INGEST_TIMEOUT" in m]
        assert timeout_logs, "REALTIME_INGEST_TIMEOUT ログが出なかった"

        continue_logs = [m for m in info_msgs if "REALTIME_CONTINUE_AFTER_INGEST_FAILURE" in m]
        assert continue_logs, "REALTIME_CONTINUE_AFTER_INGEST_FAILURE ログが出なかった"

    def test_summary_shows_notify_skip_reason_when_ingest_timeout_and_no_budget(self, caplog):
        """ingest timeout かつ残り予算不足の場合、summaryにskip_reasonが記録される。"""
        import tools.scheduler_realtime as sr

        def fake_run_step(name, cmd, *, timeout_sec=120, cwd=""):
            if name == "ingest":
                return _make_step("ingest", "timeout", float(INGEST_TIMEOUT_SEC))
            return _make_step(name, "success", 1.0)

        # DEADLINE = 0 でingest後全部skip
        with patch.object(sr, "DEADLINE_MINUTES", 0), \
             patch.object(sr, "run_step", side_effect=fake_run_step), \
             patch.object(sr, "acquire_dual_lock", return_value=(MagicMock(), MagicMock())), \
             patch.object(sr, "release_dual_lock"), \
             patch("sys.argv", ["scheduler_realtime.py", "--skip-delay"]), \
             caplog.at_level(logging.WARNING, logger="scheduler.realtime"):
            sr.main()

        warn_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        # REALTIME_ABORT_AFTER_INGEST_FAILURE か REALTIME_STEP_SKIPPED_DEADLINE が出るはず
        abort_logs = [m for m in warn_msgs if "REALTIME_ABORT_AFTER_INGEST_FAILURE" in m
                      or "REALTIME_STEP_SKIPPED_DEADLINE" in m]
        assert abort_logs, f"ingest timeout+deadline不足時の警告が出なかった: {warn_msgs}"
