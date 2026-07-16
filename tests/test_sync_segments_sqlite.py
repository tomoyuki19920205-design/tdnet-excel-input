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

    def test_zero_values_are_valid(self):
        row = {"segment_name": "環境システム", "segment_sales": 0, "segment_profit": 0, "quarter": "1Q"}
        assert _classify_skip_reason(row) == ""

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

    def test_valid_all_zero(self):
        row = {"segment_name": "環境システム", "segment_sales": 0, "segment_profit": 0, "quarter": "1Q"}
        assert _is_valid_sqlite_segment(row) is True

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


# ============================================================
# 再発防止テスト: backfill_v4_pdf source 引き継ぎ
# ============================================================
# 問題の経緯:
#   sync_segments.py の canonical dual-write で source="excel_legacy" が
#   ハードコードされており、SQLite の data_source='backfill_v4_pdf' が
#   Supabase canonical_segments では source='excel_legacy' (priority=5) に
#   格下げされ、xbrl:Other (priority=1) に敗北していた。
#
# 修正内容:
#   sync_sqlite_segments() の canonical dual-write で
#   source = rdict.get("data_source") or "excel_legacy" を使うよう変更。
#
# このテストはその修正が正しく動作することを保証する。
# ============================================================

import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock, call

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


def _create_segment_db_with_datasource(path: str, rows: list[dict]) -> None:
    """指定された data_source を持つ segment_financials DB を作成。"""
    JST = timezone(timedelta(hours=9))
    now_iso = datetime.now(JST).isoformat()

    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS segment_financials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_code TEXT NOT NULL,
            fiscal_year_end TEXT NOT NULL,
            quarter TEXT NOT NULL,
            segment_name TEXT NOT NULL,
            segment_order INTEGER NOT NULL DEFAULT 0,
            segment_sales REAL,
            segment_profit REAL,
            data_source TEXT,
            tdnet_doc_id TEXT,
            updated_at TEXT,
            UNIQUE(company_code, fiscal_year_end, quarter, segment_name)
        )
    """)
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO segment_financials "
            "(company_code, fiscal_year_end, quarter, segment_name, segment_order, "
            "segment_sales, segment_profit, data_source, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                r["company_code"], r["fiscal_year_end"], r["quarter"],
                r["segment_name"], r.get("segment_order", 0),
                r.get("segment_sales", 1000.0), r.get("segment_profit", 100.0),
                r.get("data_source"),
                r.get("updated_at", now_iso),
            ),
        )
    conn.commit()
    conn.close()


_MOCK_CANONICAL_CONFIG = {
    "url": "http://test",
    "key": "test-service-role-key",
    "rest_url": "http://test/rest/v1",
    "anon_key": "test-service-role-key",
    "headers": {
        "apikey": "test-service-role-key",
        "Authorization": "Bearer test-service-role-key",
        "Content-Type": "application/json",
    },
}


def test_id_scoped_dry_run_selects_only_requested_rows_and_never_posts(tmp_path, monkeypatch):
    db_path = str(tmp_path / "segments.db")
    _create_segment_db_with_datasource(db_path, [
        {"company_code": "8908", "fiscal_year_end": "2025-05-31", "quarter": "FY", "segment_name": "Previous", "segment_sales": 1, "segment_profit": 2, "data_source": "backfill_xbrl"},
        {"company_code": "8908", "fiscal_year_end": "2026-05-31", "quarter": "FY", "segment_name": "Current", "segment_sales": 3, "segment_profit": 4, "data_source": "backfill_xbrl"},
        {"company_code": "9999", "fiscal_year_end": "2026-03-31", "quarter": "FY", "segment_name": "Outside", "segment_sales": 5, "segment_profit": 6, "data_source": "backfill_xbrl"},
    ])
    from tools import sync_segments
    post = pytest.MonkeyPatch()
    try:
        post.setattr(sync_segments.requests, "post", lambda *args, **kwargs: pytest.fail("dry-run must not call Supabase"))
        post.setattr(sync_segments.requests, "get", lambda *args, **kwargs: pytest.fail("dry-run must not call Supabase"))
        stats = sync_segments.sync_sqlite_segment_ids(db_path, [2, 1, 2], "http://test/rest/v1", {}, True)
    finally:
        post.undo()
    assert stats["requested_segment_ids"] == [1, 2]
    assert stats["synced_segment_ids"] == [1, 2]
    assert stats["sqlite_total"] == 2
    assert stats["sync_error"] == ""


def test_8908_dry_run_payload_uses_display_keys_without_mutating_names(tmp_path, monkeypatch):
    db_path = str(tmp_path / "segments.db")
    _create_segment_db_with_datasource(db_path, [
        {"company_code": "8908", "fiscal_year_end": "2025-05-31", "quarter": "FY", "segment_name": "不動産ソリューション事業", "segment_sales": 17839, "segment_profit": 3145, "data_source": "edinet_xbrl"},
        {"company_code": "8908", "fiscal_year_end": "2026-05-31", "quarter": "FY", "segment_name": "School Life Solution", "segment_sales": 4653, "segment_profit": 393, "data_source": "backfill_xbrl"},
    ])
    from tools import sync_segments
    monkeypatch.setattr(sync_segments.requests, "post", lambda *args, **kwargs: pytest.fail("dry-run must not call Supabase"))
    monkeypatch.setattr(sync_segments.requests, "get", lambda *args, **kwargs: pytest.fail("dry-run must not call Supabase"))

    stats = sync_segments.sync_sqlite_segment_ids(db_path, [1, 2], "http://test/rest/v1", {}, True)

    assert stats["sqlite_upserted"] == 2
    assert [(row["segment_name"], row["segment_key"]) for row in stats["payloads"]] == [
        ("不動産ソリューション事業", "real estate solution"),
        ("School Life Solution", "school life support"),
    ]


def test_zero_and_null_rows_preserve_wide_and_eav_contract(tmp_path, monkeypatch):
    """Validated rows always produce wide; EAV contains exactly non-null metrics."""
    db_path = str(tmp_path / "segments.db")
    rows = [
        {"company_code": "3536", "fiscal_year_end": "2026-08-31", "quarter": "3Q", "segment_name": "Other", "segment_sales": 0, "segment_profit": 0, "data_source": "backfill_xbrl"},
        {"company_code": "7370", "fiscal_year_end": "2025-05-31", "quarter": "FY", "segment_name": "Real Estate Business", "segment_sales": None, "segment_profit": None, "data_source": "backfill_xbrl"},
        {"company_code": "7370", "fiscal_year_end": "2025-05-31", "quarter": "FY", "segment_name": "Other", "segment_sales": None, "segment_profit": None, "data_source": "backfill_xbrl"},
        {"company_code": "9999", "fiscal_year_end": "2026-03-31", "quarter": "FY", "segment_name": "Loss Only", "segment_sales": None, "segment_profit": -10, "data_source": "backfill_xbrl"},
        {"company_code": "9999", "fiscal_year_end": "2026-03-31", "quarter": "FY", "segment_name": "Core", "segment_sales": 353, "segment_profit": 4, "data_source": "backfill_xbrl"},
    ]
    _create_segment_db_with_datasource(db_path, rows)
    from tools import sync_segments
    monkeypatch.setattr(sync_segments.requests, "post", lambda *args, **kwargs: pytest.fail("dry-run must not post"))
    monkeypatch.setattr(sync_segments.requests, "get", lambda *args, **kwargs: pytest.fail("dry-run must not read Supabase"))

    stats = sync_segments.sync_sqlite_segment_ids(
        db_path, [1, 2, 3, 4, 5], "http://test/rest/v1", {}, True,
    )

    assert stats["sqlite_valid"] == 5
    assert len(stats["payloads"]) == 5
    wide = {(payload["ticker"], payload["segment_name"]): payload for payload in stats["payloads"]}
    assert wide[("3536", "Other")]["sales"] == 0
    assert wide[("3536", "Other")]["profit"] == 0
    assert wide[("7370", "Real Estate Business")]["sales"] is None
    assert wide[("7370", "Real Estate Business")]["profit"] is None

    plan = sync_segments.plan_alias_aware_segment_ids(
        db_path, [1, 2, 3, 4, 5], "http://test/rest/v1", {}, live_read=False,
    )
    assert len(plan["payloads"]) == 5
    assert sum(len(result["eav_actions"]) for result in plan["row_results"]) == 5
    eav = [
        action["payload"]
        for result in plan["row_results"]
        for action in result["eav_actions"]
    ]
    assert {(row["segment_name"], row["metric"], row["value"]) for row in eav} == {
        ("Other", "sales", 0),
        ("Other", "profit", 0),
        ("Loss Only", "profit", -10),
        ("Core", "sales", 353),
        ("Core", "profit", 4),
    }
    monkeypatch.setattr(sync_segments.requests, "get", _layer_get(plan["payloads"], eav))
    assert sync_segments._alias_plan_readback_matches(plan, "http://test/rest/v1", {}) is True


def test_id_scoped_sync_rejects_missing_id_without_post(tmp_path, monkeypatch):
    db_path = str(tmp_path / "segments.db")
    _create_segment_db_with_datasource(db_path, [{"company_code": "8908", "fiscal_year_end": "2026-05-31", "quarter": "FY", "segment_name": "Current", "segment_sales": 3, "segment_profit": 4, "data_source": "backfill_xbrl"}])
    from tools import sync_segments
    monkeypatch.setattr(sync_segments.requests, "post", lambda *args, **kwargs: pytest.fail("missing IDs must not call Supabase"))
    stats = sync_segments.sync_sqlite_segment_ids(db_path, [1, 9], "http://test/rest/v1", {}, True)
    assert stats["sync_error"] == "segment_sync_requested_ids_missing"


class _LayerResponse:
    def __init__(self, rows=None, status_code=200):
        self._rows = rows or []
        self.status_code = status_code
        self.ok = status_code == 200
        self.text = ""

    def json(self):
        return self._rows


def _layer_get(wide_rows, eav_rows):
    def fake_get(url, **kwargs):
        params = kwargs.get("params", {})
        ticker = str(params.get("ticker", "")).removeprefix("eq.")
        period = str(params.get("period", "")).removeprefix("eq.")
        quarter = str(params.get("quarter", "")).removeprefix("eq.")
        source = wide_rows if url.endswith("/segment_canonical") else eav_rows
        return _LayerResponse([
            row for row in source
            if row.get("ticker") == ticker and row.get("period") == period
            and row.get("quarter") == quarter
        ])
    return fake_get


def _rows_4057():
    common = {"company_code": "4057", "quarter": "FY", "data_source": "backfill_xbrl"}
    return [
        {**common, "fiscal_year_end": "2025-05-31", "segment_name": "Cloud Commerce Platform", "segment_sales": 2617, "segment_profit": 867},
        {**common, "fiscal_year_end": "2025-05-31", "segment_name": "Ec Business Growth", "segment_sales": 247, "segment_profit": -13},
        {**common, "fiscal_year_end": "2025-05-31", "segment_name": "Datautillization", "segment_sales": None, "segment_profit": -29},
        {**common, "fiscal_year_end": "2026-05-31", "segment_name": "Cloud Commerce Platform", "segment_sales": 2731, "segment_profit": 840},
        {**common, "fiscal_year_end": "2026-05-31", "segment_name": "Ec Business Growth", "segment_sales": 129, "segment_profit": 4},
        {**common, "fiscal_year_end": "2026-05-31", "segment_name": "Datautillization", "segment_sales": 0, "segment_profit": -59},
    ]


def _wide_previous_4057():
    common = {"ticker": "4057", "period": "2025-05-31", "quarter": "FY", "source": "edinet_xbrl"}
    return [
        {**common, "segment_name": "クラウドコマースプラットフォーム事業", "sales": 2617, "profit": 867},
        {**common, "segment_name": "ECビジネス成長支援事業", "sales": 247, "profit": -13},
        {**common, "segment_name": "データ利活用プラットフォーム事業", "sales": None, "profit": -29},
    ]


def test_4057_layered_plan_skips_previous_wide_and_continues_all_eav(tmp_path, monkeypatch):
    db_path = str(tmp_path / "segments.db")
    _create_segment_db_with_datasource(db_path, _rows_4057())
    from tools import sync_segments
    monkeypatch.setattr(sync_segments.requests, "get", _layer_get(_wide_previous_4057(), []))

    plan = sync_segments.plan_alias_aware_segment_ids(
        db_path, [1, 2, 3, 4, 5, 6], "http://test/rest/v1", {}, live_read=True,
    )

    assert plan["sync_error"] == ""
    assert plan["wide_skipped_alias_equivalent_existing"] == 3
    assert plan["wide_inserted"] == 3
    assert plan["wide_conflict"] == 0
    assert plan["eav_inserted"] == 11
    assert plan["eav_conflict"] == 0
    previous = [row for row in plan["row_results"] if row["sqlite_row_id"] <= 3]
    current = [row for row in plan["row_results"] if row["sqlite_row_id"] >= 4]
    assert {row["wide_action"] for row in previous} == {"wide_skipped_alias_equivalent_existing"}
    assert {row["wide_action"] for row in current} == {"wide_upsert"}
    assert sum(len(row["eav_actions"]) for row in previous) == 5
    assert sum(len(row["eav_actions"]) for row in current) == 6


def test_wide_alias_value_conflict_stops_before_all_posts(tmp_path, monkeypatch):
    db_path = str(tmp_path / "segments.db")
    _create_segment_db_with_datasource(db_path, [_rows_4057()[0]])
    from tools import sync_segments
    conflict = _wide_previous_4057()
    conflict[0] = {**conflict[0], "sales": 9999}
    monkeypatch.setattr(sync_segments.requests, "get", _layer_get(conflict, []))
    monkeypatch.setattr(sync_segments.requests, "post", lambda *a, **k: pytest.fail("conflict must stop before POST"))

    stats = sync_segments.sync_sqlite_segment_ids(db_path, [1], "http://test/rest/v1", {}, False)

    assert stats["sync_error"] == "segment_wide_alias_value_conflict"
    assert stats["wide_conflict"] == 1
    assert stats["synced_segment_ids"] == []


def test_wide_alias_lower_priority_existing_requires_review(tmp_path, monkeypatch):
    db_path = str(tmp_path / "segments.db")
    _create_segment_db_with_datasource(db_path, [_rows_4057()[0]])
    from tools import sync_segments
    existing = [{**_wide_previous_4057()[0], "source": "excel_legacy"}]
    monkeypatch.setattr(sync_segments.requests, "get", _layer_get(existing, []))

    plan = sync_segments.plan_alias_aware_segment_ids(db_path, [1], "http://test/rest/v1", {}, live_read=True)

    assert plan["sync_error"] == "segment_wide_alias_priority_upgrade_requires_review"
    assert plan["wide_conflict"] == 1


def test_eav_logical_duplicate_skips_even_when_source_row_key_differs(tmp_path, monkeypatch):
    db_path = str(tmp_path / "segments.db")
    row = _rows_4057()[3]
    _create_segment_db_with_datasource(db_path, [row])
    from tools import sync_segments
    eav = [
        {"ticker": "4057", "period": "2026-05-31", "quarter": "FY", "segment_name": "クラウドコマースプラットフォーム事業", "segment_key": "cloudcommerceplatform", "metric": "sales", "value": 2731, "source": "edinet_xbrl", "source_row_key": "different-sales"},
        {"ticker": "4057", "period": "2026-05-31", "quarter": "FY", "segment_name": "クラウドコマースプラットフォーム事業", "segment_key": "cloudcommerceplatform", "metric": "profit", "value": 840, "source": "edinet_xbrl", "source_row_key": "different-profit"},
    ]
    monkeypatch.setattr(sync_segments.requests, "get", _layer_get([], eav))

    plan = sync_segments.plan_alias_aware_segment_ids(db_path, [1], "http://test/rest/v1", {}, live_read=True)

    assert plan["eav_skipped_alias_equivalent_existing"] == 2
    assert plan["eav_inserted"] == 0
    assert plan["sync_error"] == ""


def test_eav_alias_value_conflict_in_last_record_prevents_partial_posts(tmp_path, monkeypatch):
    db_path = str(tmp_path / "segments.db")
    _create_segment_db_with_datasource(db_path, _rows_4057())
    from tools import sync_segments
    eav = [{
        "ticker": "4057", "period": "2026-05-31", "quarter": "FY",
        "segment_name": "データ利活用プラットフォーム事業",
        "segment_key": "datautillization", "metric": "profit", "value": -999,
        "source": "edinet_xbrl", "source_row_key": "different",
    }]
    monkeypatch.setattr(sync_segments.requests, "get", _layer_get(_wide_previous_4057(), eav))
    monkeypatch.setattr(sync_segments.requests, "post", lambda *a, **k: pytest.fail("late conflict must stop before POST"))

    stats = sync_segments.sync_sqlite_segment_ids(
        db_path, [1, 2, 3, 4, 5, 6], "http://test/rest/v1", {}, False,
    )

    assert stats["sync_error"] == "segment_eav_alias_value_conflict"
    assert stats["eav_conflict"] == 1
    assert stats["synced_segment_ids"] == []


def test_alias_plan_readback_matches_logical_keys(tmp_path, monkeypatch):
    db_path = str(tmp_path / "segments.db")
    _create_segment_db_with_datasource(db_path, [_rows_4057()[0]])
    from tools import sync_segments
    eav = [
        {"ticker": "4057", "period": "2025-05-31", "quarter": "FY", "segment_name": "Cloud Commerce Platform", "segment_key": "cloud commerce platform", "metric": "sales", "value": 2617, "source": "backfill_xbrl"},
        {"ticker": "4057", "period": "2025-05-31", "quarter": "FY", "segment_name": "Cloud Commerce Platform", "segment_key": "cloud commerce platform", "metric": "profit", "value": 867, "source": "backfill_xbrl"},
    ]
    monkeypatch.setattr(sync_segments.requests, "get", _layer_get(_wide_previous_4057(), eav))
    plan = sync_segments.plan_alias_aware_segment_ids(db_path, [1], "http://test/rest/v1", {}, live_read=True)

    assert plan["wide_skipped_alias_equivalent_existing"] == 1
    assert plan["eav_skipped_alias_equivalent_existing"] == 2
    assert sync_segments._alias_plan_readback_matches(plan, "http://test/rest/v1", {}) is True


def test_wide_alias_skip_still_executes_eav_and_marks_row_synced(tmp_path, monkeypatch):
    db_path = str(tmp_path / "segments.db")
    _create_segment_db_with_datasource(db_path, [_rows_4057()[0]])
    from tools import sync_segments
    wide = _wide_previous_4057()
    eav = []
    post_urls = []

    monkeypatch.setattr(sync_segments.requests, "get", _layer_get(wide, eav))

    def fake_post(url, **kwargs):
        post_urls.append(url)
        if url.endswith("/segment_canonical"):
            pytest.fail("alias-equivalent wide row must not be posted")
        assert url.endswith("/canonical_segments")
        eav.extend(kwargs["json"])
        return _LayerResponse(status_code=201)

    monkeypatch.setattr(sync_segments.requests, "post", fake_post)

    stats = sync_segments.sync_sqlite_segment_ids(
        db_path, [1], "http://test/rest/v1", {}, False,
    )

    assert stats["sync_error"] == ""
    assert stats["wide_skipped_alias_equivalent_existing"] == 1
    assert stats["eav_inserted"] == 2
    assert stats["row_results"][0]["wide_action"] == "wide_skipped_alias_equivalent_existing"
    assert {action["action"] for action in stats["row_results"][0]["eav_actions"]} == {"eav_inserted"}
    assert stats["synced_segment_ids"] == [1]
    assert post_urls == ["http://test/rest/v1/canonical_segments"]


class TestBackfillV4PdfSourcePassthrough:
    """backfill_v4_pdf の data_source が canonical dual-write で
    正しく source='backfill_v4_pdf' として渡されることを確認する再発防止テスト。"""

    def test_backfill_v4_pdf_source_not_excel_legacy(self, tmp_path):
        """data_source='backfill_v4_pdf' の行が canonical に
        source='excel_legacy' ではなく source='backfill_v4_pdf' で渡されること。"""
        db_path = str(tmp_path / "test.db")
        _create_segment_db_with_datasource(db_path, [
            {"company_code": "8918", "fiscal_year_end": "2026-02-28",
             "quarter": "FY", "segment_name": "不動産事業",
             "segment_sales": 5061.0, "segment_profit": 1491.0,
             "data_source": "backfill_v4_pdf"},
            {"company_code": "8918", "fiscal_year_end": "2026-02-28",
             "quarter": "FY", "segment_name": "再生可能エネルギー関連投資",
             "segment_sales": 19.0, "segment_profit": -135.0,
             "data_source": "backfill_v4_pdf"},
            {"company_code": "8918", "fiscal_year_end": "2026-02-28",
             "quarter": "FY", "segment_name": "その他（注）１",
             "segment_sales": 10.0, "segment_profit": -83.0,
             "data_source": "backfill_v4_pdf"},
        ])

        captured_calls: list[dict] = []

        def _mock_write_canonical(**kwargs):
            captured_calls.append(kwargs)
            return {"written": len(kwargs.get("segments", [])) * 2, "skipped": 0, "errors": 0}

        with patch("lib.pipeline.canonical_writer.write_segments_canonical", side_effect=_mock_write_canonical), \
             patch("lib.pipeline.db.load_env"), \
             patch("lib.pipeline.db.get_supabase_write_config",
                   return_value=_MOCK_CANONICAL_CONFIG), \
             patch("tools.sync_segments.requests") as mock_req:

            # requests.get/delete のモック（xbrl cleanup 用）
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = []  # xbrl rows なし
            mock_req.get.return_value = mock_resp
            mock_req.delete.return_value = mock_resp

            from tools.sync_segments import sync_sqlite_segments
            rest_url = "http://test/rest/v1"
            headers = {"apikey": "test", "Authorization": "Bearer test"}

            # segment_canonical への push もモック
            mock_req.post.return_value = MagicMock(status_code=201)

            stats = sync_sqlite_segments(
                db_path=db_path, rest_url=rest_url, headers=headers, dry_run=False,
            )

        # write_segments_canonical が呼ばれたことを確認
        assert len(captured_calls) > 0, "write_segments_canonical should have been called"

        # source が必ず 'backfill_v4_pdf' であること（'excel_legacy' ではない）
        for c in captured_calls:
            src = c.get("source", "")
            assert src == "backfill_v4_pdf", (
                f"source must be 'backfill_v4_pdf', got '{src}'. "
                f"Regression: data_source is being overwritten with 'excel_legacy'."
            )

    def test_excel_legacy_source_remains_excel_legacy(self, tmp_path):
        """data_source='excel_legacy' の行は source='excel_legacy' のまま渡されること。"""
        db_path = str(tmp_path / "test.db")
        _create_segment_db_with_datasource(db_path, [
            {"company_code": "1234", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "国内事業",
             "segment_sales": 1000.0, "segment_profit": 100.0,
             "data_source": "excel_legacy"},
        ])

        captured_calls: list[dict] = []

        def _mock_write_canonical(**kwargs):
            captured_calls.append(kwargs)
            return {"written": 2, "skipped": 0, "errors": 0}

        with patch("lib.pipeline.canonical_writer.write_segments_canonical", side_effect=_mock_write_canonical), \
             patch("lib.pipeline.db.load_env"), \
             patch("lib.pipeline.db.get_supabase_write_config",
                   return_value=_MOCK_CANONICAL_CONFIG), \
             patch("tools.sync_segments.requests") as mock_req:

            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = []
            mock_req.get.return_value = mock_resp
            mock_req.post.return_value = MagicMock(status_code=201)

            from tools.sync_segments import sync_sqlite_segments
            sync_sqlite_segments(
                db_path=db_path, rest_url="http://test/rest/v1",
                headers={"apikey": "test"}, dry_run=False,
            )

        assert any(c.get("source") == "excel_legacy" for c in captured_calls), (
            "excel_legacy rows should still be written as source='excel_legacy'"
        )

    def test_none_data_source_falls_back_to_excel_legacy(self, tmp_path):
        """data_source=NULL の行は source='excel_legacy' にフォールバックすること。"""
        db_path = str(tmp_path / "test.db")
        _create_segment_db_with_datasource(db_path, [
            {"company_code": "5678", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "海外事業",
             "segment_sales": 2000.0, "segment_profit": 200.0,
             "data_source": None},  # NULL
        ])

        captured_calls: list[dict] = []

        def _mock_write_canonical(**kwargs):
            captured_calls.append(kwargs)
            return {"written": 2, "skipped": 0, "errors": 0}

        with patch("lib.pipeline.canonical_writer.write_segments_canonical", side_effect=_mock_write_canonical), \
             patch("lib.pipeline.db.load_env"), \
             patch("lib.pipeline.db.get_supabase_write_config",
                   return_value=_MOCK_CANONICAL_CONFIG), \
             patch("tools.sync_segments.requests") as mock_req:

            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = []
            mock_req.get.return_value = mock_resp
            mock_req.post.return_value = MagicMock(status_code=201)

            from tools.sync_segments import sync_sqlite_segments
            sync_sqlite_segments(
                db_path=db_path, rest_url="http://test/rest/v1",
                headers={"apikey": "test"}, dry_run=False,
            )

        assert any(c.get("source") == "excel_legacy" for c in captured_calls), (
            "NULL data_source should fall back to 'excel_legacy'"
        )

    def test_backfill_v4_pdf_source_priority_is_zero(self, tmp_path):
        """expand_segments_rows が backfill_v4_pdf を source_priority=0 で展開すること。
        
        これは write_segments_canonical が内部で呼び出す expand_segments_rows の
        source_priority 設定を検証する。
        """
        sys.path.insert(0, _PROJECT_ROOT)
        from lib.pipeline.canonical_writer import expand_segments_rows

        rows, skipped = expand_segments_rows(
            ticker="8918",
            period="2026-02-28",
            quarter="FY",
            segments=[
                {"segment_name": "不動産事業", "sales": 5061, "profit": 1491},
                {"segment_name": "再生可能エネルギー関連投資", "sales": 19, "profit": -135},
            ],
            source="backfill_v4_pdf",
        )

        assert len(rows) > 0, "expand_segments_rows should produce rows"
        assert skipped == 0

        for row in rows:
            assert row["source"] == "backfill_v4_pdf", (
                f"source should be 'backfill_v4_pdf', got '{row['source']}'"
            )
            assert row["source_priority"] == 0, (
                f"backfill_v4_pdf source_priority must be 0, got {row['source_priority']}. "
                "Check lib/pipeline/source_priority.py: SOURCE_PRIORITY['backfill_v4_pdf'] should be 0."
            )

    def test_backfill_v4_pdf_higher_priority_than_xbrl(self, tmp_path):
        """backfill_v4_pdf (priority=0) が xbrl (priority=1) より高優先であること。"""
        sys.path.insert(0, _PROJECT_ROOT)
        from lib.pipeline.source_priority import get_priority

        v4_priority = get_priority("backfill_v4_pdf")
        xbrl_priority = get_priority("xbrl")
        excel_priority = get_priority("excel_legacy")

        assert v4_priority == 0, (
            f"backfill_v4_pdf must have priority=0, got {v4_priority}"
        )
        assert v4_priority < xbrl_priority, (
            f"backfill_v4_pdf ({v4_priority}) must be higher priority than "
            f"xbrl ({xbrl_priority})"
        )
        assert xbrl_priority < excel_priority, (
            f"xbrl ({xbrl_priority}) must be higher priority than "
            f"excel_legacy ({excel_priority})"
        )

    def test_xbrl_other_only_cleanup_triggers_for_v4pdf_2plus_segs(self, tmp_path):
        """backfill_v4_pdf が 2件以上のとき xbrl:Other only cleanup が発火すること。"""
        db_path = str(tmp_path / "test.db")
        _create_segment_db_with_datasource(db_path, [
            {"company_code": "8918", "fiscal_year_end": "2026-02-28",
             "quarter": "FY", "segment_name": "不動産事業",
             "segment_sales": 5061.0, "segment_profit": 1491.0,
             "data_source": "backfill_v4_pdf"},
            {"company_code": "8918", "fiscal_year_end": "2026-02-28",
             "quarter": "FY", "segment_name": "再生可能エネルギー関連投資",
             "segment_sales": 19.0, "segment_profit": -135.0,
             "data_source": "backfill_v4_pdf"},
        ])

        delete_calls: list[dict] = []
        get_calls: list[dict] = []

        def _mock_write_canonical(**kwargs):
            return {"written": 4, "skipped": 0, "errors": 0}

        def _mock_requests_get(url, **kwargs):
            get_calls.append({"url": url, "params": kwargs.get("params", {})})
            resp = MagicMock()
            resp.ok = True
            # xbrl source に Other のみが存在する状況をシミュレート
            params = kwargs.get("params", {})
            if params.get("source", "") in ("eq.xbrl", "eq.backfill_xbrl"):
                resp.json.return_value = [{"segment_name": "Other"}]
            else:
                resp.json.return_value = []
            return resp

        def _mock_requests_delete(url, **kwargs):
            delete_calls.append({"url": url, "params": kwargs.get("params", {})})
            resp = MagicMock()
            resp.ok = True
            return resp

        with patch("lib.pipeline.canonical_writer.write_segments_canonical", side_effect=_mock_write_canonical), \
             patch("lib.pipeline.db.load_env"), \
             patch("lib.pipeline.db.get_supabase_write_config",
                   return_value=_MOCK_CANONICAL_CONFIG), \
             patch("tools.sync_segments.requests") as mock_req:

            mock_req.get.side_effect = _mock_requests_get
            mock_req.delete.side_effect = _mock_requests_delete
            mock_req.post.return_value = MagicMock(status_code=201)

            from tools.sync_segments import sync_sqlite_segments
            sync_sqlite_segments(
                db_path=db_path, rest_url="http://test/rest/v1",
                headers={"apikey": "test"}, dry_run=False,
            )

        # GET が canonical_segments に対して呼ばれたこと（xbrl Other 確認）
        canonical_get_calls = [
            c for c in get_calls if "canonical_segments" in c.get("url", "")
        ]
        assert len(canonical_get_calls) > 0, (
            "Should have called GET canonical_segments to check for xbrl:Other only rows"
        )

        # DELETE が呼ばれたこと（xbrl Other only の削除）
        assert len(delete_calls) > 0, (
            "Should have called DELETE to remove xbrl:Other-only rows "
            "when backfill_v4_pdf has 2+ segments"
        )

    def test_backfill_v4_pdf_single_seg_no_cleanup(self, tmp_path):
        """backfill_v4_pdf が 1件のみの場合は xbrl:Other cleanup が発火しないこと。"""
        db_path = str(tmp_path / "test.db")
        _create_segment_db_with_datasource(db_path, [
            {"company_code": "9999", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業A",
             "segment_sales": 1000.0, "segment_profit": 100.0,
             "data_source": "backfill_v4_pdf"},  # 1件のみ
        ])

        delete_calls: list[dict] = []

        def _mock_write_canonical(**kwargs):
            return {"written": 2, "skipped": 0, "errors": 0}

        def _mock_requests_delete(url, **kwargs):
            delete_calls.append({"url": url})
            return MagicMock(ok=True)

        with patch("lib.pipeline.canonical_writer.write_segments_canonical", side_effect=_mock_write_canonical), \
             patch("lib.pipeline.db.load_env"), \
             patch("lib.pipeline.db.get_supabase_write_config",
                   return_value=_MOCK_CANONICAL_CONFIG), \
             patch("tools.sync_segments.requests") as mock_req:

            mock_req.get.return_value = MagicMock(ok=True, json=lambda: [])
            mock_req.delete.side_effect = _mock_requests_delete
            mock_req.post.return_value = MagicMock(status_code=201)

            from tools.sync_segments import sync_sqlite_segments
            sync_sqlite_segments(
                db_path=db_path, rest_url="http://test/rest/v1",
                headers={"apikey": "test"}, dry_run=False,
            )

        # 1件のみの場合は DELETE が呼ばれないこと
        assert len(delete_calls) == 0, (
            f"Should NOT delete xbrl rows when backfill_v4_pdf has only 1 segment, "
            f"but delete was called {len(delete_calls)} times"
        )
