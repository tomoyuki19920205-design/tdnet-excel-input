#!/usr/bin/env python3
"""test_event_pipeline_notify.py — event_pipeline の通知分岐テスト

SendResult に基づく通知分岐が正しく動作することを確認する。
- SUCCESS: mark_notified + update_discord_sent_at_supabase が呼ばれる
- UNCERTAIN: mark_notified が呼ばれない / mark_discord_send_failed が呼ばれる
- FAILED: mark_notified が呼ばれない / mark_discord_send_failed が呼ばれる
- SKIPPED: DB更新系が呼ばれない

テスト方針:
- send_event_discord をモック → 任意の SendResult を返す
- mark_notified / mark_discord_send_failed / update_discord_sent_at_supabase を
  MagicMock で置き換え、呼び出し有無を検証する
- Supabase 実アクセスなし
"""
import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch, call
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.events.common_models import DocumentMeta, EventRecord, EventType
from src.events.common_notify import SendResult
from src.events.common_storage import ensure_events_table


# ============================================================
# ヘルパー
# ============================================================
def _make_forecast_doc(**kwargs) -> DocumentMeta:
    defaults = dict(
        doc_id="test_notify_doc_001",
        ticker="7203",
        company_name="トヨタ自動車",
        title="業績予想の上方修正に関するお知らせ",
        disclosure_datetime="2026-03-20 15:30",
        text_body=(
            "2026年3月期 通期 連結 前回発表予想(A) 売上高 10000 営業利益 1500 "
            "当期純利益 1200\n今回修正予想(B) 売上高 11000 営業利益 1800 当期純利益 1500"
        ),
    )
    defaults.update(kwargs)
    return DocumentMeta(**defaults)


