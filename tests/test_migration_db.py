# ============================================================
# test_migration_db.py — DB基盤のユニットテスト（Phase2拡張版）
# ============================================================
from __future__ import annotations

import os
import tempfile

import pytest

from src.migration.migration_db import MigrationDB


@pytest.fixture
def db():
    """テスト用一時DBを作成

    persist_policy 依存:
      insert_log() / quarantine_record() は should_persist_intermediates() ガード付き。
      テスト環境ではデフォルト OFF のため、明示的に ON にする必要がある。
    """
    from src.persist_policy import init_persist_policy, reset_persist_policy
    init_persist_policy(cli_flag=True)
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    _db = MigrationDB(path)
    yield _db
    _db.close()
    os.unlink(path)
    reset_persist_policy()


class TestTableCreation:
    def test_tables_exist(self, db: MigrationDB):
        """6テーブルが正しく作成されているか"""
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cur.fetchall()}
        expected = {
            "quarterly_results",
            "company_memos",
            "quarterly_notes",
            "segment_financials",
            "migration_log",
            "audit_log",
        }
        assert expected.issubset(tables)


class TestQuarterlyResults:
    def test_upsert_insert(self, db: MigrationDB):
        result = db.upsert_quarterly_result(
            "7203", "2026-03-31", "1Q",
            sales=100, gross_profit=30, gross_margin=0.30,
            sga=10, operating_profit=20,
        )
        db.commit()
        assert result == "inserted"
        r = db.get_quarterly_result("7203", "2026-03-31", "1Q")
        assert r is not None
        assert r["sales"] == 100
        assert r["operating_profit"] == 20

    def test_upsert_update(self, db: MigrationDB):
        """同じキーでUPSERT → 上書き"""
        db.upsert_quarterly_result("7203", "2026-03-31", "1Q", sales=100)
        result = db.upsert_quarterly_result("7203", "2026-03-31", "1Q", sales=200)
        db.commit()
        assert result == "updated"
        r = db.get_quarterly_result("7203", "2026-03-31", "1Q")
        assert r["sales"] == 200

    def test_upsert_no_change(self, db: MigrationDB):
        """同じ値で再upsert → no_change"""
        db.upsert_quarterly_result("7203", "2026-03-31", "1Q", sales=100)
        result = db.upsert_quarterly_result("7203", "2026-03-31", "1Q", sales=100)
        assert result == "no_change"


class TestCompanyMemos:
    def test_upsert(self, db: MigrationDB):
        db.upsert_company_memo("7203", col_c="トヨタ", col_d="自動車")
        db.commit()
        m = db.get_company_memo("7203")
        assert m is not None
        assert m["col_c"] == "トヨタ"
        assert m["col_d"] == "自動車"
        assert m["col_e"] is None

    def test_overwrite(self, db: MigrationDB):
        """上書き保存（履歴なし）"""
        db.upsert_company_memo("7203", col_c="旧メモ")
        db.upsert_company_memo("7203", col_c="新メモ")
        db.commit()
        m = db.get_company_memo("7203")
        assert m["col_c"] == "新メモ"


class TestQuarterlyNotes:
    def test_history(self, db: MigrationDB):
        """履歴型: 追記保存 & 最新取得"""
        db.insert_quarterly_note("7203", "2026-03-31", "1Q", "メモ1")
        db.insert_quarterly_note("7203", "2026-03-31", "1Q", "メモ2")
        db.insert_quarterly_note("7203", "2026-03-31", "1Q", "メモ3")
        db.commit()

        latest = db.get_latest_note("7203", "2026-03-31", "1Q")
        assert latest == "メモ3"

    def test_empty_note_skipped(self, db: MigrationDB):
        """空メモは保存しない"""
        db.insert_quarterly_note("7203", "2026-03-31", "1Q", None)
        db.insert_quarterly_note("7203", "2026-03-31", "1Q", "")
        db.commit()
        latest = db.get_latest_note("7203", "2026-03-31", "1Q")
        assert latest is None


