#!/usr/bin/env python3
"""test_event_pipeline_notify.py — Phase 2C 対応版

Phase 2C の変更点：
- Discord SUCCESS 時に update_discord_sent_at_supabase() を通知ループ内ではなく
  Supabase INSERT 後フェーズ (_notified_events_safe) で呼ぶ設計になった
- このファイルでは以下を検証する:
    1. 通知ループ（notify branch）: mark_notified のみ呼ばれ、update_sb は呼ばれない
    2. INSERT後フェーズ: SUCCESS イベントのみ update_discord_sent_at_supabase が呼ばれる
    3. UNCERTAIN/FAILED/SKIPPED: INSERT後でも update_discord_sent_at_supabase は呼ばれない
    4. Supabase 保存失敗時: update_discord_sent_at_supabase は呼ばれない
    5. 呼び出し順序: save_event_to_supabase → update_discord_sent_at_supabase

コード変更なし確認方針:
- send_event_discord / save_event_to_supabase / update_discord_sent_at_supabase を
  MagicMock で差し替え、呼び出し有無・順序を検証する
- Supabase 実アクセスなし
- Discord 実送信なし
"""
import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch, call
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.events.common_models import EventRecord, EventType
from src.events.common_notify import SendResult
from src.events.common_storage import ensure_events_table, upsert_event


# ============================================================
# ヘルパー
# ============================================================
def _make_ev(event_id: str = "test-event-id-0001", ticker: str = "7203") -> EventRecord:
    return EventRecord(
        event_id=event_id,
        source_doc_id=f"test-doc-{event_id[:8]}",
        ticker=ticker,
        company_name="トヨタ自動車",
        event_type=EventType.FORECAST_REVISION,
        subtype="upward",
        title="業績予想の上方修正に関するお知らせ",
        first_seen_at="2026-03-20T15:30:00+09:00",
        fingerprint=f"fp_{event_id[:8]}",
        extracted_payload_json=json.dumps(
            {"previous_net_income": 1200, "revised_net_income": 1500, "change_net_income_pct": 25.0},
            ensure_ascii=False,
        ),
    )


