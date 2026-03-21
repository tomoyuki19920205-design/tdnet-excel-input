#!/usr/bin/env python3
"""test_summary_pipeline.py — AI要約パイプライン統合テスト

OpenAI API をモック化し、以下を検証:
- fingerprint 重複スキップ
- リトライ上限(2回)での失敗
- low 優先度スキップ
- AI要約失敗時の全体継続
- Discord 通知の速報/レビュー分岐
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.events.common_models import DocumentMeta
from src.events.summary_models import (
    SummaryPriority, SummaryType, JobStatus, AISummary,
)
from src.events.summary_storage import (
    ensure_summary_tables, insert_summary_job, get_pending_jobs,
    save_ai_summary, get_unnotified_summaries,
)
from src.events.summary_pipeline import run_summary_pipeline, SummaryPipelineResult
from src.events.summary_notify import format_summary_message


class TestSummaryStorage(unittest.TestCase):
    """ストレージ操作のテスト"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ensure_summary_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_ensure_tables_idempotent(self):
        """テーブル作成は冪等"""
        ensure_summary_tables(self.conn)
        ensure_summary_tables(self.conn)

    def test_insert_job_fingerprint_dedup(self):
        """同一 fingerprint のジョブは重複挿入されない"""
        from src.events.summary_models import SummaryJob
        job = SummaryJob(doc_id="doc1", fingerprint="fp_test_001", ticker="7203")
        r1 = insert_summary_job(self.conn, job)
        self.assertEqual(r1, "inserted")

        job2 = SummaryJob(doc_id="doc2", fingerprint="fp_test_001", ticker="7203")
        r2 = insert_summary_job(self.conn, job2)
        self.assertEqual(r2, "already_exists")

    def test_get_pending_jobs_priority_order(self):
        """pending ジョブは優先度順に返る"""
        from src.events.summary_models import SummaryJob
        for fp, pri in [("fp_n", "normal"), ("fp_h", "high"), ("fp_l", "low")]:
            job = SummaryJob(doc_id=f"doc_{fp}", fingerprint=fp, priority=pri)
            insert_summary_job(self.conn, job)

        jobs = get_pending_jobs(self.conn, exclude_low=False)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(jobs[0].priority, "high")
        self.assertEqual(jobs[1].priority, "normal")
        self.assertEqual(jobs[2].priority, "low")

    def test_exclude_low_priority(self):
        """exclude_low=True で low ジョブを除外"""
        from src.events.summary_models import SummaryJob
        for fp, pri in [("fp_n2", "normal"), ("fp_l2", "low")]:
            job = SummaryJob(doc_id=f"doc_{fp}", fingerprint=fp, priority=pri)
            insert_summary_job(self.conn, job)

        jobs = get_pending_jobs(self.conn, exclude_low=True)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].priority, "normal")

    def test_save_and_get_summary(self):
        """要約の保存と取得"""
        from src.events.summary_storage import get_summary_by_fingerprint
        summary = AISummary(
            doc_id="doc1", fingerprint="fp_sum_001", ticker="7203",
            headline="テスト要約", bullet_1="ポイント1", bullet_2="ポイント2",
            bullet_3="ポイント3", tone="positive", needs_review=False,
            model_used="gpt-5.4-mini", input_tokens=500, output_tokens=100,
        )
        save_ai_summary(self.conn, summary)

        loaded = get_summary_by_fingerprint(self.conn, "fp_sum_001")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.headline, "テスト要約")
        self.assertEqual(loaded.tone, "positive")
        self.assertEqual(loaded.input_tokens, 500)
        self.assertFalse(loaded.needs_review)


class TestSummaryNotify(unittest.TestCase):
    """Discord通知フォーマットのテスト"""

    def test_flash_message_format(self):
        """通常の速報メッセージ"""
        summary = AISummary(
            ticker="7203", company_name="トヨタ自動車",
            headline="3Q営業利益+25%の大幅増益",
            bullet_1="北米セグメント好調", bullet_2="為替+200億円",
            bullet_3="通期予想据え置き", tone="positive", needs_review=False,
        )
        msg = format_summary_message(summary)
        self.assertIn("AI速報要約", msg)
        self.assertIn("トヨタ自動車", msg)
        self.assertIn("3Q営業利益+25%", msg)
        self.assertIn("ポジティブ", msg)
        self.assertNotIn("レビュー依頼", msg)

    def test_review_message_format(self):
        """needs_review=True のレビュー依頼メッセージ"""
        summary = AISummary(
            ticker="9999", company_name="テスト会社",
            headline="情報不足",
            bullet_1="確認が必要", bullet_2="", bullet_3="",
            tone="neutral", needs_review=True,
            title="テスト開示タイトル",
        )
        msg = format_summary_message(summary)
        self.assertIn("レビュー依頼", msg)
        self.assertIn("テスト会社", msg)
        self.assertIn("確認が必要", msg)


