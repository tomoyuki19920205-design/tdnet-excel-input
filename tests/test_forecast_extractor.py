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
    _subtype_from_title,
    _calc_importance,
    _metric_direction,
    _units_compatible,
    _convert_unit_value,
    _sanitize_metrics,
    _normalize_label,
    _match_metric_label,
    _classify_table_context,
    _infer_target_type,
    _classify_text_line,
    _find_text_sections,
    _select_target_sections,
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


# ============================================================
# 7. タイトルフォールバックテスト (_subtype_from_title)
# ============================================================
class TestSubtypeFromTitle(unittest.TestCase):
    """数値未取得時のタイトルキーワードフォールバック"""

    def test_upward_from_title(self):
        self.assertEqual(_subtype_from_title("業績予想の上方修正に関するお知らせ"), "upward")

    def test_downward_from_title(self):
        self.assertEqual(_subtype_from_title("通期業績予想の下方修正に関するお知らせ"), "downward")

    def test_difference_from_title(self):
        self.assertEqual(_subtype_from_title("業績予想と実績値との差異に関するお知らせ"), "difference")

    def test_increase_from_title(self):
        """「増配」キーワードが forecast subtype の upward に寄与"""
        self.assertEqual(_subtype_from_title("業績予想の修正（増配）に関するお知らせ"), "upward")

    def test_decrease_from_title(self):
        """「減配」キーワード"""
        self.assertEqual(_subtype_from_title("業績予想の修正（減配）に関するお知らせ"), "downward")

    def test_no_keywords_undecided(self):
        """方向キーワードなし → undecided"""
        self.assertEqual(_subtype_from_title("業績予想の修正に関するお知らせ"), "undecided")

    def test_empty_title_undecided(self):
        self.assertEqual(_subtype_from_title(""), "undecided")


# ============================================================
# 8. _determine_subtype タイトルフォールバック統合テスト
# ============================================================
class TestDetermineSubtypeWithTitleFallback(unittest.TestCase):
    """_determine_subtype に title を渡した場合の挙動"""

    def test_numeric_upward_ignores_title(self):
        """数値判定が優先される（titleの内容に関わらず）"""
        ev = ForecastRevisionEvent(change_op_pct=30.0)
        result = _determine_subtype(ev, title="業績予想の下方修正に関するお知らせ")
        self.assertEqual(result, "upward")

    def test_no_numeric_uses_title_upward(self):
        """数値なし → タイトルの「上方修正」で upward"""
        ev = ForecastRevisionEvent()
        result = _determine_subtype(ev, title="通期業績予想の上方修正に関するお知らせ")
        self.assertEqual(result, "upward")

    def test_no_numeric_uses_title_downward(self):
        """数値なし → タイトルの「下方修正」で downward"""
        ev = ForecastRevisionEvent()
        result = _determine_subtype(ev, title="業績予想の下方修正に関するお知らせ")
        self.assertEqual(result, "downward")

    def test_no_numeric_no_keyword_undecided(self):
        """数値もタイトルキーワードもなし → undecided"""
        ev = ForecastRevisionEvent()
        result = _determine_subtype(ev, title="業績予想の修正に関するお知らせ")
        self.assertEqual(result, "undecided")


# ============================================================
# 9. extract_forecast_revision 統合テスト（タイトルフォールバック）
# ============================================================
class TestExtractForecastRevisionTitleFallback(unittest.TestCase):
    """空テキスト + タイトルキーワードの組み合わせテスト"""

    def test_empty_text_with_upward_title(self):
        ev = extract_forecast_revision("", "業績予想の上方修正に関するお知らせ")
        self.assertEqual(ev.subtype, "upward")
        self.assertEqual(ev.extraction_source, "fallback")

    def test_empty_text_with_downward_title(self):
        ev = extract_forecast_revision("", "通期連結業績予想の下方修正に関するお知らせ")
        self.assertEqual(ev.subtype, "downward")

    def test_empty_text_with_difference_title(self):
        ev = extract_forecast_revision("", "業績予想と実績値との差異に関するお知らせ")
        self.assertEqual(ev.subtype, "difference")

    def test_empty_text_plain_title_stays_undecided(self):
        """方向キーワードなし → undecided のまま"""
        ev = extract_forecast_revision("", "業績予想の修正に関するお知らせ")
        self.assertEqual(ev.subtype, "undecided")


