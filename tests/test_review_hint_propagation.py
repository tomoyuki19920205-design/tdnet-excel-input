"""tests/test_review_hint_propagation.py -- V2 quarantine_reason → review_hint 伝搬テスト"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from lib.backfill.retry import classify_review_hint, _V2_REASON_MAP


class TestV2ReasonMapping:
    """V2 quarantine_reason が正しく review_hint にマッピングされる。"""

    def test_no_segment_page_candidate(self):
        hint = classify_review_hint("pdf", "some error", False,
                                     v2_reason="no_segment_page_candidate")
        assert hint == "pdf_no_segment_page_candidate"

    def test_no_segment_table_candidate(self):
        hint = classify_review_hint("pdf", "table not found", False,
                                     v2_reason="no_segment_table_candidate")
        assert hint == "pdf_no_segment_table_candidate"

    def test_no_sales_profit_columns(self):
        hint = classify_review_hint("pdf", "table error", False,
                                     v2_reason="segment_table_found_but_no_sales_profit_columns")
        assert hint == "pdf_no_sales_profit_columns"

    def test_no_rows_extracted(self):
        hint = classify_review_hint("pdf", "table parse", False,
                                     v2_reason="segment_table_found_but_no_rows_extracted")
        assert hint == "pdf_no_rows_extracted"

    def test_v2_reason_takes_priority(self):
        """v2_reason が指定されれば既存のエラー解析より優先。"""
        hint = classify_review_hint("pdf", "table error", False,
                                     v2_reason="no_segment_page_candidate")
        assert hint == "pdf_no_segment_page_candidate"  # not pdf_table_parse_failed

    def test_unknown_v2_reason_falls_back(self):
        """未知の v2_reason は無視して既存ロジックへ。"""
        hint = classify_review_hint("pdf", "table error", False,
                                     v2_reason="unknown_new_reason")
        assert hint == "pdf_table_parse_failed"

    def test_no_v2_reason_uses_existing(self):
        """v2_reason なしは後方互換。"""
        hint = classify_review_hint("pdf", "table error", False)
        assert hint == "pdf_table_parse_failed"

    def test_backward_compat_download(self):
        hint = classify_review_hint("download", "404 not found", False)
        assert hint == "download_not_found"

    def test_backward_compat_xbrl(self):
        hint = classify_review_hint("xbrl", "parse error", False)
        assert hint == "xbrl_parse_failed"

    def test_all_v2_reasons_mapped(self):
        """全 V2 reason がマッピングに存在する。"""
        expected_reasons = [
            "no_segment_page_candidate",
            "no_segment_table_candidate",
            "segment_table_found_but_no_sales_profit_columns",
            "segment_table_found_but_no_rows_extracted",
        ]
        for reason in expected_reasons:
            assert reason in _V2_REASON_MAP
            hint = classify_review_hint("pdf", "", False, v2_reason=reason)
            assert hint.startswith("pdf_")