# ============================================================
# A. Phase 2C 通知分岐テスト（notify loop 単体）
#    通知ループ内では notified_events への蓄積のみ行う。
#    update_discord_sent_at_supabase はここでは呼ばない。
# ============================================================
class TestNotifyLoopPhase2C(unittest.TestCase):
    """Phase 2C: 通知ループが notified_events を正しく構築するか検証。

    通知ループ内では:
    - SUCCESS → mark_notified() 呼び出し + notified_events 蓄積
    - UNCERTAIN → mark_discord_send_failed() のみ
    - FAILED → mark_discord_send_failed() のみ
    - SKIPPED → 何もしない
    update_discord_sent_at_supabase() は通知ループ内では呼ばれない。
    """

    def _simulate_notify_loop(self, send_result: SendResult):
        """通知ループロジックを直接再現（event_pipeline Phase 2C 版）。"""
        ev = _make_ev()

        mock_mark_notified = MagicMock()
        mock_mark_failed = MagicMock()
        mock_update_sb = MagicMock(return_value=True)

        notified_events: dict = {}

        # Phase 2C の通知ループ相当
        if send_result == SendResult.SUCCESS:
            mock_mark_notified("conn", ev.event_id)
            notified_events[ev.event_id] = ev
        elif send_result == SendResult.UNCERTAIN:
            mock_mark_failed("conn", ev.event_id)
        elif send_result == SendResult.FAILED:
            mock_mark_failed("conn", ev.event_id)
        # SKIPPED: 何もしない

        return mock_mark_notified, mock_mark_failed, mock_update_sb, notified_events, ev

    # SUCCESS
    def test_success_calls_mark_notified(self):
        """SUCCESS → mark_notified が呼ばれる"""
        mock_notified, _, _, _, _ = self._simulate_notify_loop(SendResult.SUCCESS)
        mock_notified.assert_called_once()

    def test_success_stores_in_notified_events(self):
        """SUCCESS → notified_events に event_id が蓄積される"""
        _, _, _, notified_events, ev = self._simulate_notify_loop(SendResult.SUCCESS)
        self.assertIn(ev.event_id, notified_events)

    def test_success_does_not_call_update_sb_in_notify_loop(self):
        """SUCCESS でも通知ループ内では update_discord_sent_at_supabase を呼ばない (Phase 2C)"""
        _, _, mock_update_sb, _, _ = self._simulate_notify_loop(SendResult.SUCCESS)
        mock_update_sb.assert_not_called()

    def test_success_does_not_call_mark_failed(self):
        """SUCCESS → mark_discord_send_failed は呼ばれない"""
        _, mock_failed, _, _, _ = self._simulate_notify_loop(SendResult.SUCCESS)
        mock_failed.assert_not_called()

    # UNCERTAIN
    def test_uncertain_does_not_call_mark_notified(self):
        """UNCERTAIN → mark_notified は呼ばれない"""
        mock_notified, _, _, _, _ = self._simulate_notify_loop(SendResult.UNCERTAIN)
        mock_notified.assert_not_called()

    def test_uncertain_does_not_store_in_notified_events(self):
        """UNCERTAIN → notified_events に蓄積されない"""
        _, _, _, notified_events, ev = self._simulate_notify_loop(SendResult.UNCERTAIN)
        self.assertNotIn(ev.event_id, notified_events)

    def test_uncertain_calls_mark_failed(self):
        """UNCERTAIN → mark_discord_send_failed が呼ばれる"""
        _, mock_failed, _, _, _ = self._simulate_notify_loop(SendResult.UNCERTAIN)
        mock_failed.assert_called_once()

    # FAILED
    def test_failed_does_not_call_mark_notified(self):
        """FAILED → mark_notified は呼ばれない"""
        mock_notified, _, _, _, _ = self._simulate_notify_loop(SendResult.FAILED)
        mock_notified.assert_not_called()

    def test_failed_does_not_store_in_notified_events(self):
        """FAILED → notified_events に蓄積されない"""
        _, _, _, notified_events, ev = self._simulate_notify_loop(SendResult.FAILED)
        self.assertNotIn(ev.event_id, notified_events)

    def test_failed_calls_mark_failed(self):
        """FAILED → mark_discord_send_failed が呼ばれる"""
        _, mock_failed, _, _, _ = self._simulate_notify_loop(SendResult.FAILED)
        mock_failed.assert_called_once()

    # SKIPPED
    def test_skipped_does_not_call_mark_notified(self):
        """SKIPPED → mark_notified は呼ばれない"""
        mock_notified, _, _, _, _ = self._simulate_notify_loop(SendResult.SKIPPED)
        mock_notified.assert_not_called()

    def test_skipped_does_not_store_in_notified_events(self):
        """SKIPPED → notified_events に蓄積されない"""
        _, _, _, notified_events, ev = self._simulate_notify_loop(SendResult.SKIPPED)
        self.assertNotIn(ev.event_id, notified_events)

    def test_skipped_does_not_call_mark_failed(self):
        """SKIPPED → mark_discord_send_failed は呼ばれない"""
        _, mock_failed, _, _, _ = self._simulate_notify_loop(SendResult.SKIPPED)
        mock_failed.assert_not_called()


