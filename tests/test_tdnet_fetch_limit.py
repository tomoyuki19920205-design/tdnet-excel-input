"""
tests/test_tdnet_fetch_limit.py
================================
fetcher.py の300件上限到達検知・HTMLクロスチェック・dedupe・
possible_truncation フラグのテスト。

全テストはネットワーク不使用（モック）。
"""
from __future__ import annotations

import logging
import pytest
from unittest.mock import MagicMock, patch

from src.fetcher import (
    fetch_new_disclosures,
    TDNET_API_FETCH_LIMIT,
)
from src.models import DisclosureItem, DisclosureType
from src.utils import sha256


# ============================================================
# テスト用 DisclosureItem ファクトリ
# ============================================================

def _make_item(n: int, prefix: str = "api") -> DisclosureItem:
    url = f"https://example.com/{prefix}_{n:04d}.pdf"
    return DisclosureItem(
        disclosure_id=sha256(url),
        ticker="1234",
        company_name=f"テスト会社{n}",
        title="決算短信",
        doc_url=url,
        published_at="2026-06-24 16:00",
        xbrl_url=None,
        disclosure_type=DisclosureType.FINANCIAL_STATEMENT,
    )


def _make_api_items(count: int) -> list[DisclosureItem]:
    return [_make_item(i, "api") for i in range(count)]


def _make_html_items_overlap(count: int) -> list[DisclosureItem]:
    """APIと同じ番号（重複）のHTML items。"""
    return [_make_item(i, "api") for i in range(count)]


def _make_html_items_extended(api_count: int, extra: int) -> list[DisclosureItem]:
    """APIと重複する api_count 件 + 新規 extra 件。"""
    return (
        [_make_item(i, "api") for i in range(api_count)]
        + [_make_item(i + api_count, "api") for i in range(extra)]
    )


# ============================================================
# Phase 1: 300件到達時の WARN ログテスト
# ============================================================

class TestTdnetFetchLimitReachedWarn:

    def test_no_warn_below_limit(self, caplog):
        """299件ではWARNINGを出さない。"""
        api_items = _make_api_items(TDNET_API_FETCH_LIMIT - 1)

        with patch("src.fetcher._fetch_via_api", return_value=api_items), \
             caplog.at_level(logging.WARNING, logger="tdnet"):
            fetch_new_disclosures()

        warn_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("TDNET_FETCH_LIMIT_REACHED" in m for m in warn_msgs)

    def test_warns_at_limit(self, caplog):
        """300件到達時に TDNET_FETCH_LIMIT_REACHED WARNINGを出す。"""
        api_items = _make_api_items(TDNET_API_FETCH_LIMIT)
        html_items = _make_html_items_extended(TDNET_API_FETCH_LIMIT, 50)

        with patch("src.fetcher._fetch_via_api", return_value=api_items), \
             patch("src.fetcher._fetch_via_html", return_value=html_items), \
             caplog.at_level(logging.WARNING, logger="tdnet"):
            fetch_new_disclosures()

        warn_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("TDNET_FETCH_LIMIT_REACHED" in m for m in warn_msgs)


# ============================================================
# Phase 2: HTML クロスチェック + dedupe テスト
# ============================================================

class TestTdnetFetchHtmlCrossCheck:

    def test_html_crosscheck_triggered_at_limit(self):
        """300件到達時にHTMLスクレイピングも呼ばれる。"""
        api_items = _make_api_items(TDNET_API_FETCH_LIMIT)
        html_items = _make_html_items_overlap(TDNET_API_FETCH_LIMIT)

        mock_html = MagicMock(return_value=html_items)
        with patch("src.fetcher._fetch_via_api", return_value=api_items), \
             patch("src.fetcher._fetch_via_html", mock_html):
            fetch_new_disclosures()

        mock_html.assert_called_once()

    def test_html_not_called_below_limit(self):
        """299件ではHTMLクロスチェックは呼ばれない。"""
        api_items = _make_api_items(TDNET_API_FETCH_LIMIT - 1)

        mock_html = MagicMock()
        with patch("src.fetcher._fetch_via_api", return_value=api_items), \
             patch("src.fetcher._fetch_via_html", mock_html):
            fetch_new_disclosures()

        mock_html.assert_not_called()

    def test_dedupes_api_and_html_results(self, caplog):
        """API+HTML の結果がdoc_idでdedupeされる。"""
        api_items = _make_api_items(TDNET_API_FETCH_LIMIT)
        # HTML: api_0..299 (重複300) + api_300..349 (新規50) = 350件
        html_items = _make_html_items_extended(TDNET_API_FETCH_LIMIT, 50)

        with patch("src.fetcher._fetch_via_api", return_value=api_items), \
             patch("src.fetcher._fetch_via_html", return_value=html_items), \
             caplog.at_level(logging.INFO, logger="tdnet"):
            fetch_new_disclosures()

        info_msgs = [r.message for r in caplog.records]
        deduped_logs = [m for m in info_msgs if "TDNET_FETCH_DEDUPED" in m]
        assert deduped_logs, f"TDNET_FETCH_DEDUPED ログが出なかった: {info_msgs[-5:]}"

        d = deduped_logs[0]
        # before = 300(api) + 350(html) = 650, after = 350, dup = 300
        assert "before=650" in d, f"before件数不正: {d}"
        assert "after=350" in d, f"after件数不正: {d}"
        assert "duplicates=300" in d, f"重複件数不正: {d}"

    def test_html_exceeds_api_resolves_truncation(self, caplog):
        """HTMLがAPIより多い件数を取得できた場合はresolved=trueになる。"""
        api_items = _make_api_items(TDNET_API_FETCH_LIMIT)
        html_items = _make_html_items_extended(TDNET_API_FETCH_LIMIT, 50)

        with patch("src.fetcher._fetch_via_api", return_value=api_items), \
             patch("src.fetcher._fetch_via_html", return_value=html_items), \
             caplog.at_level(logging.INFO, logger="tdnet"):
            fetch_new_disclosures()

        info_msgs = [r.message for r in caplog.records]
        resolved_logs = [m for m in info_msgs if "resolved=true" in m and "TDNET_FETCH_LIMIT_REACHED" in m]
        assert resolved_logs, f"resolved=true ログが出なかった: {info_msgs}"