# ============================================================
# 10. 回帰テスト（既存 subtype 判定を壊していないことの確認）
# ============================================================
class TestSubtypeRegression(unittest.TestCase):
    """既存の数値ベース判定が破壊されていないことを確認"""

    def test_upward_with_data(self):
        ev = ForecastRevisionEvent(
            change_op_pct=25.0, change_net_income_pct=30.0,
        )
        self.assertEqual(_determine_subtype(ev), "upward")

    def test_downward_with_data(self):
        ev = ForecastRevisionEvent(
            change_op_pct=-20.0, change_net_income_pct=-15.0,
        )
        self.assertEqual(_determine_subtype(ev), "downward")

    def test_neutral_mixed(self):
        ev = ForecastRevisionEvent(
            change_op_pct=20.0, change_net_income_pct=-10.0,
        )
        self.assertEqual(_determine_subtype(ev), "neutral")

    def test_difference_flag(self):
        ev = ForecastRevisionEvent()
        self.assertEqual(_determine_subtype(ev, is_difference=True), "difference")

    def test_difference_attribute(self):
        ev = ForecastRevisionEvent(is_difference_disclosure=True)
        self.assertEqual(_determine_subtype(ev), "difference")

    def test_turnaround_to_positive(self):
        ev = ForecastRevisionEvent(
            previous_net_income=-500, revised_net_income=200,
            change_net_income_pct=140.0,
        )
        self.assertEqual(_determine_subtype(ev), "upward")

    def test_turnaround_to_negative(self):
        ev = ForecastRevisionEvent(
            previous_net_income=500, revised_net_income=-200,
            change_net_income_pct=-140.0,
        )
        self.assertEqual(_determine_subtype(ev), "downward")

    def test_sales_only_upward(self):
        ev = ForecastRevisionEvent(change_sales_pct=15.0)
        self.assertEqual(_determine_subtype(ev), "upward")

    def test_sales_only_downward(self):
        ev = ForecastRevisionEvent(change_sales_pct=-10.0)
        self.assertEqual(_determine_subtype(ev), "downward")

    def test_small_change_upward(self):
        # Phase 1.6: ±5%以内はflat → neutral（保守的判定）
        ev = ForecastRevisionEvent(change_op_pct=3.0)
        self.assertEqual(_determine_subtype(ev), "neutral")

    def test_small_change_downward(self):
        # Phase 1.6: ±5%以内はflat → neutral（保守的判定）
        ev = ForecastRevisionEvent(change_op_pct=-3.0)
        self.assertEqual(_determine_subtype(ev), "neutral")