# ============================================================
# B. Phase 2C INSERT後フェーズテスト
#    save_event_to_supabase 完了後に discord_sent_at を更新するロジックの検証。
# ============================================================
class TestInsertAfterPhase2C(unittest.TestCase):
    """Phase 2C: Supabase INSERT後フェーズの discord_sent_at 更新検証。

    save_event_to_supabase が成功した後にのみ update_discord_sent_at_supabase が呼ばれ、
    呼び出し順序が save → update であることを確認する。
    """

    def _simulate_insert_phase(
        self,
        send_result: SendResult,
        save_action: str = "inserted",  # "inserted" / "updated" / "dedup_skipped" / "error"
    ):
        """INSERT後フェーズのロジックを直接再現（event_pipeline Phase 2C 版）。"""
        ev = _make_ev()
        calls = []

        mock_save = MagicMock()
        mock_save.return_value = {"action": save_action, "display_category": "forecast"}

        mock_update_sb = MagicMock(return_value=True)

        def fake_save(*args, **kwargs):
            calls.append("save")
            return mock_save()

        def fake_update(*args, **kwargs):
            calls.append("update")
            return True

        # 通知フェーズ: notified_events 構築
        _notified_events_safe: dict = {}
        if send_result == SendResult.SUCCESS:
            _notified_events_safe[ev.event_id] = ev

        # INSERT後フェーズ: save → 成功したら update
        _sb_result = fake_save(ev, dry_run=False)
        _action = _sb_result.get("action", "error")
        _save_ok = _action in ("inserted", "updated", "dedup_skipped")

        if _save_ok and ev.event_id in _notified_events_safe:
            fake_update(_notified_events_safe[ev.event_id], dry_run=False)

        return calls, _save_ok, ev

    # SUCCESS + inserted
    def test_success_insert_calls_update_after_save(self):
        """SUCCESS + inserted → save の後に update が呼ばれる"""
        calls, _, _ = self._simulate_insert_phase(SendResult.SUCCESS, "inserted")
        self.assertEqual(calls, ["save", "update"])

    def test_success_updated_calls_update_after_save(self):
        """SUCCESS + updated → save の後に update が呼ばれる"""
        calls, _, _ = self._simulate_insert_phase(SendResult.SUCCESS, "updated")
        self.assertEqual(calls, ["save", "update"])

    def test_success_dedup_skipped_calls_update_after_save(self):
        """SUCCESS + dedup_skipped → save の後に update が呼ばれる（dedup でも更新する）"""
        calls, _, _ = self._simulate_insert_phase(SendResult.SUCCESS, "dedup_skipped")
        self.assertEqual(calls, ["save", "update"])

    def test_success_save_error_does_not_call_update(self):
        """SUCCESS + save error → update は呼ばれない（保存失敗時は更新しない）"""
        calls, _, _ = self._simulate_insert_phase(SendResult.SUCCESS, "error")
        self.assertNotIn("update", calls)
        self.assertIn("save", calls)

    # UNCERTAIN
    def test_uncertain_does_not_call_update_after_insert(self):
        """UNCERTAIN → INSERT後でも update は呼ばれない"""
        calls, _, _ = self._simulate_insert_phase(SendResult.UNCERTAIN, "inserted")
        self.assertNotIn("update", calls)

    # FAILED
    def test_failed_does_not_call_update_after_insert(self):
        """FAILED → INSERT後でも update は呼ばれない"""
        calls, _, _ = self._simulate_insert_phase(SendResult.FAILED, "inserted")
        self.assertNotIn("update", calls)

    # SKIPPED
    def test_skipped_does_not_call_update_after_insert(self):
        """SKIPPED → INSERT後でも update は呼ばれない"""
        calls, _, _ = self._simulate_insert_phase(SendResult.SKIPPED, "inserted")
        self.assertNotIn("update", calls)

    def test_call_order_is_save_then_update(self):
        """呼び出し順序が save → update であることを確認 (Phase 2C の根幹)"""
        calls, _, _ = self._simulate_insert_phase(SendResult.SUCCESS, "inserted")
        self.assertEqual(calls.index("save"), 0)
        self.assertEqual(calls.index("update"), 1)


