#!/usr/bin/env python3
"""test_v1_operational.py — V1通知 恒久修正 運用確認テスト

本番相当の統合テスト（永続DB + モックAPI/Discord）で以下を検証:
  1. 新規V1要約が出た日に、その新規件数だけ通知される
  2. 同日再実行で再送されない（fingerprint重複 + processed_fps空）
  3. V2通知件数に副作用がない

ここでは fetch / AI / Discord をすべてモック化し、
SQLite は tempfile の永続DBを使用してリアルな状態遷移を検証する。
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.events.common_models import DocumentMeta
from src.events.summary_models import AISummary
from src.events.summary_storage import (
    ensure_summary_tables, get_unnotified_summaries,
)
from src.events.summary_pipeline import (
    run_summary_pipeline, SummaryPipelineResult,
)


def _make_doc(ticker: str, title: str) -> DocumentMeta:
    return DocumentMeta(
        doc_id=f"doc_{ticker}",
        ticker=ticker,
        company_name=f"企業{ticker}",
        title=title,
        doc_url=f"https://tdnet.example.com/{ticker}.pdf",
        disclosure_datetime="2026-03-13 15:00",
    )


def _fake_ai_result():
    """AI API のモック戻り値"""
    return (
        {
            "headline": "テスト見出し",
            "bullets": ["ポイント1", "ポイント2", "ポイント3"],
            "tone": "positive",
            "needs_review": False,
            "summary_type": "flash",
        },
        {"input_tokens": 100, "output_tokens": 50, "model_used": "gpt-5.4-mini"},
    )


class TestV1OperationalFlow(unittest.TestCase):
    """本番相当の統合テスト"""

    def setUp(self):
        # 永続 DB を tempfile に作成
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    @patch("src.fetcher.fetch_new_disclosures")
    @patch("src.events.earnings_production_pipeline.run_earnings_production")
    @patch("src.events.summary_pipeline.send_summary_discord")
    @patch("src.events.summary_pipeline.call_summary_api")
    def test_full_operational_flow(
        self, mock_ai, mock_discord, mock_v2, mock_fetch,
    ):
        """3点すべてを一つのフローで検証:
        1. 初回: 新規3件 → notifications=3
        2. 再実行: 同じdocs → notifications=0
        3. V2通知件数に副作用なし
        """
        # ---- セットアップ ----
        mock_ai.return_value = _fake_ai_result()
        mock_discord.return_value = True  # Discord送信成功

        # V2: 固定結果を返す（V1とは独立）
        v2_result = SimpleNamespace(
            generated_count=5, saved_count=5, notified_count=2,
            filtered_count=3, no_yoy_count=10, already_exists_count=0,
            errors=[],
        )
        mock_v2.return_value = v2_result
        mock_fetch.return_value = []  # V2 用 fetch は空でOK（mock_v2が結果を返す）

        docs = [
            _make_doc("1001", "業績予想の修正に関するお知らせ"),
            _make_doc("1002", "通期連結業績予想と実績値との差異に関するお知らせ"),
            _make_doc("1003", "業績予想の修正に関するお知らせ"),
        ]

        # ======== 初回実行 ========
        result1 = run_summary_pipeline(
            docs=docs,
            db_path=self.db_path,
            dry_run=False,
            target_date="2026-03-13",
            webhook_url="https://discord.example.com/hook",
            model="gpt-5.4-mini",
        )

        # 検証1: 新規3件 → notifications=3
        self.assertEqual(result1.jobs_created, 3, "初回: 3ジョブ作成")
        self.assertEqual(result1.jobs_succeeded, 3, "初回: 3ジョブ成功")
        self.assertEqual(
            result1.notifications_sent, 3,
            f"初回: 新規3件のみ通知されるべき (got {result1.notifications_sent})",
        )

        # 検証3: V2通知件数に副作用なし
        self.assertEqual(result1.earnings_generated, 5, "V2: generated=5")
        self.assertEqual(result1.earnings_notified, 2, "V2: notified=2")

        # Discord呼び出し回数を記録
        discord_calls_after_run1 = mock_discord.call_count

        # ======== 同日再実行 ========
        result2 = run_summary_pipeline(
            docs=docs,
            db_path=self.db_path,
            dry_run=False,
            target_date="2026-03-13",
            webhook_url="https://discord.example.com/hook",
            model="gpt-5.4-mini",
        )

        # 検証2: 再実行で再送されない
        self.assertEqual(
            result2.jobs_created, 0,
            f"再実行: fingerprint重複で0ジョブ (got {result2.jobs_created})",
        )
        self.assertEqual(
            result2.jobs_processed, 0,
            f"再実行: pendingジョブなし (got {result2.jobs_processed})",
        )
        self.assertEqual(
            result2.notifications_sent, 0,
            f"再実行: 通知0 (got {result2.notifications_sent})",
        )

        # Discord追加呼び出しなし
        discord_calls_after_run2 = mock_discord.call_count
        self.assertEqual(
            discord_calls_after_run2, discord_calls_after_run1,
            "再実行で Discord が追加呼び出しされていない",
        )

        # 検証3 (再実行でもV2は独立動作)
        self.assertEqual(result2.earnings_generated, 5, "再実行 V2: generated=5")
        self.assertEqual(result2.earnings_notified, 2, "再実行 V2: notified=2")

    @patch("src.fetcher.fetch_new_disclosures")
    @patch("src.events.earnings_production_pipeline.run_earnings_production")
    @patch("src.events.summary_pipeline.send_summary_discord")
    @patch("src.events.summary_pipeline.call_summary_api")
    def test_discord_failure_leaves_unnotified_not_resent(
        self, mock_ai, mock_discord, mock_v2, mock_fetch,
    ):
        """Discord送信失敗 → notified_at未更新 → 次回実行でも再送しない
        (processed_fingerprints に含まれないため)
        """
        mock_ai.return_value = _fake_ai_result()
        mock_discord.return_value = False  # Discord送信失敗

        v2_result = SimpleNamespace(
            generated_count=0, saved_count=0, notified_count=0,
            filtered_count=0, no_yoy_count=0, already_exists_count=0,
            errors=[],
        )
        mock_v2.return_value = v2_result
        mock_fetch.return_value = []

        docs = [_make_doc("2001", "業績予想の修正に関するお知らせ")]

        # 初回: Discord失敗
        result1 = run_summary_pipeline(
            docs=docs, db_path=self.db_path, dry_run=False,
            target_date="2026-03-13",
            webhook_url="https://discord.example.com/hook",
            model="gpt-5.4-mini",
        )
        self.assertEqual(result1.jobs_succeeded, 1, "ジョブ自体は成功")
        self.assertEqual(result1.notifications_sent, 0, "Discord失敗で通知0")

        # DBに未通知が残っている
        conn = sqlite3.connect(self.db_path)
        unnotified = get_unnotified_summaries(conn)
        self.assertEqual(len(unnotified), 1, "未通知レコードが残る")
        conn.close()

        # 再実行: 同じdocs → fingerprint重複 → processed_fps空 → 通知0
        mock_discord.return_value = True  # 今度は成功にしても
        result2 = run_summary_pipeline(
            docs=docs, db_path=self.db_path, dry_run=False,
            target_date="2026-03-13",
            webhook_url="https://discord.example.com/hook",
            model="gpt-5.4-mini",
        )
        self.assertEqual(result2.jobs_created, 0, "重複でジョブ未作成")
        self.assertEqual(result2.notifications_sent, 0,
                         "processed_fps空で旧未通知は送信されない")


if __name__ == "__main__":
    unittest.main()
