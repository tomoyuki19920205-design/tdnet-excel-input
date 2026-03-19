#!/usr/bin/env python3
"""tests/test_backfill_source_doc_links.py"""
import pytest
import hashlib
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backfill_source_doc_links import (
    _extract_metadata_from_text,
    _find_matching_row,
    _compute_match_score,
    _sha256_file,
    DocCandidate,
)


class TestExtractMetadata:
    """PDFテキストからのメタデータ抽出"""

    def test_basic_tanshin(self):
        text = (
            "2026年3月期 第3四半期決算短信\n"
            "コード番号 6623\n"
            "売上高は増加しました。\n"
        )
        meta = _extract_metadata_from_text(text)
        assert meta["ticker"] == "6623"
        assert meta["period"] == "2026-03-31"
        assert meta["quarter"] == "3Q"

    def test_full_year(self):
        text = (
            "2026年3月期 通期決算短信\n"
            "証券コード: 1832\n"
        )
        meta = _extract_metadata_from_text(text)
        assert meta["ticker"] == "1832"
        assert meta["quarter"] in ("FY", "4Q")

    def test_first_quarter(self):
        text = (
            "2026年9月期 第１四半期\n"
            "コード番号：54610\n"
        )
        meta = _extract_metadata_from_text(text)
        assert meta["ticker"] == "5461"
        assert meta["quarter"] == "1Q"
        assert meta["period"] == "2026-09-30"

    def test_second_quarter(self):
        text = "2026年3月期 第２四半期\nコード番号 4750"
        meta = _extract_metadata_from_text(text)
        assert meta["quarter"] == "2Q"

    def test_no_metadata(self):
        text = "This is just random text without any financial info."
        meta = _extract_metadata_from_text(text)
        assert meta["ticker"] == ""
        assert meta["period"] == ""

    def test_title_snippet(self):
        text = (
            "2026年3月期 第3四半期決算短信\n"
            "コード番号 6623\n"
        )
        meta = _extract_metadata_from_text(text)
        assert "決算短信" in meta["title_snippet"]

    def test_5digit_code_normalized(self):
        text = "コード番号 66230\n2026年3月期"
        meta = _extract_metadata_from_text(text)
        assert meta["ticker"] == "6623"


class TestFindMatchingRow:
    """DB検索テスト"""

    @pytest.fixture
    def db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE quarterly_results (
                id INTEGER PRIMARY KEY,
                company_code TEXT,
                fiscal_year_end TEXT,
                quarter TEXT,
                source_doc_id TEXT,
                source_url TEXT,
                zip_hash TEXT
            )
        """)
        conn.executemany(
            "INSERT INTO quarterly_results VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "66230", "2026-03-31", "1Q", None, None, None),
                (2, "66230", "2026-03-31", "2Q", None, None, None),
                (3, "66230", "2026-03-31", "3Q", "existing_doc", None, None),
                (4, "18320", "2026-03-31", "4Q", None, None, None),
            ],
        )
        conn.commit()
        return conn

    def test_find_null_row(self, db):
        rows = _find_matching_row("6623", "2026-03-31", "1Q", db)
        assert len(rows) == 1
        assert rows[0]["id"] == 1

    def test_skip_already_linked(self, db):
        """source_doc_id IS NOT NULL の行はスキップ"""
        rows = _find_matching_row("6623", "2026-03-31", "3Q", db)
        assert len(rows) == 0

    def test_4digit_expansion(self, db):
        rows = _find_matching_row("6623", "2026-03-31", "2Q", db)
        assert len(rows) == 1
        assert rows[0]["id"] == 2

    def test_fy_and_4q_alias(self, db):
        rows = _find_matching_row("1832", "2026-03-31", "FY", db)
        assert len(rows) == 1
        assert rows[0]["id"] == 4

    def test_no_match(self, db):
        rows = _find_matching_row("9999", "2026-03-31", "1Q", db)
        assert len(rows) == 0


class TestMatchScore:
    """マッチスコア計算"""

    def test_perfect_match(self):
        candidate = DocCandidate(
            filename="test.pdf", filepath="/test.pdf", sha256="abc",
            ticker="6623", period="2026-03-31", quarter="1Q",
            title_snippet="2026年3月期 第1四半期決算短信",
        )
        row = {
            "company_code": "66230",
            "fiscal_year_end": "2026-03-31",
            "quarter": "1Q",
        }
        confidence, note = _compute_match_score(candidate, row)
        assert confidence == "high"

    def test_ticker_only_match(self):
        candidate = DocCandidate(
            filename="test.pdf", filepath="/test.pdf", sha256="abc",
            ticker="6623", period="2025-03-31", quarter="1Q",
        )
        row = {
            "company_code": "66230",
            "fiscal_year_end": "2026-03-31",
            "quarter": "1Q",
        }
        confidence, note = _compute_match_score(candidate, row)
        assert confidence in ("medium", "low")

    def test_fy_alias_match(self):
        candidate = DocCandidate(
            filename="test.pdf", filepath="/test.pdf", sha256="abc",
            ticker="1832", period="2026-03-31", quarter="FY",
            title_snippet="決算短信",
        )
        row = {
            "company_code": "18320",
            "fiscal_year_end": "2026-03-31",
            "quarter": "4Q",
        }
        confidence, note = _compute_match_score(candidate, row)
        # ticker=3 + period=3 + quarter_fy_alias=1 + tanshin=1 = 8 -> high
        assert confidence == "high"


class TestSha256:
    """SHA-256計算"""

    def test_sha256(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            f.write(b"test content")
            f.flush()
            result = _sha256_file(f.name)
        os.unlink(f.name)
        expected = hashlib.sha256(b"test content").hexdigest()
        assert result == expected
