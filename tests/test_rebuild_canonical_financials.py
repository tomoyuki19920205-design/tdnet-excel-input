#!/usr/bin/env python3
"""
test_rebuild_canonical_financials.py
canonical_financials 再生成ツールのユニットテスト
"""
import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _PROJECT_ROOT)

from tools.rebuild_canonical_financials import (
    _to_millions,
    _read_jquants_source,
    SOURCE_GROUPS,
    build_parser,
    main as rebuild_main,
)
from lib.pipeline.unit_convert import to_millions
from lib.pipeline.canonical_writer import expand_financials_rows


# ============================================================
# 1. to_millions テスト (共通モジュール + 各ファイル同値性保証)
# ============================================================
class TestToMillions:
    """to_millions の整数ベース変換と同値性テスト。"""

    def test_none(self):
        assert to_millions(None) is None

    def test_zero(self):
        assert to_millions(0) == 0

    def test_million_yen(self):
        """1,000,000円 → 1百万円"""
        assert to_millions(1_000_000) == 1

    def test_large(self):
        """70,800,000,000円 → 70,800百万円"""
        assert to_millions(70_800_000_000) == 70_800

    def test_representative_amount(self):
        """32,000,000円 → 32百万円"""
        assert to_millions(32_000_000) == 32

    def test_small_truncates(self):
        """500,000円 → 0百万円 (切り捨て)"""
        assert to_millions(500_000) == 0

    def test_negative(self):
        """-1,500,000円 → -1百万円 (truncation toward zero)"""
        assert to_millions(-1_500_000) == -1

    def test_negative_large(self):
        """-32,000,000円 → -32百万円"""
        assert to_millions(-32_000_000) == -32

    def test_negative_small_truncates(self):
        """-999,999円 → 0百万円 (truncation toward zero)"""
        assert to_millions(-999_999) == 0

    def test_float_input(self):
        """float 入力もOK"""
        assert to_millions(1_000_000.0) == 1

    def test_string_number(self):
        """文字列数値もOK"""
        assert to_millions("1000000") == 1

    def test_rebuild_uses_shared_module(self):
        """rebuild_canonical_financials の _to_millions が共通モジュールと同一。"""
        test_values = [
            None, 0, 1_000_000, 70_800_000_000, 500_000,
            -1_500_000, 1_000_000.0, 123_456_789,
            32_000_000, -32_000_000, -999_999,
        ]
        for val in test_values:
            assert _to_millions(val) == to_millions(val), (
                f"_to_millions({val!r}) differs: "
                f"rebuild={_to_millions(val)}, shared={to_millions(val)}"
            )

    def test_sync_financials_equivalence(self):
        """sync_financials.py の _to_millions と共通モジュールが同値。"""
        try:
            from tools.sync_financials import _to_millions as sf_to_millions
        except ImportError:
            pytest.skip("sync_financials.py not importable")

        test_values = [
            None, 0, 1_000_000, 70_800_000_000, 500_000,
            -1_500_000, 1_000_000.0, 123_456_789,
            32_000_000, -32_000_000, -999_999,
        ]
        for val in test_values:
            assert to_millions(val) == sf_to_millions(val), (
                f"to_millions({val!r}) differs: "
                f"shared={to_millions(val)}, sync={sf_to_millions(val)}"
            )


# ============================================================
# 2. source-group マッピングテスト
# ============================================================
class TestSourceGroups:
    """source-group → 実 source 値のマッピング。"""

    def test_jquants_group(self):
        assert SOURCE_GROUPS["jquants"] == ["jquants"]

    def test_tdnet_group_contains_expected(self):
        tdnet = SOURCE_GROUPS["tdnet"]
        assert "tdnet" in tdnet
        assert "summary_xbrl" in tdnet
        assert "attachment_xbrl" in tdnet

    def test_no_overlap(self):
        """jquants と tdnet に共通 source がないこと。"""
        jq = set(SOURCE_GROUPS["jquants"])
        td = set(SOURCE_GROUPS["tdnet"])
        assert len(jq & td) == 0


