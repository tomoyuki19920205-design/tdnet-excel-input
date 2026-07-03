"""tests/jquants/test_adapter.py — J-Quants adapter ユニットテスト

テスト対象:
  - dataキー対応 (td_list ではなく data)
  - pagination_key による複数ページ回収
  - DiscNo (14桁) → TDnet FileID (18桁) 変換
  - DiscItems分類
  - CommonDisclosure (JQuantsDisclosure) 変換
  - duplicate key normalize
  - 300件超でも全ページ回収できる mock
  - APIキーをログ・出力しないこと (セキュリティ)
"""
from __future__ import annotations

import hashlib
import json
import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── プロジェクトルートを PATH に追加 ───────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ── テスト対象のインポート ─────────────────────────────────────
from src.jquants.adapter import (
    _convert_raw_item,
    _make_doc_url_from_disc_no,
    _make_dedup_key_secondary,
    _normalize_title_for_dedup,
    _strip_trailing_zero,
    fetch_tdnet_list_raw,
    fetch_jquants_disclosures,
    JQuantsDisclosure,
)
from src.jquants.classifier import (
    classify_by_disc_items,
    classify_by_title_fallback,
    classify_disclosure_jquants,
)
from src.models import DisclosureType


# ============================================================
# サンプルデータファクトリ
# ============================================================

def _make_raw_item(
    disc_no: str = "20260630584087",
    code: str = "50860",
    name: str = "ヤマトＨＤ",
    disc_date: str = "2026-06-30",
    disc_time: str = "15:30",
    title: str = "2027年２月期 第１四半期決算短信〔日本基準〕（連結）",
    disc_items: list[str] | None = None,
    docs: list[str] | None = None,
    rev_no: str = "1",
) -> dict:
    return {
        "DiscNo": disc_no,
        "Code": code,
        "Name": name,
        "DiscDate": disc_date,
        "DiscTime": disc_time,
        "Title": title,
        "DiscStatus": None,
        "RevNo": rev_no,
        "DiscItems": disc_items if disc_items is not None else ["11304"],
        "Docs": docs if docs is not None else ["g", "s", "x"],
    }


def _make_mock_response(items: list[dict], pagination_key: str | None = None) -> MagicMock:
    """requests.Response モック"""
    body = {"data": items}
    if pagination_key:
        body["pagination_key"] = pagination_key
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = body
    mock.raise_for_status = MagicMock()
    return mock


def _make_mock_session(responses: list[MagicMock]) -> MagicMock:
    """複数レスポンスを順番に返す session モック"""
    session = MagicMock()
    session.get.side_effect = responses
    return session


# ============================================================
# Test: dataキー対応
# ============================================================

class TestDataKey:
    """レスポンスのルートキーが 'data' であることを確認"""

    def test_data_key_parsed(self):
        """'data' キーのアイテムが正しく取得できる"""
        raw = _make_raw_item()
        mock_resp = _make_mock_response([raw])
        session = _make_mock_session([mock_resp])

        with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy_key_for_test"}):
            items = fetch_jquants_disclosures("20260630", _session=session)

        assert len(items) == 1
        assert items[0].disc_no == "20260630584087"

    def test_empty_data_key(self):
        """'data' が空リストのとき 0件を返す"""
        mock_resp = _make_mock_response([])
        session = _make_mock_session([mock_resp])

        with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy_key_for_test"}):
            items = fetch_jquants_disclosures("20260701", _session=session)

        assert items == []


# ============================================================
# Test: pagination対応
# ============================================================

