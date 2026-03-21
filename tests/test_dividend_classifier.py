#!/usr/bin/env python3
"""test_dividend_classifier.py — 配当予想修正分類テスト"""
import unittest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.events.dividend_classifier import classify_dividend


class TestDividendClassifier(unittest.TestCase):

    def test_basic_dividend_revision(self):
        r = classify_dividend("配当予想の修正に関するお知らせ")
        self.assertTrue(r.is_target)
        self.assertEqual(r.event_type, "dividend_revision")

    def test_period_end_dividend(self):
        r = classify_dividend("期末配当予想の修正に関するお知らせ")
        self.assertTrue(r.is_target)

    def test_special_dividend(self):
        r = classify_dividend("特別配当に関するお知らせ")
        self.assertTrue(r.is_target)
        self.assertEqual(r.subtype_hint, "special_dividend")

    def test_commemorative_dividend(self):
        r = classify_dividend("記念配当に関するお知らせ")
        self.assertTrue(r.is_target)
        self.assertEqual(r.subtype_hint, "commemorative_dividend")

    def test_increase_keyword(self):
        r = classify_dividend("増配のお知らせ")
        self.assertTrue(r.is_target)
        self.assertEqual(r.subtype_hint, "increase")

    def test_combined_forecast_dividend(self):
        r = classify_dividend("業績予想及び配当予想の修正に関するお知らせ")
        self.assertTrue(r.is_target)

    def test_tanshin_not_target(self):
        r = classify_dividend("2025年3月期 決算短信")
        self.assertFalse(r.is_target)

    def test_shareholder_benefit_only_not_target(self):
        r = classify_dividend("株主優待制度の変更についてのお知らせ")
        self.assertFalse(r.is_target)


if __name__ == "__main__":
    unittest.main()