# ============================================================
# Phase 3: possible_truncation テスト
# ============================================================

class TestTdnetFetchPossibleTruncation:

    def test_possible_truncation_when_html_fails(self, caplog):
        """300件到達 + HTML失敗 → possible_truncation=true ログが出る。"""
        api_items = _make_api_items(TDNET_API_FETCH_LIMIT)

        with patch("src.fetcher._fetch_via_api", return_value=api_items), \
             patch("src.fetcher._fetch_via_html", side_effect=Exception("mock html error")), \
             caplog.at_level(logging.WARNING, logger="tdnet"):
            fetch_new_disclosures()

        warn_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("possible_truncation=true" in m for m in warn_msgs)

    def test_possible_truncation_when_html_same_count(self, caplog):
        """HTMLがAPIと同件数（全重複）の場合も possible_truncation 警告が出る。"""
        api_items = _make_api_items(TDNET_API_FETCH_LIMIT)
        html_items = _make_html_items_overlap(TDNET_API_FETCH_LIMIT)

        with patch("src.fetcher._fetch_via_api", return_value=api_items), \
             patch("src.fetcher._fetch_via_html", return_value=html_items), \
             caplog.at_level(logging.WARNING, logger="tdnet"):
            fetch_new_disclosures()

        warn_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        limit_warn = [m for m in warn_msgs if "html_may_also_be_limited" in m or "resolved=false" in m]
        assert limit_warn, f"HTML <= API の場合に警告が出なかった: {warn_msgs}"

    def test_no_possible_truncation_when_html_larger(self, caplog):
        """HTMLがAPIより多い場合は fetched_count ログに possible_truncation=False が含まれる。"""
        api_items = _make_api_items(TDNET_API_FETCH_LIMIT)
        html_items = _make_html_items_extended(TDNET_API_FETCH_LIMIT, 50)

        with patch("src.fetcher._fetch_via_api", return_value=api_items), \
             patch("src.fetcher._fetch_via_html", return_value=html_items), \
             caplog.at_level(logging.INFO, logger="tdnet"):
            fetch_new_disclosures()

        info_msgs = [r.message for r in caplog.records]
        fetched_logs = [m for m in info_msgs if "fetched_count=" in m and "possible_truncation=" in m]
        assert fetched_logs, f"fetched_count ログに possible_truncation が入っていない"
        assert any("possible_truncation=False" in m for m in fetched_logs), \
            f"HTML > API なのに possible_truncation=False でない: {fetched_logs}"


# ============================================================
# Phase 4: バックフィルモードは300件チェックをしない
# ============================================================

class TestTdnetFetchBackfillNotAffected:

    def test_backfill_does_not_trigger_limit_check(self, caplog):
        """過去日付指定時はHTMLのみ使用し、300件チェックは走らない。"""
        html_items = _make_html_items_overlap(10)

        with patch("src.fetcher._fetch_via_html", return_value=html_items), \
             patch("src.fetcher._fetch_via_api") as mock_api, \
             caplog.at_level(logging.WARNING, logger="tdnet"):
            fetch_new_disclosures(target_date="20260101")

        mock_api.assert_not_called()
        warn_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("TDNET_FETCH_LIMIT_REACHED" in m for m in warn_msgs)