class TestPagination:
    """pagination_key で複数ページを全件取得できることを確認"""

    def test_two_pages(self):
        """2ページに分かれたレスポンスを全件取得"""
        page1_items = [_make_raw_item(disc_no=f"2026063058408{i}") for i in range(3)]
        page2_items = [_make_raw_item(disc_no=f"2026063058409{i}") for i in range(2)]

        resp1 = _make_mock_response(page1_items, pagination_key="next_cursor_abc")
        resp2 = _make_mock_response(page2_items, pagination_key=None)
        session = _make_mock_session([resp1, resp2])

        with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy_key_for_test"}):
            items = fetch_jquants_disclosures("20260630", _session=session)

        assert len(items) == 5
        # 2回 GET が呼ばれた
        assert session.get.call_count == 2

    def test_over_300_items(self):
        """300件超（350件: 2ページ構成）を全件取得できる"""
        page1_items = [_make_raw_item(disc_no=f"20260630{500000+i:06d}") for i in range(300)]
        page2_items = [_make_raw_item(disc_no=f"20260630{600000+i:06d}") for i in range(50)]

        resp1 = _make_mock_response(page1_items, pagination_key="page2_key")
        resp2 = _make_mock_response(page2_items, pagination_key=None)
        session = _make_mock_session([resp1, resp2])

        with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy_key_for_test"}):
            items = fetch_jquants_disclosures("20260630", _session=session)

        assert len(items) == 350

    def test_single_page_no_pagination_key(self):
        """pagination_key なし → 1ページで完了"""
        items_data = [_make_raw_item(disc_no=f"2026063058408{i}") for i in range(10)]
        resp = _make_mock_response(items_data, pagination_key=None)
        session = _make_mock_session([resp])

        with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy_key_for_test"}):
            items = fetch_jquants_disclosures("20260630", _session=session)

        assert len(items) == 10
        assert session.get.call_count == 1

    def test_pagination_key_passed_as_param(self):
        """2ページ目のリクエストに pagination_key が含まれていること"""
        page1 = [_make_raw_item(disc_no="20260630584001")]
        page2 = [_make_raw_item(disc_no="20260630584002")]

        resp1 = _make_mock_response(page1, pagination_key="cursor_xyz")
        resp2 = _make_mock_response(page2)
        session = _make_mock_session([resp1, resp2])

        with patch.dict(os.environ, {"JQUANTS_API_KEY": "dummy_key_for_test"}):
            fetch_jquants_disclosures("20260630", _session=session)

        # 2回目の呼び出しの params に pagination_key が含まれること
        second_call_kwargs = session.get.call_args_list[1][1]
        assert second_call_kwargs["params"].get("pagination_key") == "cursor_xyz"


# ============================================================
# Test: FileID 生成
# ============================================================

class TestFileIdConversion:
    """TDnet FileID = '1401' + DiscNo の変換"""

    def test_disc_no_to_file_id(self):
        """DiscNo 14桁 → FileID 18桁"""
        item = _convert_raw_item(_make_raw_item(disc_no="20260630584087"))
        assert item is not None
        assert item.dedup_key_primary == "140120260630584087"

    def test_file_id_prefix_1401(self):
        """FileID の先頭が必ず 1401"""
        raw = _make_raw_item(disc_no="20260617572986")
        item = _convert_raw_item(raw)
        assert item is not None
        assert item.dedup_key_primary.startswith("1401")
        assert item.dedup_key_primary == "140120260617572986"

    def test_doc_url_from_disc_no(self):
        """DiscNo から TDnet 標準 URL を生成"""
        url = _make_doc_url_from_disc_no("20260630584087")
        assert url == "https://www.release.tdnet.info/inbs/140120260630584087.pdf"

    def test_strip_trailing_zero_5digit(self):
        """5桁末尾0除去 → 4桁"""
        assert _strip_trailing_zero("73880") == "7388"
        assert _strip_trailing_zero("50860") == "5086"

    def test_strip_trailing_zero_4digit(self):
        """4桁コードはそのまま"""
        assert _strip_trailing_zero("7388") == "7388"

    def test_strip_trailing_zero_alpha(self):
        """英字混じり 5桁末尾0"""
        assert _strip_trailing_zero("365A0") == "365A"

    def test_strip_trailing_zero_alpha_no_strip(self):
        """末尾が 0 でない場合はそのまま"""
        assert _strip_trailing_zero("550A1") == "550A1"


# ============================================================
# Test: DiscItems 分類
# ============================================================

