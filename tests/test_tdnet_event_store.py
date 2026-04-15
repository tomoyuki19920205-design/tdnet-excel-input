#!/usr/bin/env python3
"""test_tdnet_event_store.py — tdnet_event_store のユニットテスト

テスト対象:
- build_dedupe_key: 同一入力→同一キー、異なる入力→異なるキー
- compute_priority_rank: event_type + subtype → 正しいランク
- build_display_title: 各イベント種別の表示タイトル整形
- build_display_summary: 要約テキスト生成
"""
import json
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.events.common_models import EventRecord, EventType
from src.events.tdnet_event_store import (
    build_dedupe_key,
    compute_priority_rank,
    _normalize_display_category,
)
from src.events.common_notify import (
    build_display_title,
    build_display_summary,
)


def _make_event(
    ticker: str = "7203",
    company_name: str = "トヨタ自動車",
    event_type: str = EventType.BUYBACK,
    subtype: str = "resolution",
    title: str = "自己株式の取得に関するお知らせ",
    disclosure_datetime: str = "2026-03-20 15:00",
    payload: dict | None = None,
    summary_text: str = "",
) -> EventRecord:
    return EventRecord(
        ticker=ticker,
        company_name=company_name,
        event_type=event_type,
        subtype=subtype,
        title=title,
        disclosure_datetime=disclosure_datetime,
        extracted_payload_json=json.dumps(payload or {}, ensure_ascii=False),
        summary_text=summary_text,
    )


# ============================================================
# A. dedupe_key テスト
# ============================================================
class TestDedupeKey(unittest.TestCase):

    def test_same_input_same_key(self):
        """同一入力 → 同一キー"""
        ev1 = _make_event()
        ev2 = _make_event()
        self.assertEqual(build_dedupe_key(ev1), build_dedupe_key(ev2))

    def test_different_ticker_different_key(self):
        ev1 = _make_event(ticker="7203")
        ev2 = _make_event(ticker="6758")
        self.assertNotEqual(build_dedupe_key(ev1), build_dedupe_key(ev2))

    def test_different_type_different_key(self):
        ev1 = _make_event(event_type=EventType.BUYBACK)
        ev2 = _make_event(event_type=EventType.FORECAST_REVISION)
        self.assertNotEqual(build_dedupe_key(ev1), build_dedupe_key(ev2))

    def test_different_time_different_key(self):
        ev1 = _make_event(disclosure_datetime="2026-03-20 15:00")
        ev2 = _make_event(disclosure_datetime="2026-03-20 16:00")
        self.assertNotEqual(build_dedupe_key(ev1), build_dedupe_key(ev2))

    def test_key_is_hex_string(self):
        ev = _make_event()
        key = build_dedupe_key(ev)
        self.assertEqual(len(key), 40)
        self.assertTrue(all(c in "0123456789abcdef" for c in key))

    def test_empty_fields_produce_key(self):
        """空フィールドでもエラーなくキーが生成される"""
        ev = EventRecord(event_type=EventType.BUYBACK)
        key = build_dedupe_key(ev)
        self.assertIsInstance(key, str)
        self.assertEqual(len(key), 40)


# ============================================================
# B. priority_rank テスト
# ============================================================
class TestPriorityRank(unittest.TestCase):

    def test_buyback_rank_10(self):
        ev = _make_event(event_type=EventType.BUYBACK, subtype="resolution")
        self.assertEqual(compute_priority_rank(ev), 10)

    def test_forecast_upward_rank_20(self):
        ev = _make_event(event_type=EventType.FORECAST_REVISION, subtype="upward")
        self.assertEqual(compute_priority_rank(ev), 20)

    def test_dividend_increase_rank_30(self):
        ev = _make_event(event_type=EventType.DIVIDEND_REVISION, subtype="increase")
        self.assertEqual(compute_priority_rank(ev), 30)

    def test_forecast_downward_rank_50(self):
        ev = _make_event(event_type=EventType.FORECAST_REVISION, subtype="downward")
        self.assertEqual(compute_priority_rank(ev), 50)

    def test_unknown_type_rank_90(self):
        ev = _make_event(event_type="unknown_type", subtype="xyz")
        self.assertEqual(compute_priority_rank(ev), 90)

    def test_special_dividend_rank_30(self):
        ev = _make_event(event_type=EventType.DIVIDEND_REVISION, subtype="special_dividend")
        self.assertEqual(compute_priority_rank(ev), 30)


