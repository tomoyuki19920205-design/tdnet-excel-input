#!/usr/bin/env python3
"""test_forecast_extractor.py — 業績予想修正抽出テスト"""
import json
import unittest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.events.forecast_extractor import (
    _parse_cell_value,
    _detect_period,
    _detect_basis,
    _detect_unit,
    _calc_pct,
    _determine_subtype,
    _calc_importance,
    extract_forecast_revision,
)
from src.events.forecast_models import ForecastRevisionEvent
from src.events.common_notify import format_forecast_msg
from src.events.common_models import EventRecord, EventType


# ============================================================
# 1. 正規化テスト
# ============================================================
class TestCellValueParsing(unittest.TestCase):

    def test_triangle_negative(self):
        self.assertEqual(_parse_cell_value("△123"), -123.0)

    def test_triangle_with_comma(self):
        self.assertEqual(_parse_cell_value("△1,234"), -1234.0)

    def test_filled_triangle(self):
        self.assertEqual(_parse_cell_value("▲567"), -567.0)

    def test_minus_sign(self):
        self.assertEqual(_parse_cell_value("-456"), -456.0)

    def test_normal_with_comma(self):
        self.assertEqual(_parse_cell_value("1,234"), 1234.0)

    def test_large_number(self):
        self.assertEqual(_parse_cell_value("12,345,678"), 12345678.0)

    def test_decimal(self):
        self.assertEqual(_parse_cell_value("123.4"), 123.4)

    def test_dash_none(self):
        self.assertIsNone(_parse_cell_value("―"))

    def test_en_dash_none(self):
        self.assertIsNone(_parse_cell_value("–"))

    def test_em_dash_none(self):
        self.assertIsNone(_parse_cell_value("—"))

    def test_hyphen_none(self):
        self.assertIsNone(_parse_cell_value("-"))

    def test_empty_none(self):
        self.assertIsNone(_parse_cell_value(""))

    def test_percentage(self):
        self.assertEqual(_parse_cell_value("123.4%"), 123.4)

    def test_zero(self):
        self.assertEqual(_parse_cell_value("0"), 0.0)

    def test_annotation_stripped(self):
        self.assertEqual(_parse_cell_value("*1,234"), 1234.0)


# ============================================================
# 2. 行マッピングテスト
# ============================================================
class TestPeriodDetection(unittest.TestCase):

    def test_full_year(self):
        self.assertEqual(_detect_period("2026年3月期 通期"), "2026年3月期 通期")

    def test_quarter(self):
        self.assertEqual(_detect_period("2026年3月期 第2四半期"), "2026年3月期 第2四半期")

    def test_quarter_cumulative(self):
        result = _detect_period("2026年3月期 第3四半期累計")
        self.assertIn("2026年3月期", result)
        self.assertIn("第3四半期", result)

    def test_fy_only(self):
        self.assertEqual(_detect_period("2026年3月期"), "2026年3月期")

    def test_embedded(self):
        result = _detect_period("2026年3月期連結会計年度における特別損失の計上、通期業績予想")
        self.assertIn("2026年3月期", result)


class TestBasisDetection(unittest.TestCase):

    def test_consolidated(self):
        self.assertEqual(_detect_basis("連結業績予想"), "連結")

    def test_standalone(self):
        self.assertEqual(_detect_basis("個別業績予想"), "個別")

    def test_none(self):
        self.assertEqual(_detect_basis("業績予想"), "")


class TestUnitDetection(unittest.TestCase):

    def test_million_yen(self):
        self.assertEqual(_detect_unit("（単位：百万円）"), "百万円")

    def test_thousand_yen(self):
        self.assertEqual(_detect_unit("（単位：千円）"), "千円")

    def test_billion_yen(self):
        self.assertEqual(_detect_unit("（単位：億円）"), "億円")

    def test_parenthesized(self):
        self.assertEqual(_detect_unit("売上高（百万円）"), "百万円")


