"""test_sync_segments_sqlite.py -- sync_segments.py 恒久対策テスト"""
from __future__ import annotations

import os
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from tools.sync_segments import (
    _is_valid_sqlite_segment,
    _classify_skip_reason,
    _SKIP_SEGMENT_NAMES,
    _QUARTER_MAP,
    build_parser,
    count_sqlite_valid_rows,
)


# ============================================================
# _classify_skip_reason -- 詳細分類
# ============================================================
class TestClassifySkipReason:
    def test_valid_returns_empty(self):
        row = {"segment_name": "環境システム売上(円)", "segment_sales": 2917.0, "segment_profit": 297.0, "quarter": "1Q"}
        assert _classify_skip_reason(row) == ""

    def test_header_uriage(self):
        row = {"segment_name": "売上", "segment_sales": 0.0, "segment_profit": 0.0, "quarter": "1Q"}
        assert _classify_skip_reason(row) == "header"

    def test_header_rieki(self):
        row = {"segment_name": "利益", "segment_sales": 0.0, "segment_profit": 0.0, "quarter": "1Q"}
        assert _classify_skip_reason(row) == "header"

    def test_unknown(self):
        row = {"segment_name": "UNKNOWN_8", "segment_sales": 100.0, "segment_profit": 10.0, "quarter": "1Q"}
        assert _classify_skip_reason(row) == "unknown"

    def test_zero_value(self):
        row = {"segment_name": "環境システム", "segment_sales": 0, "segment_profit": 0, "quarter": "1Q"}
        assert _classify_skip_reason(row) == "zero_value"

    def test_empty_name(self):
        row = {"segment_name": "", "segment_sales": 100.0, "segment_profit": 10.0, "quarter": "1Q"}
        assert _classify_skip_reason(row) == "empty_name"

    def test_invalid_quarter(self):
        row = {"segment_name": "物流", "segment_sales": 100.0, "segment_profit": 10.0, "quarter": "?Q"}
        assert _classify_skip_reason(row) == "invalid_quarter"

    def test_ratio(self):
        row = {"segment_name": "物流", "segment_sales": 0.738, "segment_profit": 0.1, "quarter": "1Q"}
        assert _classify_skip_reason(row) == "ratio"

    def test_value_error(self):
        row = {"segment_name": "#VALUE!", "segment_sales": 0.0, "segment_profit": 0.0, "quarter": "1Q"}
        assert _classify_skip_reason(row) == "header"


# ============================================================
# _is_valid_sqlite_segment
# ============================================================
class TestIsValidSqliteSegment:
    def test_valid_row(self):
        row = {"segment_name": "環境システム売上(円)", "segment_sales": 2917.0, "segment_profit": 297.0, "quarter": "1Q"}
        assert _is_valid_sqlite_segment(row) is True

    def test_skip_header_uriage(self):
        row = {"segment_name": "売上", "segment_sales": 0.0, "segment_profit": 0.0, "quarter": "1Q"}
        assert _is_valid_sqlite_segment(row) is False

    def test_skip_unknown(self):
        row = {"segment_name": "UNKNOWN_8", "segment_sales": 100.0, "segment_profit": 10.0, "quarter": "1Q"}
        assert _is_valid_sqlite_segment(row) is False

    def test_skip_all_zero(self):
        row = {"segment_name": "環境システム", "segment_sales": 0, "segment_profit": 0, "quarter": "1Q"}
        assert _is_valid_sqlite_segment(row) is False

    def test_valid_with_negative_profit(self):
        row = {"segment_name": "管工機材売上(円)", "segment_sales": 2368.0, "segment_profit": -73.0, "quarter": "2Q"}
        assert _is_valid_sqlite_segment(row) is True

    def test_valid_4q(self):
        row = {"segment_name": "物流", "segment_sales": 100.0, "segment_profit": 10.0, "quarter": "4Q"}
        assert _is_valid_sqlite_segment(row) is True


