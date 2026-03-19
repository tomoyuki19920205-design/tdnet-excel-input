# ============================================================
# test_utils.py — ユーティリティ関数のテスト
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils import normalize_number, parse_scale_unit, convert_to_excel_unit, excel_unit_multiplier


class TestNormalizeNumber:
    """normalize_number のテスト"""

    def test_normal_integer(self):
        assert normalize_number("1234567") == 1234567

    def test_comma_separated(self):
        assert normalize_number("1,234,567") == 1234567

    def test_triangle_negative(self):
        assert normalize_number("△1,234") == -1234

    def test_filled_triangle_negative(self):
        assert normalize_number("▲1,234") == -1234

    def test_paren_negative(self):
        assert normalize_number("(1,234)") == -1234

    def test_fullwidth_paren_negative(self):
        assert normalize_number("（1,234）") == -1234

    def test_minus_negative(self):
        assert normalize_number("-1,234") == -1234

    def test_fullwidth_minus(self):
        assert normalize_number("－1,234") == -1234

    def test_fullwidth_digits(self):
        assert normalize_number("１２３４") == 1234

    def test_none(self):
        assert normalize_number(None) is None

    def test_empty(self):
        assert normalize_number("") is None

    def test_dash(self):
        assert normalize_number("—") is None

    def test_fullwidth_dash(self):
        assert normalize_number("－") is None

    def test_float_rounds(self):
        assert normalize_number("1234.5") == 1235

    def test_zero(self):
        assert normalize_number("0") == 0

    def test_large_number(self):
        assert normalize_number("123,456,789") == 123456789


class TestParseScaleUnit:
    """parse_scale_unit のテスト"""

    def test_million_yen(self):
        assert parse_scale_unit("百万円") == 1_000_000

    def test_billion_yen(self):
        assert parse_scale_unit("億円") == 100_000_000

    def test_thousand_yen(self):
        assert parse_scale_unit("千円") == 1_000

    def test_yen(self):
        assert parse_scale_unit("円") == 1

    def test_unknown(self):
        assert parse_scale_unit("ドル") == 1


class TestConvertToExcelUnit:
    """convert_to_excel_unit のテスト"""

    def test_same_unit(self):
        # 百万円 → 百万円: そのまま
        assert convert_to_excel_unit(12345, 1_000_000, 1_000_000) == 12345

    def test_thousand_to_million(self):
        # 千円 → 百万円: 12345千円 = 12百万円
        assert convert_to_excel_unit(12345, 1_000, 1_000_000) == 12

    def test_yen_to_million(self):
        # 円 → 百万円
        assert convert_to_excel_unit(12_345_000_000, 1, 1_000_000) == 12345

    def test_million_to_thousand(self):
        # 百万円 → 千円
        assert convert_to_excel_unit(12, 1_000_000, 1_000) == 12000


class TestExcelUnitMultiplier:
    """excel_unit_multiplier のテスト"""

    def test_million_yen(self):
        assert excel_unit_multiplier("million_yen") == 1_000_000

    def test_thousand_yen(self):
        assert excel_unit_multiplier("thousand_yen") == 1_000

    def test_yen(self):
        assert excel_unit_multiplier("yen") == 1

    def test_unknown(self):
        assert excel_unit_multiplier("unknown") == 1_000_000
