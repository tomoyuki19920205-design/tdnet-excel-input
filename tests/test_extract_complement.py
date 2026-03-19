"""
test_extract_complement.py — per-field PDF 補完のユニットテスト

テストケース:
1. XBRL で全項目取得 → PDF 補完なし
2. XBRL で gp=None → PDF から gp 補完
3. XBRL で gp=None + PDF でも gp=None → None のまま
4. XBRL 完全失敗 → PDF 全項目抽出 (既存 fallback)
5. field_sources が正しく設定される
6. validation: gp > sales → 補完拒否
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# プロジェクトルートを PATH に追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import ExtractedFinancials
from src.extractor import extract_financials


# ============================================================
# ヘルパー: ダミー PDF ファイル生成 (空ファイルで OK — _extract_from_pdf をモック)
# ============================================================
@pytest.fixture
def dummy_pdf(tmp_path):
    """空の PDF ファイルを生成"""
    pdf = tmp_path / "dummy.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")
    return str(pdf)


@pytest.fixture
def dummy_xbrl(tmp_path):
    """空の XBRL ファイルを生成"""
    xbrl = tmp_path / "dummy.xbrl"
    xbrl.write_bytes(b"<xbrl>dummy</xbrl>")
    return str(xbrl)


# ============================================================
# Case 1: XBRL で全項目取得 → PDF 補完なし
# ============================================================
class TestXbrlAllFields:
    """XBRL が sales/gp/op 全て返す場合、PDF は呼ばれない"""

    @patch("src.extractor._extract_from_xbrl")
    @patch("src.extractor._extract_from_pdf")
    @patch("src.extractor.extract_fiscal_info", return_value=("R8/3", "1Q"))
    @patch("src.extractor.pdfplumber")
    def test_no_pdf_fallback_when_xbrl_complete(
        self, mock_pdfp, mock_fiscal, mock_pdf, mock_xbrl, dummy_pdf, dummy_xbrl
    ):
        mock_xbrl.return_value = ExtractedFinancials(
            sales=1000, gross_profit=300, operating_profit=100,
            source_unit="円", confidence="high",
            field_sources={"sales": "xbrl", "gross_profit": "xbrl", "operating_profit": "xbrl"},
        )
        mock_pdfp.open.return_value.__enter__ = MagicMock()
        mock_pdfp.open.return_value.__exit__ = MagicMock()

        result, err = extract_financials(dummy_pdf, "第1四半期決算短信", dummy_xbrl)

        assert result is not None
        assert err == ""
        assert result.sales == 1000
        assert result.gross_profit == 300
        assert result.operating_profit == 100
        # PDF 抽出は呼ばれない
        mock_pdf.assert_not_called()
        # field_sources は全て xbrl
        assert result.field_sources.get("sales") == "xbrl"
        assert result.field_sources.get("gross_profit") == "xbrl"
        assert result.field_sources.get("operating_profit") == "xbrl"


# ============================================================
# Case 2: XBRL で gp=None → PDF から gp 補完
# ============================================================
class TestPdfFallbackGrossProfit:
    """XBRL で gp=None の場合、PDF から補完される"""

    @patch("src.extractor._extract_from_xbrl")
    @patch("src.extractor._extract_from_pdf")
    @patch("src.extractor.extract_fiscal_info", return_value=("R8/3", "1Q"))
    @patch("src.extractor.pdfplumber")
    def test_pdf_complements_missing_gp(
        self, mock_pdfp, mock_fiscal, mock_pdf, mock_xbrl, dummy_pdf, dummy_xbrl
    ):
        mock_xbrl.return_value = ExtractedFinancials(
            sales=1000, gross_profit=None, operating_profit=100,
            source_unit="円", confidence="high",
            field_sources={"sales": "xbrl", "operating_profit": "xbrl"},
        )
        mock_pdf.return_value = (
            ExtractedFinancials(
                sales=1000, gross_profit=300, operating_profit=100,
                source_unit="百万円", confidence="medium",
                field_sources={"sales": "pdf", "gross_profit": "pdf", "operating_profit": "pdf"},
            ),
            "",
        )
        mock_pdfp.open.return_value.__enter__ = MagicMock()
        mock_pdfp.open.return_value.__exit__ = MagicMock()

        result, err = extract_financials(dummy_pdf, "第1四半期決算短信", dummy_xbrl)

        assert result is not None
        assert result.sales == 1000       # XBRL のまま
        assert result.gross_profit == 300  # PDF から補完
        assert result.operating_profit == 100  # XBRL のまま
        assert result.field_sources["sales"] == "xbrl"
        assert result.field_sources["gross_profit"] == "pdf_fallback"
        assert result.field_sources["operating_profit"] == "xbrl"


# ============================================================
# Case 3: XBRL で gp=None + PDF でも gp=None → None のまま
# ============================================================
class TestPdfFallbackAlsoNone:
    """PDF でも gp=None の場合、結果は None のまま"""

    @patch("src.extractor._extract_from_xbrl")
    @patch("src.extractor._extract_from_pdf")
    @patch("src.extractor.extract_fiscal_info", return_value=("R8/3", "1Q"))
    @patch("src.extractor.pdfplumber")
    def test_gp_remains_none_when_pdf_also_none(
        self, mock_pdfp, mock_fiscal, mock_pdf, mock_xbrl, dummy_pdf, dummy_xbrl
    ):
        mock_xbrl.return_value = ExtractedFinancials(
            sales=1000, gross_profit=None, operating_profit=100,
            source_unit="円", confidence="high",
            field_sources={"sales": "xbrl", "operating_profit": "xbrl"},
        )
        mock_pdf.return_value = (
            ExtractedFinancials(
                sales=1000, gross_profit=None, operating_profit=100,
                source_unit="百万円", confidence="medium",
                field_sources={"sales": "pdf", "operating_profit": "pdf"},
            ),
            "",
        )
        mock_pdfp.open.return_value.__enter__ = MagicMock()
        mock_pdfp.open.return_value.__exit__ = MagicMock()

        result, err = extract_financials(dummy_pdf, "第1四半期決算短信", dummy_xbrl)

        assert result is not None
        assert result.gross_profit is None
        assert "gross_profit" not in result.field_sources


# ============================================================
# Case 4: XBRL 完全失敗 → PDF 全項目抽出 (既存 fallback)
# ============================================================
class TestXbrlFailsPdfFallback:
    """XBRL が None を返す場合、PDF から全項目取得"""

    @patch("src.extractor._extract_from_xbrl")
    @patch("src.extractor._extract_from_pdf")
    @patch("src.extractor.extract_fiscal_info", return_value=("R8/3", "1Q"))
    @patch("src.extractor.pdfplumber")
    def test_full_pdf_fallback(
        self, mock_pdfp, mock_fiscal, mock_pdf, mock_xbrl, dummy_pdf, dummy_xbrl
    ):
        mock_xbrl.return_value = None  # XBRL 完全失敗
        mock_pdf.return_value = (
            ExtractedFinancials(
                sales=2000, gross_profit=600, operating_profit=200,
                source_unit="百万円", confidence="medium",
                field_sources={"sales": "pdf", "gross_profit": "pdf", "operating_profit": "pdf"},
            ),
            "",
        )
        mock_pdfp.open.return_value.__enter__ = MagicMock()
        mock_pdfp.open.return_value.__exit__ = MagicMock()

        result, err = extract_financials(dummy_pdf, "第1四半期決算短信", dummy_xbrl)

        assert result is not None
        assert result.sales == 2000
        assert result.gross_profit == 600
        assert result.operating_profit == 200
        assert result.field_sources.get("sales") == "pdf"


# ============================================================
# Case 5: XBRL 成功 + 非決算短信タイトル → PDF 補完スキップ
# ============================================================
class TestNotTanshinNoPdfFallback:
    """タイトルが決算短信でない場合、PDF 補完を試みない"""

    @patch("src.extractor._extract_from_xbrl")
    @patch("src.extractor._extract_from_pdf")
    @patch("src.extractor.extract_fiscal_info", return_value=("R8/3", "1Q"))
    @patch("src.extractor.pdfplumber")
    def test_no_pdf_for_non_tanshin(
        self, mock_pdfp, mock_fiscal, mock_pdf, mock_xbrl, dummy_pdf, dummy_xbrl
    ):
        mock_xbrl.return_value = ExtractedFinancials(
            sales=1000, gross_profit=None, operating_profit=100,
            source_unit="円", confidence="high",
            field_sources={"sales": "xbrl", "operating_profit": "xbrl"},
        )
        mock_pdfp.open.return_value.__enter__ = MagicMock()
        mock_pdfp.open.return_value.__exit__ = MagicMock()

        result, err = extract_financials(dummy_pdf, "業績予想の修正に関するお知らせ", dummy_xbrl)

        assert result is not None
        assert result.gross_profit is None
        # PDF は非決算短信なので呼ばれない
        mock_pdf.assert_not_called()


# ============================================================
# Case 6: Validation — gp > sales → 補完拒否
# ============================================================
class TestValidationGpExceedsSales:
    """PDF の gp が sales を超える場合、quarantine として拒否"""

    @patch("src.extractor._extract_from_xbrl")
    @patch("src.extractor._extract_from_pdf")
    @patch("src.extractor.extract_fiscal_info", return_value=("R8/3", "1Q"))
    @patch("src.extractor.pdfplumber")
    def test_gp_exceeds_sales_rejected(
        self, mock_pdfp, mock_fiscal, mock_pdf, mock_xbrl, dummy_pdf, dummy_xbrl
    ):
        mock_xbrl.return_value = ExtractedFinancials(
            sales=1000, gross_profit=None, operating_profit=100,
            source_unit="円", confidence="high",
            field_sources={"sales": "xbrl", "operating_profit": "xbrl"},
        )
        # PDF が gp=5000 を返す (> sales=1000)
        mock_pdf.return_value = (
            ExtractedFinancials(
                sales=1000, gross_profit=5000, operating_profit=100,
                source_unit="百万円", confidence="medium",
                field_sources={"sales": "pdf", "gross_profit": "pdf", "operating_profit": "pdf"},
            ),
            "",
        )
        mock_pdfp.open.return_value.__enter__ = MagicMock()
        mock_pdfp.open.return_value.__exit__ = MagicMock()

        result, err = extract_financials(dummy_pdf, "第1四半期決算短信", dummy_xbrl)

        assert result is not None
        # gp は拒否されて None のまま
        assert result.gross_profit is None
        assert "gross_profit" not in result.field_sources


# ============================================================
# Case 7: field_sources の初期値テスト
# ============================================================
class TestFieldSourcesModel:
    """ExtractedFinancials の field_sources が正しく動作する"""

    def test_default_empty(self):
        ef = ExtractedFinancials()
        assert ef.field_sources == {}

    def test_with_sources(self):
        ef = ExtractedFinancials(
            sales=100, gross_profit=50,
            field_sources={"sales": "xbrl", "gross_profit": "pdf_fallback"},
        )
        assert ef.field_sources["sales"] == "xbrl"
        assert ef.field_sources["gross_profit"] == "pdf_fallback"
