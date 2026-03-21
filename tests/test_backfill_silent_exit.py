"""tests/test_backfill_silent_exit.py — listing 後無言終了バグの回帰テスト"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.backfill.jsonl_logger import RunLogger, generate_run_id


# ================================================================
# RunLogger テスト
# ================================================================


class TestRunLoggerEmptyFile:
    """JSONL ファイルが空にならないことを確認。"""

    def test_run_start_event_on_create(self, tmp_path):
        """コンストラクタで run_start イベントが自動書き込みされる。"""
        path = str(tmp_path / "test.jsonl")
        rl = RunLogger(path, run_id="test123")
        rl.close()

        lines = Path(path).read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) >= 2  # run_start + run_end_no_summary
        data = json.loads(lines[0])
        assert data["event"] == "run_start"
        assert data["run_id"] == "test123"

    def test_close_without_summary_writes_warning(self, tmp_path):
        """summary なしで close() すると warning event が書かれる。"""
        path = str(tmp_path / "test.jsonl")
        rl = RunLogger(path, run_id="test123")
        rl.close()

        lines = Path(path).read_text(encoding="utf-8").strip().split("\n")
        last = json.loads(lines[-1])
        assert last["event"] == "run_end_no_summary"

    def test_close_with_summary_no_warning(self, tmp_path):
        """summary ありで close() すると warning event は書かれない。"""
        path = str(tmp_path / "test.jsonl")
        rl = RunLogger(path, run_id="test123")
        rl.log_summary({"total": 10})
        rl.close()

        lines = Path(path).read_text(encoding="utf-8").strip().split("\n")
        events = [json.loads(l)["event"] for l in lines]
        assert "run_start" in events
        assert "run_summary" in events
        assert "run_end_no_summary" not in events

    def test_log_fatal_writes_event(self, tmp_path):
        """log_fatal() が fatal イベントを書く。"""
        path = str(tmp_path / "test.jsonl")
        rl = RunLogger(path, run_id="test123")
        rl.log_fatal("something broke")
        rl.close()

        lines = Path(path).read_text(encoding="utf-8").strip().split("\n")
        events = [json.loads(l) for l in lines]
        fatal = [e for e in events if e["event"] == "fatal"]
        assert len(fatal) == 1
        assert "something broke" in fatal[0]["error"]


# ================================================================
# run_backfill 統一戻り値テスト
# ================================================================


@dataclass
class FakeFiling:
    filing_id: str
    ticker: str
    disclosure_date: str = "2026-03-11"
    doc_type: str = "financial_statement"
    title: str = "決算短信"
    has_xbrl: bool = True
    doc_url: str = ""
    xbrl_url: str = ""
    listing_source: str = "tdnet_html"
    company_name: str = "テスト企業"
    published_at: str = "2026-03-11 15:00"


class FakeProvider:
    def __init__(self, filings):
        self._filings = filings

    def list_filings(self, *args, **kwargs):
        return self._filings


class FakeStore:
    def __init__(self, reg_result=None, pending=None):
        self._reg = reg_result or {"new": 0, "existing": 0}
        self._pending = pending or []

    def register_filings(self, filings):
        return self._reg

    def reset_stale_running(self, max_age_hours=2):
        return 0

    def get_pending(self, **kwargs):
        return self._pending

    def get_resume_candidates(self, **kwargs):
        return self._pending

    def stats(self):
        return {"total": 0, "queued": 0, "done": 0}

    def close(self):
        pass


class TestRunBackfillReturnFormat:
    """run_backfill の戻り値が常に統一形式 (summary key あり) であること。"""

    def test_zero_listing_returns_summary_key(self, tmp_path):
        """listing 0件 でも result['summary'] が存在する。"""
        from tools.backfill_segments_tdnet import run_backfill

        log_path = str(tmp_path / "test.jsonl")
        with patch("tools.backfill_segments_tdnet._build_provider") as mock_bp:
            mock_bp.return_value = FakeProvider([])
            result = run_backfill(
                start_date="2026-03-01",
                end_date="2026-03-11",
                limit=10,
                state_db=str(tmp_path / "state.db"),
                log_jsonl_path=log_path,
            )
        assert "summary" in result
        assert "metrics" in result
        assert "run_id" in result

    def test_all_done_returns_summary_key(self, tmp_path):
        """listing 有、pending 0件 でも result['summary'] が存在する。"""
        from tools.backfill_segments_tdnet import run_backfill

        log_path = str(tmp_path / "test.jsonl")
        filings = [FakeFiling(filing_id="FF001", ticker="6750")]

        with patch("tools.backfill_segments_tdnet._build_provider") as mock_bp, \
             patch("tools.backfill_segments_tdnet.BackfillStateStore") as mock_ss:
            mock_bp.return_value = FakeProvider(filings)
            mock_ss.return_value = FakeStore(
                reg_result={"new": 0, "existing": 1},
                pending=[],
            )
            result = run_backfill(
                start_date="2026-03-01",
                end_date="2026-03-11",
                limit=10,
                state_db=str(tmp_path / "state.db"),
                log_jsonl_path=log_path,
            )
        assert "summary" in result
        assert "metrics" in result


class TestInvariantViolation:
    """kept > 0 && register total == 0 → RuntimeError。"""

    def test_invariant_raises(self, tmp_path):
        from tools.backfill_segments_tdnet import run_backfill

        log_path = str(tmp_path / "test.jsonl")
        filings = [FakeFiling(filing_id="FF001", ticker="6750")]

        with patch("tools.backfill_segments_tdnet._build_provider") as mock_bp, \
             patch("tools.backfill_segments_tdnet.BackfillStateStore") as mock_ss:
            mock_bp.return_value = FakeProvider(filings)
            mock_ss.return_value = FakeStore(
                reg_result={"new": 0, "existing": 0},
                pending=[],
            )
            with pytest.raises(RuntimeError, match="INVARIANT VIOLATION"):
                run_backfill(
                    start_date="2026-03-01",
                    end_date="2026-03-11",
                    limit=10,
                    state_db=str(tmp_path / "state.db"),
                    log_jsonl_path=log_path,
                )

        # JSONL に fatal が書かれていること
        lines = Path(log_path).read_text(encoding="utf-8").strip().split("\n")
        events = [json.loads(l)["event"] for l in lines]
        assert "fatal" in events


class TestZeroFilingsSummaryLogged:
    """0件終了でも JSONL に run_summary が書かれる。"""

    def test_summary_in_jsonl(self, tmp_path):
        from tools.backfill_segments_tdnet import run_backfill

        log_path = str(tmp_path / "test.jsonl")
        with patch("tools.backfill_segments_tdnet._build_provider") as mock_bp:
            mock_bp.return_value = FakeProvider([])
            run_backfill(
                start_date="2026-03-01",
                end_date="2026-03-11",
                limit=10,
                state_db=str(tmp_path / "state.db"),
                log_jsonl_path=log_path,
            )

        lines = Path(log_path).read_text(encoding="utf-8").strip().split("\n")
        events = [json.loads(l)["event"] for l in lines]
        assert "run_start" in events
        assert "run_summary" in events