# ============================================================
# C. display_title テスト
# ============================================================
class TestDisplayTitle(unittest.TestCase):

    def test_buyback_with_ratio(self):
        ev = _make_event(
            event_type=EventType.BUYBACK,
            payload={"ratio_to_outstanding": 4.2}
        )
        title = build_display_title(ev)
        self.assertIn("自社株買い", title)
        self.assertIn("トヨタ", title)
        # 新フォーマット: ratio は display_summary に
        summary = build_display_summary(ev)
        self.assertIn("4.2%", summary)

    def test_buyback_without_ratio(self):
        ev = _make_event(event_type=EventType.BUYBACK)
        title = build_display_title(ev)
        self.assertIn("自社株買い", title)

    def test_forecast_upward(self):
        ev = _make_event(
            event_type=EventType.FORECAST_REVISION,
            subtype="upward",
            payload={"change_net_income_pct": 25.3}
        )
        title = build_display_title(ev)
        self.assertIn("上方修正", title)
        # 新フォーマット: display_title は短い見出し、指標は display_summary に
        self.assertIn("トヨタ", title)

    def test_forecast_downward(self):
        ev = _make_event(
            event_type=EventType.FORECAST_REVISION,
            subtype="downward",
            payload={"change_op_pct": -15.0}
        )
        title = build_display_title(ev)
        self.assertIn("下方修正", title)
        self.assertIn("トヨタ", title)

    def test_dividend_increase(self):
        ev = _make_event(
            event_type=EventType.DIVIDEND_REVISION,
            subtype="increase",
            payload={"previous_dividend_per_share": 100, "revised_dividend_per_share": 120}
        )
        title = build_display_title(ev)
        self.assertIn("増配", title)
        # 新フォーマット: display_summary に配当額変化
        summary = build_display_summary(ev)
        self.assertIn("100", summary)
        self.assertIn("120", summary)

    def test_dividend_resumption(self):
        ev = _make_event(
            event_type=EventType.DIVIDEND_REVISION,
            subtype="increase",
            payload={"previous_dividend_per_share": 0, "revised_dividend_per_share": 50}
        )
        title = build_display_title(ev)
        self.assertIn("復配", title)

    def test_dividend_special(self):
        ev = _make_event(
            event_type=EventType.DIVIDEND_REVISION,
            subtype="special_dividend",
        )
        title = build_display_title(ev)
        self.assertIn("特別配当", title)


# ============================================================
# D. display_summary テスト
# ============================================================
class TestDisplaySummary(unittest.TestCase):

    def test_buyback_summary(self):
        ev = _make_event(
            event_type=EventType.BUYBACK,
            payload={
                "ratio_to_outstanding": 5.0,
                "amount_limit_million_yen": 500,
                "start_date": "2026-04-01",
                "end_date": "2026-09-30",
            }
        )
        summary = build_display_summary(ev)
        self.assertIn("5.0%", summary)
        self.assertIn("億円", summary)

    def test_forecast_summary(self):
        ev = _make_event(
            event_type=EventType.FORECAST_REVISION,
            subtype="upward",
            payload={
                "period_label": "FY2026",
                "revised_net_income": 1500,
                "change_net_income_pct": 25.0,
            }
        )
        summary = build_display_summary(ev)
        self.assertIn("FY2026", summary)
        self.assertIn("純利益", summary)

    def test_dividend_summary(self):
        ev = _make_event(
            event_type=EventType.DIVIDEND_REVISION,
            payload={"previous_dividend_per_share": 50, "revised_dividend_per_share": 70}
        )
        summary = build_display_summary(ev)
        self.assertIn("50", summary)
        self.assertIn("70", summary)

    def test_empty_payload_fallback(self):
        """メトリクスなしの場合、titleにフォールバック"""
        ev = _make_event(event_type=EventType.BUYBACK)
        ev.title = "テストタイトル"
        summary = build_display_summary(ev)
        # buyback with no payload: title にフォールバック
        self.assertIn("テストタイトル", summary)


