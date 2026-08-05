"""
tests/test_common_ticker.py — common_ticker 正規化テスト
"""
from __future__ import annotations

import sys
from pathlib import Path

# Project root
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest
from src.common_ticker import (
    normalize_ticker,
    strip_tdnet_trailing_zero,
    ticker_to_sec_code,
    is_valid_ticker,
    JQUANTS_ALPHA_MAP,
)


# ============================================================
# normalize_ticker テスト
# ============================================================
class TestNormalizeTicker:
    """normalize_ticker の代表ケース + エッジケース。"""

    # ---- 代表ケース (ユーザー指定) ----
    def test_41800_to_4180(self):
        """Numeric ticker remains numeric after its market suffix is removed."""
        assert normalize_ticker("41800") == "4180"

    def test_421A0_to_421A(self):
        assert normalize_ticker("421A0") == "421A"

    def test_429A0_to_429A(self):
        assert normalize_ticker("429A0") == "429A"

    def test_130A0_to_130A(self):
        assert normalize_ticker("130A0") == "130A"

    def test_72030_to_7203(self):
        assert normalize_ticker("72030") == "7203"

    # ---- Numeric five-character code remains numeric ----
    def test_13000_to_1300(self):
        assert normalize_ticker("13000") == "1300"

    def test_42100_to_4210(self):
        assert normalize_ticker("42100") == "4210"

    def test_42900_to_4290(self):
        assert normalize_ticker("42900") == "4290"

    # ---- 4桁 ticker (変換不要) ----
    def test_4digit_numeric(self):
        assert normalize_ticker("7203") == "7203"

    def test_4digit_alpha(self):
        assert normalize_ticker("418A") == "418A"

    def test_alphanumeric_and_numeric_tickers_do_not_collide(self):
        assert normalize_ticker("418A0") == "418A"
        assert normalize_ticker("41800") == "4180"
        assert normalize_ticker("418A") != normalize_ticker("4180")
        assert normalize_ticker("472A0") == "472A"
        assert normalize_ticker("47200") == "4720"
        assert normalize_ticker("472A") != normalize_ticker("4720")

    def test_4digit_1301(self):
        assert normalize_ticker("1301") == "1301"

    # ---- 大文字変換 ----
    def test_lowercase_alpha(self):
        assert normalize_ticker("421a0") == "421A"

    def test_lowercase_4digit(self):
        assert normalize_ticker("418a") == "418A"

    # ---- 空白除去 ----
    def test_leading_trailing_spaces(self):
        assert normalize_ticker(" 1301 ") == "1301"

    def test_spaces_around_alpha(self):
        assert normalize_ticker(" 418A0 ") == "418A"

    # ---- 空文字・None 類 ----
    def test_empty_string(self):
        assert normalize_ticker("") == ""

    def test_none_input(self):
        assert normalize_ticker("None") == "NONE"  # str("None") → "NONE"

    def test_numeric_input(self):
        """int を渡しても str 変換される"""
        assert normalize_ticker(72030) == "7203"

    # ---- 5桁で末尾0ではない ----
    def test_5digit_no_trailing_zero(self):
        assert normalize_ticker("72031") == "72031"

    # ---- 通常の 5桁末尾0 (MAP に無い) ----
    def test_regular_5digit_trailing_zero(self):
        """通常の5桁コード (MAP に無い) は単純に末尾0除去"""
        assert normalize_ticker("67580") == "6758"


# ============================================================
# strip_tdnet_trailing_zero テスト
# ============================================================
class TestStripTrailingZero:
    def test_5digit_numeric(self):
        assert strip_tdnet_trailing_zero("18320") == "1832"

    def test_5digit_alpha(self):
        assert strip_tdnet_trailing_zero("418A0") == "418A"

    def test_4digit(self):
        assert strip_tdnet_trailing_zero("7203") == "7203"

    def test_4digit_alpha(self):
        assert strip_tdnet_trailing_zero("418A") == "418A"

    def test_no_trailing_zero(self):
        assert strip_tdnet_trailing_zero("12345") == "12345"


# ============================================================
# ticker_to_sec_code テスト
# ============================================================
class TestTickerToSecCode:
    def test_4digit_to_5digit(self):
        assert ticker_to_sec_code("7203") == "72030"

    def test_alpha_4digit(self):
        assert ticker_to_sec_code("418A") == "418A0"

    def test_already_5digit(self):
        assert ticker_to_sec_code("72030") == "72030"

    def test_lowercase(self):
        assert ticker_to_sec_code("418a") == "418A0"


# ============================================================
# is_valid_ticker テスト
# ============================================================
class TestIsValidTicker:
    def test_4digit_numeric(self):
        assert is_valid_ticker("7203") is True

    def test_3digit_alpha(self):
        assert is_valid_ticker("418A") is True

    def test_5char_with_alpha_invalid(self):
        """5文字 (4桁数字+英字) は invalid"""
        assert is_valid_ticker("72030") is False

    def test_5digit(self):
        assert is_valid_ticker("72030") is False

    def test_empty(self):
        assert is_valid_ticker("") is False

    def test_3digit(self):
        assert is_valid_ticker("130") is True


# ============================================================
# JQUANTS_ALPHA_MAP 整合性テスト
# ============================================================
class TestAlphaMap:
    def test_map_has_418A(self):
        """418A は map に含まれること"""
        assert JQUANTS_ALPHA_MAP["41800"] == "418A"

    def test_all_keys_are_5digit_numeric(self):
        for k in JQUANTS_ALPHA_MAP:
            assert len(k) == 5 and k.isdigit(), f"Invalid key: {k}"

    def test_all_values_are_4char_alpha(self):
        for v in JQUANTS_ALPHA_MAP.values():
            assert len(v) == 4 and v[3].isalpha(), f"Invalid value: {v}"

    def test_map_size(self):
        assert len(JQUANTS_ALPHA_MAP) >= 144


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
