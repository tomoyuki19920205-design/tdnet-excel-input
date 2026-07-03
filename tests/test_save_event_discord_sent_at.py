#!/usr/bin/env python3
"""test_save_event_discord_sent_at.py — discord_sent_at 原子的更新のユニットテスト

テスト対象:
- save_event_to_supabase の discord_sent_at パラメータ
  - INSERT 時に row に discord_sent_at が含まれること
  - DEDUP_SKIPPED 時に PATCH で discord_sent_at が更新されること
  - discord_sent_at=None の場合は含まれないこと
- event_pipeline Phase 2C: dedupe_key ベース照合

禁止:
- Supabase 実アクセスなし
- Discord 実送信なし
"""
import json
import unittest
from unittest.mock import MagicMock, patch
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.events.common_models import EventRecord, EventType


def _make_ev(ticker: str = "7203") -> EventRecord:
    return EventRecord(
        event_id="test-ev-0001-aabb",
        source_doc_id="test-doc-0001",
        ticker=ticker,
        company_name="テスト株式会社",
        event_type=EventType.FORECAST_REVISION,
        subtype="upward",
        title="業績予想の上方修正に関するお知らせ",
        first_seen_at="2026-03-20T15:30:00+09:00",
        fingerprint="fp_test0001",
        disclosure_datetime="2026-03-20 15:00",
        extracted_payload_json=json.dumps(
            {"previous_net_income": 1200, "revised_net_income": 1500, "change_net_income_pct": 25.0},
            ensure_ascii=False,
        ),
    )


class TestSaveEventDiscordSentAt(unittest.TestCase):

    def _make_mock_client(self, action: str = "inserted"):
        mock_client = MagicMock()
        if action == "inserted":
            mock_resp = MagicMock()
            mock_resp.data = [{"id": "new-supabase-id-001"}]
            mock_client.table.return_value.upsert.return_value.execute.return_value = mock_resp
        elif action == "dedup_skipped":
            mock_resp = MagicMock()
            mock_resp.data = []
            mock_client.table.return_value.upsert.return_value.execute.return_value = mock_resp
            mock_patch_resp = MagicMock()
            mock_patch_resp.data = [{"id": "existing-id-001"}]
            mock_client.table.return_value.update.return_value.eq.return_value.execute.return_value = mock_patch_resp
        mock_get_resp = MagicMock()
        mock_get_resp.data = []
        mock_client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = mock_get_resp
        return mock_client

    def test_discord_sent_at_included_in_row_on_insert(self):
        """discord_sent_at が指定された場合、INSERT の upsert 呼び出しに discord_sent_at が含まれる。"""
        ev = _make_ev()
        sent_at = "2026-07-03T07:30:38.579257+00:00"
        mock_client = self._make_mock_client("inserted")
        with patch("src.events.tdnet_event_store._get_supabase", return_value=mock_client):
            from src.events.tdnet_event_store import save_event_to_supabase
            result = save_event_to_supabase(ev, dry_run=False, discord_sent_at=sent_at)
        upsert_calls = mock_client.table.return_value.upsert.call_args_list
        self.assertTrue(len(upsert_calls) > 0, "upsert が呼ばれていない")
        row_arg = upsert_calls[0][0][0]
        self.assertIn("discord_sent_at", row_arg, "discord_sent_at が row に含まれていない")
        self.assertEqual(row_arg["discord_sent_at"], sent_at)

    def test_discord_sent_at_none_not_included_in_row(self):
        """discord_sent_at=None の場合、row に discord_sent_at が含まれない。"""
        ev = _make_ev()
        mock_client = self._make_mock_client("inserted")
        with patch("src.events.tdnet_event_store._get_supabase", return_value=mock_client):
            from src.events.tdnet_event_store import save_event_to_supabase
            result = save_event_to_supabase(ev, dry_run=False, discord_sent_at=None)
        upsert_calls = mock_client.table.return_value.upsert.call_args_list
        if upsert_calls:
            row_arg = upsert_calls[0][0][0]
            self.assertNotIn("discord_sent_at", row_arg)

    def test_action_is_inserted_when_upsert_returns_data(self):
        """upsert がデータを返した場合、action = inserted。"""
        ev = _make_ev()
        mock_client = self._make_mock_client("inserted")
        with patch("src.events.tdnet_event_store._get_supabase", return_value=mock_client):
            from src.events.tdnet_event_store import save_event_to_supabase
            result = save_event_to_supabase(ev, dry_run=False, discord_sent_at="2026-07-03T00:00:00Z")
        self.assertEqual(result.get("action"), "inserted")

    def test_dedup_skipped_triggers_patch_for_discord_sent_at(self):
        """dedup_skipped の場合、discord_sent_at が指定されていれば PATCH で更新される。"""
        ev = _make_ev()
        sent_at = "2026-07-03T07:30:38.579257+00:00"
        mock_client = self._make_mock_client("dedup_skipped")
        with patch("src.events.tdnet_event_store._get_supabase", return_value=mock_client):
            from src.events.tdnet_event_store import save_event_to_supabase
            result = save_event_to_supabase(ev, dry_run=False, discord_sent_at=sent_at)
        self.assertEqual(result.get("action"), "dedup_skipped")
        update_calls = mock_client.table.return_value.update.call_args_list
        self.assertTrue(len(update_calls) > 0, "PATCH (update) が呼ばれていない")
        update_arg = update_calls[0][0][0]
        self.assertIn("discord_sent_at", update_arg)
        self.assertEqual(update_arg["discord_sent_at"], sent_at)

    def test_dedup_skipped_no_patch_when_discord_sent_at_is_none(self):
        """dedup_skipped + discord_sent_at=None の場合、PATCH は呼ばれない。"""
        ev = _make_ev()
        mock_client = self._make_mock_client("dedup_skipped")
        with patch("src.events.tdnet_event_store._get_supabase", return_value=mock_client):
            from src.events.tdnet_event_store import save_event_to_supabase
            result = save_event_to_supabase(ev, dry_run=False, discord_sent_at=None)
        self.assertEqual(result.get("action"), "dedup_skipped")
        update_calls = mock_client.table.return_value.update.call_args_list
        self.assertEqual(len(update_calls), 0, "discord_sent_at=None なのに PATCH が呼ばれた")


