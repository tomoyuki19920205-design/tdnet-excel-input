#!/usr/bin/env python3
"""test_forecast_classifier.py — 業績予想修正分類テスト"""
import unittest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.events.forecast_classifier import classify_forecast


class TestForecastClassifier(unittest.TestCase):

    def test_basic_revision(self):
        r = classify_forecast("業績予想の修正に関するお知らせ")
        self.assertTrue(r.is_target)
        self.assertEqual(r.event_type, "forecast_revision")

    def test_consolidated_revision(self):
        r = classify_forecast("通期連結業績予想の修正に関するお知らせ")
        self.assertTrue(r.is_target)

    def test_upward_revision(self):
        r = classify_forecast("業績予想の上方修正に関するお知らせ")
        self.assertTrue(r.is_target)
        self.assertEqual(r.subtype_hint, "upward")

    def test_downward_revision(self):
        r = classify_forecast("業績予想の下方修正に関するお知らせ")
        self.assertTrue(r.is_target)
        self.assertEqual(r.subtype_hint, "downward")

    def test_difference_disclosure(self):
        r = classify_forecast("業績予想と実績との差異に関するお知らせ")
        self.assertTrue(r.is_target)
        self.assertEqual(r.subtype_hint, "difference")

    def test_combined_forecast_dividend(self):
        r = classify_forecast("業績予想及び配当予想の修正に関するお知らせ")
        self.assertTrue(r.is_target)

    def test_dividend_only_not_target(self):
        r = classify_forecast("配当予想の修正に関するお知らせ")
        self.assertFalse(r.is_target)

    def test_financial_statement_not_target(self):
        r = classify_forecast("2025年3月期 第2四半期決算短信")
        self.assertFalse(r.is_target)

    def test_ir_material_not_target(self):
        r = classify_forecast("決算説明会資料")
        self.assertFalse(r.is_target)


if __name__ == "__main__":
    unittest.main()