# ============================================================
# CLI: デフォルト安全テスト
# ============================================================
class TestCLIDefaults:
    """通常実行で sqlite が同期対象になることを保証"""

    def test_default_includes_sqlite(self):
        """オプションなしではデフォルト sqlite 含む"""
        parser = build_parser()
        opts = parser.parse_args(["--apply"])
        assert opts.xbrl_only is False

    def test_default_dry_run_includes_sqlite(self):
        parser = build_parser()
        opts = parser.parse_args(["--dry-run"])
        assert opts.xbrl_only is False

    def test_xbrl_only_excludes_sqlite(self):
        """--xbrl-only を指定した場合のみ sqlite を除外"""
        parser = build_parser()
        opts = parser.parse_args(["--apply", "--xbrl-only"])
        assert opts.xbrl_only is True

    def test_include_sqlite_backward_compat(self):
        """旧 --include-sqlite は受け付けるが動作に影響しない"""
        parser = build_parser()
        opts = parser.parse_args(["--apply", "--include-sqlite"])
        assert opts.xbrl_only is False

    def test_include_sqlite_not_in_default_sync_mode(self):
        """sync_mode は xbrl_only で決まる"""
        parser = build_parser()
        opts = parser.parse_args(["--apply"])
        include_sqlite = not opts.xbrl_only
        assert include_sqlite is True

    def test_xbrl_only_sync_mode(self):
        parser = build_parser()
        opts = parser.parse_args(["--apply", "--xbrl-only"])
        include_sqlite = not opts.xbrl_only
        assert include_sqlite is False


# ============================================================
# QUARTER_MAP
# ============================================================
class TestQuarterMap:
    def test_4q_to_fy(self):
        assert _QUARTER_MAP.get("4Q") == "FY"

    def test_1q_passthrough(self):
        assert _QUARTER_MAP.get("1Q", "1Q") == "1Q"


# ============================================================
# SKIP_SEGMENT_NAMES
# ============================================================
class TestSkipNames:
    def test_uriage_in_skip(self):
        assert "売上" in _SKIP_SEGMENT_NAMES

    def test_rieki_in_skip(self):
        assert "利益" in _SKIP_SEGMENT_NAMES

    def test_empty_in_skip(self):
        assert "" in _SKIP_SEGMENT_NAMES

    def test_real_name_not_in_skip(self):
        assert "環境システム売上(円)" not in _SKIP_SEGMENT_NAMES


# ============================================================
# 統合テスト
# ============================================================
class TestMixedSource:
    """TDNET と Excel のソースが混在しても片方だけ落ちないことを確認"""

    def test_excel_legacy_valid(self):
        row = {"segment_name": "環境システム売上(円)", "segment_sales": 2917.0,
               "segment_profit": 297.0, "quarter": "1Q", "data_source": "excel_legacy"}
        assert _is_valid_sqlite_segment(row) is True

    def test_tdnet_valid(self):
        row = {"segment_name": "Domestic Beverage Business", "segment_sales": 50000.0,
               "segment_profit": 5000.0, "quarter": "3Q", "data_source": "tdnet"}
        assert _is_valid_sqlite_segment(row) is True

    def test_both_sources_pass(self):
        excel_row = {"segment_name": "管工機材売上(円)", "segment_sales": 2368.0,
                     "segment_profit": -73.0, "quarter": "2Q", "data_source": "excel_legacy"}
        tdnet_row = {"segment_name": "日本", "segment_sales": 30000.0,
                     "segment_profit": 3000.0, "quarter": "FY", "data_source": "tdnet"}
        assert _is_valid_sqlite_segment(excel_row) is True
        assert _is_valid_sqlite_segment(tdnet_row) is True

    def test_1736_specific_case(self):
        """1736 障害の実データパターンを再現"""
        rows = [
            {"segment_name": "売上", "segment_sales": 0.0, "segment_profit": 0.0, "quarter": "1Q"},
            {"segment_name": "環境システム売上(円)", "segment_sales": 2917.0, "segment_profit": 297.0, "quarter": "1Q"},
            {"segment_name": "管工機材売上(円)", "segment_sales": 2368.0, "segment_profit": -73.0, "quarter": "1Q"},
        ]
        results = [_is_valid_sqlite_segment(r) for r in rows]
        assert results == [False, True, True]


# ============================================================
# ガードテスト: count_sqlite_valid_rows
# ============================================================
class TestGuard:
    def test_count_nonexistent_db(self):
        """存在しない DB はゼロ"""
        assert count_sqlite_valid_rows("/tmp/no_such_db_12345.db") == 0


# ============================================================
# Summary にモードが出ることの確認 (CLI 解析のみ)
# ============================================================
class TestSummaryMode:
    def test_default_sync_mode_label(self):
        parser = build_parser()
        opts = parser.parse_args(["--apply"])
        sync_mode = "XBRL + SQLite" if not opts.xbrl_only else "XBRL ONLY"
        assert sync_mode == "XBRL + SQLite"

    def test_xbrl_only_sync_mode_label(self):
        parser = build_parser()
        opts = parser.parse_args(["--apply", "--xbrl-only"])
        sync_mode = "XBRL + SQLite" if not opts.xbrl_only else "XBRL ONLY"
        assert sync_mode == "XBRL ONLY"
