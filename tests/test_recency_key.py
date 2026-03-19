"""test_recency_key.py -- recency_key 生成 / winner 判定テスト"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.pipeline.recency import make_recency_key, pick_winner, compare_recency


class TestMakeRecencyKey:
    def test_higher_source_gets_higher_key(self):
        """summary_xbrl (priority=1) > legacy_excel (priority=5)"""
        k1 = make_recency_key("summary_xbrl")
        k5 = make_recency_key("legacy_excel")
        assert k1 > k5

    def test_correction_beats_non_correction(self):
        k_corr = make_recency_key("summary_xbrl", correction_flag=True)
        k_norm = make_recency_key("summary_xbrl", correction_flag=False)
        assert k_corr > k_norm

    def test_newer_disclosure_wins(self):
        k_old = make_recency_key("summary_xbrl", disclosure_datetime="2025-01-01T00:00:00")
        k_new = make_recency_key("summary_xbrl", disclosure_datetime="2025-06-01T00:00:00")
        assert k_new > k_old

    def test_newer_updated_at_wins(self):
        k_old = make_recency_key("summary_xbrl", updated_at="2025-01-01T00:00:00")
        k_new = make_recency_key("summary_xbrl", updated_at="2025-06-01T00:00:00")
        assert k_new > k_old

    def test_source_priority_dominates(self):
        """高優先 source は、disclosure_datetime が古くても勝つ"""
        k_xbrl_old = make_recency_key("summary_xbrl", disclosure_datetime="2024-01-01")
        k_pdf_new = make_recency_key("pdf_table", disclosure_datetime="2025-12-31")
        assert k_xbrl_old > k_pdf_new

    def test_correction_dominates_over_date(self):
        """同じ source なら correction が non-correction に勝つ (日付が古くても)"""
        k_corr = make_recency_key("html_table", correction_flag=True, disclosure_datetime="2024-01-01")
        k_new = make_recency_key("html_table", correction_flag=False, disclosure_datetime="2025-12-31")
        assert k_corr > k_new

    def test_none_defaults(self):
        k = make_recency_key("summary_xbrl")
        assert isinstance(k, str)
        assert k.startswith("98_0_")  # 99-1=98, correction=0

    def test_datetime_object(self):
        dt = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        k = make_recency_key("summary_xbrl", disclosure_datetime=dt)
        assert "2025-06-15" in k


class TestPickWinner:
    def test_picks_highest_key(self):
        rows = [
            {"id": 1, "recency_key": make_recency_key("legacy_excel")},
            {"id": 2, "recency_key": make_recency_key("summary_xbrl")},
            {"id": 3, "recency_key": make_recency_key("pdf_table")},
        ]
        winner = pick_winner(rows)
        assert winner["id"] == 2

    def test_empty_list(self):
        assert pick_winner([]) is None

    def test_single_item(self):
        rows = [{"id": 1, "recency_key": "test"}]
        assert pick_winner(rows)["id"] == 1


class TestCompareRecency:
    def test_a_wins(self):
        assert compare_recency("98_1_2025", "94_0_2025") > 0

    def test_b_wins(self):
        assert compare_recency("94_0_2025", "98_1_2025") < 0

    def test_equal(self):
        assert compare_recency("98_0_2025", "98_0_2025") == 0
