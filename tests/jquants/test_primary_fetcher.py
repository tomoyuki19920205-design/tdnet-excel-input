"""tests/jquants/test_primary_fetcher.py - J-Quants Primary Fetcher tests (Phase 3)

Tests for JQUANTS_PRIMARY_ENABLED=1 path in src/fetcher.py fetch_new_disclosures().

Verified behaviors:
  - ENABLED unset / =0 -> YANOSHIN/API called (legacy path)
  - ENABLED=1 -> J-Quants is first choice, YANOSHIN/API NOT called
  - J-Quants success -> returns DisclosureItem list, no YANOSHIN fallback
  - J-Quants failure -> [JQUANTS_PRIMARY_FALLBACK] logged, falls back to YANOSHIN/HTML
  - DisclosureItem conversion: doc_url = "1401" + DiscNo, disclosure_id = sha256(doc_url)
  - Backfill (past date) -> JQUANTS_PRIMARY_ENABLED=1 ignored, uses HTML only
  - API key not logged
  - existing 300-limit HTML supplement logic not broken
"""
from __future__ import annotations

import hashlib
import os
import sys
import logging
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.fetcher import (
    fetch_new_disclosures, _fetch_via_jquants, _filter_linkable_materials,
)
from src.models import DisclosureItem, DisclosureType
from src.jquants.adapter import JQuantsDisclosure


# Patch targets
_PATCH_JQ_FETCH = "src.jquants.adapter.fetch_jquants_disclosures"
_PATCH_API = "src.fetcher._fetch_via_api"
_PATCH_HTML = "src.fetcher._fetch_via_html"
_PATCH_TODAY = "src.fetcher.today_yyyymmdd"


# ── helpers ──────────────────────────────────────────────────────────────────

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _make_jq_disclosure(
    disc_no: str = "20260703587460",
    code: str = "76110",
    name: str = "Test Co",
    title: str = "2026年3月期決算短信",
    disc_date: str = "2026-07-03",
    disc_time: str = "15:30",
    disc_items: list | None = None,
    docs: list | None = None,
) -> JQuantsDisclosure:
    ticker = code[:-1] if (len(code) == 5 and code.endswith("0")) else code
    doc_url = f"https://www.release.tdnet.info/inbs/1401{disc_no}.pdf"
    from src.jquants.classifier import classify_disclosure_jquants
    dtype = classify_disclosure_jquants(disc_items or ["11304"], title) or ""
    return JQuantsDisclosure(
        disclosure_id=disc_no,
        ticker=ticker,
        company_name=name,
        title=title,
        doc_url=doc_url,
        published_at=f"{disc_date} {disc_time}",
        xbrl_url=None,
        disclosure_type=dtype,
        disc_no=disc_no,
        disc_date=disc_date,
        disc_time=disc_time,
        disc_items=disc_items or ["11304"],
        docs=docs or ["g"],
        rev_no="1",
        disc_status=None,
        dedup_key_primary="1401" + disc_no,
        dedup_key_secondary="dummy",
    )


def _make_legacy_item(ticker: str = "7388") -> DisclosureItem:
    doc_url = "https://www.release.tdnet.info/inbs/140120260703584087.pdf"
    return DisclosureItem(
        disclosure_id=_sha256(doc_url),
        ticker=ticker,
        company_name="Legacy Co",
        title="2026年3月期決算短信",
        doc_url=doc_url,
        published_at="2026-07-03 15:30",
        xbrl_url=None,
        disclosure_type=DisclosureType.FINANCIAL_STATEMENT,
    )


# ============================================================
# Test 1: JQUANTS_PRIMARY_ENABLED 未設定/=0 では既存パスが使われる
# ============================================================