# ============================================================
# 3. _read_jquants_source テスト
# ============================================================
def _create_test_jquants_db(db_path: str, rows: list[dict]) -> None:
    """テスト用 jquants.db を作成。"""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE jquants_financials_normalized (
            local_code                TEXT NOT NULL,
            disclosed_date            TEXT NOT NULL,
            current_fiscal_year_end_date TEXT NOT NULL,
            type_of_current_period    TEXT NOT NULL,
            type_of_document          TEXT NOT NULL DEFAULT '',
            net_sales                 INTEGER,
            gross_profit              INTEGER,
            operating_profit          INTEGER,
            raw_json                  TEXT,
            fetched_at                TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(local_code, disclosed_date, type_of_document,
                   type_of_current_period, current_fiscal_year_end_date)
        )
    """)
    for r in rows:
        conn.execute("""
            INSERT INTO jquants_financials_normalized
                (local_code, disclosed_date, current_fiscal_year_end_date,
                 type_of_current_period, type_of_document,
                 net_sales, gross_profit, operating_profit)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            r.get("code", "72030"),
            r.get("disclosed", "2026-01-15"),
            r.get("fy_end", "2026-03-31"),
            r.get("period", "3Q"),
            r.get("doc_type", ""),
            r.get("sales"),
            r.get("gp"),
            r.get("op"),
        ))
    conn.commit()
    conn.close()


class TestReadJquantsSource:
    """J-Quants SQLite → 百万円変換 + 重複排除。"""

    def test_basic_read(self, tmp_path):
        """基本的な読み取り + 百万円変換。"""
        db = str(tmp_path / "jquants.db")
        _create_test_jquants_db(db, [{
            "code": "72030",
            "disclosed": "2026-01-15",
            "fy_end": "2026-03-31",
            "period": "3Q",
            "sales": 100_000_000_000,      # 100十億円 → 100,000百万円
            "gp": 30_000_000_000,           # → 30,000百万円
            "op": 10_000_000_000,           # → 10,000百万円
        }])
        data = _read_jquants_source(db)
        assert len(data) == 1
        d = data[0]
        assert d["ticker"] == "7203"  # 5桁→4桁正規化
        assert d["period"] == "2026-03-31"
        assert d["quarter"] == "3Q"
        assert d["source"] == "jquants"
        assert d["metrics"]["sales"] == 100_000
        assert d["metrics"]["gross_profit"] == 30_000
        assert d["metrics"]["operating_profit"] == 10_000

    def test_null_fields_skipped(self, tmp_path):
        """全メトリクス NULL の行はスキップ。"""
        db = str(tmp_path / "jquants.db")
        _create_test_jquants_db(db, [{
            "code": "72030",
            "disclosed": "2026-01-15",
            "fy_end": "2026-03-31",
            "period": "3Q",
            "sales": None,
            "gp": None,
            "op": None,
        }])
        data = _read_jquants_source(db)
        assert len(data) == 0

    def test_partial_null_kept(self, tmp_path):
        """一部が NULL でも他がある場合は行を保持。"""
        db = str(tmp_path / "jquants.db")
        _create_test_jquants_db(db, [{
            "code": "72030",
            "disclosed": "2026-01-15",
            "fy_end": "2026-03-31",
            "period": "3Q",
            "sales": 1_000_000,
            "gp": None,
            "op": None,
        }])
        data = _read_jquants_source(db)
        assert len(data) == 1
        assert data[0]["metrics"]["sales"] == 1
        assert data[0]["metrics"]["gross_profit"] is None
        assert data[0]["metrics"]["operating_profit"] is None

    def test_ticker_normalization_dedup(self, tmp_path):
        """5桁→4桁正規化で衝突する行が重複排除される。"""
        db = str(tmp_path / "jquants.db")
        _create_test_jquants_db(db, [
            {
                "code": "72030",
                "disclosed": "2026-01-15",
                "fy_end": "2026-03-31",
                "period": "3Q",
                "doc_type": "summary",
                "sales": 1_000_000, "gp": None, "op": None,
            },
            {
                "code": "72030",
                "disclosed": "2026-01-15",
                "fy_end": "2026-03-31",
                "period": "3Q",
                "doc_type": "detail",
                "sales": 2_000_000, "gp": None, "op": None,
            },
        ])
        data = _read_jquants_source(db)
        # CTE の ROW_NUMBER() で 1行に集約される
        assert len(data) == 1

    def test_field_coalesce(self, tmp_path):
        """訂正開示で NULL になったフィールドは元開示の値で補完。"""
        db = str(tmp_path / "jquants.db")
        _create_test_jquants_db(db, [
            {
                "code": "72030",
                "disclosed": "2026-01-01",  # 古い
                "fy_end": "2026-03-31",
                "period": "3Q",
                "doc_type": "",
                "sales": 1_000_000, "gp": 500_000, "op": 200_000,
            },
            {
                "code": "72030",
                "disclosed": "2026-02-01",  # 新しい (gp が NULL)
                "fy_end": "2026-03-31",
                "period": "3Q",
                "doc_type": "",
                "sales": 1_200_000, "gp": None, "op": 250_000,
            },
        ])
        data = _read_jquants_source(db)
        assert len(data) == 1
        # sales/op は新しい行、gp は古い行から COALESCE
        assert data[0]["metrics"]["sales"] == 1  # 1,200,000 / 1M = 1
        assert data[0]["metrics"]["gross_profit"] == 0  # 500,000 / 1M = 0 (切り捨て)
        assert data[0]["metrics"]["operating_profit"] == 0  # 250,000 / 1M = 0

    def test_missing_db(self):
        """DB ファイルが存在しない場合は空リストを返す。"""
        data = _read_jquants_source("/nonexistent/path/jquants.db")
        assert data == []