# ============================================================
# 11. Phase 0: PDFテーブル構造抽出テスト
# ============================================================
class TestPdfTableExtraction(unittest.TestCase):
    """pdfplumber extract_tables() 形式のデータを使った Phase 0 テスト"""

    def test_horizontal_table_with_labels(self):
        """A. 行方向テーブル + ラベル正常 → previous + revised"""
        tables = [[
            [None, "売上高", "営業利益", "経常利益", "当期純利益", "1株当たり当期純利益"],
            ["前回発表予想(A)", "10,000", "1,500", "1,600", "1,200", "120.50"],
            ["今回修正予想(B)", "11,000", "1,800", "1,900", "1,500", "150.30"],
            ["増減額(B-A)", "1,000", "300", "300", "300", "29.80"],
            ["増減率(%)", "10.0", "20.0", "18.8", "25.0", "―"],
        ]]
        text = "2026年3月期 通期連結業績予想の修正に関するお知らせ\n（単位：百万円）"
        ev = extract_forecast_revision(text, "業績予想の修正に関するお知らせ", tables=tables)
        self.assertEqual(ev.previous_sales, 10000.0)
        self.assertEqual(ev.revised_sales, 11000.0)
        self.assertEqual(ev.previous_op, 1500.0)
        self.assertEqual(ev.revised_op, 1800.0)
        self.assertEqual(ev.previous_net_income, 1200.0)
        self.assertEqual(ev.revised_net_income, 1500.0)
        self.assertEqual(ev.previous_eps, 120.50)
        self.assertEqual(ev.revised_eps, 150.30)
        self.assertEqual(ev.extraction_source, "pdf_table")
        self.assertEqual(ev.subtype, "upward")

    def test_vertical_table_with_labels(self):
        """B. 列方向テーブル（指標が行、previous/revisedが列）"""
        tables = [[
            [None, "前回発表予想", "今回修正予想", "増減額", "増減率(%)"],
            ["売上高", "10,000", "11,000", "1,000", "10.0"],
            ["営業利益", "1,500", "1,800", "300", "20.0"],
            ["経常利益", "1,600", "1,900", "300", "18.8"],
            ["当期純利益", "1,200", "1,500", "300", "25.0"],
        ]]
        text = "2026年3月期 通期連結業績予想の修正に関するお知らせ\n（単位：百万円）"
        ev = extract_forecast_revision(text, "業績予想の修正", tables=tables)
        self.assertEqual(ev.previous_sales, 10000.0)
        self.assertEqual(ev.revised_sales, 11000.0)
        self.assertEqual(ev.previous_op, 1500.0)
        self.assertEqual(ev.revised_op, 1800.0)
        self.assertEqual(ev.extraction_source, "pdf_table")

    def test_garbled_labels_numeric_fallback(self):
        """C. ラベル文字化け → 数値2行パターンで previous/revised 推定"""
        tables = [[
            [None, "売上高", "営業利益", "経常利益", "当期純利益"],
            ["????(A)", "10,000", "1,500", "1,600", "1,200"],
            ["????(B)", "11,000", "1,800", "1,900", "1,500"],
            ["????(B-A)", "1,000", "300", "300", "300"],
        ]]
        text = "2026年3月期 通期連結業績予想の修正に関するお知らせ\n（単位：百万円）"
        ev = extract_forecast_revision(text, "業績予想の修正", tables=tables)
        # ラベルなしでも数値パターンで previous=上、revised=下
        self.assertEqual(ev.previous_sales, 10000.0)
        self.assertEqual(ev.revised_sales, 11000.0)
        self.assertIsNotNone(ev.previous_op)
        self.assertIsNotNone(ev.revised_op)

    def test_revised_only_single_row(self):
        """D. 数値1行のみ → revised 扱い"""
        tables = [[
            [None, "売上高", "営業利益", "経常利益", "当期純利益", "1株当たり当期純利益"],
            ["修正予想", "11,000", "1,800", "1,900", "1,500", "150.30"],
        ]]
        text = "2026年3月期 通期連結業績予想の修正に関するお知らせ\n（単位：百万円）"
        ev = extract_forecast_revision(text, "業績予想の修正", tables=tables)
        self.assertEqual(ev.revised_sales, 11000.0)
        self.assertEqual(ev.revised_op, 1800.0)
        # previous は None
        self.assertIsNone(ev.previous_sales)

    def test_no_tables_fallback_to_text(self):
        """E. テーブルなし → 既存テキスト抽出ロジックにフォールバック"""
        text = """
2026年3月期 通期連結業績予想の修正に関するお知らせ
（単位：百万円）

                        売上高    営業利益    経常利益    当期純利益
前回発表予想(A)          10,000    1,500       1,600       1,200
今回修正予想(B)          11,000    1,800       1,900       1,500
"""
        ev = extract_forecast_revision(text, "業績予想の修正", tables=[])
        self.assertEqual(ev.previous_sales, 10000.0)
        self.assertEqual(ev.revised_sales, 11000.0)
        self.assertEqual(ev.extraction_source, "pdf_text")

    def test_difference_table(self):
        """F. 差異開示テーブル：「実績」ラベル → revised"""
        tables = [[
            [None, "売上高", "営業利益", "経常利益", "当期純利益"],
            ["前回発表予想(A)", "10,000", "1,500", "1,600", "1,200"],
            ["実績(B)", "11,500", "1,900", "2,000", "1,600"],
            ["増減額(B-A)", "1,500", "400", "400", "400"],
        ]]
        text = "2026年3月期 通期連結業績予想と実績値との差異\n（単位：百万円）"
        ev = extract_forecast_revision(text, "業績予想と実績値との差異", is_difference=True, tables=tables)
        self.assertEqual(ev.previous_sales, 10000.0)
        self.assertEqual(ev.revised_sales, 11500.0)
        self.assertEqual(ev.subtype, "difference")

    def test_unit_conversion_thousand_yen(self):
        """G. 単位換算テスト（千円 → 百万円）"""
        tables = [[
            [None, "売上高", "営業利益", "経常利益", "当期純利益"],
            ["前回発表予想", "10,000,000", "1,500,000", "1,600,000", "1,200,000"],
            ["今回修正予想", "11,000,000", "1,800,000", "1,900,000", "1,500,000"],
        ]]
        text = "2026年3月期 通期　（単位：千円）"
        ev = extract_forecast_revision(text, "業績予想の修正", tables=tables)
        # 10,000,000千円 = 10,000百万円
        self.assertAlmostEqual(ev.previous_sales, 10000.0, places=0)
        self.assertAlmostEqual(ev.revised_sales, 11000.0, places=0)

    def test_unrelated_table_excluded(self):
        """H. 関係ない表（株主構成等）を誤採用しない"""
        unrelated_table = [
            ["株主名", "持株数", "持株比率"],
            ["株主A", "1,000,000", "10.0%"],
            ["株主B", "500,000", "5.0%"],
            ["株主C", "300,000", "3.0%"],
        ]
        forecast_table = [
            [None, "売上高", "営業利益", "経常利益", "当期純利益"],
            ["前回発表予想", "10,000", "1,500", "1,600", "1,200"],
            ["今回修正予想", "11,000", "1,800", "1,900", "1,500"],
        ]
        tables = [unrelated_table, forecast_table]
        text = "2026年3月期 通期連結業績予想の修正に関するお知らせ\n（単位：百万円）"
        ev = extract_forecast_revision(text, "業績予想の修正", tables=tables)
        # 株主構成テーブルを無視し、業績テーブルを正しく取得
        self.assertEqual(ev.previous_sales, 10000.0)
        self.assertEqual(ev.revised_sales, 11000.0)
        self.assertEqual(ev.extraction_source, "pdf_table")