class TestSegmentFinancials:
    def test_upsert(self, db: MigrationDB):
        db.upsert_segment(
            "7203", "2026-03-31", "1Q",
            segment_name="自動車",
            segment_order=0,
            segment_sales=80, segment_profit=15,
        )
        db.commit()
        segs = db.get_segments("7203", "2026-03-31", "1Q")
        assert len(segs) == 1
        assert segs[0]["segment_name"] == "自動車"
        assert segs[0]["segment_order"] == 0
        assert segs[0]["segment_sales"] == 80

    def test_order_preserved(self, db: MigrationDB):
        """segment_orderで並び順が維持される"""
        db.upsert_segment("7203", "2026-03-31", "1Q", "金融", 1, 20, 5)
        db.upsert_segment("7203", "2026-03-31", "1Q", "自動車", 0, 80, 15)
        db.commit()
        segs = db.get_segments("7203", "2026-03-31", "1Q")
        assert segs[0]["segment_name"] == "自動車"
        assert segs[1]["segment_name"] == "金融"

    def test_segment_no_change(self, db: MigrationDB):
        """同じ値で再upsert → no_change"""
        db.upsert_segment("7203", "2026-03-31", "1Q", "自動車", 0, 80, 15)
        result = db.upsert_segment("7203", "2026-03-31", "1Q", "自動車", 0, 80, 15)
        assert result == "no_change"


