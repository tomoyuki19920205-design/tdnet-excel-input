#!/usr/bin/env python3
"""test_event_pipeline.py — イベントパイプライン統合テスト"""
import json
import sqlite3
import unittest
from unittest.mock import patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.events.common_models import DocumentMeta, EventType
from src.events.common_storage import ensure_events_table, get_unnotified_events
from src.events.event_pipeline import process_documents


class TestEventPipeline(unittest.TestCase):

    def test_buyback_document_detected(self):
        """自社株買いタイトルの文書がイベント検知される"""
        doc = DocumentMeta(
            doc_id="test_doc_001",
            ticker="7203",
            company_name="トヨタ自動車",
            title="自己株式の取得に係る事項の決定に関するお知らせ",
            disclosure_datetime="2026-03-20 15:00",
            text_body="自己株式の取得 取得し得る株式の総数 3,000,000株 取得価額の総額 50億円 取得期間 2026年4月1日から2026年9月30日まで",
        )
        result = process_documents(
            docs=[doc],
            db_path=":memory:",
            dry_run=True,
        )
        self.assertEqual(result.processed, 1)
        self.assertGreater(result.detected, 0)

    def test_forecast_document_detected(self):
        """業績予想修正タイトルの文書がイベント検知される"""
        doc = DocumentMeta(
            doc_id="test_doc_002",
            ticker="6758",
            company_name="ソニーグループ",
            title="業績予想の上方修正に関するお知らせ",
            disclosure_datetime="2026-03-20 15:30",
            text_body="2026年3月期 通期 連結 前回発表予想(A) 売上高 10000 営業利益 1500 経常利益 1600 当期純利益 1200\n今回修正予想(B) 売上高 11000 営業利益 1800 経常利益 1900 当期純利益 1500",
        )
        result = process_documents(
            docs=[doc],
            db_path=":memory:",
            dry_run=True,
        )
        self.assertEqual(result.processed, 1)
        self.assertGreater(result.detected, 0)

    def test_dividend_document_detected(self):
        """配当予想修正タイトルの文書がイベント検知される"""
        doc = DocumentMeta(
            doc_id="test_doc_003",
            ticker="9432",
            company_name="日本電信電話",
            title="配当予想の修正に関するお知らせ",
            disclosure_datetime="2026-03-20 16:00",
            text_body="2026年3月期 期末 前回予想 50円 今回修正予想 60円 年間 100円 110円",
        )
        result = process_documents(
            docs=[doc],
            db_path=":memory:",
            dry_run=True,
        )
        self.assertEqual(result.processed, 1)
        self.assertGreater(result.detected, 0)

    def test_fingerprint_prevents_duplicate(self):
        """同一fingerprintの文書は再検知されない"""
        import tempfile, os
        db_path = os.path.join(tempfile.gettempdir(), "test_events.db")
        try:
            doc = DocumentMeta(
                doc_id="test_doc_dup",
                ticker="7203",
                company_name="トヨタ自動車",
                title="自己株式の取得に係る事項の決定に関するお知らせ",
                text_body="自己株式の取得 取得し得る株式の総数 3,000,000株 取得価額の総額 50億円 取得期間 2026年4月1日から2026年9月30日まで",
            )
            # 1回目
            r1 = process_documents(docs=[doc], db_path=db_path, dry_run=False)
            # 2回目（同一文書）
            r2 = process_documents(docs=[doc], db_path=db_path, dry_run=False)
            self.assertGreater(r1.saved, 0)
            self.assertEqual(r2.saved, 0)  # 重複なので保存されない
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_single_doc_failure_does_not_stop_pipeline(self):
        """1文書の処理失敗が全体を止めない"""
        docs = [
            DocumentMeta(
                doc_id="good_doc",
                ticker="7203",
                title="業績予想の上方修正に関するお知らせ",
                text_body="2026年3月期 連結 前回発表予想(A) 売上高 10000\n今回修正予想(B) 売上高 11000",
            ),
            DocumentMeta(
                doc_id="unknown_doc",
                ticker="9999",
                title="その他のお知らせ",
                text_body="特に内容なし",
            ),
        ]
        result = process_documents(
            docs=docs,
            db_path=":memory:",
            dry_run=True,
        )
        self.assertEqual(result.processed, 2)
        self.assertEqual(result.errors, 0)  # 分類で対象外はエラーではない

    def test_dry_run_no_save(self):
        """dry-runではDBに保存されない"""
        doc = DocumentMeta(
            doc_id="dry_doc",
            ticker="7203",
            title="自己株式の取得に係る事項の決定に関するお知らせ",
            text_body="自己株式の取得 取得し得る株式の総数 3,000,000株",
        )
        result = process_documents(
            docs=[doc],
            db_path=":memory:",
            dry_run=True,
        )
        self.assertEqual(result.saved, 0)  # dry-runは保存0


if __name__ == "__main__":
    unittest.main()