# ============================================================
# 12. Phase 1: 連結/個別フィルタテスト
# ============================================================
class TestTableClassification(unittest.TestCase):
    """テーブル分類の単体テスト"""

    def test_consolidated_keyword(self):
        table = [
            ["連結業績予想", "売上高", "営業利益", "経常利益", "当期純利益"],
            ["前回", "10,000", "1,500", "1,600", "1,200"],
        ]
        self.assertEqual(_classify_table_context(table), "consolidated")

    def test_non_consolidated_keyword(self):
        table = [
            ["個別業績予想", "売上高", "営業利益", "経常利益", "当期純利益"],
            ["前回", "10,000", "1,500", "1,600", "1,200"],
        ]
        self.assertEqual(_classify_table_context(table), "non_consolidated")

    def test_standalone_keyword(self):
        table = [
            ["単体", "売上高", "営業利益", "経常利益", "当期純利益"],
            ["前回", "10,000", "1,500", "1,600", "1,200"],
        ]
        self.assertEqual(_classify_table_context(table), "non_consolidated")

    def test_unknown_no_keyword(self):
        table = [
            [None, "売上高", "営業利益", "経常利益", "当期純利益"],
            ["前回", "10,000", "1,500", "1,600", "1,200"],
        ]
        self.assertEqual(_classify_table_context(table), "unknown")

    def test_ambiguous_both_keywords(self):
        table = [
            ["連結 個別", "売上高", "営業利益", "経常利益", "当期純利益"],
            ["前回", "10,000", "1,500", "1,600", "1,200"],
        ]
        self.assertEqual(_classify_table_context(table), "unknown")


class TestTargetTypeInference(unittest.TestCase):
    """ドキュメントtarget type推定テスト"""

    def test_title_consolidated(self):
        result = _infer_target_type(
            "連結業績予想の修正に関するお知らせ", "", []
        )
        self.assertEqual(result, "consolidated")

    def test_title_non_consolidated(self):
        result = _infer_target_type(
            "個別業績予想の修正に関するお知らせ", "", []
        )
        self.assertEqual(result, "non_consolidated")

    def test_text_only_consolidated(self):
        result = _infer_target_type(
            "業績予想の修正",
            "連結業績予想について修正を行います",
            []
        )
        self.assertEqual(result, "consolidated")

    def test_table_majority(self):
        result = _infer_target_type(
            "業績予想の修正", "",
            ["consolidated", "consolidated", "unknown"]
        )
        self.assertEqual(result, "consolidated")

    def test_all_unknown(self):
        result = _infer_target_type(
            "業績予想の修正", "",
            ["unknown", "unknown"]
        )
        self.assertEqual(result, "unknown")