class TestSegmentProvenanceAwareUpsert:
    @staticmethod
    def _values(**overrides):
        values = {
            "company_code": "4057",
            "fiscal_year_end": "2026-05-31",
            "quarter": "FY",
            "segment_name": "EC支援",
            "segment_order": 1,
            "segment_sales": 100.0,
            "segment_profit": 20.0,
            "raw_profit_label": "営業利益",
            "data_source": "backfill_xbrl",
            "segment_name_norm": "ec支援",
            "extractor_route": "xbrl",
            "source_doc_type": "earnings_summary",
            "disclosure_date": "2026-07-13",
            "tdnet_doc_id": "20260713340570",
            "row_type": "business",
        }
        values.update(overrides)
        return values

    @staticmethod
    def _call(db: MigrationDB, **overrides):
        return db.upsert_segment_provenance_aware(
            **TestSegmentProvenanceAwareUpsert._values(**overrides)
        )

    def _insert_legacy(self, db: MigrationDB, **overrides):
        values = self._values(
            segment_sales=80.0,
            segment_profit=15.0,
            raw_profit_label="",
            data_source="excel_legacy",
            segment_name_norm=None,
            extractor_route=None,
            source_doc_type=None,
            disclosure_date=None,
            tdnet_doc_id=None,
            row_type=None,
        )
        values.update(overrides)
        db.upsert_segment(**values)
        db.commit()
        return db.get_segment_id(
            company_code=values["company_code"],
            fiscal_year_end=values["fiscal_year_end"],
            quarter=values["quarter"],
            segment_name=values["segment_name"],
        )

    def test_promotes_legacy_values_and_provenance_without_changing_row_id(self, db):
        row_id = self._insert_legacy(db)
        result = self._call(db)
        saved = db._conn.execute(
            "SELECT id, segment_sales, segment_profit, data_source, tdnet_doc_id, extractor_route "
            "FROM segment_financials"
        ).fetchone()

        assert result.status == "updated"
        assert result.accepted is True
        assert result.row_id == row_id
        assert saved == (row_id, 100.0, 20.0, "backfill_xbrl", "20260713340570", "xbrl")
        assert db._conn.execute("SELECT COUNT(*) FROM segment_financials").fetchone()[0] == 1

    def test_promotes_provenance_when_numbers_are_identical(self, db):
        self._insert_legacy(db, segment_sales=100.0, segment_profit=20.0)
        result = self._call(db)

        assert result.status == "updated"
        assert db._conn.execute(
            "SELECT data_source, tdnet_doc_id FROM segment_financials"
        ).fetchone() == ("backfill_xbrl", "20260713340570")

    def test_identical_retry_is_no_change_and_preserves_updated_at(self, db):
        first = self._call(db)
        before = db._conn.execute(
            "SELECT updated_at FROM segment_financials WHERE id=?", (first.row_id,)
        ).fetchone()[0]
        changes_before = db._conn.total_changes

        second = self._call(db)

        assert second.status == "no_change"
        assert second.row_id == first.row_id
        assert db._conn.total_changes == changes_before
        assert db._conn.execute(
            "SELECT updated_at FROM segment_financials WHERE id=?", (first.row_id,)
        ).fetchone()[0] == before

    def test_rejects_lower_priority_without_mutation(self, db):
        first = self._call(db, data_source="xbrl")
        before = db._conn.execute(
            "SELECT segment_sales, data_source, tdnet_doc_id FROM segment_financials WHERE id=?",
            (first.row_id,),
        ).fetchone()

        result = self._call(db, data_source="pdf", segment_sales=999.0)

        assert result.status == "rejected_lower_priority"
        assert result.accepted is False
        assert db._conn.execute(
            "SELECT segment_sales, data_source, tdnet_doc_id FROM segment_financials WHERE id=?",
            (first.row_id,),
        ).fetchone() == before

    def test_same_priority_same_filing_allows_update(self, db):
        first = self._call(db, data_source="backfill_xbrl")
        result = self._call(db, data_source="backfill_xbrl", segment_sales=130.0)

        assert result.status == "updated"
        assert result.row_id == first.row_id
        assert db._conn.execute(
            "SELECT segment_sales, data_source FROM segment_financials WHERE id=?", (first.row_id,)
        ).fetchone() == (130.0, "backfill_xbrl")

    def test_same_priority_different_filing_is_rejected(self, db):
        first = self._call(db)
        result = self._call(db, tdnet_doc_id="different-filing", segment_sales=130.0)

        assert result.status == "rejected_filing_conflict"
        assert result.reason == "segment_natural_key_filing_conflict"
        assert db._conn.execute(
            "SELECT segment_sales, tdnet_doc_id FROM segment_financials WHERE id=?", (first.row_id,)
        ).fetchone() == (100.0, "20260713340570")

    def test_both_filing_ids_empty_with_difference_is_rejected(self, db):
        self._insert_legacy(db)
        result = self._call(db, tdnet_doc_id=None)

        assert result.status == "rejected_filing_identity_unresolved"
        assert result.accepted is False

    def test_empty_existing_filing_is_promoted_when_priority_allows(self, db):
        row_id = self._insert_legacy(db)
        result = self._call(db)

        assert result.status == "updated"
        assert result.row_id == row_id
        assert db._conn.execute(
            "SELECT tdnet_doc_id FROM segment_financials WHERE id=?", (row_id,)
        ).fetchone()[0] == "20260713340570"

    def test_sql_failure_rolls_back_numbers_and_provenance_together(self, db):
        row_id = self._insert_legacy(db)
        before = db._conn.execute(
            "SELECT segment_sales, data_source, tdnet_doc_id FROM segment_financials WHERE id=?",
            (row_id,),
        ).fetchone()
        db._conn.execute(
            "CREATE TRIGGER reject_segment_update BEFORE UPDATE ON segment_financials "
            "BEGIN SELECT RAISE(ABORT, 'simulated update failure'); END"
        )

        with pytest.raises(Exception, match="simulated update failure"):
            self._call(db)
        db._conn.rollback()

        assert db._conn.execute(
            "SELECT segment_sales, data_source, tdnet_doc_id FROM segment_financials WHERE id=?",
            (row_id,),
        ).fetchone() == before

    def test_readback_matches_all_saved_fields(self, db):
        result = self._call(db)
        saved = db._conn.execute(
            "SELECT segment_order, segment_sales, segment_profit, raw_profit_label, data_source, "
            "segment_name_norm, extractor_route, source_doc_type, disclosure_date, tdnet_doc_id, row_type "
            "FROM segment_financials WHERE id=?",
            (result.row_id,),
        ).fetchone()

        assert saved == (
            1, 100.0, 20.0, "営業利益", "backfill_xbrl", "ec支援", "xbrl",
            "earnings_summary", "2026-07-13", "20260713340570", "business",
        )


class TestMigrationLog:
    def test_insert_and_get(self, db: MigrationDB):
        db.insert_log(
            run_id="test-run-001",
            log_level="SKIP",
            log_type="SKIP_DISTANCE",
            message="150行超過",
            company_code="7203",
            row_start=1, row_end=151,
        )
        db.commit()
        logs = db.get_logs_by_run("test-run-001")
        assert len(logs) == 1
        assert logs[0]["log_level"] == "SKIP"
        assert logs[0]["log_type"] == "SKIP_DISTANCE"
        assert logs[0]["company_code"] == "7203"


