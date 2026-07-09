"""tests/test_canonical_sync.py — canonical_sync のユニットテスト (バッチ化対応)"""
from __future__ import annotations
import os
import sys
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from contextlib import ExitStack

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


JST = timezone(timedelta(hours=9))

_MOCK_WRITE_CONFIG = {
    "url": "http://test",
    "key": "test-service-role-key",
    "rest_url": "http://test/rest/v1",
    "headers": {
        "apikey": "test-service-role-key",
        "Authorization": "Bearer test-service-role-key",
        "Content-Type": "application/json",
    },
}


def _create_test_db(path: str, *, n_financials: int = 3, n_segments: int = 3):
    """テスト用 SQLite DB を作成。"""
    conn = sqlite3.connect(path)
    now_iso = datetime.now(JST).isoformat()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS quarterly_results (
            id INTEGER PRIMARY KEY,
            company_code TEXT,
            fiscal_year_end TEXT,
            quarter TEXT,
            sales REAL,
            gross_profit REAL,
            sga REAL,
            operating_profit REAL,
            field_sources TEXT,
            disclosure_id TEXT,
            disclosure_datetime TEXT,
            revision_flag INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)
    for i in range(n_financials):
        conn.execute(
            "INSERT INTO quarterly_results "
            "(company_code, fiscal_year_end, quarter, sales, operating_profit, "
            "field_sources, disclosure_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f"{1000+i}", "2025-03", f"{i+1}Q", 1000+i*100, 100+i*10,
             "summary_xbrl", f"disc_{i}", now_iso),
        )

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
            data_source TEXT DEFAULT 'tdnet',
            tdnet_doc_id TEXT,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(company_code, fiscal_year_end, quarter, segment_name)
        )
    """)
    for i in range(n_segments):
        conn.execute(
            "INSERT INTO segment_financials "
            "(company_code, fiscal_year_end, quarter, segment_name, segment_order, "
            "segment_sales, segment_profit, data_source, tdnet_doc_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"{2000+i}", "2025-03-31", "FY", f"事業{i}", i,
             500+i*50, 50+i*5, "tdnet", f"seg_disc_{i}", now_iso),
        )

    conn.commit()
    conn.close()


def _mock_upsert_ok(*args, **kwargs):
    """supabase_upsert の成功モック"""
    payload = args[1] if len(args) > 1 else kwargs.get("payload", [])
    rows = payload if isinstance(payload, list) else [payload]
    return {
        "status": 200, "ok": True, "count": len(rows), "error": None,
        "batches_attempted": 1, "batches_succeeded": 1, "batches_failed": 0,
    }


def _patch_env_and_config():
    """load_env + get_supabase_write_config のモックを返す ExitStack パターン。"""
    stack = ExitStack()
    stack.enter_context(patch("lib.pipeline.canonical_sync.load_env"))
    stack.enter_context(
        patch("lib.pipeline.canonical_sync.get_supabase_write_config",
              return_value=_MOCK_WRITE_CONFIG)
    )
    return stack


# ============================================================
# 既存テスト (バッチ化対応)
# ============================================================

class TestSyncCanonical:
    """sync_canonical のテスト。"""

    def test_dry_run_no_supabase_call(self, tmp_path):
        """dry_run で Supabase 書き込みしないこと。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path)

        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert") as mock_upsert:

            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(db_path=db_path, dry_run=True)

            mock_upsert.assert_not_called()

            assert result["status"] == "ok"
            assert result["financials"]["targets"] == 3
            assert result["segments"]["targets"] == 3
            assert "mode" in result
            assert "summary" in result
            assert "rows_selected" in result["financials"]

    def test_target_keys_priority(self, tmp_path):
        """target_keys 指定時はそれだけが対象になること。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, n_financials=5)

        from lib.pipeline.canonical_sync import _select_financials_rows
        rows, fb = _select_financials_rows(
            db_path,
            target_keys=[("1000", "2025-03", "1Q")],
        )
        assert len(rows) == 1
        assert rows[0]["company_code"] == "1000"
        assert fb is False

    def test_lookback_fallback(self, tmp_path):
        """target_keys が None なら lookback で取得。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, n_financials=3)

        from lib.pipeline.canonical_sync import _select_financials_rows
        rows, fb = _select_financials_rows(db_path, lookback_days=7)
        assert len(rows) == 3
        assert fb is False

    def test_stats_separation(self, tmp_path):
        """返却 stats が financials / segments で分離されていること。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path)

        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=_mock_upsert_ok):

            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(db_path=db_path, dry_run=False)

            assert "financials" in result
            assert "segments" in result
            assert result["financials"]["written"] > 0
            assert result["segments"]["written"] > 0
            assert result["status"] == "ok"

    def test_partial_failure_warning(self, tmp_path):
        """financials 成功 / segments 失敗 → status=warning。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path)

        def _upsert_fin_ok_seg_fail(*args, **kwargs):
            table = args[0]
            if table == "canonical_financials":
                return _mock_upsert_ok(*args, **kwargs)
            else:
                raise Exception("segments upsert failed")

        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=_upsert_fin_ok_seg_fail):

            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(db_path=db_path, dry_run=False)

            assert result["status"] == "warning"
            assert result["financials"]["written"] > 0
            assert result["segments"]["errors"] > 0

    def test_strict_mode_raises(self, tmp_path):
        """strict=True で例外が raise されること。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path)

        def _upsert_raise(*args, **kwargs):
            raise Exception("strict failure")

        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=_upsert_raise):

            from lib.pipeline.canonical_sync import sync_canonical
            with pytest.raises(Exception, match="strict failure"):
                sync_canonical(db_path=db_path, dry_run=False, strict=True)

    def test_idempotent_write(self, tmp_path):
        """同一対象の再実行で件数が増えないこと。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, n_financials=2)

        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=_mock_upsert_ok):

            from lib.pipeline.canonical_sync import sync_canonical

            r1 = sync_canonical(db_path=db_path, dry_run=False)
            r2 = sync_canonical(db_path=db_path, dry_run=False)

            assert r1["financials"]["written"] == r2["financials"]["written"]

    def test_empty_db_ok(self, tmp_path):
        """空 DB で status=ok。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, n_financials=0, n_segments=0)

        with _patch_env_and_config():

            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(db_path=db_path, dry_run=True)

            assert result["status"] == "ok"
            assert result["financials"]["targets"] == 0
            assert result["segments"]["targets"] == 0


# ============================================================
# 新規テスト: stats 拡張 + mode + fallback 仕様
# ============================================================

class TestStatsExtension:
    """stats 拡張テスト。"""

    def test_stats_include_mode_and_counts(self, tmp_path):
        """mode / fallback_used / target_keys_count / resolved_target_count /
        rows_selected / attempted が返ること。"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        now_iso = datetime.now(JST).isoformat()
        conn.execute("""CREATE TABLE IF NOT EXISTS quarterly_results (
            id INTEGER PRIMARY KEY,
            company_code TEXT,
            fiscal_year_end TEXT, quarter TEXT,
            sales REAL, gross_profit REAL, sga REAL, operating_profit REAL,
            field_sources TEXT, disclosure_id TEXT, disclosure_datetime TEXT,
            revision_flag INTEGER DEFAULT 0, updated_at TEXT
        )""")
        conn.execute(
            "INSERT INTO quarterly_results (company_code, fiscal_year_end, quarter, sales, "
            "operating_profit, field_sources, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("1000", "2025-03-31", "1Q", 1000, 100, "summary_xbrl", now_iso),
        )
        conn.execute("""CREATE TABLE IF NOT EXISTS segment_financials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_code TEXT NOT NULL,
            fiscal_year_end TEXT NOT NULL,
            quarter TEXT NOT NULL,
            segment_name TEXT NOT NULL,
            segment_order INTEGER NOT NULL DEFAULT 0,
            segment_sales REAL,
            segment_profit REAL,
            data_source TEXT DEFAULT 'tdnet',
            tdnet_doc_id TEXT,
            updated_at TEXT,
            UNIQUE(company_code, fiscal_year_end, quarter, segment_name)
        )""")
        conn.execute(
            "INSERT INTO segment_financials (company_code, fiscal_year_end, quarter, segment_name, "
            "segment_order, segment_sales, segment_profit, data_source, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("1000", "2025-03-31", "1Q", "事業A", 0, 500, 50, "tdnet", now_iso),
        )
        conn.commit()
        conn.close()

        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=_mock_upsert_ok):

            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(
                db_path=db_path, dry_run=False,
                target_keys=[("1000", "2025-03-31", "1Q")],
            )

            # top-level keys
            assert "mode" in result
            assert "fallback_used" in result
            assert "target_keys_count" in result
            assert "resolved_target_count" in result
            assert "lookback_days" in result
            assert "summary" in result

            # sub-stats keys
            for section in ("financials", "segments"):
                s = result[section]
                assert "rows_selected" in s
                assert "attempted" in s
                assert "written" in s
                assert "skipped" in s
                assert "errors" in s
                assert "targets" in s
                assert "batches_attempted" in s
                assert "batches_succeeded" in s
                assert "batches_failed" in s

            assert result["mode"] == "target_keys"
            assert result["fallback_used"] is False
            assert result["target_keys_count"] == 1

    def test_target_keys_zero_then_fallback_sets_mode(self, tmp_path):
        """target_keys から 0件 → fallback で抽出あり →
        mode=lookback_fallback, fallback_used=True。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, n_financials=3)

        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=_mock_upsert_ok):

            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(
                db_path=db_path, dry_run=False,
                target_keys=[("9999", "2099-03", "1Q")],
            )

            assert result["mode"] == "lookback_fallback"
            assert result["fallback_used"] is True
            assert result["target_keys_count"] == 1

    def test_target_keys_zero_and_fallback_zero_is_warning(self, tmp_path):
        """target_keys あり → 0件 → fallback も 0件 → status=warning。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, n_financials=0, n_segments=0)

        with _patch_env_and_config():

            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(
                db_path=db_path, dry_run=False,
                target_keys=[("9999", "2099-03", "1Q")],
            )

            assert result["status"] == "warning"
            assert result["mode"] == "lookback_fallback"
            assert result["fallback_used"] is True
            assert "no rows" in result["summary"].lower() or result["financials"]["rows_selected"] == 0

    def test_empty_without_target_keys_is_ok(self, tmp_path):
        """target_keys なし + lookback 0件 → mode=empty, status=ok。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, n_financials=0, n_segments=0)

        with _patch_env_and_config():

            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(db_path=db_path, dry_run=False)

            assert result["status"] == "ok"
            assert result["mode"] == "empty"

    def test_summary_string_present(self, tmp_path):
        """summary が返り、status / mode / fallback_used が含まれること。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path)

        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=_mock_upsert_ok):

            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(db_path=db_path, dry_run=False)

            summary = result["summary"]
            assert isinstance(summary, str)
            assert len(summary) > 0
            assert result["status"] in summary
            assert result["mode"] in summary
            assert f"fallback_used={result['fallback_used']}" in summary

    def test_lookback_only_mode(self, tmp_path):
        """target_keys=None + データあり → mode=lookback_only。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, n_financials=2)

        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=_mock_upsert_ok):

            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(
                db_path=db_path, dry_run=False,
                target_keys=None,
            )

            assert result["mode"] == "lookback_only"
            assert result["fallback_used"] is False

    def test_lookback_days_passthrough(self, tmp_path):
        """lookback_days が結果に反映されること。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, n_financials=0, n_segments=0)

        with _patch_env_and_config():

            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(db_path=db_path, dry_run=True, lookback_days=14)
            assert result["lookback_days"] == 14


