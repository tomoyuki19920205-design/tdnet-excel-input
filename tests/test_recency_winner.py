"""tests/test_recency_winner.py — recency_key / pick_winner テスト"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.pipeline.recency import make_recency_key, pick_winner, compare_recency


class TestMakeRecencyKey:
    """recency_key の生成ルール。"""

    def test_higher_priority_wins(self):
        """source_priority が低い (高優先) ほど大きい recency_key。"""
        k_xbrl = make_recency_key("summary_xbrl")  # priority=1
        k_pdf = make_recency_key("pdf_table")       # priority=4
        assert k_xbrl > k_pdf

    def test_correction_flag_wins(self):
        """correction_flag=True が False より大きい。"""
        k_corr = make_recency_key("tdnet", correction_flag=True)
        k_no = make_recency_key("tdnet", correction_flag=False)
        assert k_corr > k_no

    def test_later_disclosure_wins(self):
        """disclosure_datetime が遅いほど大きい。"""
        k1 = make_recency_key("tdnet", disclosure_datetime="2026-03-01T10:00:00")
        k2 = make_recency_key("tdnet", disclosure_datetime="2026-03-10T10:00:00")
        assert k2 > k1

    def test_none_datetime_fallback(self):
        """None datetime は 0000... にフォールバック。"""
        k = make_recency_key("tdnet")
        assert "0000-00-00" in k


class TestPickWinner:
    """pick_winner が正しく勝者を選ぶ。"""

    def test_source_priority_determines_winner(self):
        """source_priority → 高優先が勝つ。"""
        rows = [
            {"metric": "sales", "source": "pdf_table",
             "recency_key": make_recency_key("pdf_table")},
            {"metric": "sales", "source": "summary_xbrl",
             "recency_key": make_recency_key("summary_xbrl")},
        ]
        winner = pick_winner(rows)
        assert winner["source"] == "summary_xbrl"

    def test_correction_beats_non_correction(self):
        """同一 source でも correction=True が勝つ。"""
        rows = [
            {"metric": "sales", "source": "tdnet",
             "recency_key": make_recency_key("tdnet", correction_flag=False)},
            {"metric": "sales", "source": "tdnet",
             "recency_key": make_recency_key("tdnet", correction_flag=True)},
        ]
        winner = pick_winner(rows)
        assert "1" in winner["recency_key"]  # correction_flag=True

    def test_later_disclosure_beats_earlier(self):
        """同一 source/correction でも遅い disclosure が勝つ。"""
        rows = [
            {"metric": "sales", "source": "tdnet",
             "recency_key": make_recency_key("tdnet", disclosure_datetime="2026-01-01T00:00:00")},
            {"metric": "sales", "source": "tdnet",
             "recency_key": make_recency_key("tdnet", disclosure_datetime="2026-03-01T00:00:00")},
        ]
        winner = pick_winner(rows)
        assert "2026-03-01" in winner["recency_key"]

    def test_empty_rows(self):
        assert pick_winner([]) is None

    def test_jquants_loses_to_xbrl(self):
        """jquants (priority=6) は xbrl (priority=1) に負ける。"""
        rows = [
            {"metric": "sales", "source": "jquants",
             "recency_key": make_recency_key("jquants")},
            {"metric": "sales", "source": "xbrl",
             "recency_key": make_recency_key("xbrl")},
        ]
        winner = pick_winner(rows)
        assert winner["source"] == "xbrl"

    def test_jquants_loses_to_tdnet(self):
        """jquants (priority=6) は tdnet (priority=3) に負ける。"""
        rows = [
            {"metric": "sales", "source": "jquants",
             "recency_key": make_recency_key("jquants")},
            {"metric": "sales", "source": "tdnet",
             "recency_key": make_recency_key("tdnet")},
        ]
        winner = pick_winner(rows)
        assert winner["source"] == "tdnet"


class TestCompareRecency:
    def test_a_wins(self):
        assert compare_recency("99_1_2026", "90_0_2025") > 0

    def test_b_wins(self):
        assert compare_recency("90_0_2025", "99_1_2026") < 0

    def test_equal(self):
        k = make_recency_key("tdnet")
        assert compare_recency(k, k) == 0
