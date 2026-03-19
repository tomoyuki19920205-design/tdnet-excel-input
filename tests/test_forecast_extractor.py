# ============================================================
# test_forecast_extractor.py — 予想修正・差異抽出のテスト
# ============================================================
import sys
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.extractor import (
    extract_forecast_targets,
    _find_label_row,
    _extract_table_values,
    _split_into_blocks,
    ACTUAL_B_LABELS,
    FORECAST_B_LABELS,
)
from src.year_parser import (
    detect_all_quarters,
    extract_all_fiscal_years,
)


class TestDetectAllQuarters:
    """detect_all_quarters のテスト"""

    def test_single_quarter(self):
        qs = detect_all_quarters("第2四半期連結業績")
        assert "2Q" in qs

    def test_full_year(self):
        qs = detect_all_quarters("通期連結業績予想")
        assert "4Q" in qs

    def test_multiple_quarters(self):
        text = "第2四半期（中間期）連結業績と通期連結業績予想"
        qs = detect_all_quarters(text)
        assert "2Q" in qs
        assert "4Q" in qs

    def test_chukanki(self):
        qs = detect_all_quarters("中間期連結業績")
        assert "2Q" in qs

    def test_english(self):
        qs = detect_all_quarters("1st Quarter and Full Year")
        assert "1Q" in qs
        assert "4Q" in qs

    def test_no_quarter(self):
        qs = detect_all_quarters("配当のお知らせ")
        assert len(qs) == 0


class TestExtractAllFiscalYears:
    """extract_all_fiscal_years のテスト"""

    def test_single_fy(self):
        fys = extract_all_fiscal_years("2026年3月期通期連結業績")
        assert "R8/3" in fys

    def test_multiple_fys(self):
        text = "2026年3月期第2四半期と2027年3月期通期"
        fys = extract_all_fiscal_years(text)
        assert "R8/3" in fys
        assert "R9/3" in fys

    def test_reiwa_format(self):
        fys = extract_all_fiscal_years("令和8年3月期通期")
        assert "R8/3" in fys


class TestFindLabelRow:
    """_find_label_row のテスト"""

    def test_actual_b_found(self):
        lines = [
            "前回予想（A）  10,000  1,000",
            "実績値（B）  12,000  1,200",
            "増減額（B-A）  2,000  200",
        ]
        result = _find_label_row(lines, ACTUAL_B_LABELS)
        assert result is not None
        assert result[0] == 1
        assert "実績値" in result[1]

    def test_forecast_b_found(self):
        lines = [
            "前回発表予想（A）  10,000  1,000",
            "今回修正予想（B）  12,000  1,200",
        ]
        result = _find_label_row(lines, FORECAST_B_LABELS)
        assert result is not None
        assert result[0] == 1

    def test_halfwidth_paren(self):
        lines = [
            "前回予想(A)  10,000",
            "今回修正予想(B)  12,000",
        ]
        result = _find_label_row(lines, FORECAST_B_LABELS)
        assert result is not None

    def test_not_found(self):
        lines = ["何も関係ない行"]
        assert _find_label_row(lines, ACTUAL_B_LABELS) is None


class TestExtractTableValues:
    """_extract_table_values のテスト"""

    def test_two_values(self):
        lines = [
            "前回予想（A）  10,000  1,000",
            "今回修正予想（B）  48,500  5,150",
            "増減額  500  150",
        ]
        result = _extract_table_values(lines, 1, "今回修正予想（B）")
        assert result["sales"] == 48500
        assert result["operating_profit"] == 5150

    def test_actual_b_values(self):
        lines = [
            "予想（A）  30,000  3,000",
            "実績値（B）  24,000  2,500",
        ]
        result = _extract_table_values(lines, 1, "実績値（B）")
        assert result["sales"] == 24000
        assert result["operating_profit"] == 2500

    def test_negative_values(self):
        lines = [
            "今回修正予想（B）  48,500  △1,200",
        ]
        result = _extract_table_values(lines, 0, "今回修正予想（B）")
        assert result["sales"] == 48500
        assert result["operating_profit"] == -1200


class TestSplitIntoBlocks:
    """_split_into_blocks のテスト"""

    def test_basic_split(self):
        lines = [
            "2026年3月期第2四半期 連結業績",
            "売上高  営業利益",
            "予想（A）  10,000  1,000",
            "実績値（B）  12,000  1,200",
            "",
            "2026年3月期通期 連結業績予想",
            "売上高  営業利益",
            "前回予想（A）  40,000  4,000",
            "今回修正予想（B）  48,500  5,150",
        ]
        blocks = _split_into_blocks(lines)
        assert len(blocks) >= 2

    def test_no_empty_lines(self):
        lines = [
            "今回修正予想（B）  48,500  5,150",
        ]
        blocks = _split_into_blocks(lines)
        assert len(blocks) == 1


