"""test_source_priority.py -- source 優先順位テスト"""
from __future__ import annotations

import os
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.pipeline.source_priority import (
    get_priority,
    is_higher_priority,
    all_sources_ordered,
    SOURCE_PRIORITY,
    DEFAULT_PRIORITY,
)


class TestGetPriority:
    def test_summary_xbrl(self):
        assert get_priority("summary_xbrl") == 1

    def test_attachment_xbrl(self):
        assert get_priority("attachment_xbrl") == 2

    def test_html_table(self):
        assert get_priority("html_table") == 3

    def test_pdf_table(self):
        assert get_priority("pdf_table") == 4

    def test_legacy_excel(self):
        assert get_priority("legacy_excel") == 5

    def test_unknown_source(self):
        assert get_priority("some_unknown_source") == DEFAULT_PRIORITY

    def test_alias_xbrl(self):
        assert get_priority("xbrl") == 1

    def test_alias_tdnet(self):
        assert get_priority("tdnet") == 3

    def test_alias_excel_legacy(self):
        assert get_priority("excel_legacy") == 5


class TestIsHigherPriority:
    def test_xbrl_beats_pdf(self):
        assert is_higher_priority("summary_xbrl", "pdf_table") is True

    def test_pdf_loses_to_xbrl(self):
        assert is_higher_priority("pdf_table", "summary_xbrl") is False

    def test_same_priority(self):
        assert is_higher_priority("summary_xbrl", "summary_xbrl") is False

    def test_attachment_beats_html(self):
        assert is_higher_priority("attachment_xbrl", "html_table") is True

    def test_legacy_lowest(self):
        assert is_higher_priority("legacy_excel", "summary_xbrl") is False
        assert is_higher_priority("legacy_excel", "pdf_table") is False


class TestAllSourcesOrdered:
    def test_returns_list(self):
        result = all_sources_ordered()
        assert isinstance(result, list)
        assert len(result) >= 5

    def test_ascending_order(self):
        result = all_sources_ordered()
        priorities = [pri for _, pri in result]
        assert priorities == sorted(priorities)

    def test_no_duplicate_priorities(self):
        result = all_sources_ordered()
        priorities = [pri for _, pri in result]
        assert len(priorities) == len(set(priorities))
