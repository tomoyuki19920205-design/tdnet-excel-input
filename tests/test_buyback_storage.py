#!/usr/bin/env python3
"""test_buyback_storage.py — DB 保存ロジックのテスト"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import sqlite3
import tempfile

import pytest
from src.events.buyback_storage import (
    ensure_buyback_table,
    upsert_buyback_event,
)
from src.events.buyback_models import BuybackEvent, BUYBACK_DECISION
from src.events.buyback_extractor import compute_text_hash


@pytest.fixture
def db_conn():
    """インメモリ SQLite 接続"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_buyback_table(conn)
    yield conn
    conn.close()


def _make_event(**kwargs) -> BuybackEvent:
    """テスト用 BuybackEvent 生成"""
    defaults = {
        "ticker": "6750",
        "disclosure_date": "2025-04-01",
        "event_type": BUYBACK_DECISION,
        "title": "自己株式取得に係る事項の決定に関するお知らせ",
        "source_type": "html",
        "source_path": "/tmp/test.html",
        "raw_text_hash": compute_text_hash("test text"),
        "shares_limit": 3_000_000,
        "amount_limit_million_yen": 5000.0,
        "extraction_confidence": 0.85,
        "extractor_version": "1.0.0",
        "extracted_json": json.dumps({"test": True}, ensure_ascii=False),
    }
    defaults.update(kwargs)
    return BuybackEvent(**defaults)


# ============================================================
# テーブル作成
# ============================================================
class TestEnsureTable:
    def test_table_created(self, db_conn):
        row = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='buyback_events'"
        ).fetchone()
        assert row is not None

    def test_table_idempotent(self, db_conn):
        # 2回目の呼び出しでエラーにならない
        ensure_buyback_table(db_conn)
        row = db_conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='buyback_events'"
        ).fetchone()
        assert row[0] == 1


# ============================================================
# INSERT
# ============================================================
class TestInsert:
    def test_insert_returns_id(self, db_conn):
        event = _make_event()
        record_id = upsert_buyback_event(db_conn, event)
        assert record_id is not None
        assert record_id > 0

    def test_insert_values_persisted(self, db_conn):
        event = _make_event()
        record_id = upsert_buyback_event(db_conn, event)
        row = db_conn.execute(
            "SELECT * FROM buyback_events WHERE id = ?", (record_id,)
        ).fetchone()
        assert row["ticker"] == "6750"
        assert row["event_type"] == BUYBACK_DECISION
        assert row["shares_limit"] == 3_000_000
        assert row["amount_limit_million_yen"] == 5000.0
        assert row["extraction_confidence"] == 0.85

    def test_timestamps_set(self, db_conn):
        event = _make_event()
        record_id = upsert_buyback_event(db_conn, event)
        row = db_conn.execute(
            "SELECT created_at, updated_at FROM buyback_events WHERE id = ?",
            (record_id,),
        ).fetchone()
        assert row["created_at"] is not None
        assert row["updated_at"] is not None

    def test_extracted_json_persisted(self, db_conn):
        event = _make_event()
        record_id = upsert_buyback_event(db_conn, event)
        row = db_conn.execute(
            "SELECT extracted_json FROM buyback_events WHERE id = ?", (record_id,)
        ).fetchone()
        data = json.loads(row["extracted_json"])
        assert data["test"] is True


# ============================================================
# UPSERT / 重複回避
# ============================================================
class TestUpsert:
    def test_same_raw_text_hash_updates(self, db_conn):
        """同じ raw_text_hash で再投入すると UPDATE される"""
        event1 = _make_event(shares_limit=3_000_000)
        id1 = upsert_buyback_event(db_conn, event1)

        event2 = _make_event(shares_limit=4_000_000)
        id2 = upsert_buyback_event(db_conn, event2)

        assert id1 == id2  # 同じレコードが更新
        row = db_conn.execute(
            "SELECT shares_limit FROM buyback_events WHERE id = ?", (id1,)
        ).fetchone()
        assert row["shares_limit"] == 4_000_000

    def test_different_hash_inserts_new(self, db_conn):
        """異なる raw_text_hash は別レコードとして INSERT"""
        event1 = _make_event(raw_text_hash="hash_aaa")
        id1 = upsert_buyback_event(db_conn, event1)

        event2 = _make_event(raw_text_hash="hash_bbb")
        id2 = upsert_buyback_event(db_conn, event2)

        assert id1 != id2
        count = db_conn.execute("SELECT count(*) FROM buyback_events").fetchone()[0]
        assert count == 2

    def test_source_doc_id_match_updates(self, db_conn):
        """source_doc_id が一致すれば UPDATE"""
        event1 = _make_event(source_doc_id="DOC001", raw_text_hash="hash_1")
        id1 = upsert_buyback_event(db_conn, event1)

        event2 = _make_event(
            source_doc_id="DOC001",
            raw_text_hash="hash_2",  # hash は異なるが doc_id 一致
            shares_limit=5_000_000,
        )
        id2 = upsert_buyback_event(db_conn, event2)

        assert id1 == id2
        row = db_conn.execute(
            "SELECT shares_limit FROM buyback_events WHERE id = ?", (id1,)
        ).fetchone()
        assert row["shares_limit"] == 5_000_000

    def test_count_after_multiple_upserts(self, db_conn):
        """同じイベントの3回 upsert でレコードは1件のまま"""
        for _ in range(3):
            upsert_buyback_event(db_conn, _make_event())
        count = db_conn.execute("SELECT count(*) FROM buyback_events").fetchone()[0]
        assert count == 1


# ============================================================
# no-save のシミュレーション
# ============================================================
class TestNoSave:
    def test_no_save_does_not_insert(self):
        """DB 未接続でも BuybackEvent は生成される（保存しないフロー）"""
        event = _make_event()
        assert event.ticker == "6750"
        assert event.shares_limit == 3_000_000
        # no-save: DB 操作なし
        # event.to_json() が正常に動くこと
        j = json.loads(event.to_json())
        assert j["ticker"] == "6750"
