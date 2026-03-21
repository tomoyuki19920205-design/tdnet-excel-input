#!/usr/bin/env python3
"""test_summary_prioritizer.py — 優先度分類テスト"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.events.summary_prioritizer import classify_priority
from src.events.summary_models import SummaryPriority
from src.events.common_models import EventType


class TestClassifyPriority(unittest.TestCase):
    """優先度分類のテスト"""

    # ============================================================
    # HIGH 判定: event_type + subtype
    # ============================================================
    def test_buyback_resolution_is_high(self):
        result = classify_priority("dummy", event_type=EventType.BUYBACK, subtype="resolution")
        self.assertEqual(result, SummaryPriority.HIGH)

    def test_forecast_upward_is_high(self):
        result = classify_priority("dummy", event_type=EventType.FORECAST_REVISION, subtype="upward")
        self.assertEqual(result, SummaryPriority.HIGH)

    def test_forecast_downward_is_high(self):
        result = classify_priority("dummy", event_type=EventType.FORECAST_REVISION, subtype="downward")
        self.assertEqual(result, SummaryPriority.HIGH)

    def test_dividend_increase_is_high(self):
        result = classify_priority("dummy", event_type=EventType.DIVIDEND_REVISION, subtype="increase")
        self.assertEqual(result, SummaryPriority.HIGH)

    # ============================================================
    # NORMAL 判定: event_type + subtype
    # ============================================================
    def test_forecast_difference_is_normal(self):
        result = classify_priority("dummy", event_type=EventType.FORECAST_REVISION, subtype="difference")
        self.assertEqual(result, SummaryPriority.NORMAL)

    def test_dividend_decrease_is_normal(self):
        result = classify_priority("dummy", event_type=EventType.DIVIDEND_REVISION, subtype="decrease")
        self.assertEqual(result, SummaryPriority.NORMAL)

    def test_buyback_status_is_normal(self):
        result = classify_priority("dummy", event_type=EventType.BUYBACK, subtype="status")
        self.assertEqual(result, SummaryPriority.NORMAL)

    def test_unknown_subtype_with_event_type_is_normal(self):
        """event_type があるが不明な subtype → NORMAL（安全側に倒す）"""
        result = classify_priority("dummy", event_type=EventType.BUYBACK, subtype="unknown_sub")
        self.assertEqual(result, SummaryPriority.NORMAL)

    # ============================================================
    # HIGH 判定: タイトルフォールバック
    # ============================================================
    def test_tanshin_title_is_high(self):
        result = classify_priority("2026年3月期 決算短信〔日本基準〕（連結）")
        self.assertEqual(result, SummaryPriority.HIGH)

    def test_quarterly_title_is_high(self):
        result = classify_priority("2026年3月期 第3四半期決算短信〔日本基準〕（連結）")
        self.assertEqual(result, SummaryPriority.HIGH)

    def test_upward_revision_title_is_high(self):
        result = classify_priority("業績予想の上方修正に関するお知らせ")
        self.assertEqual(result, SummaryPriority.HIGH)

    def test_downward_revision_title_is_high(self):
        result = classify_priority("通期業績予想の下方修正に関するお知らせ")
        self.assertEqual(result, SummaryPriority.HIGH)

    def test_dividend_increase_title_is_high(self):
        result = classify_priority("増配および記念配当に関するお知らせ")
        self.assertEqual(result, SummaryPriority.HIGH)

    def test_buyback_title_is_high(self):
        result = classify_priority("自己株式の取得に係る事項の決定に関するお知らせ")
        self.assertEqual(result, SummaryPriority.HIGH)

    # ============================================================
    # NORMAL 判定: タイトルフォールバック
    # ============================================================
    def test_forecast_difference_title_is_normal(self):
        result = classify_priority("業績予想と実績値との差異に関するお知らせ")
        self.assertEqual(result, SummaryPriority.NORMAL)

    def test_dividend_revision_title_is_normal(self):
        result = classify_priority("配当予想の修正に関するお知らせ")
        self.assertEqual(result, SummaryPriority.NORMAL)

    # ============================================================
    # LOW 判定: 該当なし
    # ============================================================
    def test_other_title_is_low(self):
        result = classify_priority("代表取締役の異動に関するお知らせ")
        self.assertEqual(result, SummaryPriority.LOW)

    def test_empty_title_is_low(self):
        result = classify_priority("")
        self.assertEqual(result, SummaryPriority.LOW)

    # ============================================================
    # event_type 優先のテスト
    # ============================================================
    def test_event_type_overrides_title(self):
        """event_type があればタイトルは無視される"""
        # タイトルは LOW 相当だが event_type が buyback/resolution → HIGH
        result = classify_priority(
            "その他のお知らせ",
            event_type=EventType.BUYBACK,
            subtype="resolution",
        )
        self.assertEqual(result, SummaryPriority.HIGH)


if __name__ == "__main__":
    unittest.main()