class TestConsolidatedNonConsolidatedGuard(unittest.TestCase):
    """連結/個別混在ガードテスト"""

    def test_mixed_tables_uses_consolidated_only(self):
        """連結テーブル + 個別テーブルがある場合にタイトルで連結を選択"""
        consolidated_table = [
            ["連結", "売上高", "営業利益", "経常利益", "当期純利益", "1株当たり当期純利益"],
            ["前回発表予想", "10,000", "1,500", "1,600", "1,200", "120.00"],
            ["今回修正予想", "11,000", "1,800", "1,900", "1,500", "150.00"],
        ]
        non_consolidated_table = [
            ["個別", "売上高", "営業利益", "経常利益", "当期純利益", "1株当たり当期純利益"],
            ["前回発表予想", "5,000", "800", "900", "600", "60.00"],
            ["今回修正予想", "5,500", "1,000", "1,100", "700", "70.00"],
        ]
        tables = [consolidated_table, non_consolidated_table]
        text = "（単位：百万円）"
        ev = extract_forecast_revision(
            text, "連結業績予想の修正に関するお知らせ", tables=tables
        )
        # 連結テーブルの値が採用される
        self.assertEqual(ev.revised_sales, 11000.0)
        self.assertEqual(ev.previous_sales, 10000.0)
        self.assertEqual(ev.extraction_source, "pdf_table")

    def test_mixed_tables_uses_non_consolidated_when_title_says(self):
        """タイトルが個別の場合、個別テーブルを選択"""
        consolidated_table = [
            ["連結", "売上高", "営業利益", "経常利益", "当期純利益", "1株当たり当期純利益"],
            ["前回発表予想", "10,000", "1,500", "1,600", "1,200", "120.00"],
            ["今回修正予想", "11,000", "1,800", "1,900", "1,500", "150.00"],
        ]
        non_consolidated_table = [
            ["個別", "売上高", "営業利益", "経常利益", "当期純利益", "1株当たり当期純利益"],
            ["前回発表予想", "5,000", "800", "900", "600", "60.00"],
            ["今回修正予想", "5,500", "1,000", "1,100", "700", "70.00"],
        ]
        tables = [consolidated_table, non_consolidated_table]
        text = "（単位：百万円）"
        ev = extract_forecast_revision(
            text, "個別業績予想の修正に関するお知らせ", tables=tables
        )
        # 個別テーブルの値が採用される
        self.assertEqual(ev.revised_sales, 5500.0)
        self.assertEqual(ev.previous_sales, 5000.0)

    def test_regression_ishii_hyoki_6336(self):
        """回帰テスト: 6336 石井表記の改善ケース維持"""
        tables = [[
            [None, "売上高", "営業利益", "経常利益", "当期純利益"],
            ["前回発表予想(A)", "4,800", "460", "480", "350"],
            ["実績(B)", "5,620", "700", "710", "512"],
            ["増減額(B-A)", "820", "240", "230", "162"],
        ]]
        text = "2026年3月期通期業績予想値と実績値との差異に関するお知らせ\n（単位：百万円）"
        ev = extract_forecast_revision(
            text, "2026年3月期通期業績予想値と実績値との差異",
            is_difference=True, tables=tables
        )
        self.assertIsNotNone(ev.previous_sales)
        self.assertIsNotNone(ev.revised_sales)
        self.assertEqual(ev.extraction_source, "pdf_table")

    def test_regression_kawase_7851(self):
        """回帰テスト: 7851 カワセコンピュータの改善ケース維持"""
        tables = [[
            [None, "売上高", "営業利益", "経常利益", "当期純利益", "1株当たり当期純利益"],
            ["前回発表予想", "3,000", "300", "320", "500", "50.00"],
            ["今回修正予想", "3,800", "500", "520", "800", "80.00"],
        ]]
        text = "令和7年3月期通期業績予想の修正に関するお知らせ\n（単位：百万円）"
        ev = extract_forecast_revision(
            text, "令和7年3月期通期業績予想の修正", tables=tables
        )
        self.assertIsNotNone(ev.previous_sales)
        self.assertIsNotNone(ev.revised_sales)
        self.assertEqual(ev.extraction_source, "pdf_table")

    def test_synthetic_3470_like_mixed_prevention(self):
        """3470系: 連結/個別が異なるテーブルに分かれている場合に混在しない"""
        consolidated_table = [
            ["連結業績予想の修正", "売上高", "営業利益", "経常利益", "当期純利益", "1株当たり利益"],
            ["前回発表予想", "14,700", "1,200", "1,300", "900", "90.00"],
            ["今回修正予想", "15,500", "1,400", "1,500", "1,100", "110.00"],
        ]
        non_consolidated_table = [
            ["個別業績予想の修正", "売上高", "営業利益", "経常利益", "当期純利益", "1株当たり利益"],
            ["前回発表予想", "2,020", "160", "170", "100", "10.00"],
            ["今回修正予想", "2,200", "180", "190", "120", "12.00"],
        ]
        tables = [consolidated_table, non_consolidated_table]
        text = "連結業績予想の修正に関するお知らせ\n（単位：百万円）"
        ev = extract_forecast_revision(
            text, "連結業績予想の修正に関するお知らせ", tables=tables
        )
        # 連結テーブルの値のみ使用（個別の値が混入しない）
        self.assertEqual(ev.previous_sales, 14700.0)
        self.assertEqual(ev.revised_sales, 15500.0)
        # 個別テーブルの値ではないことを確認
        self.assertNotEqual(ev.previous_sales, 2020.0)


# ============================================================
# 13. Phase 1.5: テキスト経路 連結/個別フィルタテスト
# ============================================================
class TestTextLineClassification(unittest.TestCase):
    """テキスト行のセクション分類テスト"""

    def test_consolidated_heading(self):
        self.assertEqual(_classify_text_line("連結業績予想の修正"), "consolidated")

    def test_non_consolidated_heading(self):
        self.assertEqual(_classify_text_line("個別業績予想の修正"), "non_consolidated")

    def test_standalone_heading(self):
        self.assertEqual(_classify_text_line("単体業績予想"), "non_consolidated")

    def test_short_consolidated(self):
        self.assertEqual(_classify_text_line("連結 業績予想"), "consolidated")

    def test_no_keyword(self):
        self.assertIsNone(_classify_text_line("前回発表予想 10,000 1,500 1,600"))

    def test_empty_line(self):
        self.assertIsNone(_classify_text_line(""))


