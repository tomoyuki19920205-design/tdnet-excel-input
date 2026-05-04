#!/usr/bin/env python3
"""test_forecast_extractor_column_fix.py — 数値列連結バグ修正の回帰テスト

問題: _normalize_text() の数字間スペース結合が売上高・利益・EPSを
連結して「144000950010500136960」のような巨大数値を生成していた。

修正: _normalize_text() から数字間スペース結合を削除し、
_split_row_into_columns() で2+スペース/タブを列境界として正しく分離。

テスト対象6社（2026年4月修正開示実績値）:
  6332 月島HD, 6998 日タングス, 1939 四電工, 1945 東京エネシス,
  2053 中部飼料, 4442 バルテスHD
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.events.forecast_extractor import (
    _normalize_text,
    _split_row_into_columns,
    _extract_numbers_from_columns,
    _extract_numbers_from_line,
    _sanitize_metrics,
    extract_forecast_revision,
)


# ============================================================
# 基本ユーティリティのテスト
# ============================================================
class TestNormalizeTextNoConcat(unittest.TestCase):
    """_normalize_text() が数字間のスペースを連結しないことを確認"""

    def test_numbers_with_single_space_not_concatenated(self):
        """数字間の単一スペースが除去されないことを確認"""
        result = _normalize_text("144,000 9,500 10,500 15,000 380.64")
        # 連結されていないこと
        self.assertNotIn("1440009500", result)
        # 各数値が独立して存在すること
        self.assertIn("144,000", result)
        self.assertIn("9,500", result)
        self.assertIn("380.64", result)

    def test_comma_decimal_normalization_preserved(self):
        """カンマ・小数点の正規化は維持される"""
        result = _normalize_text("1 ,234")
        self.assertEqual(result, "1,234")

    def test_decimal_normalization_preserved(self):
        """小数点の正規化は維持される"""
        result = _normalize_text("12 .34")
        self.assertEqual(result, "12.34")


class TestSplitRowIntoColumns(unittest.TestCase):
    """_split_row_into_columns() の列分離テスト"""

    def test_two_plus_space_separator(self):
        """2+スペース区切りで正しく列分離"""
        line = "前回発表予想(A)  144,000  9,500  10,500  15,000  380.64"
        cols = _split_row_into_columns(line)
        self.assertGreaterEqual(len(cols), 6)
        self.assertIn("144,000", cols)
        self.assertIn("9,500", cols)
        self.assertIn("380.64", cols)

    def test_tab_separator(self):
        """タブ区切りで正しく列分離"""
        line = "前回発表予想(A)\t144,000\t9,500\t10,500"
        cols = _split_row_into_columns(line)
        self.assertIn("144,000", cols)
        self.assertIn("9,500", cols)


class TestSanitizeMetrics(unittest.TestCase):
    """_sanitize_metrics() の安全弁テスト"""

    def test_concatenated_sales_rejected(self):
        """売上高の巨大連結数値（100兆円超）が除去される"""
        result = _sanitize_metrics({
            "previous_sales": 144000950010500000.0,
            "revised_sales": 149000980011000000.0,
            "previous_eps": 3.0,
            "revised_eps": 43.0,
            "metrics_count": 2,
        })
        self.assertIsNone(result["revised_sales"])
        self.assertIsNone(result["previous_sales"])

    def test_eps_over_10000_rejected(self):
        """EPS > 10,000 が除去される"""
        result = _sanitize_metrics({
            "previous_eps": 136960.0,
            "revised_eps": 168448.0,
            "metrics_count": 1,
        })
        self.assertIsNone(result["revised_eps"])
        self.assertIsNone(result["previous_eps"])

    def test_normal_values_pass(self):
        """正常値は通過する"""
        result = _sanitize_metrics({
            "previous_sales": 144000.0,
            "revised_sales": 149000.0,
            "previous_eps": 380.64,
            "revised_eps": 412.43,
            "metrics_count": 2,
        })
        self.assertEqual(result["previous_sales"], 144000.0)
        self.assertEqual(result["revised_sales"], 149000.0)
        self.assertEqual(result["previous_eps"], 380.64)
        self.assertEqual(result["revised_eps"], 412.43)


# ============================================================
# 6社の回帰テスト（修正値が期待値と一致することを確認）
# ============================================================
def _make_table_text(
    *,
    header="売上高    営業利益    経常利益    親会社株主に帰属する当期純利益    1株当たり当期純利益",
    prev_row,
    rev_row,
    unit="百万円",
    basis="連結",
    period="2026年3月期 通期",
    title_prefix="連結業績予想の修正に関するお知らせ",
):
    """テスト用業績予想修正表テキストを生成"""
    basis_label = f"{basis}業績予想" if basis else "業績予想"
    return (
        f"{period}{title_prefix}\n"
        f"（単位：{unit}）\n\n"
        f"         {header}\n"
        f"前回発表予想(A)  {prev_row}\n"
        f"今回修正予想(B)  {rev_row}\n"
    )


class TestTicker6332TsukishimaHD(unittest.TestCase):
    """6332 月島HD — 修正前の症状: 売上高が巨大連結数値、EPS=3→43"""

    def setUp(self):
        text = _make_table_text(
            prev_row="144,000  9,500  10,500  15,000  380.64",
            rev_row="149,000  9,800  11,000  16,900  412.43",
        )
        self.ev = extract_forecast_revision(text, "通期連結業績予想の修正に関するお知らせ")

    def test_previous_sales(self):
        self.assertAlmostEqual(self.ev.previous_sales, 144000.0, delta=1.0)

    def test_revised_sales(self):
        self.assertAlmostEqual(self.ev.revised_sales, 149000.0, delta=1.0)

    def test_previous_op(self):
        self.assertAlmostEqual(self.ev.previous_op, 9500.0, delta=1.0)

    def test_revised_op(self):
        self.assertAlmostEqual(self.ev.revised_op, 9800.0, delta=1.0)

    def test_previous_ordinary(self):
        self.assertAlmostEqual(self.ev.previous_ordinary, 10500.0, delta=1.0)

    def test_revised_ordinary(self):
        self.assertAlmostEqual(self.ev.revised_ordinary, 11000.0, delta=1.0)

    def test_previous_net_income(self):
        self.assertAlmostEqual(self.ev.previous_net_income, 15000.0, delta=1.0)

    def test_revised_net_income(self):
        self.assertAlmostEqual(self.ev.revised_net_income, 16900.0, delta=1.0)

    def test_previous_eps_decimal(self):
        """EPS小数点が保持されること（380.64 → 380台であること）"""
        self.assertIsNotNone(self.ev.previous_eps)
        self.assertGreater(self.ev.previous_eps, 100.0)
        self.assertLess(self.ev.previous_eps, 1000.0)

    def test_revised_eps_decimal(self):
        """EPS小数点が保持されること（412.43 → 412台であること）"""
        self.assertIsNotNone(self.ev.revised_eps)
        self.assertGreater(self.ev.revised_eps, 100.0)
        self.assertLess(self.ev.revised_eps, 1000.0)

    def test_no_concatenated_sales(self):
        """売上高が巨大連結数値でないこと（100兆円以下）"""
        if self.ev.revised_sales is not None:
            self.assertLess(self.ev.revised_sales, 1e8)

    def test_subtype_upward(self):
        self.assertEqual(self.ev.subtype, "upward")


class TestTicker6998NittiTungsten(unittest.TestCase):
    """6998 日タングス — 修正前の症状: EPS=202→1（連結バグ）"""

    def setUp(self):
        text = _make_table_text(
            header="売上高    営業利益    経常利益    当期純利益    1株当たり当期純利益",
            prev_row="12,800  700  960  700  144.33",
            rev_row="12,800  710  1,130  270  55.67",
            basis="",
        )
        self.ev = extract_forecast_revision(text, "業績予想の修正に関するお知らせ")

    def test_previous_sales(self):
        self.assertAlmostEqual(self.ev.previous_sales, 12800.0, delta=1.0)

    def test_revised_sales(self):
        self.assertAlmostEqual(self.ev.revised_sales, 12800.0, delta=1.0)

    def test_previous_op(self):
        self.assertAlmostEqual(self.ev.previous_op, 700.0, delta=1.0)

    def test_revised_op(self):
        self.assertAlmostEqual(self.ev.revised_op, 710.0, delta=1.0)

    def test_previous_net_income(self):
        self.assertAlmostEqual(self.ev.previous_net_income, 700.0, delta=1.0)

    def test_revised_net_income(self):
        self.assertAlmostEqual(self.ev.revised_net_income, 270.0, delta=1.0)

    def test_previous_eps_reasonable(self):
        """EPS が 100台であること（144.33 程度）"""
        self.assertIsNotNone(self.ev.previous_eps)
        self.assertGreater(self.ev.previous_eps, 10.0)
        self.assertLess(self.ev.previous_eps, 500.0)

    def test_revised_eps_reasonable(self):
        """EPS が 50台であること（55.67 程度）"""
        self.assertIsNotNone(self.ev.revised_eps)
        self.assertGreater(self.ev.revised_eps, 10.0)
        self.assertLess(self.ev.revised_eps, 500.0)

    def test_no_eps_concatenation(self):
        """EPS が 1 や 202 のような異常値でないこと"""
        if self.ev.revised_eps is not None:
            self.assertGreater(self.ev.revised_eps, 5.0)
        if self.ev.previous_eps is not None:
            self.assertGreater(self.ev.previous_eps, 5.0)


class TestTicker1939ShikokuElectric(unittest.TestCase):
    """1939 四電工 — 連結優先テスト"""

    def setUp(self):
        # 連結表と個別表を含むテキスト（連結優先が必要）
        text = (
            "2026年3月期 通期連結業績予想の修正に関するお知らせ\n"
            "連結業績予想\n"
            "（単位：百万円）\n\n"
            "         売上高    営業利益    経常利益    親会社株主に帰属する当期純利益    1株当たり当期純利益\n"
            "前回発表予想(A)  100,000  8,000  8,500  6,000  126.80\n"
            "今回修正予想(B)   99,400  8,800  9,300  7,500  158.50\n\n"
            "個別業績予想\n"
            "（単位：百万円）\n\n"
            "         売上高    営業利益\n"
            "前回発表予想(A)  50,000  4,000\n"
            "今回修正予想(B)  49,000  4,500\n"
        )
        self.ev = extract_forecast_revision(text, "通期連結業績予想の修正に関するお知らせ")

    def test_consolidated_sales_priority(self):
        """連結売上高が採用されること（個別の50,000ではなく連結の100,000）"""
        if self.ev.previous_sales is not None:
            self.assertGreater(self.ev.previous_sales, 50000.0)

    def test_previous_op(self):
        self.assertAlmostEqual(self.ev.previous_op, 8000.0, delta=1.0)

    def test_revised_net_income(self):
        self.assertAlmostEqual(self.ev.revised_net_income, 7500.0, delta=1.0)

    def test_subtype_upward(self):
        self.assertEqual(self.ev.subtype, "upward")


class TestTicker1945TokyoEnesis(unittest.TestCase):
    """1945 東京エネシス"""

    def setUp(self):
        text = _make_table_text(
            prev_row="82,000  3,900  4,100  3,400  102.07",
            rev_row="83,000  4,700  5,500  4,300  129.32",
        )
        self.ev = extract_forecast_revision(text, "通期連結業績予想の修正に関するお知らせ")

    def test_previous_sales(self):
        self.assertAlmostEqual(self.ev.previous_sales, 82000.0, delta=1.0)

    def test_revised_sales(self):
        self.assertAlmostEqual(self.ev.revised_sales, 83000.0, delta=1.0)

    def test_previous_op(self):
        self.assertAlmostEqual(self.ev.previous_op, 3900.0, delta=1.0)

    def test_revised_op(self):
        self.assertAlmostEqual(self.ev.revised_op, 4700.0, delta=1.0)

    def test_previous_net_income(self):
        self.assertAlmostEqual(self.ev.previous_net_income, 3400.0, delta=1.0)

    def test_revised_net_income(self):
        self.assertAlmostEqual(self.ev.revised_net_income, 4300.0, delta=1.0)

    def test_subtype_upward(self):
        self.assertEqual(self.ev.subtype, "upward")


class TestTicker2053ChubuFeed(unittest.TestCase):
    """2053 中部飼料"""

    def setUp(self):
        text = _make_table_text(
            prev_row="212,000  5,200  5,600  4,100  138.65",
            rev_row="211,000  6,500  7,100  5,500  188.81",
        )
        self.ev = extract_forecast_revision(text, "通期連結業績予想の修正に関するお知らせ")

    def test_previous_sales(self):
        self.assertAlmostEqual(self.ev.previous_sales, 212000.0, delta=1.0)

    def test_revised_sales(self):
        self.assertAlmostEqual(self.ev.revised_sales, 211000.0, delta=1.0)

    def test_previous_net_income(self):
        self.assertAlmostEqual(self.ev.previous_net_income, 4100.0, delta=1.0)

    def test_revised_net_income(self):
        self.assertAlmostEqual(self.ev.revised_net_income, 5500.0, delta=1.0)

    def test_subtype_upward(self):
        self.assertEqual(self.ev.subtype, "upward")


class TestTicker4442BalthusHD(unittest.TestCase):
    """4442 バルテスHD"""

    def setUp(self):
        text = _make_table_text(
            prev_row="12,000  650  647  390  19.69",
            rev_row="11,900  900  900  500  25.21",
        )
        self.ev = extract_forecast_revision(text, "通期連結業績予想の修正に関するお知らせ")

    def test_previous_sales(self):
        self.assertAlmostEqual(self.ev.previous_sales, 12000.0, delta=1.0)

    def test_revised_sales(self):
        self.assertAlmostEqual(self.ev.revised_sales, 11900.0, delta=1.0)

    def test_previous_op(self):
        self.assertAlmostEqual(self.ev.previous_op, 650.0, delta=1.0)

    def test_revised_op(self):
        self.assertAlmostEqual(self.ev.revised_op, 900.0, delta=1.0)

    def test_previous_net_income(self):
        self.assertAlmostEqual(self.ev.previous_net_income, 390.0, delta=1.0)

    def test_revised_net_income(self):
        self.assertAlmostEqual(self.ev.revised_net_income, 500.0, delta=1.0)

    def test_subtype_upward(self):
        self.assertEqual(self.ev.subtype, "upward")


if __name__ == "__main__":
    unittest.main(verbosity=2)
