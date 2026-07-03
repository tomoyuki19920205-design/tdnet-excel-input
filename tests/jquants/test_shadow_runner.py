"""tests/jquants/test_shadow_runner.py — Shadow Runner ユニットテスト

テスト対象:
  - ShadowDiffResult の計算
  - FileID ↔ DiscNo 変換
  - legacy_items との差分計算
  - [JQUANTS_SHADOW_*] ログタグの出力確認
  - legacy_items=None でも動作
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── プロジェクトルートを PATH に追加 ───────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.jquants.shadow_runner import (
    ShadowDiffResult,
    _extract_file_id_from_doc_url,
    _file_id_to_disc_no,
    _make_secondary_key_from_legacy,
    run_shadow_comparison,
    LOG_FETCH_START,
    LOG_FETCH_DONE,
    LOG_DIFF,
    LOG_MISSING,
    LOG_FILE_AVAIL,
)
from src.jquants.adapter import JQuantsDisclosure, _make_dedup_key_secondary
from src.models import DisclosureItem, DisclosureType


# ============================================================
# サンプルデータファクトリ
# ============================================================

def _make_jq_disc(
    disc_no: str = "20260630584087",
    ticker: str = "5086",
    title: str = "2027年２月期 第１四半期決算短信〔日本基準〕（連結）",
    disc_date: str = "2026-06-30",
    disc_items: list[str] | None = None,
    docs: list[str] | None = None,
    disclosure_type: str = DisclosureType.FINANCIAL_STATEMENT,
) -> JQuantsDisclosure:
    from src.jquants.adapter import _make_dedup_key_secondary
    return JQuantsDisclosure(
        disclosure_id=disc_no,
        ticker=ticker,
        company_name="テスト会社",
        title=title,
        doc_url=f"https://www.release.tdnet.info/inbs/1401{disc_no}.pdf",
        published_at=f"{disc_date} 15:30",
        xbrl_url=None,
        disclosure_type=disclosure_type,
        disc_no=disc_no,
        disc_date=disc_date,
        disc_time="15:30",
        disc_items=disc_items or ["11304"],
        docs=docs or ["g", "s", "x"],
        rev_no="1",
        disc_status=None,
        dedup_key_primary="1401" + disc_no,
        dedup_key_secondary=_make_dedup_key_secondary(disc_date, ticker, title),
    )


def _make_legacy_disc_item(
    disclosure_id: str = "sha256_abc",
    ticker: str = "5086",
    title: str = "2027年２月期 第１四半期決算短信〔日本基準〕（連結）",
    doc_url: str = "https://www.release.tdnet.info/inbs/140120260630584087.pdf",
    published_at: str = "2026-06-30 15:30",
    disclosure_type: str = DisclosureType.FINANCIAL_STATEMENT,
) -> DisclosureItem:
    return DisclosureItem(
        disclosure_id=disclosure_id,
        ticker=ticker,
        company_name="テスト会社",
        title=title,
        doc_url=doc_url,
        published_at=published_at,
        xbrl_url=None,
        disclosure_type=disclosure_type,
    )


def _make_mock_jq_response(items_raw: list[dict]) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": items_raw}
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


# ============================================================
# Test: FileID ↔ DiscNo 変換
# ============================================================

class TestFileIdDiscNoConversion:

    def test_extract_file_id_from_standard_url(self):
        """標準 TDnet URL から FileID を抽出"""
        url = "https://www.release.tdnet.info/inbs/140120260630584087.pdf"
        assert _extract_file_id_from_doc_url(url) == "140120260630584087"

    def test_extract_file_id_from_yanoshin_url(self):
        """YANOSHIN プロキシ URL から FileID を抽出"""
        url = "https://webapi.yanoshin.jp/rd.php?https://www.release.tdnet.info/inbs/140120260626581127.pdf"
        assert _extract_file_id_from_doc_url(url) == "140120260626581127"

    def test_extract_file_id_none_on_no_match(self):
        """パターン不一致は None"""
        assert _extract_file_id_from_doc_url("https://example.com/foo.pdf") is None
        assert _extract_file_id_from_doc_url("") is None

    def test_file_id_to_disc_no_18digit(self):
        """18桁 FileID → 14桁 DiscNo"""
        assert _file_id_to_disc_no("140120260630584087") == "20260630584087"

    def test_file_id_to_disc_no_14digit(self):
        """14桁はそのまま DiscNo"""
        assert _file_id_to_disc_no("20260630584087") == "20260630584087"

    def test_file_id_to_disc_no_wrong_prefix(self):
        """先頭が 1401 でない 18桁 → None"""
        assert _file_id_to_disc_no("999920260630584087") is None

    def test_roundtrip(self):
        """DiscNo → FileID → DiscNo のラウンドトリップ"""
        disc_no = "20260617572986"
        file_id = "1401" + disc_no
        recovered = _file_id_to_disc_no(file_id)
        assert recovered == disc_no


# ============================================================
# Test: run_shadow_comparison (legacy_items=None)
# ============================================================

class TestShadowRunNoLegacy:
    """legacy_items なしの J-Quants 統計のみモード"""

    def test_no_legacy_returns_jq_stats(self, caplog):
        """J-Quants取得件数のみが返る"""
        raw_item = {
            "DiscNo": "20260630584087",
            "Code": "50860",
            "Name": "テスト",
            "DiscDate": "2026-06-30",
            "DiscTime": "15:30",
            "Title": "四半期決算短信",
            "DiscStatus": None,
            "RevNo": "1",
            "DiscItems": ["11304"],
            "Docs": ["g", "s", "x"],
        }
        mock_resp = _make_mock_jq_response([raw_item])
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_resp.raise_for_status = MagicMock()

        with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy"}):
            result = run_shadow_comparison("20260630", legacy_items=None, _session=mock_session)

        assert result.jquants_total == 1
        assert result.jquants_filtered == 1
        assert result.legacy_total == 0
        assert result.fetch_error is None

    def test_log_tags_present(self, caplog):
        """FETCH_START と FETCH_DONE ログタグが出力される"""
        mock_resp = _make_mock_jq_response([])
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_resp.raise_for_status = MagicMock()

        import logging
        with caplog.at_level(logging.INFO):
            with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy"}):
                run_shadow_comparison("20260701", legacy_items=None, _session=mock_session)

        all_logs = "\n".join(caplog.messages)
        assert LOG_FETCH_START in all_logs
        assert LOG_FETCH_DONE in all_logs


# ============================================================
# Test: run_shadow_comparison (with legacy_items)
# ============================================================

class TestShadowRunWithLegacy:
    """legacy_items ありの差分比較モード"""

    def _run_with_mocked_jq(
        self,
        jq_raw_items: list[dict],
        legacy_items: list[DisclosureItem],
    ) -> ShadowDiffResult:
        mock_resp = _make_mock_jq_response(jq_raw_items)
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_resp.raise_for_status = MagicMock()

        with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy"}):
            return run_shadow_comparison(
                "20260630",
                legacy_items=legacy_items,
                _session=mock_session,
            )

    def test_matched_item(self):
        """J-Quants と legacy が同じ FileID を持つ → matched_count=1"""
        disc_no = "20260630584087"
        jq_raw = [{
            "DiscNo": disc_no, "Code": "50860", "Name": "T",
            "DiscDate": "2026-06-30", "DiscTime": "15:30",
            "Title": "四半期決算短信", "DiscStatus": None, "RevNo": "1",
            "DiscItems": ["11304"], "Docs": ["g"],
        }]
        # 既存の doc_url は "1401" + disc_no の形式
        legacy = [_make_legacy_disc_item(
            doc_url=f"https://www.release.tdnet.info/inbs/1401{disc_no}.pdf"
        )]

        result = self._run_with_mocked_jq(jq_raw, legacy)
        assert result.matched_count == 1
        assert len(result.missing_in_legacy) == 0
        assert len(result.extra_in_jquants) == 0

    def test_missing_in_legacy(self):
        """J-Quantsにあって legacy にない → missing_in_legacy に追加"""
        disc_no = "20260630999999"
        jq_raw = [{
            "DiscNo": disc_no, "Code": "73880", "Name": "テスト",
            "DiscDate": "2026-06-30", "DiscTime": "10:00",
            "Title": "業績予想の修正", "DiscStatus": None, "RevNo": "1",
            "DiscItems": ["11350"], "Docs": ["g"],
        }]
        legacy = []  # 既存は空

        result = self._run_with_mocked_jq(jq_raw, legacy)
        assert len(result.missing_in_legacy) == 1
        assert f"1401{disc_no}" in result.missing_in_legacy

    def test_truncation_gap(self):
        """J-Quants > legacy → truncation_gap が正値"""
        jq_raw = [
            {
                "DiscNo": f"20260630{i:06d}", "Code": "73880", "Name": "T",
                "DiscDate": "2026-06-30", "DiscTime": "10:00",
                "Title": "業績予想の修正", "DiscStatus": None, "RevNo": "1",
                "DiscItems": ["11350"], "Docs": ["g"],
            }
            for i in range(10)
        ]
        # legacy は 3件のみ (FileID一致なし → extra 扱い)
        legacy = [
            _make_legacy_disc_item(
                doc_url=f"https://www.release.tdnet.info/inbs/14012026063099{i:04d}.pdf"
            )
            for i in range(3)
        ]

        result = self._run_with_mocked_jq(jq_raw, legacy)
        assert result.jquants_total == 10
        assert result.legacy_total == 3
        assert result.truncation_gap == 7

    def test_diff_log_tag_present(self, caplog):
        """[JQUANTS_SHADOW_DIFF] タグがログに出力される"""
        jq_raw = [{
            "DiscNo": "20260630584087", "Code": "50860", "Name": "T",
            "DiscDate": "2026-06-30", "DiscTime": "15:30",
            "Title": "四半期決算短信", "DiscStatus": None, "RevNo": "1",
            "DiscItems": ["11304"], "Docs": ["g"],
        }]
        legacy = []

        import logging
        with caplog.at_level(logging.INFO):
            mock_resp = _make_mock_jq_response(jq_raw)
            mock_session = MagicMock()
            mock_session.get.return_value = mock_resp
            mock_resp.raise_for_status = MagicMock()

            with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy"}):
                run_shadow_comparison("20260630", legacy_items=legacy, _session=mock_session)

        assert LOG_DIFF in "\n".join(caplog.messages)

    def test_missing_log_tag_present(self, caplog):
        """[JQUANTS_SHADOW_MISSING_IN_LEGACY] タグがフィルタ対象の欠落時に出力"""
        disc_no = "20260630999999"
        jq_raw = [{
            "DiscNo": disc_no, "Code": "73880", "Name": "テスト",
            "DiscDate": "2026-06-30", "DiscTime": "10:00",
            "Title": "業績予想の修正に関するお知らせ", "DiscStatus": None, "RevNo": "1",
            "DiscItems": ["11350"], "Docs": ["g"],
        }]

        import logging
        with caplog.at_level(logging.INFO):
            mock_resp = _make_mock_jq_response(jq_raw)
            mock_session = MagicMock()
            mock_session.get.return_value = mock_resp
            mock_resp.raise_for_status = MagicMock()

            with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy"}):
                run_shadow_comparison("20260630", legacy_items=[], _session=mock_session)

        assert LOG_MISSING in "\n".join(caplog.messages)

    def test_file_available_log(self, caplog):
        """[JQUANTS_SHADOW_FILE_AVAILABLE] タグが出力される"""
        jq_raw = [{
            "DiscNo": "20260630584087", "Code": "50860", "Name": "T",
            "DiscDate": "2026-06-30", "DiscTime": "15:30",
            "Title": "四半期決算短信", "DiscStatus": None, "RevNo": "1",
            "DiscItems": ["11304"], "Docs": ["g", "x"],
        }]

        import logging
        with caplog.at_level(logging.INFO):
            mock_resp = _make_mock_jq_response(jq_raw)
            mock_session = MagicMock()
            mock_session.get.return_value = mock_resp
            mock_resp.raise_for_status = MagicMock()

            with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy"}):
                result = run_shadow_comparison("20260630", legacy_items=None, _session=mock_session)

        assert result.pdf_available_count == 1
        assert result.xbrl_available_count == 1
        assert LOG_FILE_AVAIL in "\n".join(caplog.messages)

    def test_api_key_not_in_shadow_logs(self, caplog):
        """Shadow Run のログに APIキーが含まれないこと"""
        dummy_key = "MY_SECRET_JQUANTS_KEY_NEVER_LOG"
        jq_raw = [{
            "DiscNo": "20260630584087", "Code": "50860", "Name": "T",
            "DiscDate": "2026-06-30", "DiscTime": "15:30",
            "Title": "四半期決算短信", "DiscStatus": None, "RevNo": "1",
            "DiscItems": ["11304"], "Docs": ["g"],
        }]

        import logging
        with caplog.at_level(logging.DEBUG):
            mock_resp = _make_mock_jq_response(jq_raw)
            mock_session = MagicMock()
            mock_session.get.return_value = mock_resp
            mock_resp.raise_for_status = MagicMock()

            with patch.dict(os.environ, {"JQUANTS_API_KEY": dummy_key}):
                run_shadow_comparison("20260630", legacy_items=None, _session=mock_session)

        all_logs = "\n".join(caplog.messages)
        assert dummy_key not in all_logs, "APIキーがログに漏洩している"

    def test_fetch_error_recorded(self):
        """J-Quants fetch 失敗時に fetch_error が記録される"""
        mock_session = MagicMock()
        mock_session.get.side_effect = ConnectionError("timeout")

        with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy"}):
            result = run_shadow_comparison("20260630", legacy_items=None, _session=mock_session)

        assert result.fetch_error is not None
        assert result.jquants_total == 0
