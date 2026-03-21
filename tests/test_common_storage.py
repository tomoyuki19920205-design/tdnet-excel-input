#!/usr/bin/env python3
"""test_common_storage.py — events テーブルのテスト"""
import sqlite3
import unittest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.events.common_models import EventRecord
from src.events.common_storage import ensure_events_table, upsert_event, get_unnotified_events, mark_notified


class TestEventsTable(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ensure_events_table(self.conn)

    def tearDown(self):
        self.conn.close()

    def _make_event(self, **overrides) -> EventRecord:
        defaults = {
            "source_doc_id": "doc123",
            "ticker": "7203",
            "company_name": "トヨタ自動車",
            "title": "自社株買いのお知らせ",
            "event_type": "buyback",
            "subtype": "resolution",
            "fingerprint": "fp_unique_001",
        }
        defaults.update(overrides)
        return EventRecord(**defaults)

    def test_insert_new_event(self):
        ev = self._make_event()
        action, eid = upsert_event(self.conn, ev)
        self.assertEqual(action, "inserted")
        self.assertTrue(len(eid) > 0)

    def test_duplicate_fingerprint_no_change(self):
        ev1 = self._make_event(extracted_payload_json='{"foo": 1}')
        action1, eid1 = upsert_event(self.conn, ev1)
        self.assertEqual(action1, "inserted")

        ev2 = self._make_event(extracted_payload_json='{"foo": 1}')
        action2, eid2 = upsert_event(self.conn, ev2)
        self.assertEqual(action2, "no_change")
        self.assertEqual(eid1, eid2)

    def test_duplicate_fingerprint_update(self):
        ev1 = self._make_event(extracted_payload_json='{"foo": 1}')
        action1, eid1 = upsert_event(self.conn, ev1)
        self.assertEqual(action1, "inserted")

        ev2 = self._make_event(extracted_payload_json='{"foo": 2}')
        action2, eid2 = upsert_event(self.conn, ev2)
        self.assertEqual(action2, "updated")

    def test_different_fingerprint_inserts_new(self):
        ev1 = self._make_event(fingerprint="fp_001")
        ev2 = self._make_event(fingerprint="fp_002")
        action1, _ = upsert_event(self.conn, ev1)
        action2, _ = upsert_event(self.conn, ev2)
        self.assertEqual(action1, "inserted")
        self.assertEqual(action2, "inserted")

    def test_get_unnotified_events(self):
        ev = self._make_event()
        upsert_event(self.conn, ev)
        unnotified = get_unnotified_events(self.conn)
        self.assertEqual(len(unnotified), 1)
        self.assertEqual(unnotified[0].ticker, "7203")

    def test_mark_notified(self):
        ev = self._make_event()
        _, eid = upsert_event(self.conn, ev)
        mark_notified(self.conn, eid)
        unnotified = get_unnotified_events(self.conn)
        self.assertEqual(len(unnotified), 0)


if __name__ == "__main__":
    unittest.main()