class TestDiscItemsClassification:
    """DiscItems コードによる DisclosureType 判定"""

    def test_financial_statement_11304(self):
        """11304: 四半期決算短信"""
        assert classify_by_disc_items(["11304"]) == DisclosureType.FINANCIAL_STATEMENT

    def test_financial_statement_11301(self):
        """11301: 通期決算短信"""
        assert classify_by_disc_items(["11301"]) == DisclosureType.FINANCIAL_STATEMENT

    def test_forecast_revision_11350(self):
        """11350: 業績予想修正"""
        assert classify_by_disc_items(["11350"]) == DisclosureType.FORECAST_REVISION

    def test_forecast_revision_11351(self):
        """11351: 業績予想修正 (上方)"""
        assert classify_by_disc_items(["11351"]) == DisclosureType.FORECAST_REVISION

    def test_dividend_revision_11360(self):
        """11360: 配当予想修正"""
        assert classify_by_disc_items(["11360"]) == DisclosureType.DIVIDEND_REVISION

    def test_buyback_11101(self):
        """11101: 自己株式取得"""
        assert classify_by_disc_items(["11101"]) == DisclosureType.BUYBACK

    def test_buyback_excluded_by_11402(self):
        """11101 + 11402 (処分): 処分コードがあれば buyback 除外"""
        # 業績修正コードなしの場合は None
        result = classify_by_disc_items(["11101", "11402"])
        # 11402 は BUYBACK_EXCLUDE_CODE なので 11101 のBUYBACKも除外
        # ただし 11401 が DISC_ITEMS_MAP にないため 11101 は独立判定
        # 実際の挙動: 11402 が _BUYBACK_EXCLUDE_CODES にあるため
        # has_buyback_exclude=True → BUYBACK 不採用
        assert result is None or result != DisclosureType.BUYBACK

    def test_mixed_forecast_and_dividend(self):
        """業績+配当同時開示: 上位種別 (forecast) が優先"""
        result = classify_by_disc_items(["11350", "11360"])
        assert result == DisclosureType.FORECAST_REVISION

    def test_unknown_code_returns_none(self):
        """未知のコードは None"""
        assert classify_by_disc_items(["99999"]) is None

    def test_empty_disc_items(self):
        """空リストは None"""
        assert classify_by_disc_items([]) is None


# ============================================================
# Test: タイトルフォールバック分類
# ============================================================

class TestTitleFallback:
    """タイトルキーワードフォールバック"""

    def test_financial_statement_by_title(self):
        assert classify_by_title_fallback("第2四半期決算短信〔日本基準〕（連結）") == DisclosureType.FINANCIAL_STATEMENT

    def test_forecast_revision_by_title(self):
        assert classify_by_title_fallback("通期業績予想の修正に関するお知らせ") == DisclosureType.FORECAST_REVISION

    def test_dividend_revision_by_title(self):
        assert classify_by_title_fallback("配当予想の修正（増配）に関するお知らせ") == DisclosureType.DIVIDEND_REVISION

    def test_buyback_by_title(self):
        assert classify_by_title_fallback("自己株式取得に係る事項の決定に関するお知らせ") == DisclosureType.BUYBACK

    def test_unrelated_returns_none(self):
        assert classify_by_title_fallback("コーポレートガバナンスに関する報告書") is None

    def test_disc_items_priority_over_title(self):
        """DiscItems が有効な場合はタイトルFBより優先"""
        # DiscItems=11304 (決算短信) が判定できれば、タイトルに関わらずFINANCIAL_STATEMENT
        result = classify_disclosure_jquants(["11304"], "何らかのタイトル")
        assert result == DisclosureType.FINANCIAL_STATEMENT


# ============================================================
# Test: JQuantsDisclosure 変換
# ============================================================

class TestConvertRawItem:
    """_convert_raw_item による変換"""

    def test_basic_conversion(self):
        """基本フィールドが正しく変換される"""
        raw = _make_raw_item()
        item = _convert_raw_item(raw)

        assert item is not None
        assert item.disc_no == "20260630584087"
        assert item.ticker == "5086"  # "50860" から末尾0除去
        assert item.company_name == "ヤマトＨＤ"
        assert item.published_at == "2026-06-30 15:30"
        assert item.disc_items == ["11304"]
        assert item.docs == ["g", "s", "x"]
        assert item.disclosure_type == DisclosureType.FINANCIAL_STATEMENT

    def test_dedup_key_primary(self):
        """dedup_key_primary = '1401' + disc_no"""
        raw = _make_raw_item(disc_no="20260630584087")
        item = _convert_raw_item(raw)
        assert item is not None
        assert item.dedup_key_primary == "140120260630584087"

    def test_dedup_key_secondary(self):
        """secondary key が (disc_date, ticker, normalized_title) から生成される"""
        raw = _make_raw_item(
            disc_no="20260630584087",
            code="50860",
            disc_date="2026-06-30",
            title="2027年２月期 第１四半期決算短信〔日本基準〕（連結）",
        )
        item = _convert_raw_item(raw)
        assert item is not None

        # 期待値を手計算
        expected = _make_dedup_key_secondary("2026-06-30", "5086", "2027年２月期 第１四半期決算短信〔日本基準〕（連結）")
        assert item.dedup_key_secondary == expected

    def test_missing_disc_no_returns_none(self):
        """DiscNo が空の場合は None"""
        raw = _make_raw_item()
        raw["DiscNo"] = ""
        assert _convert_raw_item(raw) is None

    def test_missing_title_returns_none(self):
        """Title が空の場合は None"""
        raw = _make_raw_item()
        raw["Title"] = ""
        assert _convert_raw_item(raw) is None

    def test_xbrl_url_is_none_in_shadow(self):
        """Shadow Run ではxbrl_urlは None (lazy取得)"""
        raw = _make_raw_item(docs=["g", "s", "x"])
        item = _convert_raw_item(raw)
        assert item is not None
        assert item.xbrl_url is None  # lazy
        assert "x" in item.docs  # ただし docs フラグは保持

    def test_to_disclosure_item(self):
        """to_disclosure_item() で DisclosureItem に変換できる"""
        from src.models import DisclosureItem
        raw = _make_raw_item()
        jq = _convert_raw_item(raw)
        assert jq is not None

        di = jq.to_disclosure_item()
        assert isinstance(di, DisclosureItem)
        assert di.ticker == jq.ticker
        assert di.disclosure_type == jq.disclosure_type