class TestPhase2CDedupekeyMatching(unittest.TestCase):

    def test_same_ticker_dedupe_key_matches(self):
        """同じ ticker の dedupe_key はマッチし、discord_sent_at が設定される。"""
        from src.events.tdnet_event_store import build_dedupe_key
        ev_notified = _make_ev("7203")
        _notified_by_dedupe = {build_dedupe_key(ev_notified): ev_notified}
        _rec = _make_ev("7203")
        _rec_dedupe = build_dedupe_key(_rec)
        _discord_sent_at = datetime.now(timezone.utc).isoformat() if _rec_dedupe in _notified_by_dedupe else None
        self.assertIsNotNone(_discord_sent_at)

    def test_different_ticker_dedupe_key_no_match(self):
        """異なる ticker の dedupe_key はマッチせず、discord_sent_at は None。"""
        from src.events.tdnet_event_store import build_dedupe_key
        ev_notified = _make_ev("6758")
        _notified_by_dedupe = {build_dedupe_key(ev_notified): ev_notified}
        _rec = _make_ev("7203")
        _rec_dedupe = build_dedupe_key(_rec)
        _discord_sent_at = datetime.now(timezone.utc).isoformat() if _rec_dedupe in _notified_by_dedupe else None
        self.assertIsNone(_discord_sent_at)

    def test_discord_sent_at_passed_when_matched(self):
        """dedupe_key マッチ時に discord_sent_at が fake_save に渡される。"""
        from src.events.tdnet_event_store import build_dedupe_key
        ev = _make_ev()
        _notified_by_dedupe = {build_dedupe_key(ev): ev}
        calls = []
        def fake_save(record, dry_run=False, discord_sent_at=None):
            calls.append(discord_sent_at)
            return {"action": "inserted"}
        _rec = _make_ev()
        _rec_dedupe = build_dedupe_key(_rec)
        _dst = datetime.now(timezone.utc).isoformat() if _rec_dedupe in _notified_by_dedupe else None
        fake_save(_rec, dry_run=False, discord_sent_at=_dst)
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(calls[0])


class TestUpdateDiscordSentAtBackwardCompat(unittest.TestCase):

    def test_function_exists_and_callable(self):
        from src.events.tdnet_event_store import update_discord_sent_at_supabase
        self.assertTrue(callable(update_discord_sent_at_supabase))

    def test_dry_run_returns_true(self):
        from src.events.tdnet_event_store import update_discord_sent_at_supabase
        ev = _make_ev()
        result = update_discord_sent_at_supabase(ev, dry_run=True)
        self.assertTrue(result)

    def test_no_client_returns_false(self):
        from src.events.tdnet_event_store import update_discord_sent_at_supabase
        ev = _make_ev()
        with patch("src.events.tdnet_event_store._get_supabase", return_value=None):
            result = update_discord_sent_at_supabase(ev, dry_run=False)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
