"""tests/test_backfill_worker.py — worker モジュールのテスト

extractor/downloader を mock して process_one_filing の振る舞いを検証。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from unittest.mock import patch, MagicMock

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.backfill.listing_sources.base import FilingInfo
from lib.backfill.worker import process_one_filing, compute_result_fingerprint


def _make_filing(**kwargs) -> FilingInfo:
    defaults = dict(
        filing_id="test_fid_001",
        ticker="6750",
        title="2025年3月期 決算短信",
        disclosure_date="2025-05-15",
        doc_url="https://example.com/doc.pdf",
        xbrl_url=None,
        doc_type="financial_statement",
        company_name="テスト株式会社",
        published_at="2025-05-15 15:00",
        listing_source="tdnet_html",
        has_xbrl=False,
    )
    defaults.update(kwargs)
    return FilingInfo(**defaults)


@dataclass
class MockExtracted:
    fiscal_year: str = "2025-03-31"
    quarter: str = "4Q"
    sales: float = 1000000
    operating_profit: float = 200000
    gross_profit: float | None = None
    cost_of_sales: float | None = None
    source: str = "xbrl"


@dataclass
class MockSegment:
    segment_name: str = "セグメントA"
    segment_order: int = 1
    segment_sales: float = 500000
    segment_profit: float = 100000
    raw_profit_label: str = "営業利益"
    raw_text: str = ""


class TestProcessOneFiling:
    """process_one_filing の基本テスト。"""

    @patch("lib.backfill.worker.shutil")
    @patch("src.extractor.extract_segment_financials")
    @patch("src.extractor.extract_financials")
    @patch("src.downloader.download_document")
    def test_success_with_segments(
        self, mock_dl, mock_fin, mock_seg, mock_shutil, tmp_path
    ):
        """PDF ダウンロード → PL 抽出 → セグメント抽出が成功。"""
        cache_root = str(tmp_path / "cache")
        pdf_path = str(tmp_path / "cache" / "test_fid_001" / "source.pdf")

        # download_document → pdf path
        mock_dl.return_value = pdf_path
        # shutil.copy2 はスキップ
        mock_shutil.copy2 = MagicMock()

        # extract_financials
        mock_fin.return_value = (MockExtracted(), "")

        # extract_segment_financials
        mock_seg.return_value = (
            [MockSegment(), MockSegment(segment_name="セグメントB", segment_order=2)],
            "",
        )

        # source.pdf を作る (has_pdf チェック回避)
        os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4 dummy")

        filing = _make_filing()
        result = process_one_filing(filing, cache_root=cache_root)

        assert result.status == "ok"
        assert len(result.segment_records) == 2
        assert result.via is not None
        assert result.result_fingerprint is not None
        assert result.metrics["total_ms"] >= 0

    @patch("src.extractor.extract_segment_financials")
    @patch("src.extractor.extract_financials")
    @patch("src.downloader.download_document")
    def test_quarantined_no_segments(
        self, mock_dl, mock_fin, mock_seg, tmp_path
    ):
        """セグメント抽出結果が空のとき quarantined。"""
        cache_root = str(tmp_path / "cache")
        pdf_path = str(tmp_path / "cache" / "test_fid_001" / "source.pdf")

        os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4 dummy")

        mock_dl.return_value = pdf_path
        mock_fin.return_value = (MockExtracted(), "")
        mock_seg.return_value = ([], "no_segment_table_found")

        result = process_one_filing(_make_filing(), cache_root=cache_root)
        assert result.status == "quarantined"
        assert result.quarantine is not None
        assert "no_segment" in result.quarantine.get("error_message", "")

    @patch("src.downloader.download_document")
    def test_failed_no_document(self, mock_dl, tmp_path):
        """ダウンロード失敗で failed。"""
        mock_dl.return_value = None
        result = process_one_filing(
            _make_filing(), cache_root=str(tmp_path / "cache")
        )
        assert result.status == "failed"


class TestResultFingerprint:
    def test_deterministic(self):
        recs = [
            {"ticker": "6750", "period": "2025-03-31", "quarter": "4Q",
             "segment_name": "A", "segment_sales": 100},
            {"ticker": "6750", "period": "2025-03-31", "quarter": "4Q",
             "segment_name": "B", "segment_sales": 200},
        ]
        fp1 = compute_result_fingerprint(recs)
        fp2 = compute_result_fingerprint(recs)
        assert fp1 == fp2

    def test_order_independent(self):
        recs1 = [
            {"ticker": "6750", "period": "2025-03-31", "quarter": "4Q",
             "segment_name": "A", "segment_sales": 100},
            {"ticker": "6750", "period": "2025-03-31", "quarter": "4Q",
             "segment_name": "B", "segment_sales": 200},
        ]
        recs2 = list(reversed(recs1))
        assert compute_result_fingerprint(recs1) == compute_result_fingerprint(recs2)

    def test_different_data_different_fingerprint(self):
        recs1 = [{"ticker": "6750", "segment_name": "A", "segment_sales": 100}]
        recs2 = [{"ticker": "6750", "segment_name": "A", "segment_sales": 999}]
        assert compute_result_fingerprint(recs1) != compute_result_fingerprint(recs2)

    def test_empty_returns_empty(self):
        assert compute_result_fingerprint([]) == "empty"
