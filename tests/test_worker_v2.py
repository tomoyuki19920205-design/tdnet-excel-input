"""tests/test_worker_v2.py — worker_v2 モジュールのテスト

extractor/downloader を mock して process_one_filing_v2 の
全分岐を検証する。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from unittest.mock import patch, MagicMock

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.backfill.listing_sources.base import FilingInfo
from lib.backfill.worker_v2 import (
    process_one_filing_v2,
    FilingResultV2,
    SourceCandidate,
    _select_best_candidate,
    validator_status_to_worker,
)
from src.segment.extraction_result_validator import (
    ExtractionStatus,
    ExtractionValidationResult,
    HardFailReason,
)


# ============================================================
# テスト用ヘルパー
# ============================================================

def _make_filing(**kwargs) -> FilingInfo:
    defaults = dict(
        filing_id="test_v2_001",
        ticker="6750",
        title="2025年3月期 決算短信",
        disclosure_date="2025-05-15",
        doc_url="https://example.com/doc.pdf",
        xbrl_url="https://example.com/xbrl.zip",
        doc_type="financial_statement",
        company_name="テスト株式会社",
        published_at="2025-05-15 15:00",
        listing_source="tdnet_html",
        has_xbrl=True,
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
class MockXbrlRow:
    raw_segment_name: str = "電子部品事業"
    normalized_segment_name: str = "電子部品事業"
    sales: float = 500000
    profit: float = 100000
    period: str = "2025-03-31"
    quarter: str = "4Q"


@dataclass
class MockSegment:
    segment_name: str = "電子部品事業"
    segment_order: int = 1
    segment_sales: float = 500000
    segment_profit: float = 100000
    raw_profit_label: str = "営業利益"
    raw_text: str = ""


def _good_xbrl_rows(count: int = 3) -> list:
    names = ["電子部品事業", "自動車部門", "ヘルスケア関連", "半導体製品", "クラウドサービス"]
    return [
        MockXbrlRow(
            raw_segment_name=n, normalized_segment_name=n,
            sales=500000 * (i + 1), profit=100000 * (i + 1),
        )
        for i, n in enumerate(names[:count])
    ]


def _good_pdf_segments(count: int = 3) -> list:
    names = ["電子部品事業", "自動車部門", "ヘルスケア関連", "半導体製品", "クラウドサービス"]
    return [
        MockSegment(
            segment_name=n, segment_order=i + 1,
            segment_sales=500000 * (i + 1), segment_profit=100000 * (i + 1),
        )
        for i, n in enumerate(names[:count])
    ]


def _pl_only_xbrl_rows() -> list:
    """PL勘定科目だけの XBRL 行 → validator で quarantine。"""
    names = ["売上原価", "営業利益", "経常利益"]
    return [
        MockXbrlRow(
            raw_segment_name=n, normalized_segment_name=n,
            sales=0, profit=0,
        )
        for n in names
    ]


def _setup_paths(tmp_path):
    """cache dir に source.pdf と xbrl.zip を作る。"""
    cache_root = str(tmp_path / "cache")
    fid = "test_v2_001"
    pdf_path = tmp_path / "cache" / fid / "source.pdf"
    xbrl_path = tmp_path / "cache" / fid / "xbrl.zip"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4 dummy")
    xbrl_path.write_bytes(b"PK dummy zip")
    return cache_root, str(pdf_path), str(xbrl_path)


# ============================================================
# 1. XBRL success → ok
# ============================================================

class TestXbrlSuccessNoFallback:
    @patch("lib.backfill.worker.shutil")
    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("src.extractor.extract_financials")
    @patch("src.downloader.download_document")
    def test_xbrl_success_no_fallback(
        self, mock_dl, mock_fin, mock_xbrl_extract, mock_shutil, tmp_path
    ):
        cache_root, pdf_path, xbrl_path = _setup_paths(tmp_path)
        mock_dl.return_value = pdf_path
        mock_shutil.copy2 = MagicMock()
        mock_fin.return_value = (MockExtracted(), "")
        mock_xbrl_extract.return_value = _good_xbrl_rows(3)

        filing = _make_filing()
        result = process_one_filing_v2(filing, cache_root=cache_root)

        assert result.status == "ok"
        assert result.selected_path == "xbrl"
        assert result.source == "xbrl"
        assert not result.fallback_used
        assert result.fallback_reason == ""
        assert result.confidence >= 0.9
        assert result.valid_segment_count >= 2
        assert result.hard_fail_reason == ""
        assert result.route_mode == "xbrl_v2"


# ============================================================
# 2. XBRL quarantine → quarantined (PDF disabled)
# ============================================================

class TestXbrlQuarantineNoPdfFallback:
    @patch("lib.backfill.worker.shutil")
    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("src.extractor.extract_financials")
    @patch("src.downloader.download_document")
    def test_xbrl_quarantine_stays_quarantined(
        self, mock_dl, mock_fin, mock_xbrl_extract, mock_shutil, tmp_path
    ):
        cache_root, pdf_path, xbrl_path = _setup_paths(tmp_path)
        mock_dl.return_value = pdf_path
        mock_shutil.copy2 = MagicMock()
        mock_fin.return_value = (MockExtracted(), "")

        # XBRL → PL勘定科目 → quarantine
        mock_xbrl_extract.return_value = _pl_only_xbrl_rows()

        result = process_one_filing_v2(_make_filing(), cache_root=cache_root)

        # PDF disabled: quarantined のまま
        assert result.status == "quarantined"
        assert result.selected_path == "xbrl"
        assert not result.fallback_used
        assert result.route_mode == "xbrl_v2"


# ============================================================
# 3. XBRL quarantine → quarantine (代表理由あり)
# ============================================================

class TestXbrlQuarantineWithReason:
    @patch("lib.backfill.worker.shutil")
    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("src.extractor.extract_financials")
    @patch("src.downloader.download_document")
    def test_xbrl_quarantine_has_representative_reason(
        self, mock_dl, mock_fin, mock_xbrl_extract, mock_shutil, tmp_path
    ):
        cache_root, pdf_path, xbrl_path = _setup_paths(tmp_path)
        mock_dl.return_value = pdf_path
        mock_shutil.copy2 = MagicMock()
        mock_fin.return_value = (MockExtracted(), "")

        # XBRL → PL勘定科目 → quarantine
        mock_xbrl_extract.return_value = _pl_only_xbrl_rows()

        result = process_one_filing_v2(_make_filing(), cache_root=cache_root)

        assert result.status == "quarantined"
        assert result.quarantine_reason != ""
        assert result.hard_fail_reason != ""
        assert result.selected_path == "xbrl"
        assert result.candidate_summary != ""
        assert result.quarantine is not None
        assert result.quarantine["hard_fail_reason"] == result.hard_fail_reason


# ============================================================
# 4. XBRL unavailable → no_xbrl_segment_source (PDF disabled)
# ============================================================

class TestNoXbrlNoSource:
    @patch("lib.backfill.worker.shutil")
    @patch("src.extractor.extract_financials")
    @patch("src.downloader.download_document")
    def test_no_xbrl_returns_no_source(
        self, mock_dl, mock_fin, mock_shutil, tmp_path
    ):
        cache_root = str(tmp_path / "cache")
        pdf_path = str(tmp_path / "cache" / "test_v2_001" / "source.pdf")
        os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4 dummy")

        mock_dl.return_value = pdf_path
        mock_shutil.copy2 = MagicMock()
        mock_fin.return_value = (MockExtracted(), "")

        # xbrl_url なし
        filing = _make_filing(xbrl_url=None, has_xbrl=False)
        result = process_one_filing_v2(filing, cache_root=cache_root)

        assert result.status == "quarantined"
        assert result.selected_path == "none"
        assert result.quarantine_reason == "no_xbrl_segment_source"
        assert result.route_mode == "xbrl_only_no_source"
        assert not result.fallback_used


# ============================================================
# 5. hard_fail_reason が quarantine_reason に反映
# ============================================================

class TestHardFailReasonReflected:
    @patch("lib.backfill.worker.shutil")
    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("src.extractor.extract_financials")
    @patch("src.downloader.download_document")
    def test_hard_fail_reason_reflected_in_quarantine(
        self, mock_dl, mock_fin, mock_xbrl_extract, mock_shutil, tmp_path
    ):
        cache_root, pdf_path, xbrl_path = _setup_paths(tmp_path)
        mock_dl.return_value = pdf_path
        mock_shutil.copy2 = MagicMock()
        mock_fin.return_value = (MockExtracted(), "")

        # XBRL → PL 勘定科目 → account_like_dominant
        mock_xbrl_extract.return_value = _pl_only_xbrl_rows()

        result = process_one_filing_v2(_make_filing(), cache_root=cache_root)

        assert result.status == "quarantined"
        assert result.hard_fail_reason in (
            "account_like_dominant", "too_few_valid_segments",
            "too_few_sales", "high_invalid_ratio",
            "narrative_contamination", "no_records",
        )
        assert result.quarantine_reason == result.hard_fail_reason


# ============================================================
# 6. selected_path, fallback_used, confidence
# ============================================================

class TestSelectedPathFields:
    @patch("lib.backfill.worker.shutil")
    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("src.extractor.extract_financials")
    @patch("src.downloader.download_document")
    def test_selected_path_and_confidence(
        self, mock_dl, mock_fin, mock_xbrl_extract, mock_shutil, tmp_path
    ):
        cache_root, pdf_path, xbrl_path = _setup_paths(tmp_path)
        mock_dl.return_value = pdf_path
        mock_shutil.copy2 = MagicMock()
        mock_fin.return_value = (MockExtracted(), "")
        mock_xbrl_extract.return_value = _good_xbrl_rows(3)

        result = process_one_filing_v2(_make_filing(), cache_root=cache_root)

        assert result.selected_path == "xbrl"
        assert result.via == result.selected_path
        assert result.confidence > 0.0
        assert isinstance(result.fallback_used, bool)


# ============================================================
# 7. compat fields
# ============================================================

class TestCompatFields:
    @patch("lib.backfill.worker.shutil")
    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("src.extractor.extract_financials")
    @patch("src.downloader.download_document")
    def test_filing_result_v2_has_compat_fields(
        self, mock_dl, mock_fin, mock_xbrl_extract, mock_shutil, tmp_path
    ):
        cache_root, pdf_path, xbrl_path = _setup_paths(tmp_path)
        mock_dl.return_value = pdf_path
        mock_shutil.copy2 = MagicMock()
        mock_fin.return_value = (MockExtracted(), "")
        mock_xbrl_extract.return_value = _good_xbrl_rows(3)

        result = process_one_filing_v2(_make_filing(), cache_root=cache_root)

        # 既存 FilingResult と同じ互換フィールド
        assert hasattr(result, "filing_id")
        assert hasattr(result, "status")
        assert hasattr(result, "via")
        assert hasattr(result, "segment_records")
        assert hasattr(result, "financial_records")
        assert hasattr(result, "metrics")
        assert hasattr(result, "cache_paths")
        assert hasattr(result, "quarantine")
        assert hasattr(result, "result_fingerprint")

        # V2 固有フィールド
        assert hasattr(result, "source")
        assert hasattr(result, "selected_path")
        assert hasattr(result, "confidence")
        assert hasattr(result, "hard_fail_reason")
        assert hasattr(result, "quarantine_reason")
        assert hasattr(result, "fallback_used")
        assert hasattr(result, "fallback_reason")
        assert hasattr(result, "raw_segment_count")
        assert hasattr(result, "valid_segment_count")
        assert hasattr(result, "invalid_segment_count")
        assert hasattr(result, "sales_non_null_count")
        assert hasattr(result, "profit_non_null_count")
        assert hasattr(result, "invalid_names")
        assert hasattr(result, "account_like_ratio")
        assert hasattr(result, "narrative_contamination")
        assert hasattr(result, "candidate_summary")


# ============================================================
# 8. ダウンロード失敗 → failed
# ============================================================

class TestDownloadFailure:
    @patch("src.downloader.download_document")
    def test_download_failure(self, mock_dl, tmp_path):
        mock_dl.return_value = None
        result = process_one_filing_v2(
            _make_filing(), cache_root=str(tmp_path / "cache"),
        )
        assert result.status == "failed"
        assert result.selected_path == "none"


# ============================================================
# 9. debug ログキー検証
# ============================================================

class TestDebugLogKeys:
    """_build_debug_log が必要なキーを持つこと。"""
    def test_debug_log_has_required_keys(self):
        from lib.backfill.worker_v2 import _build_debug_log, SourceCandidate

        # 最低限のダミー candidates
        c_xbrl = SourceCandidate(source="xbrl", attempted=False, available=False, skip_reason="not_available")
        c_html = SourceCandidate(source="html", attempted=False, available=False, skip_reason="not_implemented")
        c_pdf = SourceCandidate(source="pdf", attempted=False, available=False, skip_reason="not_available")

        entry = _build_debug_log(
            fid="fid_test", candidates=[c_xbrl, c_html, c_pdf],
            best=c_pdf, worker_status="quarantined", confidence=0.0,
            hard_fail_reason="no_records", quarantine_reason="no_records",
            fallback_used=True, fallback_reason="primary_unavailable",
            valid_seg_count=0, sales_nn=0, profit_nn=0,
            candidate_summary="xbrl:skip → html:skip → pdf:skip",
        )

        required_keys = [
            "event", "filing_id",
            "xbrl_attempted", "xbrl_available", "xbrl_skip_reason",
            "html_attempted", "html_available", "html_skip_reason",
            "pdf_attempted", "pdf_available", "pdf_skip_reason",
            "selected_source", "selected_status", "selected_confidence",
            "hard_fail_reason", "valid_segment_count",
            "sales_non_null_count", "profit_non_null_count",
            "fallback_used", "fallback_reason",
            "quarantine_reason", "candidate_summary",
        ]
        for key in required_keys:
            assert key in entry, f"Missing key: {key}"


# ============================================================
# 10. PDF disabled → pdf_v1_compat が route_mode に出ない
# ============================================================

class TestPdfDisabled:
    @patch("lib.backfill.worker.shutil")
    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("src.extractor.extract_financials")
    @patch("src.downloader.download_document")
    def test_pdf_disabled_route_mode(
        self, mock_dl, mock_fin, mock_xbrl_extract, mock_shutil, tmp_path
    ):
        cache_root, pdf_path, xbrl_path = _setup_paths(tmp_path)
        mock_dl.return_value = pdf_path
        mock_shutil.copy2 = MagicMock()
        mock_fin.return_value = (MockExtracted(), "")
        mock_xbrl_extract.return_value = _good_xbrl_rows(3)

        result = process_one_filing_v2(_make_filing(), cache_root=cache_root)

        assert result.status == "ok"
        assert result.selected_path == "xbrl"
        assert result.route_mode == "xbrl_v2"
        assert result.route_mode != "pdf_v1_compat"
        # PDF candidate は disabled
        pdf_cands = [c for c in result.candidates if c.source == "pdf"]
        assert len(pdf_cands) == 1
        assert pdf_cands[0].skip_reason == "disabled"
        assert not pdf_cands[0].attempted


# ============================================================
# 11. 全 quarantine → 代表理由が安定して返る
# ============================================================

class TestAllQuarantineStableReason:
    """全 source quarantine でも hard_fail_reason / quarantine_reason / selected_path が空にならない。"""
    def test_stable_reason_with_select_best_candidate(self):
        """_select_best_candidate が全 quarantine でも SourceCandidate を返す。"""
        # 2 candidates, both quarantine
        from src.segment.extraction_result_validator import ExtractionValidationResult
        v_xbrl = ExtractionValidationResult(
            status=ExtractionStatus.QUARANTINE, confidence=0.0,
            reason="xbrl: too_few_sales", hard_fail_reason=HardFailReason.TOO_FEW_SALES,
            raw_segment_count=3, valid_segment_count=3, invalid_segment_count=0,
            sales_non_null_count=0, profit_non_null_count=0,
            invalid_names=[], account_like_ratio=0.0, narrative_contamination=False,
        )
        v_pdf = ExtractionValidationResult(
            status=ExtractionStatus.QUARANTINE, confidence=0.0,
            reason="pdf: account_like_dominant", hard_fail_reason=HardFailReason.ACCOUNT_LIKE_DOMINANT,
            raw_segment_count=5, valid_segment_count=0, invalid_segment_count=5,
            sales_non_null_count=0, profit_non_null_count=0,
            invalid_names=["売上原価"], account_like_ratio=1.0, narrative_contamination=False,
        )
        c_xbrl = SourceCandidate(
            source="xbrl", attempted=True, available=True,
            segment_records=[{"segment_name": "A"}], validation=v_xbrl,
        )
        c_html = SourceCandidate(
            source="html", attempted=False, available=False, skip_reason="not_implemented",
        )
        c_pdf = SourceCandidate(
            source="pdf", attempted=True, available=True,
            segment_records=[{"segment_name": "B"}], validation=v_pdf,
        )

        best = _select_best_candidate([c_xbrl, c_html, c_pdf])

        # None ではなく候補が返る
        assert best is not None
        assert best.validation is not None
        assert best.validation.status == ExtractionStatus.QUARANTINE
        # source priority で xbrl が選ばれる (同 confidence, 同 status)
        assert best.source == "xbrl"
        assert best.validation.hard_fail_reason.value != ""


# ============================================================
# 12. validator_status_to_worker
# ============================================================

class TestValidatorStatusConversion:
    def test_success_to_ok(self):
        assert validator_status_to_worker("success") == "ok"

    def test_partial_to_partial(self):
        assert validator_status_to_worker("partial") == "partial"

    def test_quarantine_to_quarantined(self):
        assert validator_status_to_worker("quarantine") == "quarantined"

    def test_unknown_defaults_to_quarantined(self):
        assert validator_status_to_worker("unknown") == "quarantined"