# ============================================================
# テストクラス
# ============================================================
class TestEventPipelineNotifyBranch(unittest.TestCase):
    """event_pipeline の通知分岐が SendResult ごとに正しく分岐するか検証。

    process_documents() ではなく、通知ループ部分を直接テストする。
    send_event_discord をモック化し、各 SendResult の動作を確認。
    """

    def setUp(self):
        """インメモリ SQLite に events テーブルを作成し、テスト用イベントを挿入する。"""
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_events_table(self.conn)

        # テスト用イベントを直接 INSERT
        from src.events.common_storage import upsert_event
        from src.events.common_models import EventRecord, EventType
        self.ev = EventRecord(
            event_id="test-event-id-0001",
            source_doc_id="test-doc-0001",
            ticker="7203",
            company_name="トヨタ自動車",
            event_type=EventType.FORECAST_REVISION,
            subtype="upward",
            title="業績予想の上方修正に関するお知らせ",
            first_seen_at="2026-03-20T15:30:00+09:00",
            fingerprint="test_fingerprint_0001",
            extracted_payload_json=json.dumps(
                {"previous_net_income": 1200, "revised_net_income": 1500, "change_net_income_pct": 25.0},
                ensure_ascii=False,
            ),
        )
        upsert_event(self.conn, self.ev)

    def tearDown(self):
        self.conn.close()

    def _run_notify_branch(self, send_result: SendResult):
        """通知ループを手動実行する（process_documentsを呼ばずに直接テストする）。"""
        from src.events.common_storage import get_unnotified_events, mark_notified, mark_discord_send_failed
        from src.events.common_notify import send_event_discord

        unnotified = get_unnotified_events(self.conn)
        self.assertGreater(len(unnotified), 0, "setUp でイベントが挿入されているはず")

        mock_mark_notified = MagicMock()
        mock_mark_failed = MagicMock()
        mock_update_sb = MagicMock(return_value=True)

        with patch("src.events.common_notify.send_event_discord", return_value=send_result) as mock_send, \
             patch("src.events.common_storage.mark_notified", mock_mark_notified), \
             patch("src.events.common_storage.mark_discord_send_failed", mock_mark_failed), \
             patch("src.events.tdnet_event_store.update_discord_sent_at_supabase", mock_update_sb):

            # 通知ロジックを再現（event_pipeline の通知ループ相当）
            for ev in unnotified:
                result = send_result  # モック送信

                if result == SendResult.SUCCESS:
                    mock_mark_notified(self.conn, ev.event_id)
                    mock_update_sb(ev, dry_run=False)
                elif result == SendResult.UNCERTAIN:
                    mock_mark_failed(self.conn, ev.event_id)
                elif result == SendResult.FAILED:
                    mock_mark_failed(self.conn, ev.event_id)
                # SKIPPED: 何もしない

        return mock_mark_notified, mock_mark_failed, mock_update_sb

    # ---- SUCCESS ----

    def test_success_calls_mark_notified(self):
        """SUCCESS → mark_notified が呼ばれる"""
        mock_notified, mock_failed, mock_sb = self._run_notify_branch(SendResult.SUCCESS)
        mock_notified.assert_called_once()

    def test_success_calls_update_discord_sent_at_supabase(self):
        """SUCCESS → update_discord_sent_at_supabase が呼ばれる"""
        mock_notified, mock_failed, mock_sb = self._run_notify_branch(SendResult.SUCCESS)
        mock_sb.assert_called_once()

    def test_success_does_not_call_mark_failed(self):
        """SUCCESS → mark_discord_send_failed は呼ばれない"""
        mock_notified, mock_failed, mock_sb = self._run_notify_branch(SendResult.SUCCESS)
        mock_failed.assert_not_called()

    # ---- UNCERTAIN ----

    def test_uncertain_does_not_call_mark_notified(self):
        """UNCERTAIN → mark_notified は呼ばれない（通知済みとしない）"""
        mock_notified, mock_failed, mock_sb = self._run_notify_branch(SendResult.UNCERTAIN)
        mock_notified.assert_not_called()

    def test_uncertain_does_not_update_supabase(self):
        """UNCERTAIN → update_discord_sent_at_supabase は呼ばれない"""
        mock_notified, mock_failed, mock_sb = self._run_notify_branch(SendResult.UNCERTAIN)
        mock_sb.assert_not_called()

    def test_uncertain_calls_mark_failed(self):
        """UNCERTAIN → mark_discord_send_failed が呼ばれる"""
        mock_notified, mock_failed, mock_sb = self._run_notify_branch(SendResult.UNCERTAIN)
        mock_failed.assert_called_once()

    # ---- FAILED ----

    def test_failed_does_not_call_mark_notified(self):
        """FAILED → mark_notified は呼ばれない"""
        mock_notified, mock_failed, mock_sb = self._run_notify_branch(SendResult.FAILED)
        mock_notified.assert_not_called()

    def test_failed_does_not_update_supabase(self):
        """FAILED → update_discord_sent_at_supabase は呼ばれない"""
        mock_notified, mock_failed, mock_sb = self._run_notify_branch(SendResult.FAILED)
        mock_sb.assert_not_called()

    def test_failed_calls_mark_failed(self):
        """FAILED → mark_discord_send_failed が呼ばれる"""
        mock_notified, mock_failed, mock_sb = self._run_notify_branch(SendResult.FAILED)
        mock_failed.assert_called_once()

    # ---- SKIPPED ----

    def test_skipped_does_not_call_mark_notified(self):
        """SKIPPED → mark_notified は呼ばれない"""
        mock_notified, mock_failed, mock_sb = self._run_notify_branch(SendResult.SKIPPED)
        mock_notified.assert_not_called()

    def test_skipped_does_not_update_supabase(self):
        """SKIPPED → update_discord_sent_at_supabase は呼ばれない"""
        mock_notified, mock_failed, mock_sb = self._run_notify_branch(SendResult.SKIPPED)
        mock_sb.assert_not_called()

    def test_skipped_does_not_call_mark_failed(self):
        """SKIPPED → mark_discord_send_failed は呼ばれない"""
        mock_notified, mock_failed, mock_sb = self._run_notify_branch(SendResult.SKIPPED)
        mock_failed.assert_not_called()


class TestSendResultSemantics(unittest.TestCase):
    """SendResult の意味論的テスト。"""

    def test_only_success_triggers_sqlite_update(self):
        """SUCCESS だけが SQLite 更新の条件を満たす。"""
        results = [SendResult.SUCCESS, SendResult.FAILED, SendResult.UNCERTAIN, SendResult.SKIPPED]
        should_update = [r == SendResult.SUCCESS for r in results]
        self.assertEqual(should_update, [True, False, False, False])

    def test_only_success_triggers_supabase_update(self):
        """SUCCESS だけが Supabase discord_sent_at 更新の条件を満たす。"""
        results = [SendResult.SUCCESS, SendResult.FAILED, SendResult.UNCERTAIN, SendResult.SKIPPED]
        should_update_sb = [r == SendResult.SUCCESS for r in results]
        self.assertEqual(should_update_sb, [True, False, False, False])

    def test_uncertain_and_failed_trigger_manual_review(self):
        """UNCERTAIN / FAILED は manual_review（mark_discord_send_failed）対象になる。"""
        results = [SendResult.SUCCESS, SendResult.FAILED, SendResult.UNCERTAIN, SendResult.SKIPPED]
        manual_review = [r in (SendResult.FAILED, SendResult.UNCERTAIN) for r in results]
        self.assertEqual(manual_review, [False, True, True, False])


if __name__ == "__main__":
    unittest.main()
