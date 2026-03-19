"""tests/test_filing_id.py — filing_id 決定的生成のテスト"""
from __future__ import annotations

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.backfill.listing_sources.base import (
    make_filing_id,
    normalize_title,
    canonicalize_url,
)


class TestMakeFilingId:
    """filing_id が決定的で安定している。"""

    def test_deterministic(self):
        """同じ入力は同じ ID を返す。"""
        id1 = make_filing_id("2025-03-01", "6750", "決算短信", "https://example.com/doc.pdf")
        id2 = make_filing_id("2025-03-01", "6750", "決算短信", "https://example.com/doc.pdf")
        assert id1 == id2
        assert len(id1) == 24

    def test_different_date_different_id(self):
        """日付が違えば別 ID。"""
        id1 = make_filing_id("2025-03-01", "6750", "決算短信", "https://example.com/doc.pdf")
        id2 = make_filing_id("2025-03-02", "6750", "決算短信", "https://example.com/doc.pdf")
        assert id1 != id2

    def test_different_ticker_different_id(self):
        """ticker が違えば別 ID。"""
        id1 = make_filing_id("2025-03-01", "6750", "決算短信", "https://example.com/doc.pdf")
        id2 = make_filing_id("2025-03-01", "4062", "決算短信", "https://example.com/doc.pdf")
        assert id1 != id2

    def test_title_normalization_absorbed(self):
        """全角/半角差、スペース差は正規化で吸収。"""
        id1 = make_filing_id("2025-03-01", "6750", "決算短信", "https://example.com/doc.pdf")
        id2 = make_filing_id("2025-03-01", "6750", "決算短信　", "https://example.com/doc.pdf")  # 全角スペース
        assert id1 == id2

    def test_url_query_params_ignored(self):
        """URL のクエリパラメータは無視される。"""
        id1 = make_filing_id("2025-03-01", "6750", "決算短信", "https://example.com/doc.pdf")
        id2 = make_filing_id("2025-03-01", "6750", "決算短信", "https://example.com/doc.pdf?v=2")
        assert id1 == id2

    def test_http_https_normalized(self):
        """http/https の違いは正規化される。"""
        id1 = make_filing_id("2025-03-01", "6750", "決算短信", "http://example.com/doc.pdf")
        id2 = make_filing_id("2025-03-01", "6750", "決算短信", "https://example.com/doc.pdf")
        assert id1 == id2

    def test_hex_format(self):
        """24文字の hex string。"""
        fid = make_filing_id("2025-03-01", "6750", "決算短信", "https://example.com/doc.pdf")
        assert len(fid) == 24
        assert all(c in "0123456789abcdef" for c in fid)


class TestNormalizeTitle:
    def test_fullwidth_to_halfwidth(self):
        assert normalize_title("Ａ　Ｂ") == "ab"

    def test_newlines_removed(self):
        assert normalize_title("決算\n短信") == "決算短信"


class TestCanonicalizeUrl:
    def test_strip_query(self):
        assert canonicalize_url("https://ex.com/a?b=1") == "https://ex.com/a"

    def test_http_to_https(self):
        assert canonicalize_url("http://ex.com/a") == "https://ex.com/a"

    def test_strip_trailing_slash(self):
        assert canonicalize_url("https://ex.com/a/") == "https://ex.com/a"
