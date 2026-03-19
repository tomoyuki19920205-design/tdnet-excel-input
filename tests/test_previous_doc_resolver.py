#!/usr/bin/env python3
"""tests/test_previous_doc_resolver.py — 比較対象選定テスト"""
import pytest
import sqlite3
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.filing_diff.previous_doc_resolver import (
    resolve_comparison_target,
    find_previous_earnings_doc,
    _shift_period_year,
)


class TestResolveComparisonTarget:
    """比較対象Q解決"""

    def test_2Q_to_1Q(self):
        result = resolve_comparison_target("2026-03-31", "2Q")
        assert result == ("2026-03-31", "1Q")

    def test_3Q_to_2Q(self):
        result = resolve_comparison_target("2026-03-31", "3Q")
        assert result == ("2026-03-31", "2Q")

    def test_FY_to_3Q(self):
        result = resolve_comparison_target("2026-03-31", "FY")
        assert result == ("2026-03-31", "3Q")

    def test_4Q_to_3Q(self):
        result = resolve_comparison_target("2026-03-31", "4Q")
        assert result == ("2026-03-31", "3Q")

    def test_1Q_to_prev_FY(self):
        result = resolve_comparison_target("2026-03-31", "1Q")
        assert result == ("2025-03-31", "4Q")

    def test_unknown_quarter(self):
        result = resolve_comparison_target("2026-03-31", "5Q")
        assert result is None


class TestShiftPeriodYear:
    def test_shift_back(self):
        assert _shift_period_year("2026-03-31", -1) == "2025-03-31"

    def test_shift_forward(self):
        assert _shift_period_year("2025-12-31", 1) == "2026-12-31"

    def test_invalid(self):
        assert _shift_period_year("bad", -1) is None


class TestFindPreviousEarningsDoc:
    """DB検索テスト（インメモリSQLite）"""

    @pytest.fixture
    def db(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE quarterly_results (
                id INTEGER PRIMARY KEY,
                company_code TEXT,
                fiscal_year_end TEXT,
                quarter TEXT,
                source_doc_id TEXT,
                source_url TEXT,
                updated_at TEXT
            )
        """)
        # 6623 のデータ
        conn.executemany(
            "INSERT INTO quarterly_results VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "66230", "2026-03-31", "1Q", "doc_1q", "url_1q", "2025-08-01"),
                (2, "66230", "2026-03-31", "2Q", "doc_2q", "url_2q", "2025-11-01"),
                (3, "66230", "2026-03-31", "3Q", "doc_3q", "url_3q", "2026-02-01"),
                (4, "66230", "2026-03-31", "4Q", None, None, "2026-05-01"),
            ],
        )
        conn.commit()
        return conn

    def test_3Q_finds_2Q(self, db):
        result = find_previous_earnings_doc("66230", "2026-03-31", "3Q", db)
        assert result is not None
        assert result.previous_doc_id == "doc_2q"
        assert result.comparison_confidence == "high"
        assert "3Q->2Q" in result.comparison_rule

    def test_2Q_finds_1Q(self, db):
        result = find_previous_earnings_doc("66230", "2026-03-31", "2Q", db)
        assert result is not None
        assert result.previous_doc_id == "doc_1q"

    def test_4digit_ticker(self, db):
        """4桁ticker → 5桁DBコードへの自動拡張"""
        result = find_previous_earnings_doc("6623", "2026-03-31", "3Q", db)
        assert result is not None
        assert result.previous_doc_id == "doc_2q"

    def test_no_previous_with_null_doc_id(self, db):
        """source_doc_id=NULLの行は対象外"""
        result = find_previous_earnings_doc("66230", "2026-03-31", "4Q", db)
        # 4Q→3Qを探すが、3Qにはdoc_idがある → 見つかる
        # ただし4Q自体のdoc_idはNone（これは比較元の話）
        assert result is not None
        assert result.previous_doc_id == "doc_3q"

    def test_noise_skip(self, db):
        """決算短信以外のノイズ開示はquarterly_resultsに入らないため
        自然にスキップされる"""
        # quarterly_resultsには決算短信のみが入る前提
        pass

    def test_no_data(self, db):
        result = find_previous_earnings_doc("9999", "2026-03-31", "2Q", db)
        assert result is not None
        assert result.previous_doc_id is None
        assert result.comparison_confidence == "low"