# ============================================================
# バッチ化固有テスト
# ============================================================

class TestBatchWrite:
    """バッチ化の正しさを確認。"""

    def test_supabase_upsert_called_with_batch_size(self, tmp_path):
        """supabase_upsert が CANONICAL_BATCH_SIZE で呼ばれること。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, n_financials=3)

        upsert_calls = []

        def _capture_upsert(*args, **kwargs):
            upsert_calls.append(kwargs)
            return _mock_upsert_ok(*args, **kwargs)

        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=_capture_upsert):

            from lib.pipeline.canonical_sync import sync_canonical, CANONICAL_BATCH_SIZE
            sync_canonical(db_path=db_path, dry_run=False)

            fin_calls = [c for c in upsert_calls if c.get("batch_size") is not None]
            for call in fin_calls:
                assert call["batch_size"] == CANONICAL_BATCH_SIZE
                assert call["on_conflict"] == "source_row_key"

    def test_session_is_passed(self, tmp_path):
        """session が supabase_upsert に渡されること。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, n_financials=1, n_segments=1)

        sessions = []

        def _capture_upsert(*args, **kwargs):
            sessions.append(kwargs.get("session"))
            return _mock_upsert_ok(*args, **kwargs)

        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=_capture_upsert):

            from lib.pipeline.canonical_sync import sync_canonical
            sync_canonical(db_path=db_path, dry_run=False)

            assert all(s is not None for s in sessions)

    def test_batch_stats_in_result(self, tmp_path):
        """batch 統計が result に含まれること。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, n_financials=2, n_segments=2)

        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=_mock_upsert_ok):

            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(db_path=db_path, dry_run=False)

            for section in ("financials", "segments"):
                s = result[section]
                assert s["batches_attempted"] >= 1
                assert s["batches_succeeded"] >= 1
                assert s["batches_failed"] == 0

    def test_no_individual_write_calls(self, tmp_path):
        """write_financials_canonical / write_segments_canonical が
        sync_canonical 内で直接呼ばれていないこと。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, n_financials=2, n_segments=2)

        import lib.pipeline.canonical_sync as mod
        assert not hasattr(mod, "write_financials_canonical")
        assert not hasattr(mod, "write_segments_canonical")