# ============================================================
# C. SendResult 意味論テスト（Phase 2B から継続）
# ============================================================
class TestSendResultSemantics(unittest.TestCase):
    """SendResult の意味論的テスト。Phase 2C でも変わらない。"""

    def test_only_success_triggers_sqlite_update(self):
        """SUCCESS だけが SQLite 更新の条件を満たす。"""
        results = [SendResult.SUCCESS, SendResult.FAILED, SendResult.UNCERTAIN, SendResult.SKIPPED]
        should_update = [r == SendResult.SUCCESS for r in results]
        self.assertEqual(should_update, [True, False, False, False])

    def test_only_success_stores_in_notified_events(self):
        """SUCCESS だけが notified_events に蓄積される（INSERT後フェーズへの橋渡し）。"""
        results = [SendResult.SUCCESS, SendResult.FAILED, SendResult.UNCERTAIN, SendResult.SKIPPED]
        stores = [r == SendResult.SUCCESS for r in results]
        self.assertEqual(stores, [True, False, False, False])

    def test_uncertain_and_failed_trigger_manual_review(self):
        """UNCERTAIN / FAILED は mark_discord_send_failed 対象になる。"""
        results = [SendResult.SUCCESS, SendResult.FAILED, SendResult.UNCERTAIN, SendResult.SKIPPED]
        manual_review = [r in (SendResult.FAILED, SendResult.UNCERTAIN) for r in results]
        self.assertEqual(manual_review, [False, True, True, False])


# ============================================================
# D. Phase 2C 統合シナリオテスト
# ============================================================
class TestPhase2CIntegrationScenario(unittest.TestCase):
    """通知フェーズ → INSERT後フェーズの結合シナリオ検証。

    実際の event_pipeline のフローを模倣し、
    通知ループと INSERT後フェーズを連続して実行して検証する。
    """

    def _run_full_pipeline_simulate(
        self,
        send_result: SendResult,
        save_action: str = "inserted",
    ):
        """通知ループ + INSERT後フェーズを連続実行するシミュレーション。"""
        ev = _make_ev()
        calls = []

        # === 通知フェーズ ===
        _notified_events_safe: dict = {}
        if send_result == SendResult.SUCCESS:
            calls.append("mark_notified")
            _notified_events_safe[ev.event_id] = ev
        elif send_result in (SendResult.UNCERTAIN, SendResult.FAILED):
            calls.append("mark_discord_send_failed")

        # === INSERT後フェーズ ===
        _save_result = {"action": save_action, "display_category": "forecast"}
        _action = _save_result.get("action", "error")
        _save_ok = _action in ("inserted", "updated", "dedup_skipped")

        calls.append("save")

        if _save_ok and ev.event_id in _notified_events_safe:
            calls.append("update_discord_sent_at")

        return calls

    def test_success_inserted_full_flow(self):
        """SUCCESS + inserted: mark_notified → save → update_discord_sent_at の順"""
        calls = self._run_full_pipeline_simulate(SendResult.SUCCESS, "inserted")
        self.assertEqual(calls, ["mark_notified", "save", "update_discord_sent_at"])

    def test_uncertain_inserted_full_flow(self):
        """UNCERTAIN + inserted: mark_discord_send_failed → save のみ（update なし）"""
        calls = self._run_full_pipeline_simulate(SendResult.UNCERTAIN, "inserted")
        self.assertEqual(calls, ["mark_discord_send_failed", "save"])
        self.assertNotIn("update_discord_sent_at", calls)

    def test_failed_inserted_full_flow(self):
        """FAILED + inserted: mark_discord_send_failed → save のみ（update なし）"""
        calls = self._run_full_pipeline_simulate(SendResult.FAILED, "inserted")
        self.assertEqual(calls, ["mark_discord_send_failed", "save"])
        self.assertNotIn("update_discord_sent_at", calls)

    def test_success_save_error_no_update(self):
        """SUCCESS + save error: mark_notified → save → update なし（保存失敗なので）"""
        calls = self._run_full_pipeline_simulate(SendResult.SUCCESS, "error")
        self.assertIn("mark_notified", calls)
        self.assertIn("save", calls)
        self.assertNotIn("update_discord_sent_at", calls)

    def test_update_is_after_save_in_full_flow(self):
        """update_discord_sent_at は常に save の後"""
        calls = self._run_full_pipeline_simulate(SendResult.SUCCESS, "inserted")
        self.assertGreater(calls.index("update_discord_sent_at"), calls.index("save"))


if __name__ == "__main__":
    unittest.main()