# ============================================================
# E. _normalize_display_category テスト
# ============================================================
class TestNormalizeDisplayCategory(unittest.TestCase):

    # --- event_type 直接マッピング ---
    def test_buyback_event_type(self):
        ev = _make_event(event_type="buyback", title="何かのお知らせ")
        self.assertEqual(_normalize_display_category(ev), "buyback")

    def test_forecast_revision_event_type(self):
        ev = _make_event(event_type="forecast_revision", title="何かのお知らせ")
        self.assertEqual(_normalize_display_category(ev), "forecast")

    def test_dividend_revision_event_type(self):
        ev = _make_event(event_type="dividend_revision", title="何かのお知らせ")
        self.assertEqual(_normalize_display_category(ev), "dividend")

    # --- headline キーワードマッチ (自己株式取得系) ---
    def test_headline_buyback_jikokabushiki(self):
        ev = _make_event(event_type="undecided", title="自己株式取得に係る事項の決定")
        self.assertEqual(_normalize_display_category(ev), "buyback")

    def test_headline_buyback_jikokabushiki_no_shutoku(self):
        ev = _make_event(event_type="", title="自己株式の取得状況に関するお知らせ")
        self.assertEqual(_normalize_display_category(ev), "buyback")

    def test_headline_buyback_jishakabugai(self):
        ev = _make_event(event_type="unknown", title="自社株買いに関するお知らせ")
        self.assertEqual(_normalize_display_category(ev), "buyback")

    def test_headline_buyback_shoukyaku(self):
        ev = _make_event(event_type="", title="自己株式消却に関するお知らせ")
        self.assertEqual(_normalize_display_category(ev), "buyback")

    # --- headline キーワードマッチ (業績予想系) ---
    def test_headline_forecast_gyoseki(self):
        ev = _make_event(event_type="unknown", title="業績予想の修正に関するお知らせ")
        self.assertEqual(_normalize_display_category(ev), "forecast")

    def test_headline_forecast_jouhou(self):
        ev = _make_event(event_type="", title="通期業績予想の上方修正に関するお知らせ")
        self.assertEqual(_normalize_display_category(ev), "forecast")

    def test_headline_forecast_kahou(self):
        ev = _make_event(event_type="", title="通期連結業績予想の下方修正に関するお知らせ")
        self.assertEqual(_normalize_display_category(ev), "forecast")

    # --- headline キーワードマッチ (配当系) ---
    def test_headline_dividend_haitou(self):
        ev = _make_event(event_type="", title="配当予想の修正に関するお知らせ")
        self.assertEqual(_normalize_display_category(ev), "dividend")

    def test_headline_dividend_zohai(self):
        ev = _make_event(event_type="undecided", title="増配に関するお知らせ")
        self.assertEqual(_normalize_display_category(ev), "dividend")

    def test_headline_dividend_kinen(self):
        ev = _make_event(event_type="", title="創立50周年記念配当に関するお知らせ")
        self.assertEqual(_normalize_display_category(ev), "dividend")

    # --- headline キーワードマッチ (決算系) ---
    def test_headline_earnings_tanshin(self):
        ev = _make_event(event_type="", title="2026年3月期 第3四半期決算短信〔日本基準〕")
        self.assertEqual(_normalize_display_category(ev), "earnings")

    def test_headline_earnings_kessan(self):
        ev = _make_event(event_type="undecided", title="2026年3月期 決算概要")
        self.assertEqual(_normalize_display_category(ev), "earnings")

    # --- headline キーワードマッチ (大量保有系) ---
    def test_headline_shareholder_tairyou(self):
        ev = _make_event(event_type="", title="大量保有報告書の提出に関するお知らせ")
        self.assertEqual(_normalize_display_category(ev), "shareholder")

    def test_headline_shareholder_henkou(self):
        ev = _make_event(event_type="unknown", title="変更報告書の提出に関するお知らせ")
        self.assertEqual(_normalize_display_category(ev), "shareholder")

    # --- フォールバック ---
    def test_fallback_other(self):
        ev = _make_event(event_type="unknown_xyz", title="組織変更のお知らせ")
        self.assertEqual(_normalize_display_category(ev), "other")

    def test_fallback_empty(self):
        ev = _make_event(event_type="", title="")
        self.assertEqual(_normalize_display_category(ev), "other")

    def test_case_insensitive_event_type(self):
        ev = _make_event(event_type="BUYBACK", title="何か")
        self.assertEqual(_normalize_display_category(ev), "buyback")

    def test_summary_text_keyword_match(self):
        ev = _make_event(
            event_type="undecided",
            title="重要なお知らせ",
            summary_text="自己株式取得に関する決議",
        )
        self.assertEqual(_normalize_display_category(ev), "buyback")


if __name__ == "__main__":
    unittest.main()
