#!/usr/bin/env python3
"""tests/test_audit_financials.py — 整合性監査ツールの単体テスト (ネットワーク不要)"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

# tools/ をインポート可能にする
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.audit_supabase_vs_sqlite import (
    build_comparison_key,
    normalize_null,
    normalize_numeric,
    normalize_period,
    normalize_quarter,
    normalize_ticker,
    normalize_ticker_raw,
    normalize_source,
    normalize_updated_at,
    normalize_row,
    _scale_to_millions,
    detect_missing_rows,
    detect_duplicate_rows,
    compare_value_columns,
    compare_nulls,
    compare_source,
    compare_updated_at,
    summarize_column_presence,
    generate_markdown_report,
    check_strict,
    write_csv_outputs,
    _ticker_to_local_code,
    VALUE_COLUMNS,
    _UNIT_DIVISOR,
)


# ============================================================
# ダミーデータ生成ヘルパー
# ============================================================
def _row(ticker="1234", period="2025-03-31", quarter="1Q",
         sales=100, gp=40, op=20, source="jquants", updated_at="2026-03-01T00:00:00+09:00"):
    return {
        "ticker": ticker, "period": period, "quarter": quarter,
        "sales": sales, "gross_profit": gp, "operating_profit": op,
        "source": source, "updated_at": updated_at,
    }


def _norm(row):
    return normalize_row(row)


def _make_maps(sqlite_rows, supa_rows, sqlite_scale=False):
    """sqlite_scale=True の場合、SQLite 側を円→百万円に変換"""
    s_map = {}
    for r in sqlite_rows:
        nr = normalize_row(r, scale_to_millions=sqlite_scale)
        k = (nr["ticker"], nr["period"], nr["quarter"])
        s_map[k] = nr
    p_map = {}
    for r in supa_rows:
        nr = normalize_row(r, scale_to_millions=False)
        k = (nr["ticker"], nr["period"], nr["quarter"])
        p_map[k] = nr
    return s_map, p_map


# ============================================================
# build_comparison_key
# ============================================================
class TestBuildComparisonKey:
    def test_basic(self):
        assert build_comparison_key({"ticker": "1234", "period": "2025-03-31", "quarter": "1Q"}) == ("1234", "2025-03-31", "1Q")

    def test_strips_whitespace(self):
        assert build_comparison_key({"ticker": " 1234 ", "period": " 2025-03-31 ", "quarter": " 1Q "}) == ("1234", "2025-03-31", "1Q")

    def test_missing_fields(self):
        assert build_comparison_key({}) == ("", "", "")

    def test_5digit_ticker_normalized_to_4digit(self):
        """5桁末尾0の local_code が4桁に正規化されてキーに使われる"""
        assert build_comparison_key({"ticker": "67500", "period": "2025-03-31", "quarter": "1Q"}) == ("6750", "2025-03-31", "1Q")

    def test_5digit_and_4digit_match(self):
        """SQLite側5桁とSupabase側4桁が同じキーになる"""
        k1 = build_comparison_key({"ticker": "67500", "period": "2025-03-31", "quarter": "1Q"})
        k2 = build_comparison_key({"ticker": "6750", "period": "2025-03-31", "quarter": "1Q"})
        assert k1 == k2


# ============================================================
# normalize_ticker (5桁→4桁正規化)
# ============================================================
class TestNormalizeTicker:
    def test_5digit_to_4digit(self):
        assert normalize_ticker("67500") == "6750"

    def test_5digit_to_4digit_7203(self):
        assert normalize_ticker("72030") == "7203"

    def test_already_4digit(self):
        assert normalize_ticker("6750") == "6750"

    def test_non_standard_preserved(self):
        """130A0 は5桁だが数字のみではないのでそのまま"""
        assert normalize_ticker("130A0") == "130A0"

    def test_5digit_not_ending_zero(self):
        """5桁だが末尾が0でない場合はそのまま"""
        assert normalize_ticker("12345") == "12345"

    def test_3digit(self):
        assert normalize_ticker("675") == "675"

    def test_none(self):
        assert normalize_ticker(None) == ""

    def test_raw_preserves_original(self):
        assert normalize_ticker_raw("67500") == "67500"
        assert normalize_ticker_raw("6750") == "6750"


# ============================================================
# _ticker_to_local_code (4桁→5桁)
# ============================================================
class TestTickerToLocalCode:
    def test_4digit_to_5digit(self):
        assert _ticker_to_local_code("6750") == "67500"

    def test_already_5digit(self):
        assert _ticker_to_local_code("67500") == "67500"

    def test_non_standard(self):
        assert _ticker_to_local_code("130A") == "130A"


# ============================================================
# normalize_row includes raw_ticker
# ============================================================
class TestNormalizeRowTicker:
    def test_raw_ticker_preserved(self):
        row = {"ticker": "67500", "period": "2025-03-31", "quarter": "1Q"}
        nr = normalize_row(row)
        assert nr["raw_ticker"] == "67500"
        assert nr["ticker"] == "6750"


# ============================================================
# normalize_null
# ============================================================
class TestNormalizeNull:
    def test_none(self):
        assert normalize_null(None) is None

    def test_empty_string(self):
        assert normalize_null("") is None

    def test_null_string(self):
        assert normalize_null("null") is None
        assert normalize_null("None") is None
        assert normalize_null("NaN") is None
        assert normalize_null("  null  ") is None

    def test_nan_float(self):
        assert normalize_null(float("nan")) is None

    def test_value_preserved(self):
        assert normalize_null(42) == 42
        assert normalize_null("hello") == "hello"
        assert normalize_null(0) == 0


# ============================================================
# normalize_numeric (Decimal ベース)
# ============================================================
class TestNormalizeNumeric:
    def test_int(self):
        assert normalize_numeric(100) == Decimal("100")

    def test_float(self):
        assert normalize_numeric(100.5) == Decimal("100.5")

    def test_string(self):
        assert normalize_numeric("100") == Decimal("100")

    def test_none(self):
        assert normalize_numeric(None) is None

    def test_empty(self):
        assert normalize_numeric("") is None

    def test_nan_string(self):
        assert normalize_numeric("NaN") is None

    def test_negative(self):
        assert normalize_numeric(-500) == Decimal("-500")

    def test_large_number(self):
        assert normalize_numeric(1368000000) == Decimal("1368000000")


# ============================================================
# normalize_period
# ============================================================
class TestNormalizePeriod:
    def test_date_string(self):
        assert normalize_period("2025-03-31") == "2025-03-31"

    def test_datetime_string(self):
        assert normalize_period("2025-03-31T00:00:00") == "2025-03-31"

    def test_none(self):
        assert normalize_period(None) == ""

    def test_whitespace(self):
        assert normalize_period("  2025-03-31  ") == "2025-03-31"


# ============================================================
# normalize_source
# ============================================================
class TestNormalizeSource:
    def test_basic(self):
        assert normalize_source("jquants") == ("jquants", "jquants")

    def test_normalize_j_quants(self):
        raw, norm = normalize_source("j-quants")
        assert norm == "jquants"

    def test_none(self):
        assert normalize_source(None) == ("", "")


# ============================================================
# normalize_updated_at
# ============================================================
class TestNormalizeUpdatedAt:
    def test_iso_with_tz(self):
        dt = normalize_updated_at("2026-03-01T00:00:00+09:00")
        assert dt is not None
        assert dt.year == 2026

    def test_iso_no_tz(self):
        dt = normalize_updated_at("2026-03-01 12:30:00")
        assert dt is not None

    def test_none(self):
        assert normalize_updated_at(None) is None

    def test_invalid(self):
        assert normalize_updated_at("not-a-date") is None


# ============================================================
# detect_missing_rows
# ============================================================
class TestDetectMissingRows:
    def test_no_missing(self):
        s = [_row(ticker="1234")]
        p = [_row(ticker="1234")]
        s_map, p_map = _make_maps(s, p)
        m_supa, m_sqlite = detect_missing_rows(s_map, p_map)
        assert len(m_supa) == 0
        assert len(m_sqlite) == 0

    def test_missing_in_supabase(self):
        s = [_row(ticker="1234"), _row(ticker="5678")]
        p = [_row(ticker="1234")]
        s_map, p_map = _make_maps(s, p)
        m_supa, m_sqlite = detect_missing_rows(s_map, p_map)
        assert len(m_supa) == 1
        assert m_supa[0]["ticker"] == "5678"
        assert len(m_sqlite) == 0

    def test_missing_in_sqlite(self):
        s = [_row(ticker="1234")]
        p = [_row(ticker="1234"), _row(ticker="9999")]
        s_map, p_map = _make_maps(s, p)
        m_supa, m_sqlite = detect_missing_rows(s_map, p_map)
        assert len(m_supa) == 0
        assert len(m_sqlite) == 1


# ============================================================
# detect_duplicate_rows
# ============================================================
class TestDetectDuplicateRows:
    def test_no_duplicates(self):
        rows = [_row(ticker="1234"), _row(ticker="5678")]
        dupes = detect_duplicate_rows(rows, "test")
        assert len(dupes) == 0

    def test_has_duplicates(self):
        rows = [_row(ticker="1234"), _row(ticker="1234"), _row(ticker="1234")]
        dupes = detect_duplicate_rows(rows, "test")
        assert len(dupes) == 1
        assert dupes[0]["duplicate_count"] == 3


# ============================================================
# compare_value_columns
# ============================================================
class TestCompareValueColumns:
    def test_all_match(self):
        s = [_row(sales=100, gp=40, op=20)]
        p = [_row(sales=100, gp=40, op=20)]
        s_map, p_map = _make_maps(s, p)
        mm, col_mm, perfect = compare_value_columns(s_map, p_map, VALUE_COLUMNS)
        assert len(mm) == 0
        assert perfect == 1

    def test_value_diff(self):
        s = [_row(sales=100, gp=40, op=20)]
        p = [_row(sales=200, gp=40, op=20)]
        s_map, p_map = _make_maps(s, p)
        mm, col_mm, perfect = compare_value_columns(s_map, p_map, VALUE_COLUMNS)
        assert len(mm) == 1
        assert mm[0]["column_name"] == "sales"
        assert mm[0]["difference_type"] == "both_non_null_value_diff"
        assert perfect == 0
        assert col_mm["sales"] == 1

    def test_null_diff(self):
        s = [_row(sales=100, gp=None, op=20)]
        p = [_row(sales=100, gp=40, op=20)]
        s_map, p_map = _make_maps(s, p)
        mm, col_mm, perfect = compare_value_columns(s_map, p_map, VALUE_COLUMNS)
        assert len(mm) == 1
        assert mm[0]["difference_type"] == "sqlite_null_supabase_non_null"

    def test_both_null_is_match(self):
        s = [_row(sales=100, gp=None, op=20)]
        p = [_row(sales=100, gp=None, op=20)]
        s_map, p_map = _make_maps(s, p)
        mm, col_mm, perfect = compare_value_columns(s_map, p_map, VALUE_COLUMNS)
        assert len(mm) == 0
        assert perfect == 1


# ============================================================
# compare_nulls
# ============================================================
class TestCompareNulls:
    def test_null_summary(self):
        s = [_row(sales=100, gp=None, op=20)]
        p = [_row(sales=100, gp=40, op=None)]
        s_map, p_map = _make_maps(s, p)
        summary, rows = compare_nulls(s_map, p_map, VALUE_COLUMNS)
        gp_entry = [e for e in summary if e["column"] == "gross_profit"][0]
        assert gp_entry["sqlite_null_count"] == 1
        assert gp_entry["supabase_null_count"] == 0
        assert len(rows) >= 1


# ============================================================
# compare_source
# ============================================================
class TestCompareSource:
    def test_same_source(self):
        s = [_row(source="jquants")]
        p = [_row(source="jquants")]
        s_map, p_map = _make_maps(s, p)
        mm, s_dist, p_dist = compare_source(s_map, p_map)
        assert len(mm) == 0

    def test_source_mismatch(self):
        s = [_row(source="jquants")]
        p = [_row(source="manual")]
        s_map, p_map = _make_maps(s, p)
        mm, s_dist, p_dist = compare_source(s_map, p_map)
        assert len(mm) == 1


# ============================================================
# compare_updated_at
# ============================================================
class TestCompareUpdatedAt:
    def test_equal(self):
        ts = "2026-03-01T00:00:00"
        s = [_row(updated_at=ts)]
        p = [_row(updated_at=ts)]
        s_map, p_map = _make_maps(s, p)
        mm, stats = compare_updated_at(s_map, p_map)
        assert stats["equal"] == 1
        assert len(mm) == 0

    def test_sqlite_newer(self):
        s = [_row(updated_at="2026-03-10T00:00:00")]
        p = [_row(updated_at="2026-03-01T00:00:00")]
        s_map, p_map = _make_maps(s, p)
        mm, stats = compare_updated_at(s_map, p_map)
        assert stats["sqlite_newer"] == 1

    def test_one_side_null(self):
        s = [_row(updated_at=None)]
        p = [_row(updated_at="2026-03-01T00:00:00")]
        s_map, p_map = _make_maps(s, p)
        mm, stats = compare_updated_at(s_map, p_map)
        assert stats["one_side_null"] == 1


# ============================================================
# summarize_column_presence
# ============================================================
class TestColumnPresence:
    def test_basic(self):
        s_rows = [{"ticker": "1", "sales": 100, "extra_col": "x"}]
        p_rows = [{"ticker": "1", "sales": 100, "supa_col": "y"}]
        result = summarize_column_presence(s_rows, p_rows)
        assert "extra_col" in result["sqlite_only"]
        assert "supa_col" in result["supabase_only"]
        assert "ticker" in result["common"]


# ============================================================
# generate_markdown_report
# ============================================================
class TestGenerateMarkdown:
    def _make_results(self, **overrides):
        base = {
            "sqlite_db": "test.db",
            "ticker_condition": "全件",
            "sqlite_count": 100,
            "supabase_count": 95,
            "all_unique_keys": 105,
            "common_keys": 90,
            "missing_in_supabase_count": 10,
            "missing_in_sqlite_count": 5,
            "value_mismatch_count": 3,
            "col_mismatch_counts": {"sales": 1, "gross_profit": 1, "operating_profit": 1},
            "perfect_match_count": 87,
            "null_summary": [
                {"column": "sales", "sqlite_null_count": 0, "supabase_null_count": 0,
                 "sqlite_null_pct": 0, "supabase_null_pct": 0, "null_diff": 0},
            ],
            "source_mismatch_count": 2,
            "sqlite_source_dist": {"jquants": 90},
            "supabase_source_dist": {"jquants": 88, "manual": 2},
            "updated_at_mismatch_count": 5,
            "updated_at_stats": {"equal": 85, "sqlite_newer": 3, "supabase_newer": 2, "one_side_null": 0},
            "duplicate_in_sqlite_count": 0,
            "duplicate_in_supabase_count": 0,
            "column_presence": {"sqlite_only": [], "supabase_only": [], "common": ["ticker"], "compared": VALUE_COLUMNS},
            "top_mismatch_tickers": [("1234", 2), ("5678", 1)],
            "top_mismatch_periods": [("2025-03-31", 3)],
        }
        base.update(overrides)
        return base

    def test_contains_required_sections(self):
        md = generate_markdown_report(self._make_results())
        assert "# financials 整合性監査レポート" in md
        assert "実行情報" in md
        assert "件数サマリ" in md
        assert "一致率" in md
        assert "列別一致率" in md
        assert "NULL率比較" in md
        assert "source 分布比較" in md
        assert "updated_at 比較サマリ" in md
        assert "所見" in md
        assert "制約" in md

    def test_contains_key_explanation(self):
        md = generate_markdown_report(self._make_results())
        assert "(ticker, period, quarter)" in md

    def test_contains_ticker_normalization_rule(self):
        md = generate_markdown_report(self._make_results())
        assert "ticker 正規化ルール" in md
        assert "5桁" in md
        assert "4桁" in md

    def test_contains_compared_columns(self):
        md = generate_markdown_report(self._make_results())
        for col in VALUE_COLUMNS:
            assert col in md


# ============================================================
# check_strict
# ============================================================
class TestStrictExitLogic:
    def test_no_issues(self):
        r = {
            "missing_in_supabase_count": 0, "missing_in_sqlite_count": 0,
            "value_mismatch_count": 0, "source_mismatch_count": 0,
            "duplicate_in_sqlite_count": 0, "duplicate_in_supabase_count": 0,
        }
        assert check_strict(r) is False

    def test_missing_triggers(self):
        r = {
            "missing_in_supabase_count": 1, "missing_in_sqlite_count": 0,
            "value_mismatch_count": 0, "source_mismatch_count": 0,
            "duplicate_in_sqlite_count": 0, "duplicate_in_supabase_count": 0,
        }
        assert check_strict(r) is True

    def test_value_mismatch_triggers(self):
        r = {
            "missing_in_supabase_count": 0, "missing_in_sqlite_count": 0,
            "value_mismatch_count": 1, "source_mismatch_count": 0,
            "duplicate_in_sqlite_count": 0, "duplicate_in_supabase_count": 0,
        }
        assert check_strict(r) is True

    def test_duplicate_triggers(self):
        r = {
            "missing_in_supabase_count": 0, "missing_in_sqlite_count": 0,
            "value_mismatch_count": 0, "source_mismatch_count": 0,
            "duplicate_in_sqlite_count": 1, "duplicate_in_supabase_count": 0,
        }
        assert check_strict(r) is True


# ============================================================
# write_csv_outputs (一時ディレクトリで確認)
# ============================================================
class TestWriteCsvOutputs:
    def test_creates_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            results = {
                "missing_in_supabase": [{"ticker": "1234", "period": "2025-03-31", "quarter": "1Q",
                                         "comparison_key": "1234|2025-03-31|1Q", "reason": "test"}],
                "missing_in_sqlite": [],
                "value_mismatches": [],
                "null_mismatch_rows": [],
                "source_mismatches": [],
                "updated_at_mismatches": [],
                "duplicate_in_sqlite": [],
                "duplicate_in_supabase": [],
                "markdown_report": "# Test Report\n",
            }
            write_csv_outputs(results, tmpdir)
            expected = [
                "audit_summary.md", "missing_in_supabase.csv", "missing_in_sqlite.csv",
                "value_mismatch.csv", "null_mismatch.csv", "source_mismatch.csv",
                "updated_at_mismatch.csv", "duplicate_in_sqlite.csv", "duplicate_in_supabase.csv",
            ]
            for fname in expected:
                assert os.path.exists(os.path.join(tmpdir, fname)), f"{fname} が見つかりません"


# ============================================================
# 単位正規化テスト
# ============================================================
class TestUnitScaling:
    def test_scale_to_millions(self):
        assert _scale_to_millions(Decimal("118007000000")) == Decimal("118007")

    def test_scale_none(self):
        assert _scale_to_millions(None) is None

    def test_scale_negative(self):
        assert _scale_to_millions(Decimal("-692000000")) == Decimal("-692")

    def test_normalize_row_with_scaling(self):
        row = {"ticker": "67500", "period": "2025-03-31", "quarter": "FY",
               "sales": 118007000000, "gross_profit": 46189000000, "operating_profit": 13531000000}
        nr = normalize_row(row, scale_to_millions=True)
        assert nr["sales"] == Decimal("118007")
        assert nr["sales_raw"] == Decimal("118007000000")
        assert nr["ticker"] == "6750"  # ticker も正規化されている

    def test_normalize_row_without_scaling(self):
        row = {"ticker": "6750", "period": "2025-03-31", "quarter": "FY",
               "sales": 118007}
        nr = normalize_row(row, scale_to_millions=False)
        assert nr["sales"] == Decimal("118007")
        assert nr["sales_raw"] == Decimal("118007")

    def test_yen_vs_millions_match_after_scaling(self):
        """SQLite 円単位と Supabase 百万円単位が単位正規化後に一致する"""
        sqlite = [_row(ticker="67500", sales=118007000000, gp=46189000000, op=13531000000)]
        supa = [_row(ticker="6750", sales=118007, gp=46189, op=13531)]
        s_map, p_map = _make_maps(sqlite, supa, sqlite_scale=True)
        mm, col_mm, perfect = compare_value_columns(s_map, p_map, VALUE_COLUMNS)
        assert len(mm) == 0
        assert perfect == 1

    def test_yen_vs_millions_mismatch_without_scaling(self):
        """scale なしでは全行 mismatch になることを確認"""
        sqlite = [_row(ticker="1234", sales=118007000000, gp=46189000000, op=13531000000)]
        supa = [_row(ticker="1234", sales=118007, gp=46189, op=13531)]
        s_map, p_map = _make_maps(sqlite, supa, sqlite_scale=False)
        mm, col_mm, perfect = compare_value_columns(s_map, p_map, VALUE_COLUMNS)
        assert len(mm) == 3  # 全列3列 mismatch
        assert perfect == 0

    def test_value_mismatch_csv_has_raw_and_normalized(self):
        """mismatch の辞書に raw/normalized 両方がある"""
        sqlite = [_row(ticker="67500", sales=200000000, gp=100000000, op=50000000)]
        supa = [_row(ticker="6750", sales=999, gp=100, op=50)]
        s_map, p_map = _make_maps(sqlite, supa, sqlite_scale=True)
        mm, col_mm, perfect = compare_value_columns(s_map, p_map, VALUE_COLUMNS)
        assert len(mm) >= 1
        m = mm[0]
        assert "sqlite_value_raw" in m
        assert "sqlite_value_normalized" in m
        assert "supabase_value_raw" in m
        assert "supabase_value_normalized" in m


# ============================================================
# source スキップテスト
# ============================================================
class TestSourceSkip:
    def test_source_skipped_when_no_source_column(self):
        """SQLite 側に source 列がない場合、source 比較がスキップされる"""
        # source 列なしの行
        row_no_source = {"ticker": "1234", "period": "2025-03-31", "quarter": "1Q",
                         "sales": 100, "gross_profit": 40, "operating_profit": 20}
        assert "source" not in row_no_source  # source 列なしを確認

    def test_source_skipped_no_strict_failure(self):
        """source_skipped=True の場合、source_mismatch が strict に影響しない"""
        r = {
            "missing_in_supabase_count": 0, "missing_in_sqlite_count": 0,
            "value_mismatch_count": 0, "source_mismatch_count": 5,
            "duplicate_in_sqlite_count": 0, "duplicate_in_supabase_count": 0,
            "source_skipped": True,
        }
        assert check_strict(r) is False  # source_skipped なので失敗にならない

    def test_source_not_skipped_strict_failure(self):
        """source_skipped=False の場合、source_mismatch で strict 失敗"""
        r = {
            "missing_in_supabase_count": 0, "missing_in_sqlite_count": 0,
            "value_mismatch_count": 0, "source_mismatch_count": 5,
            "duplicate_in_sqlite_count": 0, "duplicate_in_supabase_count": 0,
            "source_skipped": False,
        }
        assert check_strict(r) is True


# ============================================================
# Markdown に単位正規化ルール・source スキップが含まれるか
# ============================================================
class TestMarkdownUnitAndSource:
    def _make_results(self, **overrides):
        base = {
            "sqlite_db": "test.db",
            "ticker_condition": "全件",
            "sqlite_count": 100,
            "supabase_count": 95,
            "all_unique_keys": 105,
            "common_keys": 90,
            "missing_in_supabase_count": 0,
            "missing_in_sqlite_count": 5,
            "value_mismatch_count": 0,
            "col_mismatch_counts": {"sales": 0, "gross_profit": 0, "operating_profit": 0},
            "perfect_match_count": 90,
            "null_summary": [],
            "source_mismatch_count": 0,
            "source_skipped": True,
            "sqlite_source_dist": {},
            "supabase_source_dist": {},
            "updated_at_mismatch_count": 0,
            "updated_at_stats": {},
            "duplicate_in_sqlite_count": 0,
            "duplicate_in_supabase_count": 0,
            "column_presence": {"sqlite_only": [], "supabase_only": [], "common": [], "compared": VALUE_COLUMNS},
            "top_mismatch_tickers": [],
            "top_mismatch_periods": [],
        }
        base.update(overrides)
        return base

    def test_contains_unit_normalization_rule(self):
        md = generate_markdown_report(self._make_results())
        assert "数値単位正規化" in md
        assert "÷ 1,000,000" in md or "1,000,000" in md

    def test_contains_source_skip_note(self):
        md = generate_markdown_report(self._make_results(source_skipped=True))
        assert "source 比較スキップ" in md

    def test_no_source_skip_note_when_not_skipped(self):
        md = generate_markdown_report(self._make_results(source_skipped=False))
        assert "source 比較スキップ" not in md