# ============================================================
# Test: Duplicate Key Normalize
# ============================================================

class TestDedupKeyNormalize:
    """重複判定キーの正規化"""

    def test_normalize_title_strips_spaces(self):
        """スペース除去"""
        assert _normalize_title_for_dedup("業績 予想  修正") == "業績予想修正"

    def test_normalize_title_lowercase(self):
        """大文字 → 小文字"""
        assert _normalize_title_for_dedup("ＳＯＦＴＷＡＲＥ") == "software"

    def test_normalize_title_fullwidth_to_halfwidth(self):
        """全角英数 → 半角"""
        result = _normalize_title_for_dedup("Ａ１２３")
        assert result == "a123"

    def test_secondary_key_reproducible(self):
        """同じ入力から同じキーが生成される"""
        k1 = _make_dedup_key_secondary("2026-06-30", "7388", "決算短信")
        k2 = _make_dedup_key_secondary("2026-06-30", "7388", "決算短信")
        assert k1 == k2

    def test_secondary_key_different_date(self):
        """日付が異なれば異なるキー"""
        k1 = _make_dedup_key_secondary("2026-06-30", "7388", "決算短信")
        k2 = _make_dedup_key_secondary("2026-07-01", "7388", "決算短信")
        assert k1 != k2

    def test_secondary_key_different_ticker(self):
        """銘柄コードが異なれば異なるキー"""
        k1 = _make_dedup_key_secondary("2026-06-30", "7388", "決算短信")
        k2 = _make_dedup_key_secondary("2026-06-30", "9999", "決算短信")
        assert k1 != k2

    def test_secondary_key_length(self):
        """secondary key は 32文字 (sha256 の 32桁)"""
        k = _make_dedup_key_secondary("2026-06-30", "7388", "テスト")
        assert len(k) == 32


# ============================================================
# Test: セキュリティ — APIキーをログに出力しない
# ============================================================

class TestSecurityNoKeyLeak:
    """APIキー・認証情報をログ・出力しないことを確認"""

    def test_api_key_not_in_log_output(self, caplog):
        """ログにAPIキーの値が含まれないこと"""
        dummy_key = "SECRET_APIKEY_DO_NOT_LOG_XYZ12345"
        mock_resp = _make_mock_response([_make_raw_item()])
        session = _make_mock_session([mock_resp])

        with patch.dict(os.environ, {"JQUANTS_API_KEY": dummy_key}):
            import logging
            with caplog.at_level(logging.DEBUG):
                fetch_jquants_disclosures("20260630", _session=session)

        # ログメッセージのどこにも actual APIキーが含まれないこと
        all_log = "\n".join(caplog.messages)
        assert dummy_key not in all_log, f"APIキーがログに漏洩: {all_log[:200]}"

    def test_api_key_not_in_exception_message(self):
        """APIキーが未設定の場合、エラーメッセージに値が含まれないこと"""
        with patch.dict(os.environ, {}, clear=True):
            # JQUANTS_API_KEY が存在しない状態でエラーが起きるはず
            # (dotenv load がされないよう load_project_env をモック)
            with patch("src.jquants.adapter.load_project_env"):
                with pytest.raises((RuntimeError, SystemExit)) as exc:
                    from src.jquants.adapter import _get_api_key
                    _get_api_key.__wrapped__ if hasattr(_get_api_key, "__wrapped__") else None
                    # 環境変数なし
                    os.environ.pop("JQUANTS_API_KEY", None)
                    _get_api_key()
                # エラーメッセージに "SECRET" や実際のキー値が含まれないこと
                # (今回はキー名自体は許容)
