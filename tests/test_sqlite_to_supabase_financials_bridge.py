# ============================================================
# test_sqlite_to_supabase_financials_bridge.py
# TDnet → public.financials ブリッジのユニットテスト
# ============================================================
"""
テスト対象:
  - ticker 正規化 (common_ticker 経由)
  - quarter 正規化 (4Q → FY 等)
  - 単位変換 (_convert_tdnet_amount: 百万円/千円 → 円)
  - 名証銘柄 (1832, 6623) のブリッジ
  - 英字付きコード (418A, 429A) の安全性

Note: quarterly_results の金額は extractor が iXBRL scale 適用済みで
常に円建て。_build_financials_rows_from_tdnet では再スケーリングしない。
"""
import sys
import math
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.sqlite_to_supabase import (
    _coerce_numeric,
    _normalize_financials_quarter,
    _convert_tdnet_amount,
    _build_financials_rows_from_tdnet,
)
from src.common_ticker import normalize_ticker


# ============================================================
# 1. ticker正規化テスト (common_ticker 経由)
# ============================================================
class TestTickerNormalization:
    """ticker正規化テスト"""

    def test_18320_to_1832(self):
        assert normalize_ticker("18320") == "1832"

    def test_66230_to_6623(self):
        assert normalize_ticker("66230") == "6623"

    def test_429A0_to_429A(self):
        assert normalize_ticker("429A0") == "429A"

    def test_418A0_to_418A(self):
        assert normalize_ticker("418A0") == "418A"


# ============================================================
# 2. quarter正規化テスト
# ============================================================
class TestQuarterNormalization:
    """quarter正規化テスト"""

    def test_1Q(self):
        assert _normalize_financials_quarter("1Q") == "1Q"

    def test_2Q(self):
        assert _normalize_financials_quarter("2Q") == "2Q"

    def test_3Q(self):
        assert _normalize_financials_quarter("3Q") == "3Q"

    def test_4Q_to_FY(self):
        """4Q は FY に変換される"""
        assert _normalize_financials_quarter("4Q") == "FY"

    def test_FY(self):
        assert _normalize_financials_quarter("FY") == "FY"

    def test_numeric_1(self):
        assert _normalize_financials_quarter("1") == "1Q"

    def test_numeric_4(self):
        assert _normalize_financials_quarter("4") == "FY"

    def test_invalid(self):
        assert _normalize_financials_quarter("invalid") is None

    def test_lowercase(self):
        """小文字の 1q も 1Q に変換"""
        assert _normalize_financials_quarter("1q") == "1Q"


# ============================================================
# 3. 単位変換テスト
# ============================================================
class TestUnitConversion:
    """TDnet百万円 → public.financials円 の変換テスト"""

    def test_million_yen(self):
        """70800百万円 → 70,800,000,000円"""
        result = _convert_tdnet_amount(70800.0, "百万円")
        assert result == 70_800_000_000

    def test_thousand_yen(self):
        """1000千円 → 1,000,000円"""
        result = _convert_tdnet_amount(1000.0, "千円")
        assert result == 1_000_000

    def test_yen(self):
        """1000円 → 1000円"""
        result = _convert_tdnet_amount(1000.0, "円")
        assert result == 1000

    def test_none_returns_none(self):
        assert _convert_tdnet_amount(None, "百万円") is None

    def test_empty_string_returns_none(self):
        assert _convert_tdnet_amount("", "百万円") is None

    def test_nan_returns_none(self):
        assert _convert_tdnet_amount(float("nan"), "百万円") is None

    def test_inf_returns_none(self):
        assert _convert_tdnet_amount(float("inf"), "百万円") is None


# ============================================================
# 4. _coerce_numeric テスト
# ============================================================
class TestCoerceNumeric:

    def test_float(self):
        assert _coerce_numeric(70800.0) == 70800.0

    def test_int(self):
        assert _coerce_numeric(100) == 100.0

    def test_string_number(self):
        assert _coerce_numeric("123.45") == 123.45

    def test_none(self):
        assert _coerce_numeric(None) is None

    def test_empty(self):
        assert _coerce_numeric("") is None

    def test_nan(self):
        assert _coerce_numeric(float("nan")) is None

    def test_inf(self):
        assert _coerce_numeric(float("inf")) is None

    def test_non_numeric(self):
        assert _coerce_numeric("abc") is None