# ============================================================
# 3. 表抽出テスト
# ============================================================
class TestTableExtraction(unittest.TestCase):

    def _make_standard_table(self, *, unit="百万円", period="2026年3月期 通期"):
        """標準的な業績予想修正表テキストを生成"""
        return f"""
{period}の連結業績予想の修正に関するお知らせ
（単位：{unit}）

                        売上高    営業利益    経常利益    親会社株主に帰属する当期純利益    1株当たり当期純利益
前回発表予想(A)          10,000    1,500       1,600       1,200                           120.50
今回修正予想(B)          11,000    1,800       1,900       1,500                           150.30
増減額(B-A)                1,000      300         300         300                            29.80
増減率(%)                    10.0      20.0        18.8        25.0                            ―
"""

    def test_standard_table_extraction(self):
        text = self._make_standard_table()
        ev = extract_forecast_revision(text, "業績予想の修正に関するお知らせ")
        self.assertEqual(ev.previous_sales, 10000.0)
        self.assertEqual(ev.revised_sales, 11000.0)
        self.assertEqual(ev.previous_op, 1500.0)
        self.assertEqual(ev.revised_op, 1800.0)
        self.assertEqual(ev.previous_net_income, 1200.0)
        self.assertEqual(ev.revised_net_income, 1500.0)
        self.assertGreater(ev.extracted_metrics_count, 0)
        self.assertEqual(ev.subtype, "upward")
        self.assertEqual(ev.extraction_source, "pdf_text")

    def test_difference_table(self):
        text = """
2026年3月期 通期連結業績予想と実績値との差異に関するお知らせ
（単位：百万円）

                        売上高    営業利益    経常利益    当期純利益
前回発表予想(A)          10,000    1,500       1,600       1,200
実績(B)                 11,500    1,900       2,000       1,600
増減額(B-A)                1,500      400         400         400
増減率(%)                    15.0      26.7        25.0        33.3
"""
        ev = extract_forecast_revision(text, "業績予想と実績値との差異", is_difference=True)
        self.assertTrue(ev.is_difference_disclosure)
        self.assertEqual(ev.subtype, "difference")
        self.assertEqual(ev.previous_sales, 10000.0)
        self.assertEqual(ev.revised_sales, 11500.0)

    def test_downward_revision(self):
        text = """
2026年3月期 通期連結業績予想の修正に関するお知らせ
（単位：百万円）

                        売上高    営業利益    経常利益    当期純利益
前回発表予想(A)          10,000    1,500       1,600       1,200
今回修正予想(B)           9,000    1,000       1,100         800
増減額(B-A)              △1,000    △500       △500        △400
増減率(%)                  △10.0    △33.3      △31.3       △33.3
"""
        ev = extract_forecast_revision(text, "業績予想の下方修正に関するお知らせ")
        self.assertEqual(ev.subtype, "downward")
        self.assertEqual(ev.previous_sales, 10000.0)
        self.assertEqual(ev.revised_sales, 9000.0)
        self.assertEqual(ev.revised_op, 1000.0)

    def test_consolidated_priority(self):
        text = """
2026年3月期 通期連結業績予想の修正に関するお知らせ
（連結）
（単位：百万円）

                        売上高    営業利益
前回発表予想(A)          10,000    1,500
今回修正予想(B)          11,000    1,800

（個別）
（単位：百万円）

                        売上高    営業利益
前回発表予想(A)           5,000      800
今回修正予想(B)           5,500      900
"""
        ev = extract_forecast_revision(text, "業績予想の修正")
        # 連結が優先されるべき
        self.assertEqual(ev.basis, "連結")

    def test_period_label_extraction(self):
        text = self._make_standard_table(period="2026年9月期 第2四半期累計")
        ev = extract_forecast_revision(text, "業績予想の修正")
        self.assertIn("2026年9月期", ev.period_label)

    def test_thousand_yen_unit(self):
        text = self._make_standard_table(unit="千円")
        ev = extract_forecast_revision(text, "業績予想の修正")
        # 千円→百万円変換: 10,000千円 = 10百万円
        if ev.previous_sales is not None:
            self.assertAlmostEqual(ev.previous_sales, 10.0, places=1)

    def test_fallback_on_empty_text(self):
        """テキストが空の場合はフォールバック"""
        ev = extract_forecast_revision("", "業績予想の修正に関するお知らせ")
        self.assertEqual(ev.extraction_source, "fallback")
        self.assertEqual(ev.subtype, "undecided")