class TestExtractForecastTargetsIntegration:
    """extract_forecast_targets の統合テスト（PDFモック使用）"""

    def _mock_pdf(self, pages_text: list[str]):
        """pdfplumber のモックを生成"""
        mock_pdf = MagicMock()
        mock_pages = []
        for text in pages_text:
            page = MagicMock()
            page.extract_text.return_value = text
            mock_pages.append(page)
        mock_pdf.pages = mock_pages
        mock_pdf.__enter__ = lambda self: mock_pdf
        mock_pdf.__exit__ = lambda self, *a: None
        return mock_pdf

    @patch("src.extractor.pdfplumber", create=True)
    def test_single_4q(self, mock_pdfplumber):
        """通期のみの修正予想"""
        page_text = """
2026年3月期 通期連結業績予想の修正に関するお知らせ
（単位：百万円）

売上高  営業利益  経常利益  当期純利益
前回発表予想（A）  45,000  4,500  4,600  3,000
今回修正予想（B）  48,500  5,150  5,200  3,400
増減額（B-A）  3,500  650  600  400
"""
        mock_pdfplumber.open.return_value = self._mock_pdf([page_text])

        targets = extract_forecast_targets("dummy.pdf", "2026年3月期通期連結業績予想の修正")
        assert len(targets) >= 1
        t = targets[0]
        assert t.fiscal_year == "R8/3"
        assert t.quarter == "4Q"
        assert t.sales == 48500
        assert t.operating_profit == 5150
        assert t.source == "forecastB"

    @patch("src.extractor.pdfplumber", create=True)
    def test_single_2q_actual(self, mock_pdfplumber):
        """2Qのみの差異（実績値B）"""
        page_text = """
2026年3月期 第2四半期（中間期）連結業績予想と実績値との差異
（単位：百万円）

売上高  営業利益  経常利益
予想（A）  20,000  2,000  2,100
実績値（B）  24,000  2,500  2,600
増減額（B-A）  4,000  500  500
"""
        mock_pdfplumber.open.return_value = self._mock_pdf([page_text])

        targets = extract_forecast_targets(
            "dummy.pdf",
            "2026年3月期第2四半期業績予想と実績値との差異"
        )
        assert len(targets) >= 1
        t = targets[0]
        assert t.fiscal_year == "R8/3"
        assert t.quarter == "2Q"
        assert t.source == "actualB"
        assert t.sales == 24000
        assert t.operating_profit == 2500

    @patch("src.extractor.pdfplumber", create=True)
    def test_2q_and_4q_combined(self, mock_pdfplumber):
        """2Q差異 + 4Q修正予想 同時開示"""
        page_text = """
2026年3月期 第2四半期（中間期）連結業績予想と実績値との差異
および2026年3月期 通期連結業績予想数値の修正に関するお知らせ
（単位：百万円）

1. 2026年3月期 第2四半期 連結業績
売上高  営業利益
予想（A）  20,000  2,000
実績値（B）  24,000  2,500

2. 2026年3月期 通期連結業績予想
売上高  営業利益
前回予想（A）  40,000  4,000
今回修正予想（B）  48,500  5,150
"""
        mock_pdfplumber.open.return_value = self._mock_pdf([page_text])

        targets = extract_forecast_targets(
            "dummy.pdf",
            "2026年3月期第2四半期業績予想と実績値との差異および通期連結業績予想の修正"
        )
        assert len(targets) >= 2
        quarters = {t.quarter for t in targets}
        assert "2Q" in quarters
        assert "4Q" in quarters

        # 2Qは実績値B、4Qは修正予想B
        t2q = [t for t in targets if t.quarter == "2Q"][0]
        t4q = [t for t in targets if t.quarter == "4Q"][0]
        assert t2q.source == "actualB"
        assert t4q.source == "forecastB"

    @patch("src.extractor.pdfplumber", create=True)
    def test_thousand_yen_unit(self, mock_pdfplumber):
        """千円単位"""
        page_text = """
2026年3月期 通期業績予想の修正
（単位：千円）

売上高  営業利益
前回予想（A）  45,000,000  4,500,000
今回修正予想（B）  48,500,000  5,150,000
"""
        mock_pdfplumber.open.return_value = self._mock_pdf([page_text])

        targets = extract_forecast_targets("dummy.pdf", "2026年3月期通期業績予想の修正")
        assert len(targets) >= 1
        assert targets[0].source_unit == "千円"

    @patch("src.extractor.pdfplumber", create=True)
    def test_future_fy_skipped(self, mock_pdfplumber):
        """複数年度：未来年度はスキップ"""
        page_text = """
2026年3月期 第2四半期 連結業績予想と実績値との差異
および2027年3月期 通期連結業績予想数値の修正

1. 2026年3月期 第2四半期
（単位：百万円）
売上高  営業利益
予想（A）  20,000  2,000
実績値（B）  24,000  2,500

2. 2027年3月期 通期
（単位：百万円）
売上高  営業利益
前回予想（A）  50,000  5,000
今回修正予想（B）  55,000  5,800
"""
        mock_pdfplumber.open.return_value = self._mock_pdf([page_text])

        targets = extract_forecast_targets(
            "dummy.pdf",
            "2026年3月期第2四半期業績と2027年3月期通期予想の修正"
        )
        # 2026年のみ対象、2027年はスキップ
        for t in targets:
            assert t.fiscal_year == "R8/3", f"未来年度が混入: {t.fiscal_year}"
