#!/usr/bin/env python3
"""test_persist_policy.py — persist_policy と中間データ非永続化のテスト"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from src.persist_policy import (
    resolve_persist_intermediates,
    should_persist_intermediates,
    init_persist_policy,
    reset_persist_policy,
)


# ============================================================
# resolve_persist_intermediates
# ============================================================
class TestResolvePersistIntermediates:
    def test_default_off(self):
        """デフォルトは OFF"""
        assert resolve_persist_intermediates(cli_flag=None, env={}) is False

    def test_env_on(self):
        """環境変数 TDNET_PERSIST_INTERMEDIATES=1 で ON"""
        assert resolve_persist_intermediates(
            cli_flag=None, env={"TDNET_PERSIST_INTERMEDIATES": "1"}
        ) is True

    def test_env_true(self):
        assert resolve_persist_intermediates(
            cli_flag=None, env={"TDNET_PERSIST_INTERMEDIATES": "true"}
        ) is True

    def test_env_yes(self):
        assert resolve_persist_intermediates(
            cli_flag=None, env={"TDNET_PERSIST_INTERMEDIATES": "yes"}
        ) is True

    def test_env_off(self):
        assert resolve_persist_intermediates(
            cli_flag=None, env={"TDNET_PERSIST_INTERMEDIATES": "0"}
        ) is False

    def test_env_empty(self):
        assert resolve_persist_intermediates(
            cli_flag=None, env={"TDNET_PERSIST_INTERMEDIATES": ""}
        ) is False

    def test_cli_flag_true(self):
        """CLI フラグ True で ON"""
        assert resolve_persist_intermediates(cli_flag=True, env={}) is True

    def test_cli_flag_false(self):
        """CLI フラグ False で OFF"""
        assert resolve_persist_intermediates(cli_flag=False, env={}) is False

    def test_cli_overrides_env(self):
        """CLI フラグが環境変数より優先"""
        assert resolve_persist_intermediates(
            cli_flag=False, env={"TDNET_PERSIST_INTERMEDIATES": "1"}
        ) is False
        assert resolve_persist_intermediates(
            cli_flag=True, env={"TDNET_PERSIST_INTERMEDIATES": "0"}
        ) is True


# ============================================================
# should_persist_intermediates (global)
# ============================================================
class TestShouldPersistIntermediates:
    def setup_method(self):
        reset_persist_policy()

    def teardown_method(self):
        reset_persist_policy()

    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("TDNET_PERSIST_INTERMEDIATES", raising=False)
        assert should_persist_intermediates() is False

    def test_init_on(self):
        init_persist_policy(cli_flag=True)
        assert should_persist_intermediates() is True

    def test_init_off(self):
        init_persist_policy(cli_flag=False)
        assert should_persist_intermediates() is False


# ============================================================
# migration_db — policy OFF で write されない
# ============================================================
class TestMigrationDBPolicyOff:
    def setup_method(self):
        reset_persist_policy()
        init_persist_policy(cli_flag=False)

    def teardown_method(self):
        reset_persist_policy()

    def test_insert_log_noop(self, tmp_path):
        """policy OFF で migration_log に行が増えない"""
        from src.migration.migration_db import MigrationDB
        db = MigrationDB(str(tmp_path / "test.db"))

        before = db._conn.execute("SELECT COUNT(*) FROM migration_log").fetchone()[0]
        db.insert_log("run1", "INFO", "test", "message")
        db.commit()
        after = db._conn.execute("SELECT COUNT(*) FROM migration_log").fetchone()[0]
        assert after == before
        db.close()

    def test_quarantine_record_noop(self, tmp_path):
        """policy OFF で quarantine に行が増えない"""
        from src.migration.migration_db import MigrationDB
        db = MigrationDB(str(tmp_path / "test.db"))

        before = db._conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]
        db.quarantine_record("1234", "test reason")
        db.commit()
        after = db._conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]
        assert after == before
        db.close()


# ============================================================
# migration_db — policy ON で write される
# ============================================================
class TestMigrationDBPolicyOn:
    def setup_method(self):
        reset_persist_policy()
        init_persist_policy(cli_flag=True)

    def teardown_method(self):
        reset_persist_policy()

    def test_insert_log_writes(self, tmp_path):
        """policy ON で migration_log に書き込まれる"""
        from src.migration.migration_db import MigrationDB
        db = MigrationDB(str(tmp_path / "test.db"))

        db.insert_log("run1", "INFO", "test", "message")
        db.commit()
        count = db._conn.execute("SELECT COUNT(*) FROM migration_log").fetchone()[0]
        assert count == 1
        db.close()

    def test_quarantine_record_writes(self, tmp_path):
        """policy ON で quarantine に書き込まれる"""
        from src.migration.migration_db import MigrationDB
        db = MigrationDB(str(tmp_path / "test.db"))

        db.quarantine_record("1234", "test reason")
        db.commit()
        count = db._conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]
        assert count == 1
        db.close()


# ============================================================
# ir_doc_schema — policy OFF で write されない
# ============================================================
class TestIrDocSchemaPolicyOff:
    def setup_method(self):
        reset_persist_policy()
        init_persist_policy(cli_flag=False)

    def teardown_method(self):
        reset_persist_policy()

    def test_insert_facts_noop(self, tmp_path):
        """policy OFF で extracted_facts に行が増えない"""
        from src.extraction.ir_doc_schema import ensure_tables, insert_facts
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        ensure_tables(conn)

        facts = [{"ticker": "1234", "metric_name": "sales", "metric_value": 100,
                   "source_type": "test"}]
        inserted = insert_facts(conn, facts)
        assert inserted == 0
        count = conn.execute("SELECT COUNT(*) FROM extracted_facts").fetchone()[0]
        assert count == 0
        conn.close()


# ============================================================
# ir_doc_schema — policy ON で write される
# ============================================================
class TestIrDocSchemaPolicyOn:
    def setup_method(self):
        reset_persist_policy()
        init_persist_policy(cli_flag=True)

    def teardown_method(self):
        reset_persist_policy()

    def test_insert_facts_writes(self, tmp_path):
        """policy ON で extracted_facts に書き込まれる"""
        from src.extraction.ir_doc_schema import ensure_tables, insert_facts, insert_document
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        ensure_tables(conn)

        doc_id = insert_document(conn, "1234", "2026-01-01", "test", "tanshin", "pdf", url="u1")
        facts = [{"document_id": doc_id, "ticker": "1234", "metric_name": "sales",
                   "metric_value": 100, "source_type": "test"}]
        inserted = insert_facts(conn, facts)
        assert inserted == 1
        count = conn.execute("SELECT COUNT(*) FROM extracted_facts").fetchone()[0]
        assert count == 1
        conn.close()


# ============================================================
# cleanup_intermediate_data — dry-run / execute
# ============================================================
class TestCleanupTool:
    def _prepare_db(self, path: str) -> str:
        """テスト用 DB を作成し中間データを投入する。"""
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE IF NOT EXISTS migration_log (
            id INTEGER PRIMARY KEY, message TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS quarantine (
            id INTEGER PRIMARY KEY, reason TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS extracted_facts (
            id INTEGER PRIMARY KEY, metric TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY, info TEXT)""")
        for i in range(10):
            conn.execute("INSERT INTO migration_log (message) VALUES (?)", (f"log{i}",))
            conn.execute("INSERT INTO quarantine (reason) VALUES (?)", (f"q{i}",))
            conn.execute("INSERT INTO extracted_facts (metric) VALUES (?)", (f"m{i}",))
            conn.execute("INSERT INTO audit_log (info) VALUES (?)", (f"a{i}",))
        conn.commit()
        conn.close()
        return path

    def test_dryrun_no_delete(self, tmp_path):
        """dry-run では削除しない"""
        from tools.cleanup_intermediate_data import cleanup_db
        db_path = self._prepare_db(str(tmp_path / "test.db"))
        result = cleanup_db(db_path, ["migration_log", "quarantine"], execute=False)
        assert result["migration_log"]["before"] == 10
        assert result["migration_log"]["after"] == 10  # 削除されていない

    def test_execute_deletes(self, tmp_path):
        """--execute で削除される"""
        from tools.cleanup_intermediate_data import cleanup_db
        db_path = self._prepare_db(str(tmp_path / "test.db"))
        result = cleanup_db(db_path, ["migration_log", "quarantine", "extracted_facts"],
                            execute=True)
        assert result["migration_log"]["after"] == 0
        assert result["quarantine"]["after"] == 0
        assert result["extracted_facts"]["after"] == 0

    def test_vacuum(self, tmp_path):
        """--vacuum で VACUUM が走る"""
        from tools.cleanup_intermediate_data import cleanup_db
        db_path = self._prepare_db(str(tmp_path / "test.db"))
        result = cleanup_db(db_path, ["migration_log"], execute=True, vacuum=True)
        assert result["migration_log"]["after"] == 0
        # VACUUM 後もファイルは存在
        assert os.path.exists(db_path)

    def test_audit_log_not_deleted_by_default(self, tmp_path):
        """audit_log はデフォルトで削除しない"""
        from tools.cleanup_intermediate_data import cleanup_db, _DEFAULT_TARGETS
        db_path = self._prepare_db(str(tmp_path / "test.db"))
        result = cleanup_db(db_path, _DEFAULT_TARGETS, execute=True)
        # audit_log は対象外
        assert "audit_log" not in result

    def test_audit_log_deleted_when_included(self, tmp_path):
        """--include-audit-log で audit_log も削除"""
        from tools.cleanup_intermediate_data import cleanup_db, _DEFAULT_TARGETS
        db_path = self._prepare_db(str(tmp_path / "test.db"))
        targets = _DEFAULT_TARGETS + ["audit_log"]
        result = cleanup_db(db_path, targets, execute=True)
        assert result["audit_log"]["after"] == 0

    def test_nonexistent_table(self, tmp_path):
        """存在しないテーブルは skip"""
        from tools.cleanup_intermediate_data import cleanup_db
        db_path = str(tmp_path / "empty.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE dummy (id INTEGER)")
        conn.commit()
        conn.close()
        result = cleanup_db(db_path, ["nonexistent_table"], execute=True)
        assert result["nonexistent_table"]["exists"] is False


# ============================================================
# 再実行非増殖テスト
# ============================================================
class TestNonAccumulation:
    """同一資料を2回実行しても中間テーブル件数が増えないことを確認。"""

    def setup_method(self):
        reset_persist_policy()
        init_persist_policy(cli_flag=False)  # 通常モード

    def teardown_method(self):
        reset_persist_policy()

    def test_double_run_no_growth(self, tmp_path):
        """通常モードで2回実行しても中間テーブルが増えない"""
        from src.migration.migration_db import MigrationDB
        db = MigrationDB(str(tmp_path / "test.db"))

        # 1回目
        db.quarantine_record("1234", "reason1")
        db.insert_log("run1", "INFO", "test", "message1")
        db.commit()
        q1 = db._conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]
        m1 = db._conn.execute("SELECT COUNT(*) FROM migration_log").fetchone()[0]

        # 2回目
        db.quarantine_record("1234", "reason2")
        db.insert_log("run1", "INFO", "test", "message2")
        db.commit()
        q2 = db._conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]
        m2 = db._conn.execute("SELECT COUNT(*) FROM migration_log").fetchone()[0]

        assert q1 == q2 == 0
        assert m1 == m2 == 0
        db.close()
