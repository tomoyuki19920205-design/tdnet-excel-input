"""tests/test_phase2_source_priority.py — source_priority 拡張テスト"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.pipeline.source_priority import (
    get_priority,
    is_higher_priority,
    all_sources_ordered,
    SOURCE_PRIORITY,
)


class TestJquantsPriority:
    """jquants = 6 が正しく定義されていること。"""

    def test_jquants_priority_is_6(self):
        assert get_priority("jquants") == 6

    def test_jquants_lower_than_pdf(self):
        """jquants は pdf_table (4) より低優先。"""
        assert not is_higher_priority("jquants", "pdf_table")

    def test_jquants_lower_than_xbrl(self):
        """jquants は xbrl (1) より低優先。"""
        assert not is_higher_priority("jquants", "xbrl")


class TestSegmentSources:
    """segment 系 source が実データに合致していること。"""

    def test_xbrl(self):
        assert get_priority("xbrl") == 1

    def test_html(self):
        assert get_priority("html") == 3

    def test_pdf(self):
        assert get_priority("pdf") == 4

    def test_tdnet(self):
        assert get_priority("tdnet") == 3


class TestUnknownSource:
    """未知 source は default (99)。"""

    def test_unknown(self):
        assert get_priority("unknown_source") == 99


class TestAllSourcesOrdered:
    """all_sources_ordered が昇順で返ること。"""

    def test_ordered(self):
        ordered = all_sources_ordered()
        priorities = [p for _, p in ordered]
        assert priorities == sorted(priorities)


class TestPriorityOrder:
    """優先順: summary_xbrl > attachment_xbrl > html_table > pdf_table > legacy_excel > jquants"""

    def test_full_order(self):
        sources = ["summary_xbrl", "attachment_xbrl", "html_table", "pdf_table", "legacy_excel", "jquants"]
        priorities = [get_priority(s) for s in sources]
        assert priorities == sorted(priorities), f"Expected ascending: {priorities}"