class TestTextSections(unittest.TestCase):
    """テキストセクション分割テスト"""

    def test_single_consolidated(self):
        lines = [
            "連結業績予想の修正",
            "前回予想 10,000 1,500",
            "今回修正 11,000 1,800",
        ]
        sections = _find_text_sections(lines)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["type"], "consolidated")

    def test_consolidated_then_non_consolidated(self):
        lines = [
            "連結業績予想の修正",
            "前回予想 10,000 1,500",
            "今回修正 11,000 1,800",
            "個別業績予想の修正",
            "前回予想 5,000 800",
            "今回修正 5,500 1,000",
        ]
        sections = _find_text_sections(lines)
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0]["type"], "consolidated")
        self.assertEqual(sections[1]["type"], "non_consolidated")
        self.assertEqual(sections[0]["end"], 3)  # 個別の前まで
        self.assertEqual(sections[1]["start"], 3)

    def test_no_section_markers(self):
        lines = ["前回予想 10,000", "今回修正 11,000"]
        sections = _find_text_sections(lines)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["type"], "unknown")

    def test_unknown_before_consolidated(self):
        lines = [
            "業績予想の修正について",
            "",
            "連結業績予想",
            "前回予想 10,000",
        ]
        sections = _find_text_sections(lines)
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0]["type"], "unknown")
        self.assertEqual(sections[1]["type"], "consolidated")


class TestTextExtractionGuard(unittest.TestCase):
    """テキスト経路の連結/個別混在ガードテスト"""

    def test_text_mixed_uses_consolidated_only(self):
        """テキストに連結+個別がある場合、タイトルが連結なら連結セクションのみ使用"""
        text = """\n連結業績予想の修正
（単位：百万円）
前回発表予想 14,700 1,200 1,300 900 90.00
今回修正予想 15,500 1,400 1,500 1,100 110.00
個別業績予想の修正
前回発表予想 2,020 160 170 100 10.00
今回修正予想 2,200 180 190 120 12.00
"""
        ev = extract_forecast_revision(
            text, "連結業績予想の修正に関するお知らせ"
        )
        # 連結セクションの値が採用される
        self.assertEqual(ev.previous_sales, 14700.0)
        self.assertEqual(ev.revised_sales, 15500.0)
        # 個別の値が混入していない
        self.assertNotEqual(ev.previous_sales, 2020.0)

    def test_text_non_consolidated_title_selects_kobetsu(self):
        """タイトルが個別なら個別セクションのみ使用"""
        text = """\n連結業績予想の修正
（単位：百万円）
前回発表予想 14,700 1,200 1,300 900 90.00
今回修正予想 15,500 1,400 1,500 1,100 110.00
個別業績予想の修正
前回発表予想 2,020 160 170 100 10.00
今回修正予想 2,200 180 190 120 12.00
"""
        ev = extract_forecast_revision(
            text, "個別業績予想の修正に関するお知らせ"
        )
        # 個別セクションの値が採用される
        self.assertEqual(ev.previous_sales, 2020.0)
        self.assertEqual(ev.revised_sales, 2200.0)

    def test_synthetic_9041_text_path(self):
        """連結子会社の業績修正: 連結と個別が混在するテキストで混在しない"""
        text = """\n連結業績予想の修正
（単位：百万円）
前回発表予想 37,710 3,000 3,200 2,000 200.00
今回修正予想 38,810 3,100 3,400 2,550 255.00
個別業績予想の修正
前回発表予想 1,110 310 340 250 25.00
今回修正予想 1,200 350 380 280 28.00
"""
        ev = extract_forecast_revision(
            text, "連結子会社の業績予想の修正に関するお知らせ"
        )
        # 連結セクションから抜く(タイトルに「連結」)
        self.assertEqual(ev.previous_sales, 37710.0)
        self.assertEqual(ev.revised_sales, 38810.0)
        # 個別の1,110が混入していない
        self.assertNotEqual(ev.previous_sales, 1110.0)


# ============================================================
# 14. Phase 1.6: subtype weighted scoring テスト
# ============================================================
class TestMetricDirection(unittest.TestCase):
    """単一指標の方向判定テスト"""

    def test_upward_by_pct(self):
        self.assertEqual(_metric_direction(1000, 1200, 20.0), "upward")

    def test_downward_by_pct(self):
        self.assertEqual(_metric_direction(1200, 1000, -16.7), "downward")

    def test_flat(self):
        self.assertEqual(_metric_direction(1000, 1020, 2.0), "flat")

    def test_turnaround_positive(self):
        self.assertEqual(_metric_direction(-500, 200, 140.0), "upward")

    def test_turnaround_negative(self):
        self.assertEqual(_metric_direction(500, -200, -140.0), "downward")

    def test_unknown_no_data(self):
        self.assertEqual(_metric_direction(None, None, None), "unknown")


