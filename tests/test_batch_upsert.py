"""tests/test_batch_upsert.py — batch_upsert のテスト

明示的 transaction (BEGIN/COMMIT/ROLLBACK) の動作を検証。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.backfill import batch_upsert
from lib.backfill.batch_upsert import (
    BatchUpsertStats,
    batch_upsert_segments,
    dry_run_upsert_segments,
    normalize_and_validate_rec,
)


class MockDB:
    """MigrationDB の upsert_segment を模倣する最小 mock。"""

    def __init__(self, db_path: str, *, with_documents: bool = True):
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS segment_financials (
                company_code TEXT,
                fiscal_year_end TEXT,
                quarter TEXT,
                segment_name TEXT,
                segment_order INTEGER,
                segment_sales REAL,
                segment_profit REAL,
                raw_profit_label TEXT DEFAULT '',
                data_source TEXT DEFAULT '',
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT '',
                PRIMARY KEY (company_code, fiscal_year_end, quarter, segment_name)
            )
        """)
        if with_documents:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    tdnet_doc_id TEXT,
                    title TEXT
                )
            """)
        self._conn.commit()

    def upsert_segment(
        self,
        company_code,
        fiscal_year_end,
        quarter,
        segment_name,
        segment_order=0,
        segment_sales=None,
        segment_profit=None,
        raw_profit_label="",
        data_source="",
        actor="",
        source="",
        **kwargs,
    ) -> str:
        existing = self._conn.execute(
            "SELECT 1 FROM segment_financials WHERE company_code=? AND fiscal_year_end=? AND quarter=? AND segment_name=?",
            (company_code, fiscal_year_end, quarter, segment_name),
        ).fetchone()

        if existing is None:
            self._conn.execute(
                "INSERT INTO segment_financials (company_code, fiscal_year_end, quarter, segment_name, segment_order, segment_sales, segment_profit) VALUES (?,?,?,?,?,?,?)",
                (company_code, fiscal_year_end, quarter, segment_name, segment_order, segment_sales, segment_profit),
            )
            return "inserted"
        else:
            self._conn.execute(
                "UPDATE segment_financials SET segment_sales=?, segment_profit=? WHERE company_code=? AND fiscal_year_end=? AND quarter=? AND segment_name=?",
                (segment_sales, segment_profit, company_code, fiscal_year_end, quarter, segment_name),
            )
            return "updated"

    def close(self):
        self._conn.close()


def _make_records(count: int) -> list[dict]:
    return [
        {
            "ticker": "6750",
            "period": "2025-03-31",
            "quarter": "FY",
            "segment_name": f"Seg{i}",
            "segment_order": i,
            "segment_sales": 100000 * i,
            "segment_profit": 10000 * i,
        }
        for i in range(count)
    ]


def _make_fy_segment(**overrides) -> dict:
    record = {
        "ticker": "1234",
        "period": "2024-03-31",
        "quarter": "FY",
        "segment_name": "Core",
        "segment_order": 1,
        "segment_sales": 1200.0,
        "segment_profit": 300.0,
        "source": "xbrl",
        "tdnet_doc_id": "",
        "disclosure_date": "",
    }
    record.update(overrides)
    return record


def _create_earnings_summaries(db: MockDB) -> None:
    db._conn.execute("""
        CREATE TABLE earnings_summaries (
            ticker TEXT,
            quarter TEXT,
            disclosure_date TEXT,
            title TEXT,
            fiscal_year TEXT,
            company_name TEXT
        )
    """)
    db._conn.execute(
        "INSERT INTO earnings_summaries VALUES (?, ?, ?, ?, ?, ?)",
        ("1234", "1Q", "20240601", "FY 2024", "2024", "Example Co."),
    )
    db._conn.execute(
        "INSERT INTO earnings_summaries VALUES (?, ?, ?, ?, ?, ?)",
        ("1234", "FY", "20240501", "FY 2024", "2024-03-31", "Example Co."),
    )
    db._conn.commit()


def _make_1q_segment(**overrides) -> dict:
    values = {
        "period": "2023-06-30",
        "quarter": "1Q",
        "disclosure_date": "20240601",
    }
    values.update(overrides)
    return _make_fy_segment(**values)


class TestBatchUpsert:
    def test_single_batch(self, tmp_path):
        db = MockDB(str(tmp_path / "test.db"))
        records = _make_records(5)
        stats = batch_upsert_segments(records, db, batch_size=100)

        assert stats.total_records == 5
        assert stats.total_batches == 1
        assert stats.succeeded_batches == 1
        assert stats.inserted == 5
        assert stats.failed_batches == 0
        db.close()

    def test_multiple_batches(self, tmp_path):
        db = MockDB(str(tmp_path / "test.db"))
        records = _make_records(10)
        stats = batch_upsert_segments(records, db, batch_size=3)

        assert stats.total_records == 10
        assert stats.total_batches == 4  # ceil(10/3)
        assert stats.succeeded_batches == 4
        assert stats.inserted == 10
        db.close()

    def test_update_existing(self, tmp_path):
        db = MockDB(str(tmp_path / "test.db"))
        records = _make_records(3)
        batch_upsert_segments(records, db, batch_size=100)

        # 同じレコードを再upsert → updated
        for r in records:
            r["segment_sales"] = 999999
        stats = batch_upsert_segments(records, db, batch_size=100)
        assert stats.updated == 3
        assert stats.inserted == 0
        db.close()

    def test_empty_records(self, tmp_path):
        db = MockDB(str(tmp_path / "test.db"))
        stats = batch_upsert_segments([], db)
        assert stats.total_records == 0
        assert stats.total_batches == 0
        db.close()

    def test_transaction_boundary(self, tmp_path):
        """batch 間で transaction が分離されている。"""
        db = MockDB(str(tmp_path / "test.db"))
        records = _make_records(6)
        stats = batch_upsert_segments(records, db, batch_size=3)

        # 2 batches, 各 3 records
        assert stats.total_batches == 2
        assert stats.succeeded_batches == 2

        # DB に全件入っている
        count = db._conn.execute("SELECT COUNT(*) FROM segment_financials").fetchone()[0]
        assert count == 6
        db.close()


class TestBatchUpsertFailed:
    def test_rollback_on_error(self, tmp_path):
        """upsert_segment がエラーを投げた場合 ROLLBACK される。"""
        db = MockDB(str(tmp_path / "test.db"))

        # 最初のバッチは成功させ、2番目で例外
        original_upsert = db.upsert_segment
        call_count = [0]

        def failing_upsert(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] > 3:
                raise RuntimeError("simulated DB error")
            return original_upsert(*args, **kwargs)

        db.upsert_segment = failing_upsert

        records = _make_records(6)
        stats = batch_upsert_segments(records, db, batch_size=3)

        # batch 1 成功、batch 2 失敗
        assert stats.succeeded_batches == 1
        assert stats.failed_batches == 1

        # batch 1 の 3 件のみ DB に入っている
        count = db._conn.execute("SELECT COUNT(*) FROM segment_financials").fetchone()[0]
        assert count == 3
        db.close()


class TestOptionalEarningsSummaries:
    def test_table_absent_dry_run_keeps_valid_segment_scheduled(self, tmp_path, capsys):
        db = MockDB(str(tmp_path / "dry-run.db"), with_documents=False)
        record = _make_fy_segment()

        stats = dry_run_upsert_segments([record], db)

        output = capsys.readouterr().out
        assert stats.total_records == 1
        assert stats.inserted == 1
        assert record["period"] == "2024-03-31"
        assert record["segment_sales"] == 1200.0
        assert "Company: Unknown" in output
        assert db._conn.execute("SELECT COUNT(*) FROM segment_financials").fetchone()[0] == 0
        assert db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='earnings_summaries'"
        ).fetchone() is None
        db.close()

    def test_table_absent_apply_saves_valid_segment(self, tmp_path):
        db = MockDB(str(tmp_path / "apply.db"), with_documents=False)
        record = _make_fy_segment()

        stats = batch_upsert_segments([record], db)

        saved = db._conn.execute(
            "SELECT fiscal_year_end, quarter, segment_sales FROM segment_financials"
        ).fetchone()
        assert stats.inserted == 1
        assert stats.failed_batches == 0
        assert saved == ("2024-03-31", "FY", 1200.0)
        assert db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='earnings_summaries'"
        ).fetchone() is None
        db.close()

    def test_table_present_dry_run_keeps_fiscal_year_and_company_name(self, tmp_path, capsys):
        db = MockDB(str(tmp_path / "dry-run-summary.db"))
        _create_earnings_summaries(db)
        record = _make_1q_segment()

        stats = dry_run_upsert_segments([record], db)

        output = capsys.readouterr().out
        assert stats.inserted == 1
        assert record["period"] == "2024-03-31"
        assert "Company: Example Co." in output
        db.close()

    def test_table_present_apply_keeps_fiscal_year_enrichment(self, tmp_path):
        db = MockDB(str(tmp_path / "apply-summary.db"))
        _create_earnings_summaries(db)
        record = _make_1q_segment()

        stats = batch_upsert_segments([record], db)

        saved = db._conn.execute(
            "SELECT fiscal_year_end, quarter FROM segment_financials"
        ).fetchone()
        assert stats.inserted == 1
        assert record["period"] == "2024-03-31"
        assert saved == ("2024-03-31", "1Q")
        db.close()

    def test_present_but_malformed_summary_table_does_not_get_treated_as_absent(self, tmp_path):
        db = MockDB(str(tmp_path / "malformed-summary.db"))
        db._conn.execute("CREATE TABLE earnings_summaries (ticker TEXT)")
        db._conn.commit()

        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            dry_run_upsert_segments([_make_fy_segment()], db)
        db.close()

    def test_table_presence_is_checked_once_per_public_batch_call(self, tmp_path, monkeypatch):
        db = MockDB(str(tmp_path / "presence-count.db"))
        original = batch_upsert._has_earnings_summaries_table
        calls = []

        def count_calls(conn):
            calls.append(conn)
            return original(conn)

        monkeypatch.setattr(batch_upsert, "_has_earnings_summaries_table", count_calls)
        dry_run_upsert_segments([_make_fy_segment(), _make_fy_segment(segment_name="Other")], db)
        assert len(calls) == 1

        calls.clear()
        batch_upsert_segments([_make_fy_segment(), _make_fy_segment(segment_name="Other")], db, batch_size=1)
        assert len(calls) == 1
        db.close()

    def test_direct_shared_caller_uses_default_table_detection(self, tmp_path):
        db = MockDB(str(tmp_path / "shared-caller.db"), with_documents=False)
        record = _make_fy_segment()

        is_ok, reason, source = normalize_and_validate_rec(db._conn, record)

        assert is_ok is True
        assert reason == "ok"
        assert source == "fy_default"
        assert record["period"] == "2024-03-31"
        db.close()

    def test_non_fy_without_documents_is_rejected_without_stopping_apply(self, tmp_path):
        db = MockDB(str(tmp_path / "non-fy.db"), with_documents=False)
        rejected = _make_1q_segment()
        accepted = _make_fy_segment(segment_name="Accepted")

        stats = batch_upsert_segments([rejected, accepted], db)

        assert stats.inserted == 1
        assert rejected["period"] == "2023-06-30"
        assert db._conn.execute("SELECT COUNT(*) FROM segment_financials").fetchone()[0] == 1
        db.close()

    def test_7921_3q_without_documents_uses_safe_reason(self, tmp_path):
        db = MockDB(str(tmp_path / "7921.db"), with_documents=False)
        record = _make_1q_segment(ticker="7921", quarter="3Q", tdnet_doc_id="7921-doc")

        is_ok, reason, source = normalize_and_validate_rec(db._conn, record)

        assert is_ok is False
        assert reason == "7921_quarter_skew_unverifiable_without_documents"
        assert source == "none"
        db.close()
