# ============================================================
# test_main_db.py — main.py Phase2 DB書き込みのユニットテスト
# ============================================================
from __future__ import annotations

import os
import tempfile

import pytest

from src.main import _reiwa_to_fiscal_year_end, _calc_latency_sec


class TestReiwaToFiscalYearEnd:
    def test_r8_3(self):
        assert _reiwa_to_fiscal_year_end("R8/3") == "2026-03-31"

    def test_r7_12(self):
        assert _reiwa_to_fiscal_year_end("R7/12") == "2025-12-31"

    def test_r7_2(self):
        assert _reiwa_to_fiscal_year_end("R7/2") == "2025-02-28"

    def test_r10_6(self):
        assert _reiwa_to_fiscal_year_end("R10/6") == "2028-06-30"

    def test_invalid(self):
        assert _reiwa_to_fiscal_year_end("2026/3") is None

    def test_empty(self):
        assert _reiwa_to_fiscal_year_end("") is None


class TestCalcLatency:
    def test_valid_format(self):
        # 過去の日時→正の遅延
        from datetime import datetime, timedelta
        from src.main import JST
        past = (datetime.now(JST) - timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")
        lat = _calc_latency_sec(past)
        assert lat is not None
        assert lat >= 9  # 少なくとも9秒以上

    def test_invalid_format(self):
        assert _calc_latency_sec("invalid") is None
