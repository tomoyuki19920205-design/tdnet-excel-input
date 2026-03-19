# ============================================================
# test_extractor.py — 数値抽出のテスト
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.extractor import (
    _detect_scale,
    _extract_value_near_keyword,
    _extract_numbers_from_line,
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