# ============================================================
# 4. CLI パーサーテスト
# ============================================================
class TestCLIParser:
    """CLI パーサーの引数解析。"""

    def test_init_sql(self):
        parser = build_parser()
        opts = parser.parse_args(["init-sql"])
        assert opts.command == "init-sql"

    def test_rebuild_jquants_dry_run(self):
        parser = build_parser()
        opts = parser.parse_args([
            "rebuild", "--source-group", "jquants", "--dry-run"
        ])
        assert opts.command == "rebuild"
        assert opts.source_group == "jquants"
        assert not opts.apply

    def test_rebuild_jquants_apply(self):
        parser = build_parser()
        opts = parser.parse_args([
            "rebuild", "--source-group", "jquants", "--apply"
        ])
        assert opts.command == "rebuild"
        assert opts.source_group == "jquants"
        assert opts.apply

    def test_compare(self):
        parser = build_parser()
        opts = parser.parse_args([
            "compare", "--source-group", "jquants"
        ])
        assert opts.command == "compare"
        assert opts.source_group == "jquants"

    def test_verify(self):
        parser = build_parser()
        opts = parser.parse_args(["verify"])
        assert opts.command == "verify"

    def test_switch_sql(self):
        parser = build_parser()
        opts = parser.parse_args(["switch-sql"])
        assert opts.command == "switch-sql"