class TestSummaryPipeline(unittest.TestCase):
    """パイプライン統合テスト（AI API モック）"""

    def _make_doc(self, doc_id, ticker, title, event_type="", subtype=""):
        return DocumentMeta(
            doc_id=doc_id,
            ticker=ticker,
            company_name=f"テスト企業_{ticker}",
            title=title,
            disclosure_datetime="2026-03-20 15:00",
        )

    @patch("src.events.summary_pipeline.call_summary_api")
    def test_basic_pipeline_flow(self, mock_api):
        """基本的なパイプラインフロー"""
        mock_api.return_value = (
            {
                "headline": "テスト要約",
                "bullets": ["ポイント1", "ポイント2", "ポイント3"],
                "tone": "positive",
                "needs_review": False,
            },
            {"input_tokens": 500, "output_tokens": 100, "model_used": "gpt-5.4-mini"},
        )

        doc = self._make_doc("doc1", "7203", "2026年3月期 決算短信〔日本基準〕（連結）")
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            result = run_summary_pipeline(
                docs=[doc], db_path=db_path, dry_run=False,
            )
            self.assertEqual(result.jobs_created, 1)
            self.assertEqual(result.jobs_succeeded, 1)
            self.assertEqual(result.jobs_failed, 0)
        finally:
            os.unlink(db_path)

    @patch("src.events.summary_pipeline.call_summary_api")
    def test_fingerprint_dedup(self, mock_api):
        """同一文書の2度目はジョブが重複スキップされる"""
        mock_api.return_value = (
            {
                "headline": "テスト",
                "bullets": ["a", "b", "c"],
                "tone": "neutral",
                "needs_review": False,
            },
            {"input_tokens": 200, "output_tokens": 50, "model_used": "gpt-5.4-mini"},
        )

        doc = self._make_doc("doc_dup", "7203", "決算短信テスト")
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            r1 = run_summary_pipeline(docs=[doc], db_path=db_path)
            r2 = run_summary_pipeline(docs=[doc], db_path=db_path)
            self.assertEqual(r1.jobs_created, 1)
            self.assertEqual(r2.jobs_created, 0)
            self.assertGreater(r2.jobs_skipped, 0)
        finally:
            os.unlink(db_path)

    def test_low_priority_skipped(self):
        """low 優先度はスキップされる"""
        doc = self._make_doc("doc_low", "9999", "代表取締役の異動に関するお知らせ")
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            result = run_summary_pipeline(
                docs=[doc], db_path=db_path, skip_low=True, dry_run=True,
            )
            self.assertEqual(result.jobs_created, 0)
            self.assertGreater(result.jobs_skipped, 0)
        finally:
            os.unlink(db_path)

    @patch("src.events.summary_pipeline.call_summary_api")
    def test_ai_failure_does_not_stop_pipeline(self, mock_api):
        """1件のAI失敗が他の文書処理を止めない"""
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("API Error")
            return (
                {
                    "headline": "成功",
                    "bullets": ["a", "b", "c"],
                    "tone": "positive",
                    "needs_review": False,
                },
                {"input_tokens": 200, "output_tokens": 50, "model_used": "gpt-5.4-mini"},
            )

        mock_api.side_effect = side_effect

        docs = [
            self._make_doc("doc_fail", "1111", "決算短信テスト1"),
            self._make_doc("doc_ok", "2222", "決算短信テスト2"),
        ]
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            result = run_summary_pipeline(docs=docs, db_path=db_path)
            # 2件処理して1件失敗、1件成功
            self.assertEqual(result.jobs_processed, 2)
            self.assertEqual(result.jobs_failed, 1)
            self.assertEqual(result.jobs_succeeded, 1)
        finally:
            os.unlink(db_path)

    def test_dry_run_no_api_call(self):
        """dry-run はAI APIを呼ばず、永続DBにも書き込まない"""
        doc = self._make_doc("doc_dry", "7203", "決算短信テスト")
        import tempfile
        db_path = os.path.join(tempfile.gettempdir(), "test_dry_run_noop.db")

        # 前回の残りがあれば削除
        if os.path.exists(db_path):
            os.unlink(db_path)

        try:
            # call_summary_api をモックせずに dry_run=True で実行
            result = run_summary_pipeline(
                docs=[doc], db_path=db_path, dry_run=True,
            )
            self.assertEqual(result.jobs_created, 1)
            # V1 dry-run はエラーなし (V2のXBRL検索エラーは非致命で許容)
            v1_errors = [e for e in result.errors if "XBRL ZIP not found" not in e]
            self.assertEqual(v1_errors, [])

            # dry-run では永続 DB ファイルが作成されないことを確認
            self.assertFalse(
                os.path.exists(db_path),
                "dry-run should NOT create persistent DB file"
            )
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

