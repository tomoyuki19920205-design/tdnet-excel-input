"""tests/test_phase2_dual_write_best_effort.py — dual-write best-effort テスト"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.pipeline.canonical_writer import (
    write_financials_canonical,
    write_segments_canonical,
)


class TestDualWriteBestEffort:
    """canonical write 失敗時も errors を返し、例外を投げないこと。"""

    def test_financials_upsert_fails_returns_error_count(self):
        mock_upsert = MagicMock(return_value={"ok": False, "count": 0, "error": "RLS violation"})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            result = write_financials_canonical(
                ticker="6750",
                period="2026-03-31",
                quarter="FY",
                metrics_dict={"sales": 100},
                source="tdnet",
                config={"url": "x", "key": "y"},
            )
        assert result["written"] == 0
        assert result["errors"] == 1

    def test_segments_upsert_fails_returns_error_count(self):
        mock_upsert = MagicMock(return_value={"ok": False, "count": 0, "error": "table not found"})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            result = write_segments_canonical(
                ticker="6750",
                period="2026-03-31",
                quarter="FY",
                segments=[{"segment_name": "テスト", "sales": 100, "profit": 50}],
                source="xbrl",
                config={"url": "x", "key": "y"},
            )
        assert result["written"] == 0
        assert result["errors"] == 2

    def test_financials_exception_returns_error_count(self):
        """supabase_upsert が例外を投げても errors を返す"""
        mock_upsert = MagicMock(side_effect=ConnectionError("network error"))
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            result = write_financials_canonical(
                ticker="6750",
                period="2026-03-31",
                quarter="FY",
                metrics_dict={"sales": 100},
                source="tdnet",
                config={"url": "x", "key": "y"},
            )
        assert result["written"] == 0
        assert result["errors"] == 1

    def test_segments_exception_returns_error_count(self):
        mock_upsert = MagicMock(side_effect=TimeoutError("timeout"))
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            result = write_segments_canonical(
                ticker="6750",
                period="2026-03-31",
                quarter="FY",
                segments=[{"segment_name": "テスト", "sales": 100, "profit": 50}],
                source="xbrl",
                config={"url": "x", "key": "y"},
            )
        assert result["written"] == 0
        assert result["errors"] >= 1

    def test_no_config_returns_gracefully(self):
        """config=None のケース (db.py が None を返す)"""
        mock_upsert = MagicMock(return_value={"ok": False, "count": 0, "error": "no_write_config"})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            result = write_financials_canonical(
                ticker="6750",
                period="2026-03-31",
                quarter="FY",
                metrics_dict={"sales": 100},
                source="tdnet",
                config={"url": "", "key": ""},
            )
        # 失敗しても例外にならない
        assert isinstance(result, dict)
