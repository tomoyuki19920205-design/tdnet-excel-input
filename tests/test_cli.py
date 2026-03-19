# ============================================================
# test_cli.py — 手動修正CLIのユニットテスト
# ============================================================
from __future__ import annotations

import os
import tempfile

import pytest

from src.migration.migration_db import MigrationDB
from src.cli import main as cli_main


@pytest.fixture
def db_path():
    """テスト用一時DBパス"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # テーブルを初期化
    db = MigrationDB(path)
    db.close()
    yield path
    try:
        os.unlink(path)
    except PermissionError:
        pass  # Windows: WALファイルロック


class TestQuarterCommand:
    def test_insert_new(self, db_path: str):
        """新規四半期レコードの作成"""
        cli_main([
            "--db", db_path, "--actor", "tester",
            "quarter",
            "--code", "7203", "--fy", "2026-03-31", "--q", "1Q",
            "--sales", "1000", "--op", "200",
        ])
        db = MigrationDB(db_path)
        r = db.get_quarterly_result("7203", "2026-03-31", "1Q")
        assert r is not None
        assert r["sales"] == 1000
        assert r["operating_profit"] == 200
        db.close()

    def test_update_existing(self, db_path: str):
        """既存レコードの更新 + audit_log"""
        # まず初期データ
        db = MigrationDB(db_path)
        db.upsert_quarterly_result("7203", "2026-03-31", "1Q", sales=500)
        db.commit()
        db.close()

        # CLIで更新
        cli_main([
            "--db", db_path, "--actor", "takuya",
            "quarter",
            "--code", "7203", "--fy", "2026-03-31", "--q", "1Q",
            "--sales", "1000",
        ])

        db = MigrationDB(db_path)
        r = db.get_quarterly_result("7203", "2026-03-31", "1Q")
        assert r["sales"] == 1000

        # audit_logを確認
        logs = db.get_audit_log(company_code="7203")
        assert len(logs) >= 1
        sales_log = [l for l in logs if l["field_name"] == "sales"]
        assert len(sales_log) == 1
        assert sales_log[0]["actor"] == "takuya"
        assert sales_log[0]["source"] == "manual"
        assert sales_log[0]["old_value"] == "500.0"
        assert sales_log[0]["new_value"] == "1000.0"
        db.close()


class TestSegmentCommand:
    def test_insert_segment(self, db_path: str):
        """セグメントの新規作成"""
        cli_main([
            "--db", db_path, "--actor", "tester",
            "segment",
            "--code", "7203", "--fy", "2026-03-31", "--q", "1Q",
            "--seg-name", "自動車", "--seg-order", "0",
            "--sales", "800", "--profit", "150",
        ])
        db = MigrationDB(db_path)
        segs = db.get_segments("7203", "2026-03-31", "1Q")
        assert len(segs) == 1
        assert segs[0]["segment_name"] == "自動車"
        assert segs[0]["segment_sales"] == 800
        db.close()


class TestMemoCommand:
    def test_insert_memo(self, db_path: str):
        """メモの追記"""
        cli_main([
            "--db", db_path, "--actor", "tester",
            "memo",
            "--code", "7203", "--fy", "2026-03-31", "--q", "1Q",
            "--text", "好調\\n増収増益",
        ])
        db = MigrationDB(db_path)
        note = db.get_latest_note("7203", "2026-03-31", "1Q")
        assert note == "好調\n増収増益"
        # audit_log
        logs = db.get_audit_log(company_code="7203")
        assert len(logs) == 1
        assert logs[0]["entity_type"] == "note"
        db.close()

    def test_duplicate_memo_skipped(self, db_path: str):
        """同一メモは追加しない"""
        cli_main([
            "--db", db_path,
            "memo",
            "--code", "7203", "--fy", "2026-03-31", "--q", "1Q",
            "--text", "好調",
        ])
        cli_main([
            "--db", db_path,
            "memo",
            "--code", "7203", "--fy", "2026-03-31", "--q", "1Q",
            "--text", "好調",
        ])
        db = MigrationDB(db_path)
        cur = db._conn.execute(
            "SELECT COUNT(*) FROM quarterly_notes WHERE company_code=?",
            ("7203",),
        )
        assert cur.fetchone()[0] == 1  # 重複なし
        db.close()


class TestValidation:
    def test_invalid_code(self, db_path: str):
        """不正な企業コード"""
        with pytest.raises(SystemExit):
            cli_main([
                "--db", db_path,
                "quarter",
                "--code", "abc", "--fy", "2026-03-31", "--q", "1Q",
                "--sales", "100",
            ])

    def test_invalid_fy(self, db_path: str):
        """不正な年度形式"""
        with pytest.raises(SystemExit):
            cli_main([
                "--db", db_path,
                "quarter",
                "--code", "7203", "--fy", "R8/3", "--q", "1Q",
                "--sales", "100",
            ])

    def test_invalid_quarter(self, db_path: str):
        """不正な四半期"""
        with pytest.raises(SystemExit):
            cli_main([
                "--db", db_path,
                "quarter",
                "--code", "7203", "--fy", "2026-03-31", "--q", "5Q",
                "--sales", "100",
            ])