class TestSubtypeScoringPhase16(unittest.TestCase):
    """Phase 1.6 weighted scoring テスト"""

    def test_single_metric_upward(self):
        """売上のみ上方"""
        ev = ForecastRevisionEvent(change_sales_pct=15.0)
        self.assertEqual(_determine_subtype(ev), "upward")

    def test_single_metric_downward(self):
        """売上のみ下方"""
        ev = ForecastRevisionEvent(change_sales_pct=-10.0)
        self.assertEqual(_determine_subtype(ev), "downward")

    def test_op_down_but_net_income_up(self):
        """営利下方だが純利益上方: 純利益weight(4) > 営利weight(3) → neutral (4 < 3*2=6)"""
        ev = ForecastRevisionEvent(
            change_op_pct=-15.0, change_net_income_pct=20.0,
        )
        self.assertEqual(_determine_subtype(ev), "neutral")

    def test_sales_up_profits_down(self):
        """売上上方、利益系全下方: down(3+2+4=9) >> up(1) → downward"""
        ev = ForecastRevisionEvent(
            change_sales_pct=15.0,
            change_op_pct=-15.0,
            change_ordinary_pct=-15.0,
            change_net_income_pct=-15.0,
        )
        self.assertEqual(_determine_subtype(ev), "downward")

    def test_majority_upward(self):
        """3指標上方 + 1指標下方: up(1+3+2=6) > down(4), 6 >= 4*2=8 → NO → neutral"""
        ev = ForecastRevisionEvent(
            change_sales_pct=10.0,
            change_op_pct=10.0,
            change_ordinary_pct=10.0,
            change_net_income_pct=-10.0,
        )
        self.assertEqual(_determine_subtype(ev), "neutral")

    def test_majority_downward(self):
        """3指標下方 + 1指標上方: down(3+2+4=9) vs up(1) → downward (9 >= 1*2=2)"""
        ev = ForecastRevisionEvent(
            change_sales_pct=10.0,
            change_op_pct=-10.0,
            change_ordinary_pct=-10.0,
            change_net_income_pct=-10.0,
        )
        self.assertEqual(_determine_subtype(ev), "downward")

    def test_title_upward_but_mixed_metrics(self):
        """タイトル上方修正だが指標割れ: title prior(+1) で up=4, down=3 → 4 < 3*2 → neutral"""
        ev = ForecastRevisionEvent(
            change_op_pct=15.0,
            change_net_income_pct=-10.0,
        )
        result = _determine_subtype(ev, title="業績予想の上方修正に関するお知らせ")
        self.assertEqual(result, "neutral")

    def test_difference_title(self):
        """差異通知タイトル"""
        ev = ForecastRevisionEvent(is_difference_disclosure=True)
        self.assertEqual(_determine_subtype(ev, title="業績予想と実績値との差異に関するお知らせ"), "difference")

    def test_all_metrics_upward(self):
        """全指標上方 → upward"""
        ev = ForecastRevisionEvent(
            change_sales_pct=10.0,
            change_op_pct=20.0,
            change_ordinary_pct=15.0,
            change_net_income_pct=25.0,
        )
        self.assertEqual(_determine_subtype(ev), "upward")

    def test_all_metrics_downward(self):
        """全指標下方 → downward"""
        ev = ForecastRevisionEvent(
            change_sales_pct=-10.0,
            change_op_pct=-20.0,
            change_ordinary_pct=-15.0,
            change_net_income_pct=-25.0,
        )
        self.assertEqual(_determine_subtype(ev), "downward")


# ============================================================
# 15. Phase 2: unit normalization テスト
# ============================================================
class TestUnitCompatibility(unittest.TestCase):
    """単位互換性チェック"""

    def test_same_unit(self):
        self.assertTrue(_units_compatible("百万円", "百万円"))

    def test_different_unit(self):
        self.assertFalse(_units_compatible("百万円", "千円"))

    def test_one_unknown(self):
        self.assertTrue(_units_compatible("百万円", ""))

    def test_both_unknown(self):
        self.assertTrue(_units_compatible("", ""))

    def test_billion_vs_million(self):
        self.assertFalse(_units_compatible("億円", "百万円"))


class TestUnitConversion(unittest.TestCase):
    """単位変換テスト"""

    def test_thousand_to_million(self):
        # 1000千円 = 1百万円
        result = _convert_unit_value(1000.0, "千円", "百万円")
        self.assertEqual(result, 1.0)

    def test_billion_to_million(self):
        # 1億円 = 100百万円
        result = _convert_unit_value(1.0, "億円", "百万円")
        self.assertEqual(result, 100.0)

    def test_same_unit_no_change(self):
        result = _convert_unit_value(500.0, "百万円", "百万円")
        self.assertEqual(result, 500.0)

    def test_unknown_returns_none(self):
        result = _convert_unit_value(500.0, "", "百万円")
        self.assertIsNone(result)

    def test_eps_no_conversion(self):
        result = _convert_unit_value(120.5, "千円", "百万円", is_eps=True)
        self.assertEqual(result, 120.5)


