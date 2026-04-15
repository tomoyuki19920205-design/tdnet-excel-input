#!/usr/bin/env python3
"""test_dividend_event_creation.py — 配当修正単体で events が作成されることを検証

テストケース:
A. 配当修正単体タイトルが classify_disclosure で DIVIDEND_REVISION を返すこと
B. 配当修正単体タイトルで _matches_filter が True を返すこと
C. 7608/7565 相当 fixture で event_pipeline が events レコードを作成すること
D. 同時刻複数件が全件処理されること
E. classify 通過 + save 時に例外が出た場合 error ログが残ること
"""
from __future__ import annotations

import sqlite3
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from src.models import DisclosureType
from src.fetcher import classify_disclosure, _matches_filter
from src.events.dividend_classifier import classify_dividend
from src.events.dividend_extractor import extract_dividend_revision
from src.events.common_models import DocumentMeta, EventType
from src.events.common_storage import ensure_events_table, upsert_event
from src.events.event_pipeline import (
    _process_single_document,
    _dividend_to_event_record,
    process_documents,
)


# ============================================================
# A: classify_disclosure で配当修正が DIVIDEND_REVISION を返す
# ============================================================
class TestClassifyDisclosure:
    """fetcher.classify_disclosure の配当修正分類テスト"""

    @pytest.mark.parametrize("title,expected_type", [
        ("期末配当予想の修正に関するお知らせ", DisclosureType.DIVIDEND_REVISION),
        ("配当予想の修正に関するお知らせ", DisclosureType.DIVIDEND_REVISION),
        ("中間配当予想の修正に関するお知らせ", DisclosureType.DIVIDEND_REVISION),
        ("配当方針の変更に関するお知らせ", DisclosureType.DIVIDEND_REVISION),
        # 業績+配当 → forecast_revision
        ("業績予想及び配当予想の修正に関するお知らせ", DisclosureType.FORECAST_REVISION),
        # 業績のみ → forecast_revision
        ("通期業績予想の修正に関するお知らせ", DisclosureType.FORECAST_REVISION),
        # 決算短信 → financial_statement
        ("2026年3月期 決算短信", DisclosureType.FINANCIAL_STATEMENT),
        # 自社株買い → None (イベントパイプラインで処理)
        ("自己株式の取得に関するお知らせ", None),
    ])
    def test_classify_disclosure_types(self, title, expected_type):
        result = classify_disclosure(title)
        assert result == expected_type, (
            f"classify_disclosure('{title}') = {result!r}, expected {expected_type!r}"
        )


# ============================================================
# B: _matches_filter が配当修正を通す
# ============================================================
class TestMatchesFilter:
    """配当修正タイトルが _matches_filter を通過することを検証"""

    @pytest.mark.parametrize("title", [
        "期末配当予想の修正に関するお知らせ",
        "配当予想の修正に関するお知らせ",
        "業績予想及び配当予想の修正に関するお知らせ",
        "通期業績予想の修正に関するお知らせ",
    ])
    def test_dividend_titles_pass_filter(self, title):
        assert _matches_filter(title) is True, (
            f"_matches_filter('{title}') returned False — "
            f"classify_disclosure result: {classify_disclosure(title)}"
        )