class TestPrimaryDisabledByDefault:

    def test_legacy_api_called_when_disabled(self):
        """JQUANTS_PRIMARY_ENABLED 未設定時は YANOSHIN API が呼ばれる"""
        called = []
        env = {k: v for k, v in os.environ.items() if k != "JQUANTS_PRIMARY_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
            with patch(_PATCH_TODAY, return_value="20260703"):
                with patch(_PATCH_API, side_effect=lambda **kw: called.append("api") or []) as mock_api:
                    with patch(_PATCH_JQ_FETCH, side_effect=lambda *a, **kw: called.append("jq") or []):
                        fetch_new_disclosures()
        assert "api" in called, "YANOSHIN API が呼ばれなかった"
        assert "jq" not in called, "J-Quants が呼ばれてしまった"

    def test_legacy_api_called_when_zero(self):
        """JQUANTS_PRIMARY_ENABLED=0 でも YANOSHIN API が呼ばれる"""
        called = []
        with patch.dict(os.environ, {"JQUANTS_PRIMARY_ENABLED": "0"}):
            with patch(_PATCH_TODAY, return_value="20260703"):
                with patch(_PATCH_API, side_effect=lambda **kw: called.append("api") or []):
                    with patch(_PATCH_JQ_FETCH, side_effect=lambda *a, **kw: called.append("jq") or []):
                        fetch_new_disclosures()
        assert "api" in called
        assert "jq" not in called


# ============================================================
# Test 2: JQUANTS_PRIMARY_ENABLED=1 で J-Quants が第一候補になる
# ============================================================

class TestPrimaryEnabled:

    def test_jquants_called_when_enabled(self):
        """JQUANTS_PRIMARY_ENABLED=1 で J-Quants が呼ばれる"""
        jq_items = [_make_jq_disclosure()]
        called = []

        with patch.dict(os.environ, {"JQUANTS_PRIMARY_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TODAY, return_value="20260703"):
                with patch(_PATCH_JQ_FETCH, side_effect=lambda *a, **kw: called.append("jq") or jq_items):
                    with patch(_PATCH_API, side_effect=lambda **kw: called.append("api") or []):
                        fetch_new_disclosures()

        assert "jq" in called, "J-Quants が呼ばれなかった"

    def test_yanoshin_not_called_on_jquants_success(self):
        """J-Quants 成功時は YANOSHIN API が呼ばれない"""
        jq_items = [_make_jq_disclosure()]
        api_called = []

        with patch.dict(os.environ, {"JQUANTS_PRIMARY_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TODAY, return_value="20260703"):
                with patch(_PATCH_JQ_FETCH, return_value=jq_items):
                    with patch(_PATCH_API, side_effect=lambda **kw: api_called.append(True) or []):
                        fetch_new_disclosures()

        assert api_called == [], "J-Quants 成功後に YANOSHIN API が呼ばれた"

    def test_jquants_items_returned_as_disclosure_items(self):
        """J-Quants 結果が DisclosureItem リストに変換されて返る"""
        disc_no = "20260703587460"
        jq_items = [_make_jq_disclosure(disc_no=disc_no)]

        with patch.dict(os.environ, {"JQUANTS_PRIMARY_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TODAY, return_value="20260703"):
                with patch(_PATCH_JQ_FETCH, return_value=jq_items):
                    result = fetch_new_disclosures()

        assert len(result) >= 1
        item = result[0]
        assert isinstance(item, DisclosureItem)

    def test_doc_url_uses_1401_prefix(self):
        """doc_url が '1401' + DiscNo 形式になる"""
        disc_no = "20260703587460"
        jq_items = [_make_jq_disclosure(disc_no=disc_no)]

        with patch.dict(os.environ, {"JQUANTS_PRIMARY_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TODAY, return_value="20260703"):
                with patch(_PATCH_JQ_FETCH, return_value=jq_items):
                    result = fetch_new_disclosures()

        assert len(result) >= 1
        expected_url = f"https://www.release.tdnet.info/inbs/1401{disc_no}.pdf"
        assert result[0].doc_url == expected_url

    def test_disclosure_id_is_sha256_of_doc_url(self):
        """disclosure_id が sha256(doc_url) になる（既存仕様準拠）"""
        disc_no = "20260703587460"
        jq_items = [_make_jq_disclosure(disc_no=disc_no)]
        expected_url = f"https://www.release.tdnet.info/inbs/1401{disc_no}.pdf"
        expected_id = _sha256(expected_url)

        with patch.dict(os.environ, {"JQUANTS_PRIMARY_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TODAY, return_value="20260703"):
                with patch(_PATCH_JQ_FETCH, return_value=jq_items):
                    result = fetch_new_disclosures()

        assert len(result) >= 1
        assert result[0].disclosure_id == expected_id


# ============================================================
# Test 3: J-Quants 失敗時に YANOSHIN/HTML へ fallback する
# ============================================================

class TestFallbackOnJQuantsFailure:

    def test_fallback_to_yanoshin_on_runtime_error(self, caplog):
        """J-Quants RuntimeError → YANOSHIN/HTML へ fallback"""
        api_called = []

        with patch.dict(os.environ, {"JQUANTS_PRIMARY_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TODAY, return_value="20260703"):
                with patch(_PATCH_JQ_FETCH, side_effect=RuntimeError("API down")):
                    with patch(_PATCH_API, side_effect=lambda **kw: api_called.append(True) or []):
                        with caplog.at_level(logging.WARNING, logger="tdnet"):
                            fetch_new_disclosures()

        assert api_called, "fallback 先の YANOSHIN API が呼ばれなかった"
        assert "[JQUANTS_PRIMARY_FALLBACK]" in "\n".join(caplog.messages)

    def test_fallback_to_yanoshin_on_connection_error(self):
        """ConnectionError でも fallback する"""
        api_called = []

        with patch.dict(os.environ, {"JQUANTS_PRIMARY_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TODAY, return_value="20260703"):
                with patch(_PATCH_JQ_FETCH, side_effect=ConnectionError("unreachable")):
                    with patch(_PATCH_API, side_effect=lambda **kw: api_called.append(True) or []):
                        fetch_new_disclosures()

        assert api_called

    def test_fallback_log_tag_present(self, caplog):
        """fallback 時に [JQUANTS_PRIMARY_FALLBACK] タグが出力される"""
        with patch.dict(os.environ, {"JQUANTS_PRIMARY_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TODAY, return_value="20260703"):
                with patch(_PATCH_JQ_FETCH, side_effect=RuntimeError("timeout")):
                    with patch(_PATCH_API, return_value=[]):
                        with caplog.at_level(logging.WARNING, logger="tdnet"):
                            fetch_new_disclosures()

        assert "[JQUANTS_PRIMARY_FALLBACK]" in "\n".join(caplog.messages)

    def test_api_key_missing_falls_back(self):
        """APIキー未設定の RuntimeError でも fallback して本番を止めない"""
        api_called = []
        env = {k: v for k, v in os.environ.items()
               if k not in ("JQUANTS_PRIMARY_ENABLED", "JQUANTS_API_KEY")}
        env["JQUANTS_PRIMARY_ENABLED"] = "1"

        with patch.dict(os.environ, env, clear=True):
            with patch(_PATCH_TODAY, return_value="20260703"):
                with patch(_PATCH_JQ_FETCH, side_effect=RuntimeError("API key missing")):
                    with patch(_PATCH_API, side_effect=lambda **kw: api_called.append(True) or []):
                        try:
                            fetch_new_disclosures()
                        except Exception as e:
                            pytest.fail(f"例外が伝播した: {e}")

        assert api_called


# ============================================================
# Test 4: バックフィル時は J-Quants PRIMARY を無視する
# ============================================================

class TestBackfillIgnoresPrimary:

    def test_backfill_uses_html_not_jquants(self):
        """過去日付バックフィル時は JQUANTS_PRIMARY_ENABLED=1 でも HTML が使われる"""
        jq_called = []
        html_called = []

        with patch.dict(os.environ, {"JQUANTS_PRIMARY_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TODAY, return_value="20260703"):
                with patch(_PATCH_JQ_FETCH, side_effect=lambda *a, **kw: jq_called.append(True) or []):
                    with patch(_PATCH_HTML, side_effect=lambda *a, **kw: html_called.append(True) or []):
                        # target_date を過去日付に設定
                        fetch_new_disclosures(target_date="20260701")

        assert html_called, "バックフィル時に HTML が呼ばれなかった"
        assert jq_called == [], "バックフィル時に J-Quants が呼ばれてしまった"


# ============================================================
# Test 5: _fetch_via_jquants の単体テスト
# ============================================================

class TestFetchViaJquants:

    def test_returns_disclosure_items(self):
        """_fetch_via_jquants が DisclosureItem リストを返す"""
        jq_items = [_make_jq_disclosure()]

        with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_JQ_FETCH, return_value=jq_items):
                result = _fetch_via_jquants("20260703")

        assert len(result) == 1
        assert isinstance(result[0], DisclosureItem)

    def test_doc_url_1401_prefix(self):
        """TDnet FileID = '1401' + DiscNo でURLが構築される"""
        disc_no = "20260703587460"
        jq_items = [_make_jq_disclosure(disc_no=disc_no)]

        with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_JQ_FETCH, return_value=jq_items):
                result = _fetch_via_jquants("20260703")

        assert result[0].doc_url == f"https://www.release.tdnet.info/inbs/1401{disc_no}.pdf"

    def test_disclosure_id_sha256_of_doc_url(self):
        """disclosure_id = sha256(doc_url)"""
        disc_no = "20260703587460"
        jq_items = [_make_jq_disclosure(disc_no=disc_no)]
        expected_url = f"https://www.release.tdnet.info/inbs/1401{disc_no}.pdf"

        with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_JQ_FETCH, return_value=jq_items):
                result = _fetch_via_jquants("20260703")

        assert result[0].disclosure_id == _sha256(expected_url)

    def test_ticker_trailing_zero_stripped(self):
        """5桁コードの末尾0が除去されている"""
        jq_items = [_make_jq_disclosure(code="76110")]

        with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_JQ_FETCH, return_value=jq_items):
                result = _fetch_via_jquants("20260703")

        assert result[0].ticker == "7611"

    def test_exception_propagates_for_fallback(self):
        """例外は伝播して呼び出し元 fallback に任せる"""
        with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_JQ_FETCH, side_effect=RuntimeError("API error")):
                with pytest.raises(RuntimeError):
                    _fetch_via_jquants("20260703")


class TestMaterialUrlValidation:
    class Response:
        def __init__(self, status, content_type="application/pdf", body=b"%PDF-1.7"):
            self.status_code = status
            self.headers = {"Content-Type": content_type}
            self._body = body

        def iter_content(self, _size):
            yield self._body

        def close(self):
            pass

    class Session:
        def __init__(self, response):
            self.response = response

        def get(self, *_args, **_kwargs):
            return self.response

    def test_valid_pdf_is_retained_and_marked(self):
        item = _make_jq_disclosure(
            title="FY2026 Financial Results Presentation",
            disc_items=["11443"],
        )
        converted = DisclosureItem(
            disclosure_id="valid", ticker=item.ticker, company_name=item.company_name,
            title=item.title, doc_url=item.doc_url, published_at=item.published_at,
            disclosure_type="earnings_material", source_doc_id=item.disclosure_id,
        )
        result = _filter_linkable_materials(
            [converted], session=self.Session(self.Response(200)),
        )
        assert result == [converted]
        assert converted.link_validated is True

    def test_404_guessed_pdf_is_dropped(self):
        item = DisclosureItem(
            disclosure_id="broken", ticker="6294", company_name="オカダアイヨン",
            title="Financial Results Presentation for Q1 FY3/27",
            doc_url="https://www.release.tdnet.info/inbs/140120260826525430.pdf",
            published_at="2026-08-26 13:00", disclosure_type="earnings_material",
            source_doc_id="20260826525430",
        )
        assert _filter_linkable_materials(
            [item], session=self.Session(self.Response(404, "text/html", b"<html>")),
        ) == []

    def test_non_material_events_are_untouched(self):
        item = _make_legacy_item()
        assert _filter_linkable_materials([item], session=None) == [item]

    def test_two_distinct_same_day_materials_are_both_retained(self):
        first = DisclosureItem(
            disclosure_id="first", ticker="8218", company_name="Test",
            title="第1四半期決算説明資料",
            doc_url="https://example.test/presentation.pdf",
            published_at="2026-08-26 13:00", disclosure_type="earnings_material",
        )
        second = DisclosureItem(
            disclosure_id="second", ticker="8218", company_name="Test",
            title="第1四半期決算補足データ",
            doc_url="https://example.test/data-sheet.pdf",
            published_at="2026-08-26 13:00", disclosure_type="earnings_material",
        )
        result = _filter_linkable_materials(
            [first, second], session=self.Session(self.Response(200)),
        )
        assert result == [first, second]

    def test_internal_guessed_route_is_dropped_without_request(self):
        item = DisclosureItem(
            disclosure_id="internal", ticker="6294", company_name="Test",
            title="第1四半期決算説明資料", doc_url="/api/pdf/guessed",
            published_at="2026-08-26 13:00", disclosure_type="earnings_material",
        )
        assert _filter_linkable_materials([item], session=None) == []


# ============================================================
# Test 6: APIキー秘匿確認
# ============================================================

class TestApiKeyNotLogged:

    def test_api_key_not_in_fetch_logs(self, caplog):
        """APIキーがログに出力されない（成功時）"""
        dummy_key = "PHASE3_SECRET_JQUANTS_KEY_DO_NOT_LOG"
        jq_items = [_make_jq_disclosure()]

        with patch.dict(os.environ, {
            "JQUANTS_PRIMARY_ENABLED": "1",
            "JQUANTS_API_KEY": dummy_key,
        }):
            with patch(_PATCH_TODAY, return_value="20260703"):
                with patch(_PATCH_JQ_FETCH, return_value=jq_items):
                    with caplog.at_level(logging.DEBUG):
                        fetch_new_disclosures()

        assert dummy_key not in "\n".join(caplog.messages)

    def test_api_key_not_in_fallback_logs(self, caplog):
        """APIキーがログに出力されない（fallback 時）"""
        dummy_key = "PHASE3_FALLBACK_KEY_NEVER_LOG"

        with patch.dict(os.environ, {
            "JQUANTS_PRIMARY_ENABLED": "1",
            "JQUANTS_API_KEY": dummy_key,
        }):
            with patch(_PATCH_TODAY, return_value="20260703"):
                with patch(_PATCH_JQ_FETCH, side_effect=RuntimeError("timeout")):
                    with patch(_PATCH_API, return_value=[]):
                        with caplog.at_level(logging.DEBUG):
                            fetch_new_disclosures()

        assert dummy_key not in "\n".join(caplog.messages)


# ============================================================
# Test 7: 既存 300件上限 HTML 補完ロジックが壊れていない
# ============================================================

class TestLegacyLogicPreserved:

    def test_300_limit_html_fallback_still_works(self):
        """JQUANTS_PRIMARY disabled 時に 300件到達 → HTML 補完が働く"""
        import hashlib as _hl

        def _make_unique_item(idx: int) -> DisclosureItem:
            """インデックスごとに一意の doc_url / disclosure_id を持つ DisclosureItem"""
            doc_url = f"https://www.release.tdnet.info/inbs/14012026070358{idx:04d}.pdf"
            return DisclosureItem(
                disclosure_id=_hl.sha256(doc_url.encode()).hexdigest(),
                ticker=str(idx % 9000 + 1000),
                company_name=f"Co {idx}",
                title="2026年3月期決算短信",
                doc_url=doc_url,
                published_at="2026-07-03 15:30",
                xbrl_url=None,
                disclosure_type=DisclosureType.FINANCIAL_STATEMENT,
            )

        # YANOSHIN API が 300件返す（全件ユニーク）
        api_items = [_make_unique_item(i) for i in range(300)]
        # HTML は 350件（うち 300件は API と重複、50件は新規）
        html_items = [_make_unique_item(i) for i in range(350)]

        env = {k: v for k, v in os.environ.items() if k != "JQUANTS_PRIMARY_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
            with patch(_PATCH_TODAY, return_value="20260703"):
                with patch(_PATCH_API, return_value=api_items):
                    with patch(_PATCH_HTML, return_value=html_items):
                        result = fetch_new_disclosures()

        # API 300件 + HTML 50件新規 = 350件が dedupe 後に返る
        assert len(result) > 300, f"300件補完が働いていない: result={len(result)}"

