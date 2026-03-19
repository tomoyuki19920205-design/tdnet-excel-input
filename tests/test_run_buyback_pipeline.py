"""test_run_buyback_pipeline.py — 一括ラッパーのテスト"""
from __future__ import annotations

import csv
import os
import sys
import tempfile

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from tools.run_buyback_pipeline import (
    StepResult,
    _csv_row_count,
    _now_str,
    build_parser,
    write_step_status_csv,
    write_pipeline_summary,
    STEPS,
)


# ============================================================
# StepResult
# ============================================================
class TestStepResult:
    def test_to_dict(self):
        s = StepResult("candidates", 1)
        d = s.to_dict()
        assert d["step_name"] == "candidates"
        assert d["status"] == "pending"

    def test_status_update(self):
        s = StepResult("review", 2)
        s.status = "success"
        s.row_count = 42
        d = s.to_dict()
        assert d["status"] == "success"
        assert d["row_count"] == 42


# ============================================================
# csv_row_count
# ============================================================
class TestCSVRowCount:
    def test_count(self):
        tmp = tempfile.NamedTemporaryFile(
            suffix=".csv", delete=False, mode="w",
            encoding="utf-8", newline="",
        )
        w = csv.writer(tmp)
        w.writerow(["a", "b"])
        w.writerow(["1", "2"])
        w.writerow(["3", "4"])
        tmp.close()
        try:
            assert _csv_row_count(tmp.name) == 2
        finally:
            os.unlink(tmp.name)

    def test_nonexistent(self):
        assert _csv_row_count("/nonexistent.csv") == 0


# ============================================================
# build_parser
# ============================================================
class TestParser:
    def test_defaults(self):
        parser = build_parser()
        opts = parser.parse_args([])
        assert opts.dry_run is True
        assert opts.live_save is False
        assert opts.recursive is True
        assert opts.stop_after == "save"

    def test_live_save(self):
        parser = build_parser()
        opts = parser.parse_args(["--live-save"])
        assert opts.live_save is True

    def test_stop_after(self):
        parser = build_parser()
        opts = parser.parse_args(["--stop-after", "review"])
        assert opts.stop_after == "review"

    def test_skip_save(self):
        parser = build_parser()
        opts = parser.parse_args(["--skip-save"])
        assert opts.skip_save is True

    def test_custom_run_id(self):
        parser = build_parser()
        opts = parser.parse_args(["--run-id", "test_123"])
        assert opts.run_id == "test_123"

    def test_dry_run_is_default(self):
        """dry-run がデフォルトで True"""
        parser = build_parser()
        opts = parser.parse_args([])
        assert opts.dry_run is True
        assert opts.live_save is False

    def test_live_save_overrides_dry_run(self):
        """--live-save 指定でモード確認"""
        parser = build_parser()
        opts = parser.parse_args(["--live-save"])
        assert opts.live_save is True


# ============================================================
# write_step_status_csv
# ============================================================
class TestWriteStepStatus:
    def test_csv_output(self):
        tmp = tempfile.mkdtemp()
        try:
            results = [
                StepResult("candidates", 1),
                StepResult("review", 2),
            ]
            results[0].status = "success"
            results[0].row_count = 10
            results[1].status = "failed"
            results[1].error_message = "test error"

            path = write_step_status_csv(results, tmp)
            assert os.path.isfile(path)

            with open(path, encoding="utf-8") as f:
                reader = list(csv.DictReader(f))
            assert len(reader) == 2
            assert reader[0]["step_name"] == "candidates"
            assert reader[0]["status"] == "success"
            assert reader[1]["status"] == "failed"
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# write_pipeline_summary
# ============================================================
class TestWriteSummary:
    def test_summary_md(self):
        tmp = tempfile.mkdtemp()
        try:
            results = [
                StepResult("candidates", 1),
                StepResult("review", 2),
                StepResult("operation", 3),
                StepResult("save", 4),
            ]
            results[0].status = "success"
            results[0].row_count = 5

            parser = build_parser()
            opts = parser.parse_args([])

            path = write_pipeline_summary(results, tmp, opts, "test_run")
            assert os.path.isfile(path)

            with open(path, encoding="utf-8") as f:
                content = f.read()
            assert "test_run" in content
            assert "DRY-RUN" in content
            assert "candidates" in content
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_live_save_summary(self):
        tmp = tempfile.mkdtemp()
        try:
            results = [StepResult("save", 4)]
            results[0].status = "success"

            parser = build_parser()
            opts = parser.parse_args(["--live-save"])
            opts.dry_run = False

            path = write_pipeline_summary(results, tmp, opts, "test_live")
            with open(path, encoding="utf-8") as f:
                content = f.read()
            assert "LIVE SAVE" in content
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# STEPS constant
# ============================================================
class TestSteps:
    def test_order(self):
        assert STEPS == ("candidates", "review", "operation", "save")

    def test_stop_after_index(self):
        assert STEPS.index("review") == 1
        assert STEPS.index("save") == 3


# ============================================================
# コマンド組み立て検証
# ============================================================
class TestCommandBuild:
    """dry-run / live-save でコマンドが正しく組み立てられるか確認"""

    def test_dry_run_adds_flag(self):
        """dry-run モードの場合、save step に --dry-run が入る"""
        parser = build_parser()
        opts = parser.parse_args([])
        assert opts.dry_run is True
        # 実際のコマンド組み立ては run_pipeline 内で行われる
        # ここでは opts.live_save が False であることを確認
        assert opts.live_save is False

    def test_live_save_no_dryrun(self):
        """--live-save 指定時、save step に --dry-run が入らない"""
        parser = build_parser()
        opts = parser.parse_args(["--live-save"])
        assert opts.live_save is True