# ============================================================
# 5. 名証銘柄ブリッジテスト
# ============================================================
def _make_sqlite_row(**kwargs):
    """テスト用SQLite行を作成。
    quarterly_results は円建て (extractor が scale 適用済み)。
    """
    defaults = {
        "id": 1,
        "company_code": "18320",
        "fiscal_year_end": "2026-03-31",
        "quarter": "3Q",
        "sales": 49_169_000_000.0,       # 円建て
        "gross_profit": 4_880_000_000.0,  # 円建て
        "operating_profit": 2_799_000_000.0,  # 円建て
        "unit": "百万円",
        "created_at": "2026-03-01",
        "updated_at": "2026-03-01",
        "source_doc_id": "test",
        "source_url": None,
        "zip_hash": None,
        "parser_version": "v2",
    }
    defaults.update(kwargs)
    return defaults


class TestBuildFinancialsFromTdnet:
    """名証銘柄ブリッジテスト"""

    def test_1832_row_built(self):
        """1832の行が正しく構築される"""
        rows = [_make_sqlite_row(company_code="18320")]
        result = _build_financials_rows_from_tdnet(rows)
        assert len(result) == 1
        assert result[0]["ticker"] == "1832"
        assert result[0]["period"] == "2026-03-31"
        assert result[0]["quarter"] == "3Q"
        # 円建てそのまま (再スケーリングなし)
        assert result[0]["sales"] == 49_169_000_000
        assert result[0]["gross_profit"] == 4_880_000_000
        assert result[0]["operating_profit"] == 2_799_000_000
        assert result[0]["source"] == "tdnet"

    def test_6623_row_built(self):
        """6623の行が正しく構築される"""
        rows = [_make_sqlite_row(
            company_code="66230",
            sales=92_756_000_000.0,       # 円建て
            gross_profit=15_653_000_000.0,
            operating_profit=8_303_000_000.0,
        )]
        result = _build_financials_rows_from_tdnet(rows)
        assert len(result) == 1
        assert result[0]["ticker"] == "6623"
        assert result[0]["sales"] == 92_756_000_000

    def test_4Q_becomes_FY(self):
        """4Q は FY に変換される"""
        rows = [_make_sqlite_row(quarter="4Q")]
        result = _build_financials_rows_from_tdnet(rows)
        assert result[0]["quarter"] == "FY"

    def test_all_null_values_skipped(self):
        """sales/gross_profit/operating_profit全てNullなら行スキップ"""
        rows = [_make_sqlite_row(
            sales=None, gross_profit=None, operating_profit=None
        )]
        result = _build_financials_rows_from_tdnet(rows)
        assert len(result) == 0

    def test_partial_null_kept(self):
        """一部がNullでも他がある場合は行を保持"""
        rows = [_make_sqlite_row(sales=100_000_000.0, gross_profit=None)]
        result = _build_financials_rows_from_tdnet(rows)
        assert len(result) == 1
        assert result[0]["sales"] == 100_000_000
        assert result[0]["gross_profit"] is None


# ============================================================
# 6. 英字付きコードテスト
# ============================================================
class TestAlphaTickerBridge:
    """英字付きコードのブリッジテスト"""

    def test_418A_passthrough(self):
        """418Aコード（4桁）はそのまま通る"""
        rows = [_make_sqlite_row(
            company_code="418A",
            sales=500.0,
        )]
        result = _build_financials_rows_from_tdnet(rows)
        assert len(result) == 1
        assert result[0]["ticker"] == "418A"

    def test_429A0_normalized(self):
        """429A0（5桁）→429Aに正規化"""
        rows = [_make_sqlite_row(
            company_code="429A0",
            sales=1200.0,
        )]
        result = _build_financials_rows_from_tdnet(rows)
        assert len(result) == 1
        assert result[0]["ticker"] == "429A"

    def test_429A_not_broken(self):
        """429A（既に4桁）はそのまま通る"""
        rows = [_make_sqlite_row(
            company_code="429A",
            sales=1200.0,
        )]
        result = _build_financials_rows_from_tdnet(rows)
        assert len(result) == 1
        assert result[0]["ticker"] == "429A"
