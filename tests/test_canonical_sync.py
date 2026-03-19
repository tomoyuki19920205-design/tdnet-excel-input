"""tests/test_canonical_sync.py — canonical_sync のユニットテスト"""
from __future__ import annotations
import os
import sys
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


JST = timezone(timedelta(hours=9))


def _create_test_db(path: str, *, n_financials: int = 3, n_segments: int = 3):
    """テスト用 SQLite DB を作成。"""
    conn = sqlite3.connect(path)
    now_iso = datetime.now(JST).isoformat()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS quarterly_results (
            id INTEGER PRIMARY KEY,
            company_code TEXT,
            period TEXT,
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
            "(company_code, period, quarter, sales, operating_profit, "
            "field_sources, disclosure_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f"{1000+i}", "2025-03", f"{i+1}Q", 1000+i*100, 100+i*10,
             "summary_xbrl", f"disc_{i}", now_iso),
        )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS segment_financials (
            ticker TEXT,
            period TEXT,
            quarter TEXT,
            segment_name TEXT,
            segment_sales REAL,
            segment_profit REAL,
            source TEXT,
            filing_id TEXT,
            updated_at TEXT
        )
    """)
    for i in range(n_segments):
        conn.execute(
            "INSERT INTO segment_financials "
            "(ticker, period, quarter, segment_name, segment_sales, "
            "segment_profit, source, filing_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"{2000+i}", "2025-03", "FY", f"事業{i}", 500+i*50, 50+i*5,
             "tdnet", f"seg_disc_{i}", now_iso),
        )

    conn.commit()
    conn.close()


# ============================================================
# 既存テスト (更新)
# ============================================================

class TestSyncCanonical:
    """sync_canonical のテスト。"""

    def test_dry_run_no_supabase_call(self, tmp_path):
        """dry_run で Supabase 書き込みしないこと。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path)

        with patch("lib.pipeline.canonical_sync.load_env") as mock_env:
            mock_env.return_value = {"SUPABASE_URL": "http://test", "SUPABASE_ANON_KEY": "key"}

            with patch("lib.pipeline.canonical_sync.write_financials_canonical") as mock_fin, \
                 patch("lib.pipeline.canonical_sync.write_segments_canonical") as mock_seg:

                from lib.pipeline.canonical_sync import sync_canonical
                result = sync_canonical(db_path=db_path, dry_run=True)

                mock_fin.assert_not_called()
                mock_seg.assert_not_called()

                assert result["status"] == "ok"
                assert result["financials"]["targets"] == 3
                assert result["segments"]["targets"] == 3
                # new fields present
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
        assert fb is False  # target_keys=None なので fallback ではない

    def test_stats_separation(self, tmp_path):
        """返却 stats が financials / segments で分離されていること。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path)

        with patch("lib.pipeline.canonical_sync.load_env") as mock_env:
            mock_env.return_value = {"SUPABASE_URL": "http://test", "SUPABASE_ANON_KEY": "key"}

            with patch("lib.pipeline.canonical_sync.write_financials_canonical") as mock_fin, \
                 patch("lib.pipeline.canonical_sync.write_segments_canonical") as mock_seg:
                mock_fin.return_value = {"written": 4, "skipped": 0, "errors": 0}
                mock_seg.return_value = {"written": 2, "skipped": 0, "errors": 0}

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

        with patch("lib.pipeline.canonical_sync.load_env") as mock_env:
            mock_env.return_value = {"SUPABASE_URL": "http://test", "SUPABASE_ANON_KEY": "key"}

            with patch("lib.pipeline.canonical_sync.write_financials_canonical") as mock_fin, \
                 patch("lib.pipeline.canonical_sync.write_segments_canonical") as mock_seg:
                mock_fin.return_value = {"written": 4, "skipped": 0, "errors": 0}
                mock_seg.side_effect = Exception("segments upsert failed")

                from lib.pipeline.canonical_sync import sync_canonical
                result = sync_canonical(db_path=db_path, dry_run=False)

                assert result["status"] == "warning"
                assert result["financials"]["written"] > 0
                assert result["segments"]["errors"] > 0

    def test_strict_mode_raises(self, tmp_path):
        """strict=True で例外が raise されること。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path)

        with patch("lib.pipeline.canonical_sync.load_env") as mock_env:
            mock_env.return_value = {"SUPABASE_URL": "http://test", "SUPABASE_ANON_KEY": "key"}

            with patch("lib.pipeline.canonical_sync.write_financials_canonical") as mock_fin:
                mock_fin.side_effect = Exception("strict failure")

                from lib.pipeline.canonical_sync import sync_canonical
                with pytest.raises(Exception, match="strict failure"):
                    sync_canonical(db_path=db_path, dry_run=False, strict=True)

    def test_idempotent_write(self, tmp_path):
        """同一対象の再実行で件数が増えないこと。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, n_financials=2)

        call_count = 0

        def _mock_write(**kwargs):
            nonlocal call_count
            call_count += 1
            return {"written": len(kwargs.get("metrics_dict", {})), "skipped": 0, "errors": 0}

        with patch("lib.pipeline.canonical_sync.load_env") as mock_env:
            mock_env.return_value = {"SUPABASE_URL": "http://test", "SUPABASE_ANON_KEY": "key"}

            with patch("lib.pipeline.canonical_sync.write_financials_canonical", side_effect=_mock_write), \
                 patch("lib.pipeline.canonical_sync.write_segments_canonical") as mock_seg:
                mock_seg.return_value = {"written": 0, "skipped": 0, "errors": 0}

                from lib.pipeline.canonical_sync import sync_canonical

                r1 = sync_canonical(db_path=db_path, dry_run=False)
                count1 = call_count

                r2 = sync_canonical(db_path=db_path, dry_run=False)
                count2 = call_count - count1

                assert count1 == count2
                assert r1["financials"]["written"] == r2["financials"]["written"]

    def test_empty_db_ok(self, tmp_path):
        """空 DB で status=ok。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, n_financials=0, n_segments=0)

        with patch("lib.pipeline.canonical_sync.load_env") as mock_env:
            mock_env.return_value = {"SUPABASE_URL": "http://test", "SUPABASE_ANON_KEY": "key"}

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
        # financials と segments で同じ ticker/period/quarter を持つ DB を用意
        conn = sqlite3.connect(db_path)
        now_iso = datetime.now(JST).isoformat()
        conn.execute("""CREATE TABLE IF NOT EXISTS quarterly_results (
            id INTEGER PRIMARY KEY, company_code TEXT, period TEXT, quarter TEXT,
            sales REAL, gross_profit REAL, sga REAL, operating_profit REAL,
            field_sources TEXT, disclosure_id TEXT, disclosure_datetime TEXT,
            revision_flag INTEGER DEFAULT 0, updated_at TEXT
        )""")
        conn.execute(
            "INSERT INTO quarterly_results (company_code, period, quarter, sales, "
            "operating_profit, field_sources, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("1000", "2025-03", "1Q", 1000, 100, "summary_xbrl", now_iso),
        )
        conn.execute("""CREATE TABLE IF NOT EXISTS segment_financials (
            ticker TEXT, period TEXT, quarter TEXT, segment_name TEXT,
            segment_sales REAL, segment_profit REAL, source TEXT, filing_id TEXT,
            updated_at TEXT
        )""")
        conn.execute(
            "INSERT INTO segment_financials (ticker, period, quarter, segment_name, "
            "segment_sales, segment_profit, source, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("1000", "2025-03", "1Q", "事業A", 500, 50, "tdnet", now_iso),
        )
        conn.commit()
        conn.close()

        with patch("lib.pipeline.canonical_sync.load_env") as mock_env:
            mock_env.return_value = {"SUPABASE_URL": "http://test", "SUPABASE_ANON_KEY": "key"}

            with patch("lib.pipeline.canonical_sync.write_financials_canonical") as mock_fin, \
                 patch("lib.pipeline.canonical_sync.write_segments_canonical") as mock_seg:
                mock_fin.return_value = {"written": 4, "skipped": 0, "errors": 0}
                mock_seg.return_value = {"written": 2, "skipped": 0, "errors": 0}

                from lib.pipeline.canonical_sync import sync_canonical
                result = sync_canonical(
                    db_path=db_path, dry_run=False,
                    target_keys=[("1000", "2025-03", "1Q")],
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

                assert result["mode"] == "target_keys"
                assert result["fallback_used"] is False
                assert result["target_keys_count"] == 1

    def test_target_keys_zero_then_fallback_sets_mode(self, tmp_path):
        """target_keys から 0件 → fallback で抽出あり →
        mode=lookback_fallback, fallback_used=True。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, n_financials=3)

        with patch("lib.pipeline.canonical_sync.load_env") as mock_env:
            mock_env.return_value = {"SUPABASE_URL": "http://test", "SUPABASE_ANON_KEY": "key"}

            with patch("lib.pipeline.canonical_sync.write_financials_canonical") as mock_fin, \
                 patch("lib.pipeline.canonical_sync.write_segments_canonical") as mock_seg:
                mock_fin.return_value = {"written": 4, "skipped": 0, "errors": 0}
                mock_seg.return_value = {"written": 2, "skipped": 0, "errors": 0}

                from lib.pipeline.canonical_sync import sync_canonical
                # 存在しない target_keys → 0件 → fallback で既存3件が取れる
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
        # 全行の updated_at を1年前にして lookback に引っかからなくする
        _create_test_db(db_path, n_financials=0, n_segments=0)

        with patch("lib.pipeline.canonical_sync.load_env") as mock_env:
            mock_env.return_value = {"SUPABASE_URL": "http://test", "SUPABASE_ANON_KEY": "key"}

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

        with patch("lib.pipeline.canonical_sync.load_env") as mock_env:
            mock_env.return_value = {"SUPABASE_URL": "http://test", "SUPABASE_ANON_KEY": "key"}

            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(db_path=db_path, dry_run=False)

            assert result["status"] == "ok"
            assert result["mode"] == "empty"

    def test_summary_string_present(self, tmp_path):
        """summary が返り、status / mode / fallback_used が含まれること。"""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path)

        with patch("lib.pipeline.canonical_sync.load_env") as mock_env:
            mock_env.return_value = {"SUPABASE_URL": "http://test", "SUPABASE_ANON_KEY": "key"}

            with patch("lib.pipeline.canonical_sync.write_financials_canonical") as mock_fin, \
                 patch("lib.pipeline.canonical_sync.write_segments_canonical") as mock_seg:
                mock_fin.return_value = {"written": 4, "skipped": 0, "errors": 0}
                mock_seg.return_value = {"written": 2, "skipped": 0, "errors": 0}

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

        with patch("lib.pipeline.canonical_sync.load_env") as mock_env:
            mock_env.return_value = {"SUPABASE_URL": "http://test", "SUPABASE_ANON_KEY": "key"}

            with patch("lib.pipeline.canonical_sync.write_financials_canonical") as mock_fin, \
                 patch("lib.pipeline.canonical_sync.write_segments_canonical") as mock_seg:
                mock_fin.return_value = {"written": 4, "skipped": 0, "errors": 0}
                mock_seg.return_value = {"written": 2, "skipped": 0, "errors": 0}

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

        with patch("lib.pipeline.canonical_sync.load_env") as mock_env:
            mock_env.return_value = {"SUPABASE_URL": "http://test", "SUPABASE_ANON_KEY": "key"}

            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(db_path=db_path, dry_run=True, lookback_days=14)
            assert result["lookback_days"] == 14


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
            "financials": {"written": 10, "errors": 0, "rows_selected": 5},
            "segments": {"written": 4, "errors": 0, "rows_selected": 3},
        }
        s = _build_summary(result)
        assert "ok" in s
        assert "target_keys" in s

    def test_init_sub_stats(self):
        from lib.pipeline.canonical_sync import _init_sub_stats
        s = _init_sub_stats()
        for k in ("targets", "rows_selected", "attempted", "written", "skipped", "errors"):
            assert k in s
            assert s[k] == 0