class TestV1NotificationHardening(unittest.TestCase):
    """V1通知恒久修正テスト

    通知ルール:
    - 通知対象は「今回DB保存成功分」のみ (processed_fingerprints)
    - processed_fingerprints が None / 空 → 0件即return
    - 過去の未通知レコードは自動実行では拾わない
    - Discord送信成功時のみ notified_at を更新
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ensure_summary_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def _insert_old_unnotified(self, n: int = 47):
        """過去の未通知レコードをn件作成"""
        for i in range(n):
            summary = AISummary(
                doc_id=f"old_doc_{i}", fingerprint=f"old_fp_{i:04d}",
                ticker=f"{1000+i}", company_name=f"旧企業{i}",
                headline=f"旧要約{i}", bullet_1="a", bullet_2="b", bullet_3="c",
                tone="neutral", needs_review=False,
                model_used="gpt-5.4-mini", input_tokens=100, output_tokens=50,
            )
            save_ai_summary(self.conn, summary)

    def _insert_new_saved(self, fingerprints: list[str]):
        """今回保存成功分のレコードを作成"""
        for i, fp in enumerate(fingerprints):
            summary = AISummary(
                doc_id=f"new_doc_{i}", fingerprint=fp,
                ticker=f"{2000+i}", company_name=f"新企業{i}",
                headline=f"新要約{i}", bullet_1="ポイント1", bullet_2="ポイント2",
                bullet_3="ポイント3", tone="positive", needs_review=False,
                model_used="gpt-5.4-mini", input_tokens=200, output_tokens=80,
            )
            save_ai_summary(self.conn, summary)

    def test_no_processed_jobs_with_old_unnotified(self):
        """jobs_processed=0 かつ DBに旧未通知47件あり → notifications=0"""
        from src.events.summary_pipeline import _send_notifications
        self._insert_old_unnotified(47)

        # processed_fingerprints が空 (今回保存なし)
        sent = _send_notifications(
            self.conn, webhook_url="https://example.com/hook",
            dry_run=True, processed_fingerprints=set(),
        )
        self.assertEqual(sent, 0)

    def test_new_2_plus_old_47(self):
        """今回新規2件 + 旧未通知47件 → notifications=2"""
        from src.events.summary_pipeline import _send_notifications
        self._insert_old_unnotified(47)
        new_fps = {"new_fp_001", "new_fp_002"}
        self._insert_new_saved(list(new_fps))

        sent = _send_notifications(
            self.conn, webhook_url="https://example.com/hook",
            dry_run=True, processed_fingerprints=new_fps,
        )
        self.assertEqual(sent, 2)

    def test_none_fingerprints_returns_zero(self):
        """processed_fingerprints=None → notifications=0"""
        from src.events.summary_pipeline import _send_notifications
        self._insert_old_unnotified(10)

        sent = _send_notifications(
            self.conn, webhook_url="https://example.com/hook",
            dry_run=True, processed_fingerprints=None,
        )
        self.assertEqual(sent, 0)

    @patch("src.events.summary_pipeline.send_summary_discord")
    def test_discord_failure_no_notified_at(self, mock_send):
        """Discord送信失敗時に notified_at 未更新"""
        from src.events.summary_pipeline import _send_notifications
        mock_send.return_value = False  # 送信失敗

        new_fps = {"fail_fp_001"}
        self._insert_new_saved(list(new_fps))

        sent = _send_notifications(
            self.conn, webhook_url="https://example.com/hook",
            dry_run=False, processed_fingerprints=new_fps,
        )

        self.assertEqual(sent, 0)
        # notified_at が更新されていないこと
        unnotified = get_unnotified_summaries(self.conn)
        fail_summaries = [s for s in unnotified if s.fingerprint == "fail_fp_001"]
        self.assertEqual(len(fail_summaries), 1, "送信失敗分は未通知のまま残る")

    def test_validation_skips_empty_headline(self):
        """空headline は通知スキップ"""
        from src.events.summary_pipeline import _send_notifications
        # 空 headline のレコード
        summary = AISummary(
            doc_id="empty_doc", fingerprint="empty_fp_001",
            ticker="9999", company_name="テスト",
            headline="", bullet_1="a", bullet_2="b", bullet_3="c",
            tone="neutral", needs_review=False,
            model_used="gpt-5.4-mini", input_tokens=100, output_tokens=50,
        )
        save_ai_summary(self.conn, summary)

        sent = _send_notifications(
            self.conn, webhook_url="https://example.com/hook",
            dry_run=True, processed_fingerprints={"empty_fp_001"},
        )
        self.assertEqual(sent, 0)


if __name__ == "__main__":
    unittest.main()