# ============================================================
# 5. rebuild dry-run テスト (end-to-end、Supabase無し)
# ============================================================
class TestRebuildDryRun:
    """rebuild --source-group jquants --dry-run の E2E テスト。"""

    def test_dry_run_no_write(self, tmp_path, capsys):
        """dry-run では Supabase に書き込まない。"""
        db = str(tmp_path / "jquants.db")
        _create_test_jquants_db(db, [{
            "code": "72030",
            "disclosed": "2026-01-15",
            "fy_end": "2026-03-31",
            "period": "3Q",
            "sales": 100_000_000_000,
            "gp": 30_000_000_000,
            "op": 10_000_000_000,
        }])

        # PROJECT_ROOT/data/jquants.db をテストDBに差し替え
        with patch(
            "tools.rebuild_canonical_financials._PROJECT_ROOT",
            str(tmp_path),
        ):
            # data/ ディレクトリとjquants.dbを配置
            data_dir = tmp_path / "data"
            data_dir.mkdir(exist_ok=True)
            import shutil
            shutil.copy(db, str(data_dir / "jquants.db"))

            result = rebuild_main([
                "rebuild", "--source-group", "jquants", "--dry-run"
            ])

        assert result == 0
        output = capsys.readouterr().out
        assert "DRY-RUN" in output
        assert "jquants" in output


# ============================================================
# 6. expand_financials_rows 連携テスト
# ============================================================
class TestExpandIntegration:
    """expand_financials_rows が期待通り呼ばれることを確認。"""

    def test_expand_produces_correct_rows(self):
        """jquants source で expand すると正しい long rows が生成される。"""
        expanded, skipped = expand_financials_rows(
            ticker="7203",
            period="2026-03-31",
            quarter="3Q",
            metrics_dict={
                "sales": 100_000,
                "gross_profit": 30_000,
                "operating_profit": 10_000,
            },
            source="jquants",
            unit="millions_jpy",
        )
        assert len(expanded) == 3
        assert skipped == 0

        metrics = {r["metric"]: r for r in expanded}
        assert "sales" in metrics
        assert "gross_profit" in metrics
        assert "operating_profit" in metrics

        # 値チェック
        assert metrics["sales"]["value"] == 100_000
        assert metrics["sales"]["source"] == "jquants"
        assert metrics["sales"]["unit"] == "millions_jpy"

        # source_row_key が決定的に生成されること
        for r in expanded:
            assert r.get("source_row_key")
            assert r["source_row_key"].startswith("cf|")

    def test_expand_null_metrics(self):
        """全 NULL メトリクスでは 0 行 + 3 skipped。"""
        expanded, skipped = expand_financials_rows(
            ticker="7203",
            period="2026-03-31",
            quarter="3Q",
            metrics_dict={
                "sales": None,
                "gross_profit": None,
                "operating_profit": None,
            },
            source="jquants",
            unit="millions_jpy",
        )
        assert len(expanded) == 0
        assert skipped == 3

    def test_expand_partial_null(self):
        """一部 NULL は非 NULL 行のみ生成。"""
        expanded, skipped = expand_financials_rows(
            ticker="7203",
            period="2026-03-31",
            quarter="3Q",
            metrics_dict={
                "sales": 100,
                "gross_profit": None,
                "operating_profit": None,
            },
            source="jquants",
            unit="millions_jpy",
        )
        assert len(expanded) == 1
        assert skipped == 2
        assert expanded[0]["metric"] == "sales"


# ============================================================
# 7. init-sql テスト
# ============================================================
class TestInitSQL:
    """init-sql がマイグレーション SQL を出力すること。"""

    def test_outputs_sql(self, capsys):
        """SQL が stdout に出力される。"""
        result = rebuild_main(["init-sql"])
        assert result == 0
        output = capsys.readouterr().out
        assert "canonical_financials_rebuild" in output
        assert "CREATE TABLE" in output
        assert "source_row_key" in output


# ============================================================
# 8. switch-sql テスト
# ============================================================
class TestSwitchSQL:
    """switch-sql が事前チェック + 切替 + ロールバック SQL を出力すること。"""

    def test_outputs_all_sections(self, capsys):
        result = rebuild_main(["switch-sql"])
        assert result == 0
        output = capsys.readouterr().out
        # 事前チェック
        assert "pg_views" in output
        assert "pg_policies" in output
        assert "pg_proc" in output
        # 切替
        assert "RENAME TO" in output
        assert "canonical_financials_rebuild" in output
        # ロールバック
        assert "canonical_financials_backup_" in output
