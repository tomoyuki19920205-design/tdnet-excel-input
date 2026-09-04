"""tests/test_unit_normalization.py — 単位正規化の回帰テスト"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ================================================================
# sync_financials._to_millions テスト
# ================================================================

class TestToMillions:
    """_to_millions ヘルパーの単体テスト"""

    def test_basic_conversion(self):
        from tools.sync_financials import _to_millions
        assert _to_millions(11134000000) == 11134

    def test_none_passthrough(self):
        from tools.sync_financials import _to_millions
        assert _to_millions(None) is None

    def test_zero(self):
        from tools.sync_financials import _to_millions
        assert _to_millions(0) == 0

    def test_negative(self):
        from tools.sync_financials import _to_millions
        assert _to_millions(-659000000) == -659

    def test_small_value_truncates(self):
        from tools.sync_financials import _to_millions
        # 500,000 円 = 0.5 百万円 → int で 0
        assert _to_millions(500_000) == 0

    def test_float_input(self):
        from tools.sync_financials import _to_millions
        assert _to_millions(2597000000.0) == 2597

    def test_string_numeric(self):
        """文字列で渡された数値も変換可能"""
        from tools.sync_financials import _to_millions
        assert _to_millions("11134000000") == 11134

    def test_kosel_2q_sales(self):
        """コーセル 2Q の実データ: sales=11134000000 → 11134"""
        from tools.sync_financials import _to_millions
        assert _to_millions(11134000000) == 11134

    def test_kosel_2q_gp(self):
        """コーセル 2Q の実データ: gross_profit=2597000000 → 2597"""
        from tools.sync_financials import _to_millions
        assert _to_millions(2597000000) == 2597

    def test_kosel_2q_op(self):
        """コーセル 2Q の実データ: operating_profit=-659000000 → -659"""
        from tools.sync_financials import _to_millions
        assert _to_millions(-659000000) == -659


# ================================================================
# sync_financials で百万円に正規化されることの統合テスト
# ================================================================

class TestSyncFinancialsNormalization:
    """sync_financials の read_sqlite 結果が百万円に正規化されること"""

    def test_jquants_yen_to_millions(self, tmp_path):
        """J-Quants 円データが百万円に変換されること"""
        import sqlite3

        db_path = str(tmp_path / "test_jquants.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE jquants_financials_normalized (
                local_code TEXT,
                disclosed_date TEXT,
                current_fiscal_year_end_date TEXT,
                type_of_current_period TEXT,
                type_of_document TEXT,
                net_sales REAL,
                gross_profit REAL,
                operating_profit REAL,
                raw_json TEXT,
                fetched_at TEXT
            )
        """)
        # コーセル 2Q のデータ (円単位)
        conn.execute("""
            INSERT INTO jquants_financials_normalized
            (local_code, disclosed_date, current_fiscal_year_end_date,
             type_of_current_period, type_of_document,
             net_sales, gross_profit, operating_profit, raw_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "69050", "2025-12-19", "2026-05-20", "2Q",
            "2QFinancialStatements_Consolidated_JP",
            11134000000, 2597000000, -659000000, "{}", "2026-03-02"
        ))
        conn.commit()
        conn.close()

        with patch("tools.sync_financials.logger"):
            from tools.sync_financials import read_sqlite
            data, _ = read_sqlite(db_path, recent_days=9999)

        assert len(data) == 1
        row = data[0]
        # 百万円に正規化されていること
        assert row["sales"] == 11134
        assert row["gross_profit"] == 2597
        assert row["operating_profit"] == -659
        assert row["source"] == "jquants"


# ================================================================
# canonical_sync の unit 設定テスト
# ================================================================

class TestCanonicalSyncUnit:
    """canonical_sync が unit=millions_jpy で書き込むこと"""

    def test_financials_unit_is_millions_jpy(self, tmp_path):
        """canonical_sync の financials 書き込みで unit=millions_jpy が設定されること"""
        import sqlite3
        from datetime import datetime, timezone, timedelta

        JST = timezone(timedelta(hours=9))
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
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
        conn.execute(
            "INSERT INTO quarterly_results "
            "(company_code, fiscal_year_end, quarter, sales, operating_profit, "
            "field_sources, disclosure_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("6905", "2026-05-31", "3Q", 17346, -899,
             "summary_xbrl", "disc_1", now_iso),
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS segment_financials (
                ticker TEXT, period TEXT, quarter TEXT,
                segment_name TEXT, segment_sales REAL, segment_profit REAL,
                source TEXT, filing_id TEXT, updated_at TEXT
            )
        """)
        conn.commit()
        conn.close()

        captured_rows = []
        def capture(table, rows, **kwargs):
            if table == "canonical_financials":
                captured_rows.extend(rows)
            return {"ok": True, "count": len(rows), "error": None}

        with patch("lib.pipeline.canonical_sync.load_env"), \
             patch("lib.pipeline.canonical_sync.get_supabase_write_config", return_value={"url":"x","key":"y"}), \
             patch("lib.pipeline.canonical_sync.supabase_upsert", side_effect=capture):
            from lib.pipeline.canonical_sync import sync_canonical
            result = sync_canonical(db_path=db_path, dry_run=False, sync_segments=False)
        assert result["financials"]["written"] > 0
        assert {r["unit"] for r in captured_rows} == {"millions_jpy"}
        assert next(r["value"] for r in captured_rows if r["metric"] == "sales") == 17346


