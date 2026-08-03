# ============================================================
# test_extractor.py — 数値抽出のテスト
# ============================================================
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.extractor import (
    _detect_scale,
    _extract_value_near_keyword,
    _extract_numbers_from_line,
    _parse_xbrl_content,
    SALES_KEYWORDS,
    OP_KEYWORDS,
    GROSS_PROFIT_KEYWORDS,
)


class TestDetectScale:
    """_detect_scale のテスト"""

    def test_million_yen(self):
        assert _detect_scale("（単位：百万円）") == "百万円"

    def test_million_yen_half(self):
        assert _detect_scale("(単位:百万円)") == "百万円"

    def test_thousand_yen(self):
        assert _detect_scale("（単位：千円）") == "千円"

    def test_billion_yen(self):
        assert _detect_scale("（単位：億円）") == "億円"

    def test_default(self):
        assert _detect_scale("何も書いてないテキスト") == "百万円"


class TestExtractNumbersFromLine:
    """_extract_numbers_from_line のテスト"""

    def test_simple(self):
        nums = _extract_numbers_from_line("売上高 1,234,567")
        assert 1234567 in nums

    def test_negative_triangle(self):
        nums = _extract_numbers_from_line("営業損失 △1,234")
        assert -1234 in nums

    def test_excludes_percentage(self):
        # YoY%の小さい値は除外される
        nums = _extract_numbers_from_line("1,234,567  8.2  1,100,000")
        assert 1234567 in nums
        assert 1100000 in nums

    def test_excludes_fiscal_year_but_keeps_amount(self):
        nums = _extract_numbers_from_line("2027年3月期 51,812")
        assert 2027 not in nums
        assert nums == [51812]


class TestExtractValueNearKeyword:
    """_extract_value_near_keyword のテスト"""

    def test_sales_same_line(self):
        lines = [
            "（単位：百万円）",
            "売上高  1,234,567  1,100,000",
            "営業利益  123,456  100,000",
        ]
        result = _extract_value_near_keyword(lines, SALES_KEYWORDS)
        assert result == 1234567

    def test_operating_profit(self):
        lines = [
            "売上高  500,000",
            "営業利益  50,000",
        ]
        result = _extract_value_near_keyword(lines, OP_KEYWORDS)
        assert result == 50000

    def test_operating_loss(self):
        lines = [
            "売上高  500,000",
            "営業損失  △1,234",
        ]
        result = _extract_value_near_keyword(lines, OP_KEYWORDS)
        assert result == -1234

    def test_not_found(self):
        lines = ["何も関係ない行"]
        result = _extract_value_near_keyword(lines, SALES_KEYWORDS)
        assert result is None

    def test_next_line(self):
        lines = [
            "売上高",
            "1,234,567",
        ]
        result = _extract_value_near_keyword(lines, SALES_KEYWORDS)
        assert result == 1234567

    def test_workman_reference_sales_is_not_a_pl_row(self):
        lines = ["（参考）チェーン全店売上高 2027年3月期（累計） 67,561百万円"]
        assert _extract_value_near_keyword(lines, SALES_KEYWORDS) is None

    def test_litalico_narrative_is_not_a_pl_row(self):
        lines = ["これに伴い、前四半期連結累計期間の売上収益、営業利益について", "そのため2026年3月期"]
        assert _extract_value_near_keyword(lines, SALES_KEYWORDS) is None
        assert _extract_value_near_keyword(lines, OP_KEYWORDS) is None

    def test_ricoh_contents_forecast_is_not_a_pl_row(self):
        lines = ["（3）分野別売上高見通し（連結）", "2027年3月期 第1四半期決算のお知らせ"]
        assert _extract_value_near_keyword(lines, SALES_KEYWORDS) is None


@pytest.mark.parametrize(
    ("company", "sales_tag", "sales", "op_tag", "op"),
    [
        ("workman", "GrossOperatingRevenues", 51_812_000_000, "OperatingIncome", 12_031_000_000),
        ("litalico", "SalesIFRS", 10_844_000_000, "OperatingIncomeIFRS", 1_434_000_000),
        ("ricoh", "NetSalesIFRS", 629_812_000_000, "OperatingIncomeIFRS", 47_762_000_000),
    ],
)
def test_20260803_xbrl_pl_tags_win_over_pdf_patterns(company, sales_tag, sales, op_tag, op):
    """Actual 2026-08-03 tag forms: J-GAAP, IFRS SalesIFRS, and NetSalesIFRS."""
    raw = f'''<root xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">
      <ix:nonFraction name="tse:{sales_tag}" contextRef="CurrentAccumulatedQ1Duration_ConsolidatedMember_ResultMember" scale="6">{sales // 1_000_000}</ix:nonFraction>
      <ix:nonFraction name="tse:{op_tag}" contextRef="CurrentAccumulatedQ1Duration_ConsolidatedMember_ResultMember" scale="6">{op // 1_000_000}</ix:nonFraction>
    </root>'''.encode()

    result = _parse_xbrl_content(raw, source_label="summary_xbrl")

    assert result is not None, company
    assert result.sales == sales
    assert result.operating_profit == op
    assert result.field_sources == {"sales": "summary_xbrl", "operating_profit": "summary_xbrl"}