# ============================================================
# 4. subtype テスト
# ============================================================
class TestSubtypeDetermination(unittest.TestCase):

    def test_upward(self):
        ev = ForecastRevisionEvent(
            previous_op=1000, revised_op=1500,
            change_op_pct=50.0,
        )
        result = _determine_subtype(ev)
        self.assertEqual(result, "upward")

    def test_downward(self):
        ev = ForecastRevisionEvent(
            previous_op=1500, revised_op=1000,
            change_op_pct=-33.3,
        )
        result = _determine_subtype(ev)
        self.assertEqual(result, "downward")

    def test_difference(self):
        ev = ForecastRevisionEvent(is_difference_disclosure=True)
        result = _determine_subtype(ev, is_difference=True)
        self.assertEqual(result, "difference")

    def test_neutral(self):
        ev = ForecastRevisionEvent(
            change_op_pct=20.0, change_net_income_pct=-15.0,
        )
        result = _determine_subtype(ev)
        self.assertEqual(result, "neutral")

    def test_undecided(self):
        ev = ForecastRevisionEvent()
        result = _determine_subtype(ev)
        self.assertEqual(result, "undecided")

    def test_turnaround_positive(self):
        """赤字→黒字転換"""
        ev = ForecastRevisionEvent(
            previous_net_income=-500, revised_net_income=200,
            change_net_income_pct=140.0,
        )
        result = _determine_subtype(ev)
        self.assertEqual(result, "upward")

    def test_turnaround_negative(self):
        """黒字→赤字転落"""
        ev = ForecastRevisionEvent(
            previous_net_income=500, revised_net_income=-200,
            change_net_income_pct=-140.0,
        )
        result = _determine_subtype(ev)
        self.assertEqual(result, "downward")


# ============================================================
# 5. importance テスト
# ============================================================
class TestImportanceCalculation(unittest.TestCase):

    def test_turnaround_positive_high(self):
        ev = ForecastRevisionEvent(
            previous_net_income=-500, revised_net_income=200,
            subtype="upward",
        )
        self.assertEqual(_calc_importance(ev), 95)

    def test_turnaround_negative(self):
        ev = ForecastRevisionEvent(
            previous_op=500, revised_op=-200,
            subtype="downward",
        )
        self.assertEqual(_calc_importance(ev), 85)

    def test_large_change(self):
        ev = ForecastRevisionEvent(
            change_op_pct=60.0, subtype="upward",
        )
        self.assertEqual(_calc_importance(ev), 90)

    def test_undecided_low(self):
        ev = ForecastRevisionEvent(subtype="undecided")
        self.assertEqual(_calc_importance(ev), 50)


# ============================================================
# 6. 通知文面テスト
# ============================================================
class TestNotificationFormat(unittest.TestCase):

    def test_with_metrics(self):
        payload = {
            "period_label": "2026年3月期 通期",
            "previous_op": 1500.0,
            "revised_op": 1800.0,
            "change_op_pct": 20.0,
            "previous_net_income": 1200.0,
            "revised_net_income": 1500.0,
            "change_net_income_pct": 25.0,
        }
        ev = EventRecord(
            event_type=EventType.FORECAST_REVISION,
            subtype="upward",
            ticker="7203",
            company_name="トヨタ自動車",
            title="業績予想の上方修正に関するお知らせ",
            extracted_payload_json=json.dumps(payload, ensure_ascii=False),
        )
        msg = format_forecast_msg(ev)
        self.assertIn("🔺", msg)
        self.assertIn("上方修正", msg)
        self.assertIn("トヨタ自動車", msg)
        self.assertIn("純利益", msg)
        self.assertIn("営業利益", msg)
        self.assertIn("→", msg)
        self.assertIn("+20.0%", msg)

    def test_without_metrics_fallback(self):
        payload = {
            "period_label": "2026年3月期 通期",
        }
        ev = EventRecord(
            event_type=EventType.FORECAST_REVISION,
            subtype="undecided",
            ticker="7203",
            company_name="トヨタ自動車",
            title="業績予想の修正に関するお知らせ",
            extracted_payload_json=json.dumps(payload, ensure_ascii=False),
        )
        msg = format_forecast_msg(ev)
        self.assertIn("予想修正", msg)
        self.assertIn("2026年3月期", msg)
        # 数値行は含まれない
        self.assertNotIn("→", msg)

    def test_max_two_metrics(self):
        payload = {
            "previous_sales": 10000.0, "revised_sales": 11000.0, "change_sales_pct": 10.0,
            "previous_op": 1500.0, "revised_op": 1800.0, "change_op_pct": 20.0,
            "previous_ordinary": 1600.0, "revised_ordinary": 1900.0, "change_ordinary_pct": 18.8,
            "previous_net_income": 1200.0, "revised_net_income": 1500.0, "change_net_income_pct": 25.0,
        }
        ev = EventRecord(
            event_type=EventType.FORECAST_REVISION,
            subtype="upward",
            ticker="7203",
            company_name="トヨタ自動車",
            title="業績予想の修正に関するお知らせ",
            extracted_payload_json=json.dumps(payload, ensure_ascii=False),
        )
        msg = format_forecast_msg(ev)
        arrow_count = msg.count("→")
        self.assertLessEqual(arrow_count, 2)


if __name__ == "__main__":
    unittest.main()