# ================================================================
# 値の magnitude テスト (円/百万円の混在防止)
# ================================================================

class TestMagnitudeValidation:
    """同一テーブル内で円と百万円が混在しないことのバリデーション"""

    def test_detect_yen_in_millions_context(self):
        """百万円コンテキストで円単位の値を検出"""
        from tools.sync_financials import _ABNORMAL_MILLIONS_THRESHOLD

        # 百万円として正常な値 (トヨタの売上 ~45,000,000)
        assert 45_000_000 < _ABNORMAL_MILLIONS_THRESHOLD

        # 円単位がそのまま入った場合 (11,134,000,000) → 異常と判定
        assert 11_134_000_000 > _ABNORMAL_MILLIONS_THRESHOLD

    def test_negative_abnormal_also_detected(self):
        """負の異常値も検出される"""
        from tools.sync_financials import _ABNORMAL_MILLIONS_THRESHOLD
        assert abs(-11_134_000_000) > _ABNORMAL_MILLIONS_THRESHOLD


# ================================================================
# canonical_writer unit パラメータテスト
# ================================================================

class TestCanonicalWriterUnit:
    """canonical_writer が unit パラメータを正しく伝播すること"""

    def test_unit_propagated_to_rows(self):
        """unit パラメータが upsert 行に反映されること"""
        from lib.pipeline.canonical_writer import write_financials_canonical

        mock_upsert = MagicMock(return_value={"ok": True, "count": 1, "error": None})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            write_financials_canonical(
                ticker="6905",
                period="2026-05-31",
                quarter="3Q",
                metrics_dict={"sales": 17346},
                source="tdnet",
                unit="millions_jpy",
                config={"url": "x", "key": "y"},
            )

        rows = mock_upsert.call_args[0][1]
        assert rows[0]["unit"] == "millions_jpy"

    def test_default_unit_is_millions_jpy(self):
        """The writer default matches normalized caller amounts."""
        from lib.pipeline.canonical_writer import write_financials_canonical

        mock_upsert = MagicMock(return_value={"ok": True, "count": 1, "error": None})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            write_financials_canonical(
                ticker="6905",
                period="2026-05-31",
                quarter="3Q",
                metrics_dict={"sales": 17346},
                source="tdnet",
                config={"url": "x", "key": "y"},
            )

        rows = mock_upsert.call_args[0][1]
        assert rows[0]["unit"] == "millions_jpy"