# ============================================================
# Helper テスト
# ============================================================

class TestHelpers:
    """helper 関数テスト。"""

    def test_normalize_target_keys_dedup(self):
        from lib.pipeline.canonical_sync import _normalize_target_keys
        keys = [("1000", "2025-03", "1Q"), ("1000", "2025-03", "1Q"), ("2000", "2025-03", "FY")]
        result = _normalize_target_keys(keys)
        assert len(result) == 2

    def test_normalize_target_keys_none(self):
        from lib.pipeline.canonical_sync import _normalize_target_keys
        assert _normalize_target_keys(None) is None

    def test_normalize_target_keys_empty(self):
        from lib.pipeline.canonical_sync import _normalize_target_keys
        assert _normalize_target_keys([]) is None

    def test_determine_status_ok(self):
        from lib.pipeline.canonical_sync import _determine_status
        s = _determine_status(
            fin_stats={"errors": 0, "written": 5, "rows_selected": 3},
            seg_stats={"errors": 0, "written": 2, "rows_selected": 2},
            mode="target_keys", target_keys_count=1,
        )
        assert s == "ok"

    def test_determine_status_error(self):
        from lib.pipeline.canonical_sync import _determine_status
        s = _determine_status(
            fin_stats={"errors": 3, "written": 0, "rows_selected": 3},
            seg_stats={"errors": 1, "written": 0, "rows_selected": 1},
            mode="target_keys", target_keys_count=1,
        )
        assert s == "error"

    def test_determine_status_warning_partial(self):
        from lib.pipeline.canonical_sync import _determine_status
        s = _determine_status(
            fin_stats={"errors": 1, "written": 3, "rows_selected": 4},
            seg_stats={"errors": 0, "written": 2, "rows_selected": 2},
            mode="target_keys", target_keys_count=1,
        )
        assert s == "warning"

    def test_determine_status_warning_empty_fallback(self):
        from lib.pipeline.canonical_sync import _determine_status
        s = _determine_status(
            fin_stats={"errors": 0, "written": 0, "rows_selected": 0},
            seg_stats={"errors": 0, "written": 0, "rows_selected": 0},
            mode="lookback_fallback", target_keys_count=3,
        )
        assert s == "warning"

    def test_build_summary(self):
        from lib.pipeline.canonical_sync import _build_summary
        result = {
            "status": "ok", "mode": "target_keys",
            "fallback_used": False,
            "financials": {"written": 10, "errors": 0, "rows_selected": 5,
                           "batches_succeeded": 1, "batches_attempted": 1},
            "segments": {"written": 4, "errors": 0, "rows_selected": 3,
                         "batches_succeeded": 1, "batches_attempted": 1},
        }
        s = _build_summary(result)
        assert "ok" in s
        assert "target_keys" in s
        assert "batches=" in s

    def test_init_sub_stats(self):
        from lib.pipeline.canonical_sync import _init_sub_stats
        s = _init_sub_stats()
        for k in ("targets", "rows_selected", "attempted", "written", "skipped", "errors",
                   "batches_attempted", "batches_succeeded", "batches_failed"):
            assert k in s
            assert s[k] == 0