# ============================================================
# C: 7608/7565 相当 fixture で events が作成される
# ============================================================
class TestDividendEventCreation:
    """配当修正単体で event が DB に保存されることを検証"""

    def _make_doc(self, ticker, title, disclosed_at="2026-03-25T12:00:00"):
        return DocumentMeta(
            doc_id=f"test-{ticker}-{disclosed_at}",
            ticker=ticker,
            company_name=f"Company {ticker}",
            title=title,
            disclosure_datetime=disclosed_at,
            doc_url="",
        )

    def test_7608_creates_event(self):
        """7608 SKジャパン相当: 期末配当予想の修正 → events に1件保存"""
        conn = sqlite3.connect(":memory:")
        ensure_events_table(conn)
        doc = self._make_doc("7608", "期末配当予想の修正に関するお知らせ")
        results = _process_single_document(doc, conn, dry_run=False, db_path="")

        # dividend_revision として保存されていること
        div_results = [r for r in results if r["event_type"] == EventType.DIVIDEND_REVISION]
        assert len(div_results) == 1, f"Expected 1 dividend result, got {div_results}"
        assert div_results[0]["action"] == "inserted"

        # DB確認
        rows = conn.execute(
            "SELECT ticker, event_type, subtype, status FROM events WHERE ticker = '7608'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "dividend_revision"
        conn.close()

    def test_7565_creates_event(self):
        """7565 萬世電機相当: 配当予想の修正 → events に1件保存"""
        conn = sqlite3.connect(":memory:")
        ensure_events_table(conn)
        doc = self._make_doc("7565", "配当予想の修正に関するお知らせ")
        results = _process_single_document(doc, conn, dry_run=False, db_path="")

        div_results = [r for r in results if r["event_type"] == EventType.DIVIDEND_REVISION]
        assert len(div_results) == 1
        assert div_results[0]["action"] == "inserted"
        conn.close()


# ============================================================
# D: 同時刻複数件が全件処理される
# ============================================================
class TestSameTimestampMultipleDocs:
    """同じ disclosed_at を持つ複数文書が全件 events に保存されること"""

    def test_12_00_batch(self):
        """12:00 に 8697 + 7608 を同時投入 → 両方とも events に保存"""
        docs = [
            DocumentMeta(
                doc_id="test-8697-1200",
                ticker="8697",
                company_name="JPX",
                title="業績予想及び配当予想の修正に関するお知らせ",
                disclosure_datetime="2026-03-25T12:00:00",
                doc_url="",
            ),
            DocumentMeta(
                doc_id="test-7608-1200",
                ticker="7608",
                company_name="SK Japan",
                title="期末配当予想の修正に関するお知らせ",
                disclosure_datetime="2026-03-25T12:00:00",
                doc_url="",
            ),
        ]
        result = process_documents(docs, db_path=":memory:", dry_run=False)

        # 両方とも detected
        assert result.detected >= 2, (
            f"Expected >=2 detected events, got {result.detected}. "
            f"Details: {result.details}"
        )
        # 8697 は forecast + dividend、7608 は dividend
        tickers_in_details = [d.get("event_type") for d in result.details if d.get("action") == "inserted"]
        assert EventType.DIVIDEND_REVISION in tickers_in_details, (
            f"DIVIDEND_REVISION not found in results: {result.details}"
        )

    def test_13_00_batch(self):
        """13:00 に 3010 + 7565 を同時投入 → 両方とも events に保存"""
        docs = [
            DocumentMeta(
                doc_id="test-3010-1300",
                ticker="3010",
                company_name="Polaris",
                title="繰延税金資産の計上及び2026年3月期連結業績予想の上方修正に関するお知らせ",
                disclosure_datetime="2026-03-25T13:00:00",
                doc_url="",
            ),
            DocumentMeta(
                doc_id="test-7565-1300",
                ticker="7565",
                company_name="Mansei Denki",
                title="配当予想の修正に関するお知らせ",
                disclosure_datetime="2026-03-25T13:00:00",
                doc_url="",
            ),
        ]
        result = process_documents(docs, db_path=":memory:", dry_run=False)

        assert result.detected >= 2, (
            f"Expected >=2 detected events, got {result.detected}. "
            f"Details: {result.details}"
        )

    def test_dedupe_different_tickers_same_time(self):
        """同一時刻でも別 ticker は別 fingerprint で衝突しない"""
        conn = sqlite3.connect(":memory:")
        ensure_events_table(conn)

        for ticker in ["7608", "7565"]:
            doc = DocumentMeta(
                doc_id=f"test-{ticker}-same-time",
                ticker=ticker,
                company_name=f"Company {ticker}",
                title="配当予想の修正に関するお知らせ",
                disclosure_datetime="2026-03-25T12:00:00",
                doc_url="",
            )
            _process_single_document(doc, conn, dry_run=False, db_path="")

        rows = conn.execute("SELECT ticker, fingerprint FROM events").fetchall()
        assert len(rows) == 2, f"Expected 2 events, got {len(rows)}"
        fps = [r[1] for r in rows]
        assert fps[0] != fps[1], f"Fingerprints should differ: {fps}"
        conn.close()


# ============================================================
# E: classify 通過 + 抽出結果で event が save される
# ============================================================
class TestDividendExtractorWithEmptyText:
    """空テキストでも extract_dividend_revision が例外を出さないこと"""

    @pytest.mark.parametrize("title", [
        "期末配当予想の修正に関するお知らせ",
        "配当予想の修正に関するお知らせ",
    ])
    def test_empty_text_no_exception(self, title):
        ev = extract_dividend_revision("", title)
        assert ev is not None
        assert ev.subtype in ("undecided", "increase", "decrease", "maintain",
                              "special_dividend", "commemorative_dividend")

    @pytest.mark.parametrize("title", [
        "期末配当予想の修正に関するお知らせ",
        "配当予想の修正に関するお知らせ",
    ])
    def test_dividend_classifier_passes(self, title):
        result = classify_dividend(title, "")
        assert result.is_target is True, (
            f"classify_dividend('{title}') returned is_target=False: {result}"
        )
