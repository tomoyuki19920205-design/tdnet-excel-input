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

from lib.backfill.batch_upsert import batch_upsert_segments, BatchUpsertStats


class MockDB:
    """MigrationDB の upsert_segment を模倣する最小 mock。"""

    def __init__(self, db_path: str):
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
            "quarter": "4Q",
            "segment_name": f"Seg{i}",
            "segment_order": i,
            "segment_sales": 100000 * i,
            "segment_profit": 10000 * i,
        }
        for i in range(count)
    ]


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