# ============================================================
# 再発防止テスト: スキーマ不一致検出
# ============================================================

class TestSegmentSchemaCompat:
    """segment_financials が本番スキーマ(company_code/fiscal_year_end)の時、
    canonical_segments が正しく written > 0 になること。
    旧スキーマ(ticker/period)前提の row.get で空キーにならないことを確認。
    """

    def test_production_schema_segments_written(self, tmp_path):
        """本番スキーマで segments written > 0 になること。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, n_financials=1, n_segments=2)

        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=_mock_upsert_ok):

            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(db_path=db_path, dry_run=False)

            assert result["segments"]["written"] > 0, (
                f"segments should have written > 0, got {result['segments']}"
            )
            assert result["segments"]["errors"] == 0

    def test_grouping_keys_not_empty(self, tmp_path):
        """grouping key が ('', '', 'FY') のような空キーにならないこと。"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        now_iso = datetime.now(JST).isoformat()

        conn.execute("""CREATE TABLE IF NOT EXISTS quarterly_results (
            id INTEGER PRIMARY KEY,
            company_code TEXT,
            fiscal_year_end TEXT, quarter TEXT,
            sales REAL, gross_profit REAL, sga REAL, operating_profit REAL,
            field_sources TEXT, disclosure_id TEXT, disclosure_datetime TEXT,
            revision_flag INTEGER DEFAULT 0, updated_at TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS segment_financials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_code TEXT NOT NULL,
            fiscal_year_end TEXT NOT NULL,
            quarter TEXT NOT NULL,
            segment_name TEXT NOT NULL,
            segment_order INTEGER NOT NULL DEFAULT 0,
            segment_sales REAL,
            segment_profit REAL,
            data_source TEXT DEFAULT 'tdnet',
            tdnet_doc_id TEXT,
            updated_at TEXT,
            UNIQUE(company_code, fiscal_year_end, quarter, segment_name)
        )""")
        conn.execute(
            "INSERT INTO segment_financials (company_code, fiscal_year_end, quarter, "
            "segment_name, segment_order, segment_sales, segment_profit, data_source, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("6750", "2025-03-31", "3Q", "テスト事業", 0, 1000, 100, "tdnet", now_iso),
        )
        conn.commit()
        conn.close()

        from lib.pipeline.canonical_sync import _select_segments_rows, _seg_ticker, _seg_period
        rows, _, _ = _select_segments_rows(db_path, lookback_days=7)

        assert len(rows) > 0
        for r in rows:
            tk = _seg_ticker(r)
            pd = _seg_period(r)
            assert tk != "", f"ticker should not be empty, row keys={list(r.keys())}"
            assert pd != "", f"period should not be empty, row keys={list(r.keys())}"
            assert tk == "6750"
            assert pd == "2025-03-31"

    def test_no_invalid_ticker_with_production_schema(self, tmp_path):
        """本番スキーマの segment row が INVALID ticker にならないこと。"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        now_iso = datetime.now(JST).isoformat()

        conn.execute("""CREATE TABLE IF NOT EXISTS quarterly_results (
            id INTEGER PRIMARY KEY,
            company_code TEXT,
            fiscal_year_end TEXT, quarter TEXT,
            sales REAL, gross_profit REAL, sga REAL, operating_profit REAL,
            field_sources TEXT, disclosure_id TEXT, disclosure_datetime TEXT,
            revision_flag INTEGER DEFAULT 0, updated_at TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS segment_financials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_code TEXT NOT NULL,
            fiscal_year_end TEXT NOT NULL,
            quarter TEXT NOT NULL,
            segment_name TEXT NOT NULL,
            segment_order INTEGER NOT NULL DEFAULT 0,
            segment_sales REAL,
            segment_profit REAL,
            data_source TEXT DEFAULT 'tdnet',
            tdnet_doc_id TEXT,
            updated_at TEXT,
            UNIQUE(company_code, fiscal_year_end, quarter, segment_name)
        )""")
        # 3件の異なる ticker で投入
        for i, code in enumerate(["6750", "7203", "9984"]):
            conn.execute(
                "INSERT INTO segment_financials (company_code, fiscal_year_end, quarter, "
                "segment_name, segment_order, segment_sales, segment_profit, data_source, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (code, "2025-03-31", "FY", f"事業{i}", i, 1000+i*100, 100+i*10, "tdnet", now_iso),
            )
        conn.commit()
        conn.close()

        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=_mock_upsert_ok):

            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(db_path=db_path, dry_run=False)

            # skipped が 0 = INVALID ticker でスキップされていない
            assert result["segments"]["skipped"] == 0, (
                f"No segments should be skipped due to INVALID ticker, "
                f"got skipped={result['segments']['skipped']}"
            )
            assert result["segments"]["written"] > 0


# ============================================================
# historical_backfill 除外テスト
# ============================================================

def _create_segment_only_db(path: str, rows: list[dict]):
    """segment_financials のみのテスト DB を作成。"""
    conn = sqlite3.connect(path)
    now_iso = datetime.now(JST).isoformat()

    conn.execute("""CREATE TABLE IF NOT EXISTS quarterly_results (
            id INTEGER PRIMARY KEY,
            company_code TEXT,
            fiscal_year_end TEXT, quarter TEXT,
        sales REAL, gross_profit REAL, sga REAL, operating_profit REAL,
        field_sources TEXT, disclosure_id TEXT, disclosure_datetime TEXT,
        revision_flag INTEGER DEFAULT 0, updated_at TEXT
    )""")

    conn.execute("""CREATE TABLE IF NOT EXISTS segment_financials (
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
    )""")
    for r in rows:
        conn.execute(
            "INSERT INTO segment_financials "
            "(company_code, fiscal_year_end, quarter, segment_name, segment_order, "
            "segment_sales, segment_profit, data_source, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                r["company_code"], r["fiscal_year_end"], r["quarter"],
                r["segment_name"], r.get("segment_order", 0),
                r.get("segment_sales", 500), r.get("segment_profit", 50),
                r.get("data_source"),
                r.get("updated_at", now_iso),
            ),
        )
    conn.commit()
    conn.close()


class TestHistoricalBackfillExclusion:
    """historical_backfill source が canonical segments から除外されること。"""

    def test_historical_backfill_excluded(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        _create_segment_only_db(db_path, [
            {"company_code": "fffe", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業A",
             "data_source": "historical_backfill"},
            {"company_code": "ffda", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業B",
             "data_source": "historical_backfill"},
        ])
        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=_mock_upsert_ok):
            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(db_path=db_path, dry_run=False)
            assert result["segments"]["written"] == 0
            assert result["segments"]["excluded_historical_backfill_sql"] == 2

    def test_tdnet_rows_pass_through(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        _create_segment_only_db(db_path, [
            {"company_code": "6750", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業A",
             "data_source": "tdnet"},
            {"company_code": "6750", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業B",
             "data_source": "tdnet"},
        ])
        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=_mock_upsert_ok):
            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(db_path=db_path, dry_run=False)
            assert result["segments"]["written"] > 0
            assert result["segments"]["excluded_historical_backfill_sql"] == 0
            assert result["segments"]["excluded_historical_backfill_safety"] == 0

    def test_mixed_source_only_excludes_backfill(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        _create_segment_only_db(db_path, [
            {"company_code": "6750", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業A",
             "data_source": "tdnet"},
            {"company_code": "fffe", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業B",
             "data_source": "historical_backfill"},
            {"company_code": "ffda", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業C",
             "data_source": "historical_backfill"},
            {"company_code": "ffda", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業D",
             "data_source": "historical_backfill"},
        ])
        from lib.pipeline.canonical_sync import _select_segments_rows
        rows, fb, excluded = _select_segments_rows(db_path, lookback_days=7)
        assert len(rows) == 1
        assert rows[0]["company_code"] == "6750"
        assert excluded == 3

    def test_stats_include_excluded_count(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        _create_segment_only_db(db_path, [
            {"company_code": "6750", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業A",
             "data_source": "tdnet"},
            {"company_code": "fffe", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業B",
             "data_source": "historical_backfill"},
        ])
        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=_mock_upsert_ok):
            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(db_path=db_path, dry_run=False)
            assert result["segments"]["excluded_historical_backfill_sql"] == 1
            assert "excluded_hb=" in result["summary"]

    def test_null_data_source_is_excluded(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        _create_segment_only_db(db_path, [
            {"company_code": "6750", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業A",
             "data_source": None},
            {"company_code": "7203", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業B",
             "data_source": ""},
            {"company_code": "9984", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業C",
             "data_source": "tdnet"},
        ])
        from lib.pipeline.canonical_sync import _select_segments_rows
        rows, fb, excluded = _select_segments_rows(db_path, lookback_days=7)
        assert len(rows) == 1
        assert excluded == 2

    def test_excluded_count_is_scoped_not_global(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        now_iso = datetime.now(JST).isoformat()
        old_iso = (datetime.now(JST) - timedelta(days=30)).isoformat()
        _create_segment_only_db(db_path, [
            {"company_code": "fffe", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業A",
             "data_source": "historical_backfill", "updated_at": now_iso},
            {"company_code": "ffda", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業B",
             "data_source": "historical_backfill", "updated_at": now_iso},
            {"company_code": "fcec", "fiscal_year_end": "2024-03-31",
             "quarter": "FY", "segment_name": "事業C",
             "data_source": "historical_backfill", "updated_at": old_iso},
            {"company_code": "fc01", "fiscal_year_end": "2024-03-31",
             "quarter": "FY", "segment_name": "事業D",
             "data_source": "historical_backfill", "updated_at": old_iso},
            {"company_code": "fc02", "fiscal_year_end": "2024-03-31",
             "quarter": "FY", "segment_name": "事業E",
             "data_source": "historical_backfill", "updated_at": old_iso},
            {"company_code": "6750", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業F",
             "data_source": "tdnet", "updated_at": now_iso},
        ])
        from lib.pipeline.canonical_sync import _select_segments_rows
        rows, fb, excluded = _select_segments_rows(db_path, lookback_days=7)
        assert excluded == 2, f"excluded should be 2 (scoped), got {excluded}"
        assert len(rows) == 1
        assert rows[0]["company_code"] == "6750"

    def test_safety_skip_remains_zero_when_sql_filter_works(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        _create_segment_only_db(db_path, [
            {"company_code": "6750", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業A",
             "data_source": "tdnet"},
            {"company_code": "fffe", "fiscal_year_end": "2025-03-31",
             "quarter": "FY", "segment_name": "事業B",
             "data_source": "historical_backfill"},
        ])
        with _patch_env_and_config(), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=_mock_upsert_ok):
            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(db_path=db_path, dry_run=False)
            assert result["segments"]["excluded_historical_backfill_safety"] == 0
            assert result["segments"]["excluded_historical_backfill_sql"] == 1
            assert result["segments"]["written"] > 0