# ==============================================================
# Phase2: audit_log テスト
# ==============================================================
class TestAuditLog:
    def test_no_audit_for_migration_source(self, db: MigrationDB):
        """source='migration' → audit_logに記録されない"""
        db.upsert_quarterly_result(
            "7203", "2026-03-31", "1Q", sales=100,
            source="migration", actor="migration",
        )
        db.commit()
        logs = db.get_audit_log(company_code="7203")
        assert len(logs) == 0

    def test_audit_on_tdnet_insert(self, db: MigrationDB):
        """source='tdnet' INSERT → audit_logに記録"""
        db.upsert_quarterly_result(
            "7203", "2026-03-31", "1Q",
            sales=100, operating_profit=20,
            source="tdnet", actor="tdnet",
            tdnet_disclosure_id="disc-001", run_id="run-001",
        )
        db.commit()
        logs = db.get_audit_log(company_code="7203")
        assert len(logs) == 2  # sales + operating_profit
        fields = {l["field_name"] for l in logs}
        assert fields == {"sales", "operating_profit"}
        assert logs[0]["source"] == "tdnet"
        assert logs[0]["old_value"] is None
        assert logs[0]["tdnet_disclosure_id"] == "disc-001"

    def test_audit_on_tdnet_update(self, db: MigrationDB):
        """source='migration'でINSERT → source='tdnet'でUPDATE → 差分のみaudit"""
        db.upsert_quarterly_result(
            "7203", "2026-03-31", "1Q",
            sales=100, operating_profit=20,
        )
        db.commit()
        # TDnet更新: salesのみ変更
        db.upsert_quarterly_result(
            "7203", "2026-03-31", "1Q",
            sales=150, operating_profit=20,  # opは同じ
            source="tdnet", actor="tdnet",
            run_id="run-002",
        )
        db.commit()
        logs = db.get_audit_log(run_id="run-002")
        assert len(logs) == 1  # salesのみ変更
        assert logs[0]["field_name"] == "sales"
        assert logs[0]["old_value"] == "100.0"
        assert logs[0]["new_value"] == "150"

    def test_audit_on_manual_update(self, db: MigrationDB):
        """source='manual' → audit_logに記録"""
        db.upsert_quarterly_result(
            "7203", "2026-03-31", "1Q", sales=100,
        )
        db.upsert_quarterly_result(
            "7203", "2026-03-31", "1Q", sales=200,
            source="manual", actor="takuya",
        )
        db.commit()
        logs = db.get_audit_log(company_code="7203")
        assert len(logs) == 1
        assert logs[0]["actor"] == "takuya"
        assert logs[0]["source"] == "manual"

    def test_no_audit_on_no_change(self, db: MigrationDB):
        """変更なし → audit_logなし"""
        db.upsert_quarterly_result(
            "7203", "2026-03-31", "1Q", sales=100,
            source="tdnet", actor="tdnet",
        )
        db.commit()
        # 同じ値で再更新
        db.upsert_quarterly_result(
            "7203", "2026-03-31", "1Q", sales=100,
            source="tdnet", actor="tdnet",
            run_id="run-dup",
        )
        db.commit()
        logs = db.get_audit_log(run_id="run-dup")
        assert len(logs) == 0

    def test_audit_note_manual(self, db: MigrationDB):
        """メモ追記にaudit_log"""
        db.insert_quarterly_note(
            "7203", "2026-03-31", "1Q", "好調",
            source="manual", actor="takuya",
        )
        db.commit()
        logs = db.get_audit_log(company_code="7203")
        assert len(logs) == 1
        assert logs[0]["entity_type"] == "note"
        assert logs[0]["new_value"] == "好調"

    def test_audit_segment_tdnet(self, db: MigrationDB):
        """セグメント更新にaudit_log"""
        db.upsert_segment(
            "7203", "2026-03-31", "1Q", "自動車", 0, 80, 15,
            source="tdnet", actor="tdnet",
        )
        db.commit()
        logs = db.get_audit_log(company_code="7203")
        assert len(logs) == 2  # sales + profit
        fields = {l["field_name"] for l in logs}
        assert "自動車.segment_sales" in fields
        assert "自動車.segment_profit" in fields
