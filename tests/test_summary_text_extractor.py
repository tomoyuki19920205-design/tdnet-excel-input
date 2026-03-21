#!/usr/bin/env python3
"""test_summary_text_extractor.py — AI要約入力テキスト構築テスト"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.events.summary_text_extractor import extract_summary_input


class TestExtractSummaryInput(unittest.TestCase):
    """AI要約用入力テキスト構築のテスト"""

    def test_title_always_included(self):
        """タイトルは常に含まれる"""
        result = extract_summary_input(title="テストタイトル")
        self.assertIn("テストタイトル", result)
        self.assertIn("【タイトル】", result)

    def test_structured_data_priority(self):
        """構造化データが優先される"""
        payload = {
            "period_label": "2026年3月期通期",
            "previous_op": 1500,
            "revised_op": 1800,
            "change_op_pct": 20.0,
            "subtype": "upward",
        }
        result = extract_summary_input(
            title="業績予想の上方修正",
            event_type="forecast_revision",
            subtype="upward",
            extracted_payload_json=json.dumps(payload),
        )
        self.assertIn("【構造化データ】", result)
        self.assertIn("業績予想修正", result)
        self.assertIn("1500→1800", result)
        self.assertIn("+20.0%", result)

    def test_buyback_structured_data(self):
        """自社株買い構造化データの変換"""
        payload = {
            "shares_limit": 3_000_000,
            "amount_limit_million_yen": 5000,
            "start_date": "2026-04-01",
            "end_date": "2026-09-30",
        }
        result = extract_summary_input(
            title="自社株買い",
            event_type="buyback",
            subtype="resolution",
            extracted_payload_json=json.dumps(payload),
        )
        self.assertIn("3,000,000株", result)
        self.assertIn("5000百万円", result)
        self.assertIn("2026-04-01", result)

    def test_dividend_structured_data(self):
        """配当修正構造化データの変換"""
        payload = {
            "fiscal_period": "2026年3月期",
            "dividend_basis": "期末",
            "previous_dividend_per_share": 50,
            "revised_dividend_per_share": 60,
            "delta_dividend_per_share": 10,
        }
        result = extract_summary_input(
            title="配当予想の修正",
            event_type="dividend_revision",
            subtype="increase",
            extracted_payload_json=json.dumps(payload),
        )
        self.assertIn("50円→60円", result)
        self.assertIn("+10円", result)

    def test_body_keyword_extraction(self):
        """本文キーワード近傍が補助として抽出される"""
        body = "前文...\n売上高は1000億円で前年比10%増加しました。営業利益も好調です。\n後文..."
        result = extract_summary_input(
            title="決算短信",
            text_body=body,
        )
        self.assertIn("売上高", result)

    def test_max_chars_limit(self):
        """最大文字数制限が守られる"""
        long_body = "テスト文 " * 1000  # 5000文字
        result = extract_summary_input(
            title="テスト",
            text_body=long_body,
            max_chars=500,
        )
        self.assertLessEqual(len(result), 500)

    def test_empty_inputs(self):
        """空入力でもエラーにならない"""
        result = extract_summary_input(title="")
        self.assertIsInstance(result, str)

    def test_invalid_json_handled(self):
        """不正なJSONでもエラーにならない"""
        result = extract_summary_input(
            title="テスト",
            extracted_payload_json="invalid json {{{",
        )
        self.assertIn("テスト", result)
        self.assertNotIn("構造化データ", result)  # パース失敗なので含まれない

    def test_structured_data_before_body(self):
        """構造化データがある場合、本文は予算内でのみ追加される"""
        payload = {
            "period_label": "2026年3月期通期",
            "revised_sales": 10000,
            "revised_op": 1800,
        }
        result = extract_summary_input(
            title="業績予想の上方修正",
            text_body="セグメント別業績..." + "x" * 500,
            event_type="forecast_revision",
            subtype="upward",
            extracted_payload_json=json.dumps(payload),
            max_chars=2000,
        )
        # 構造化データは必ず含まれる
        self.assertIn("【構造化データ】", result)


if __name__ == "__main__":
    unittest.main()