class TestSanitizeMetricsPhase2(unittest.TestCase):
    """異常値検出 Phase 2 強化テスト"""

    def test_sales_300pct_anomaly(self):
        """売上300%超変化は単位ミスとしてNone化"""
        data = {
            "previous_sales": 100.0, "revised_sales": 500.0,
            "delta_sales": 400.0, "change_sales_pct": 400.0,
        }
        result = _sanitize_metrics(data)
        self.assertIsNone(result["revised_sales"])

    def test_profit_1000pct_anomaly(self):
        """利益1000%超変化は単位ミスとしてNone化"""
        data = {
            "previous_op": 10.0, "revised_op": 200.0,
            "delta_op": 190.0, "change_op_pct": 1900.0,
        }
        result = _sanitize_metrics(data)
        self.assertIsNone(result["revised_op"])

    def test_50x_ratio_anomaly(self):
        """50倍超比率は単位ミスとしてNone化"""
        data = {
            "previous_net_income": 1.0, "revised_net_income": 100.0,
            "delta_net_income": 99.0, "change_net_income_pct": 9900.0,
        }
        result = _sanitize_metrics(data)
        self.assertIsNone(result["revised_net_income"])

    def test_normal_not_flagged(self):
        """正常範囲の変化はフラグされない"""
        data = {
            "previous_sales": 1000.0, "revised_sales": 1200.0,
            "delta_sales": 200.0, "change_sales_pct": 20.0,
            "previous_op": 100.0, "revised_op": 120.0,
            "delta_op": 20.0, "change_op_pct": 20.0,
        }
        result = _sanitize_metrics(data)
        self.assertEqual(result["revised_sales"], 1200.0)
        self.assertEqual(result["revised_op"], 120.0)
        self.assertEqual(result["metrics_count"], 2)

    def test_unit_supplement_same_unit(self):
        """同一unit時はPhase 1補完される"""
        tables = [[
            [None, "売上高", "営業利益", "経常利益", "当期純利益"],
            ["前回発表予想", None, "1,500", "1,600", "1,200"],
            ["今回修正予想", None, "1,800", "1,900", "1,500"],
        ]]
        text = """業績予想の修正
（単位：百万円）
前回発表予想 10,000 1,500 1,600 1,200
今回修正予想 11,000 1,800 1,900 1,500"""
        ev = extract_forecast_revision(
            text, "業績予想の修正に関するお知らせ", tables=tables,
        )
        self.assertEqual(ev.revised_op, 1800.0)
        # text経路から sales 補完される
        self.assertIsNotNone(ev.revised_sales)


# ============================================================
# 16. Phase 2.5: CMap文字化け対策テスト
# ============================================================
class TestNormalizeLabel(unittest.TestCase):
    """ラベル正規化テスト"""

    def test_basic_nfkc(self):
        self.assertEqual(_normalize_label("売上高"), "売上高")

    def test_zero_width_char(self):
        # ゼロ幅スペースが含まれていても除去
        self.assertEqual(_normalize_label("売\u200b上\u200b高"), "売上高")

    def test_control_char(self):
        # 制御文字(Cf)が含まれていても除去
        self.assertEqual(_normalize_label("営業\u00ad利益"), "営業利益")

    def test_fullwidth_digit(self):
        # 全角数字の正規化(NFKC)
        self.assertEqual(_normalize_label("１株当たり"), "1株当たり")


class TestMatchMetricLabel(unittest.TestCase):
    """多段階ラベルマッチングテスト"""

    def test_tier1_exact(self):
        self.assertEqual(_match_metric_label("売上高"), "sales")

    def test_tier1_partial(self):
        self.assertEqual(_match_metric_label("連結 売上高"), "sales")

    def test_tier2_short(self):
        """CMap崩れで一部だけ読めたケース"""
        self.assertEqual(_match_metric_label("xx売上yy"), "sales")

    def test_tier2_op_short(self):
        self.assertEqual(_match_metric_label("営利zz"), "op")

    def test_no_match(self):
        self.assertIsNone(_match_metric_label("株主総会"))

    def test_already_matched_skipped(self):
        """すでにマッチ済みの指標はスキップ"""
        self.assertIsNone(
            _match_metric_label("売上高", already_matched={"sales"})
        )


class TestCMapGarbledTable(unittest.TestCase):
    """CMap文字化けテーブルのfallbackテスト"""

    def test_partial_garbled_labels(self):
        """ラベルの一部が文字化けしても短縮マッチで認識可能"""
        tables = [[
            [None, "x売上x", "xx営利xx", "xx経常xx", "xx純利xx"],
            ["前回発表予想(A)", "10,000", "1,500", "1,600", "1,200"],
            ["今回修正予想(B)", "11,000", "1,800", "1,900", "1,500"],
            ["増減率(%)", "10.0", "20.0", "18.8", "25.0"],
        ]]
        text = "2026年3月期 通期連結業績予想の修正に関するお知らせ\n（単位：百万円）"
        ev = extract_forecast_revision(text, "業績予想の修正に関するお知らせ", tables=tables)
        # 短縮マッチでラベル認識できる
        self.assertIsNotNone(ev.revised_sales)
        self.assertIsNotNone(ev.revised_op)
        self.assertEqual(ev.extraction_source, "pdf_table")

