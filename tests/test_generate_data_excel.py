"""
test_generate_data_excel.py — data.xlsx 生成ツールのユニットテスト
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import openpyxl
import pytest

# テスト対象
import sys
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from tools.generate_data_excel import (
    generate,
    _fye_to_label,
    _quarter_label,
    _fetch_all,
    HEADERS,
    SHEET_NAME,
    UserError,
)


# ============================================================
# ヘルパー: モック応答生成
# ============================================================
def _mock_companies(n=3):
    return [
        {"company_id": i + 1, "ticker_code": f"{i + 1:04d}"}
        for i in range(n)
    ]


def _mock_periods(n=3):
    return [
        {
            "period_id": i + 1,
            "company_id": i + 1,
            "fiscal_year_end": "2026-03-31",
            "quarter": 2,
        }
        for i in range(n)
    ]


def _mock_facts(n=3):
    rows = []
    for i in range(n):
        for metric in ("NET_SALES", "GROSS_PROFIT", "OP_INCOME"):
            rows.append({
                "company_id": i + 1,
                "period_id": i + 1,
                "disclosure_id": 100 + i,
                "metric": metric,
                "scope": "CONSOLIDATED",
                "value": (i + 1) * 1_000_000,
                "disclosed_at": "2026-02-28T10:00:00+09:00",
                "created_at": "2026-02-28T10:00:00+09:00",
            })
    return rows


def _mock_get_response(data_list):
    """requests.get のモックレスポンスを生成"""
    resp = MagicMock()
    resp.json.return_value = data_list
    resp.raise_for_status.return_value = None
    return resp


# ============================================================
# テスト
# ============================================================

class TestFyeToLabel:
    def test_normal(self):
        assert _fye_to_label("2026-03-31") == "R8/3"

    def test_december(self):
        assert _fye_to_label("2025-12-31") == "R7/12"

    def test_invalid(self):
        assert _fye_to_label("invalid") == "invalid"


class TestQuarterLabel:
    def test_1q(self):
        assert _quarter_label(1) == "1Q"

    def test_4q(self):
        assert _quarter_label(4) == "4Q"


class TestGenerate:
    @patch("tools.generate_data_excel.requests.get")
    def test_normal_generation(self, mock_get):
        """正常にdata.xlsxが生成される"""
        companies = _mock_companies(3)
        periods = _mock_periods(3)
        facts = _mock_facts(3)

        # 3回のAPI呼び出し（companies, periods, facts）
        mock_get.side_effect = [
            _mock_get_response(companies),
            _mock_get_response(periods),
            _mock_get_response(facts),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "test_data.xlsx")
            result = generate(
                output_path=out,
                supabase_url="https://test.supabase.co",
                supabase_key="test-key",
            )

            assert result["rows"] == 3
            assert result["errors"] == 0
            assert os.path.exists(out)

            # Excel内容確認
            wb = openpyxl.load_workbook(out)
            ws = wb[SHEET_NAME]

            # ヘッダー確認
            for col, h in enumerate(HEADERS, 1):
                assert ws.cell(row=1, column=col).value == h

            # データ行確認
            assert ws.cell(row=2, column=1).value is not None  # ticker
            assert ws.cell(row=2, column=4).value == 1_000_000  # net_sales

    @patch("tools.generate_data_excel.requests.get")
    def test_empty_data(self, mock_get):
        """空データでもヘッダーのみのExcelが生成される"""
        mock_get.side_effect = [
            _mock_get_response([]),  # companies
            _mock_get_response([]),  # periods
            _mock_get_response([]),  # facts
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "test_data.xlsx")
            result = generate(
                output_path=out,
                supabase_url="https://test.supabase.co",
                supabase_key="test-key",
            )

            assert result["rows"] == 0
            assert os.path.exists(out)

            wb = openpyxl.load_workbook(out)
            ws = wb[SHEET_NAME]
            # ヘッダーのみ
            assert ws.cell(row=1, column=1).value == "ticker"
            assert ws.cell(row=2, column=1).value is None

    @patch("tools.generate_data_excel.requests.get")
    def test_nine_columns(self, mock_get):
        """9列が正しい順序で出力される"""
        mock_get.side_effect = [
            _mock_get_response(_mock_companies(1)),
            _mock_get_response(_mock_periods(1)),
            _mock_get_response(_mock_facts(1)),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "test_data.xlsx")
            generate(
                output_path=out,
                supabase_url="https://test.supabase.co",
                supabase_key="test-key",
            )

            wb = openpyxl.load_workbook(out)
            ws = wb[SHEET_NAME]

            expected = [
                "ticker", "period", "quarter",
                "net_sales", "gross_profit", "operating_profit",
                "source_doc_id", "disclosed_at", "updated_at",
            ]
            for col, name in enumerate(expected, 1):
                assert ws.cell(row=1, column=col).value == name

    @patch("tools.generate_data_excel.requests.get")
    def test_atomic_save(self, mock_get):
        """原子的保存：tempファイルが残らない"""
        mock_get.side_effect = [
            _mock_get_response(_mock_companies(1)),
            _mock_get_response(_mock_periods(1)),
            _mock_get_response(_mock_facts(1)),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "test_data.xlsx")
            generate(
                output_path=out,
                supabase_url="https://test.supabase.co",
                supabase_key="test-key",
            )

            # 本体は存在
            assert os.path.exists(out)
            # tmpファイルは残っていない
            tmp_files = [f for f in os.listdir(tmpdir) if f.endswith(".tmp")]
            assert len(tmp_files) == 0

    @patch("tools.generate_data_excel._load_dotenv", return_value=False)
    @patch.dict(os.environ, {}, clear=True)
    def test_missing_credentials(self, mock_dotenv):
        """接続情報がない場合はUserErrorが出る"""
        with pytest.raises(UserError, match="接続情報が未設定"):
            generate(
                output_path="/tmp/test.xlsx",
                supabase_url="",
                supabase_key="",
            )

    @patch("tools.generate_data_excel.requests.get")
    def test_pagination_large_dataset(self, mock_get):
        """ページネーション: 2ページ分のデータが全件取得される"""
        # 1ページ目: 1000件, 2ページ目: 500件 (companies)
        page1_companies = [
            {"company_id": i, "ticker_code": f"{i:04d}"}
            for i in range(1, 1001)
        ]
        page2_companies = [
            {"company_id": i, "ticker_code": f"{i:04d}"}
            for i in range(1001, 1501)
        ]

        # periods: 全部同じ設定
        all_periods = [
            {"period_id": i, "company_id": i, "fiscal_year_end": "2026-03-31", "quarter": 2}
            for i in range(1, 1501)
        ]
        page1_periods = all_periods[:1000]
        page2_periods = all_periods[1000:]

        # facts: 最初の5社分だけ
        facts_data = []
        for i in range(1, 6):
            facts_data.append({
                "company_id": i, "period_id": i, "disclosure_id": i,
                "metric": "NET_SALES", "scope": "CONSOLIDATED",
                "value": i * 100, "disclosed_at": "2026-01-01T00:00:00+09:00",
                "created_at": "2026-01-01",
            })

        mock_get.side_effect = [
            _mock_get_response(page1_companies),
            _mock_get_response(page2_companies),
            _mock_get_response(page1_periods),
            _mock_get_response(page2_periods),
            _mock_get_response(facts_data),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "test_data.xlsx")
            result = generate(
                output_path=out,
                supabase_url="https://test.supabase.co",
                supabase_key="test-key",
            )

            assert result["rows"] == 5
            assert os.path.exists(out)
