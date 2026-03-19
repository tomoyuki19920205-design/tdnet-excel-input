# ============================================================
# test_year_parser.py — 年度抽出・R表記変換のテスト
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.year_parser import (
    to_reiwa,
    parse_reiwa,
    detect_quarter,
    extract_fiscal_year_from_title,
    extract_fiscal_info,
)


class TestToReiwa:
    """to_reiwa のテスト"""

    def test_r8(self):
        assert to_reiwa(2026, 3) == "R8/3"

    def test_r7(self):
        assert to_reiwa(2025, 3) == "R7/3"

    def test_r10(self):
        assert to_reiwa(2028, 12) == "R10/12"

    def test_r1(self):
        assert to_reiwa(2019, 3) == "R1/3"


class TestParseReiwa:
    """parse_reiwa のテスト"""

    def test_r8_3(self):
        assert parse_reiwa("R8/3") == (2026, 3)

    def test_r10_12(self):
        assert parse_reiwa("R10/12") == (2028, 12)

    def test_invalid(self):
        assert parse_reiwa("2026/3") is None

    def test_empty(self):
        assert parse_reiwa("") is None


class TestDetectQuarter:
    """detect_quarter のテスト"""

    def test_first_quarter(self):
        assert detect_quarter("第1四半期決算短信") == "1Q"

    def test_second_quarter_fullwidth(self):
        assert detect_quarter("第２四半期決算短信") == "2Q"

    def test_third_quarter(self):
        assert detect_quarter("第3四半期決算短信") == "3Q"

    def test_full_year(self):
        assert detect_quarter("通期決算短信") == "4Q"

    def test_annual(self):
        assert detect_quarter("本決算") == "4Q"

    def test_english_quarter(self):
        assert detect_quarter("1st Quarter Financial Results") == "1Q"

    def test_full_year_english(self):
        assert detect_quarter("Full-Year Financial Results") == "4Q"

    def test_no_quarter(self):
        assert detect_quarter("配当のお知らせ") is None


class TestExtractFiscalYearFromTitle:
    """extract_fiscal_year_from_title のテスト"""

    def test_reiwa(self):
        assert extract_fiscal_year_from_title("令和8年3月期 第2四半期決算短信") == "R8/3"

    def test_reiwa_fullwidth(self):
        assert extract_fiscal_year_from_title("令和８年３月期 第１四半期") == "R8/3"

    def test_reiwa10(self):
        assert extract_fiscal_year_from_title("令和10年12月期 通期決算短信") == "R10/12"

    def test_ad_year(self):
        assert extract_fiscal_year_from_title("2026年3月期 第3四半期決算短信") == "R8/3"

    def test_no_year(self):
        assert extract_fiscal_year_from_title("決算短信") is None


class TestExtractFiscalInfo:
    """extract_fiscal_info 統合テスト"""

    def test_full_extraction(self):
        year, q = extract_fiscal_info("令和8年3月期 第2四半期決算短信〔IFRS〕（連結）")
        assert year == "R8/3"
        assert q == "2Q"

    def test_ad_year_with_quarter(self):
        year, q = extract_fiscal_info("2026年3月期 第1四半期決算短信")
        assert year == "R8/3"
        assert q == "1Q"

    def test_full_year(self):
        year, q = extract_fiscal_info("令和8年3月期 通期決算短信")
        assert year == "R8/3"
        assert q == "4Q"

    def test_year_from_text(self):
        year, q = extract_fiscal_info(
            "第3四半期決算短信",
            text="令和8年3月期 連結経営成績"
        )
        assert year == "R8/3"
        assert q == "3Q"
