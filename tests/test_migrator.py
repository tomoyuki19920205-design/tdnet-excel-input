# ============================================================
# test_migrator.py — Excel→DB 統合テスト
# ============================================================
from __future__ import annotations

import os
import tempfile

import openpyxl
import pytest

from src.migration.excel_parser import parse_excel
from src.migration.migration_db import MigrationDB
from src.migration.migrator import run_migration


def _create_test_excel(rows: list[dict], sheet_name: str = "PL") -> str:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    for i, row_data in enumerate(rows, start=1):
        for col_letter, value in row_data.items():
            ws[f"{col_letter}{i}"] = value
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    wb.save(path)
    wb.close()
    return path


class TestEndToEnd:
    """Excel → パース → DB書き込み → 読み取り検証"""

    def test_full_migration(self):
        rows = [
            # 会社1
            {"A": "7203", "C": "トヨタ自動車", "D": "東京"},
            {"O": "売上", "P": "粗利", "Q": "粗利率", "R": "管理費",
             "AA": "自動車売上", "AB": "自動車利益"},
            {"M": "R8/3", "N": "1Q", "O": 100, "P": 30, "Q": 0.30,
             "R": 10, "S": 20, "Z": "好調な滑り出し",
             "AA": 80, "AB": 15},
            {"N": "2Q", "O": 210, "P": 65, "Q": 0.31, "R": 22, "S": 43,
             "AA": 170, "AB": 33},
            # 会社2
            {"A": "6758"},
            {"O": "売上", "P": "粗利", "Q": "粗利率", "R": "管理費"},
            {"M": "R7/12", "N": "1Q", "O": 50, "P": 15, "Q": 0.30,
             "R": 5, "S": 10},
        ]
        excel_path = _create_test_excel(rows)
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            # パース
            result = parse_excel(excel_path, "PL")
            assert len(result.blocks) == 2

            # DB書き込み
            db = MigrationDB(db_path)
            summary = run_migration(
                result, db, "test-run-001", sheet_name="PL",
            )
            db.close()

            # サマリ検証
            assert summary.companies_processed == 2
            assert summary.quarters_inserted == 3
            assert summary.segments_inserted == 2

            # DB読み取り検証
            db2 = MigrationDB(db_path)

            # 会社1 1Q
            r = db2.get_quarterly_result("7203", "2026-03-31", "1Q")
            assert r is not None
            assert r["sales"] == 100
            assert r["gross_profit"] == 30
            assert r["operating_profit"] == 20

            # 会社1 2Q（年度引継ぎ）
            r2 = db2.get_quarterly_result("7203", "2026-03-31", "2Q")
            assert r2 is not None
            assert r2["sales"] == 210

            # メモ
            memo = db2.get_company_memo("7203")
            assert memo["col_c"] == "トヨタ自動車"
            assert memo["col_d"] == "東京"

            # 四半期メモ
            note = db2.get_latest_note("7203", "2026-03-31", "1Q")
            assert note == "好調な滑り出し"

            # セグメント
            segs = db2.get_segments("7203", "2026-03-31", "1Q")
            assert len(segs) == 1
            assert segs[0]["segment_name"] == "自動車売上"
            assert segs[0]["segment_sales"] == 80
            assert segs[0]["segment_profit"] == 15

            # 会社2（12月決算）
            r3 = db2.get_quarterly_result("6758", "2025-12-31", "1Q")
            assert r3 is not None
            assert r3["sales"] == 50

            db2.close()
        finally:
            os.unlink(excel_path)
            os.unlink(db_path)

    def test_dry_run(self):
        """dry-runではDBに書き込まない"""
        rows = [
            {"A": "7203"},
            {"O": "売上", "P": "粗利", "Q": "粗利率", "R": "管理費"},
            {"M": "R8/3", "N": "1Q", "O": 100, "P": 30, "Q": 0.30,
             "R": 10, "S": 20},
        ]
        excel_path = _create_test_excel(rows)
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            result = parse_excel(excel_path, "PL")
            db = MigrationDB(db_path)
            summary = run_migration(
                result, db, "dry-run-001", dry_run=True,
            )

            # サマリにはカウントされる
            assert summary.companies_processed == 1
            assert summary.quarters_inserted == 1

            # DBには書かれていない
            r = db.get_quarterly_result("7203", "2026-03-31", "1Q")
            assert r is None

            db.close()
        finally:
            os.unlink(excel_path)
            os.unlink(db_path)

    def test_log_entries_stored(self):
        """ログエントリがDBに保存される"""
        # 150行超過をトリガー
        rows: list[dict] = [{"A": "9999"}]
        for _ in range(155):
            rows.append({"B": "dummy"})
        excel_path = _create_test_excel(rows)
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            result = parse_excel(excel_path, "PL")
            db = MigrationDB(db_path)
            summary = run_migration(result, db, "log-test-001")

            assert summary.skips >= 1

            logs = db.get_logs_by_run("log-test-001")
            assert len(logs) >= 1
            assert any(l["log_type"] == "SKIP_DISTANCE" for l in logs)

            db.close()
        finally:
            os.unlink(excel_path)
            os.unlink(db_path)
