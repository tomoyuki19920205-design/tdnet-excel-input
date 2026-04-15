#!/usr/bin/env python3
"""test_notify_rules.py — 通知判定・ソートルールのテスト

テスト対象:
- should_notify_event: 各イベント種別の通知条件
- get_notification_sort_key: 通知順ソート
- filter_and_sort_events: 一括フィルタ+ソート
- ステータス管理: 非通知が notified にならないこと
"""
import json
import sqlite3
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.events.common_models import EventRecord, EventType
from src.events.notify_rules import (
    should_notify_event,
    get_notification_sort_key,
    filter_and_sort_events,
    compute_dividend_increase_ratio,
    is_resumption_dividend,
)
from src.events.common_storage import (
    ensure_events_table,
    upsert_event,
    get_unnotified_events,
    mark_notified,
    mark_filtered,
)


def _make_buyback(ratio: float | None = None, subtype: str = "resolution",
                  ticker: str = "1234", disclosure_dt: str = "2026-03-20 15:00") -> EventRecord:
    payload = {}
    if ratio is not None:
        payload["ratio_to_outstanding"] = ratio
    return EventRecord(
        ticker=ticker,
        company_name="テスト社",
        disclosure_datetime=disclosure_dt,
        title="自社株買いテスト",
        event_type=EventType.BUYBACK,
        subtype=subtype,
        extracted_payload_json=json.dumps(payload),
    )


def _make_forecast(subtype: str = "upward", change_net_pct: float | None = None,
                   change_op_pct: float | None = None, change_ordinary_pct: float | None = None,
                   change_sales_pct: float | None = None, ticker: str = "2345",
                   disclosure_dt: str = "2026-03-20 15:00") -> EventRecord:
    payload = {}
    if change_net_pct is not None:
        payload["change_net_income_pct"] = change_net_pct
    if change_op_pct is not None:
        payload["change_op_pct"] = change_op_pct
    if change_ordinary_pct is not None:
        payload["change_ordinary_pct"] = change_ordinary_pct
    if change_sales_pct is not None:
        payload["change_sales_pct"] = change_sales_pct
    return EventRecord(
        ticker=ticker,
        company_name="テスト社",
        disclosure_datetime=disclosure_dt,
        title="業績予想修正テスト",
        event_type=EventType.FORECAST_REVISION,
        subtype=subtype,
        extracted_payload_json=json.dumps(payload),
    )


def _make_dividend(subtype: str = "increase", prev: float | None = None,
                   rev: float | None = None, ticker: str = "3456",
                   disclosure_dt: str = "2026-03-20 15:00") -> EventRecord:
    payload = {}
    if prev is not None:
        payload["previous_dividend_per_share"] = prev
    if rev is not None:
        payload["revised_dividend_per_share"] = rev
    return EventRecord(
        ticker=ticker,
        company_name="テスト社",
        disclosure_datetime=disclosure_dt,
        title="配当予想修正テスト",
        event_type=EventType.DIVIDEND_REVISION,
        subtype=subtype,
        extracted_payload_json=json.dumps(payload),
    )


# ============================================================
# A. 自社株買い通知条件
# ============================================================
class TestBuybackNotifyRules(unittest.TestCase):

    def test_ratio_3_9_not_notified(self):
        """3.9% → 通知しない"""
        ev = _make_buyback(ratio=3.9)
        self.assertFalse(should_notify_event(ev))

    def test_ratio_4_0_notified(self):
        """4.0% → 通知する"""
        ev = _make_buyback(ratio=4.0)
        self.assertTrue(should_notify_event(ev))

    def test_ratio_8_0_notified(self):
        """8.0% → 通知する"""
        ev = _make_buyback(ratio=8.0)
        self.assertTrue(should_notify_event(ev))

    def test_ratio_none_not_notified(self):
        """ratio None → 通知しない"""
        ev = _make_buyback(ratio=None)
        self.assertFalse(should_notify_event(ev))

    def test_ratio_5_2_notified(self):
        """5.2% → 通知する"""
        ev = _make_buyback(ratio=5.2)
        self.assertTrue(should_notify_event(ev))

    def test_ratio_1_0_not_notified(self):
        """1.0% → 通知しない"""
        ev = _make_buyback(ratio=1.0)
        self.assertFalse(should_notify_event(ev))


# ============================================================
# B. 増配通知条件
# ============================================================
class TestDividendNotifyRules(unittest.TestCase):

    def test_increase_19pct_not_notified(self):
        """100 → 119 (+19%) → 通知しない"""
        ev = _make_dividend(prev=100, rev=119)
        self.assertFalse(should_notify_event(ev))

    def test_increase_20pct_notified(self):
        """100 → 120 (+20%) → 通知する"""
        ev = _make_dividend(prev=100, rev=120)
        self.assertTrue(should_notify_event(ev))

    def test_increase_50pct_notified(self):
        """100 → 150 (+50%) → 通知する"""
        ev = _make_dividend(prev=100, rev=150)
        self.assertTrue(should_notify_event(ev))

    def test_resumption_notified(self):
        """0 → 10 (復配) → 通知する"""
        ev = _make_dividend(prev=0, rev=10)
        self.assertTrue(should_notify_event(ev))
        self.assertTrue(is_resumption_dividend(ev))

    def test_special_dividend_notified(self):
        """特別配当 → 通知する (20%条件関係なし)"""
        ev = _make_dividend(subtype="special_dividend", prev=100, rev=105)
        self.assertTrue(should_notify_event(ev))

    def test_commemorative_dividend_notified(self):
        """記念配当 → 通知する (20%条件関係なし)"""
        ev = _make_dividend(subtype="commemorative_dividend", prev=100, rev=105)
        self.assertTrue(should_notify_event(ev))

    def test_decrease_not_notified(self):
        """減配 → 通知しない"""
        ev = _make_dividend(subtype="decrease", prev=100, rev=80)
        self.assertFalse(should_notify_event(ev))

    def test_maintain_not_notified(self):
        """据え置き → 通知しない"""
        ev = _make_dividend(subtype="maintain", prev=100, rev=100)
        self.assertFalse(should_notify_event(ev))

    def test_no_prev_with_rev_notified(self):
        """prev=None, rev=20 (無配→有配相当) → 通知する"""
        ev = _make_dividend(prev=None, rev=20)
        self.assertTrue(should_notify_event(ev))

    def test_increase_ratio_calc(self):
        """増配率が正しく計算される"""
        ev = _make_dividend(prev=100, rev=130)
        ratio = compute_dividend_increase_ratio(ev)
        self.assertAlmostEqual(ratio, 0.3)

    def test_increase_ratio_zero_prev(self):
        """prev=0 → 増配率は None (特殊ケース)"""
        ev = _make_dividend(prev=0, rev=10)
        ratio = compute_dividend_increase_ratio(ev)
        self.assertIsNone(ratio)

    def test_rev_none_notified_as_simple(self):
        """rev=None (金額未抽出) でも配当修正として簡易通知する"""
        ev = _make_dividend(subtype="increase", prev=100, rev=None)
        self.assertTrue(should_notify_event(ev))

    def test_rev_none_undecided_notified(self):
        """rev=None + subtype=undecided → 簡易通知する"""
        ev = _make_dividend(subtype="undecided", prev=None, rev=None)
        self.assertTrue(should_notify_event(ev))

    def test_rev_none_decrease_not_notified(self):
        """rev=None + subtype=decrease → 通知しない"""
        ev = _make_dividend(subtype="decrease", prev=100, rev=None)
        self.assertFalse(should_notify_event(ev))


# ============================================================
# C. 上方修正通知条件
# ============================================================
class TestForecastNotifyRules(unittest.TestCase):

    def test_upward_notified(self):
        """upward → 通知する"""
        ev = _make_forecast(subtype="upward")
        self.assertTrue(should_notify_event(ev))

    def test_downward_not_notified(self):
        """downward → 通知しない"""
        ev = _make_forecast(subtype="downward")
        self.assertFalse(should_notify_event(ev))

    def test_neutral_not_notified(self):
        """neutral → 通知しない"""
        ev = _make_forecast(subtype="neutral")
        self.assertFalse(should_notify_event(ev))

    def test_undecided_not_notified(self):
        """undecided → 通知しない"""
        ev = _make_forecast(subtype="undecided")
        self.assertFalse(should_notify_event(ev))

    def test_difference_not_notified(self):
        """difference → 通知しない (今回の仕様)"""
        ev = _make_forecast(subtype="difference")
        self.assertFalse(should_notify_event(ev))


# ============================================================
# D. 並び順
# ============================================================
class TestNotificationSortOrder(unittest.TestCase):

    def test_type_priority_order(self):
        """buyback > forecast > dividend の優先順で並ぶ"""
        buyback = _make_buyback(ratio=5.0)
        forecast = _make_forecast(subtype="upward", change_net_pct=30.0)
        dividend = _make_dividend(prev=100, rev=150)

        events = [dividend, forecast, buyback]
        notifiable, _ = filter_and_sort_events(events)

        self.assertEqual(len(notifiable), 3)
        self.assertEqual(notifiable[0].event_type, EventType.BUYBACK)
        self.assertEqual(notifiable[1].event_type, EventType.FORECAST_REVISION)
        self.assertEqual(notifiable[2].event_type, EventType.DIVIDEND_REVISION)

    def test_buyback_ratio_descending(self):
        """buyback は ratio 降順"""
        b1 = _make_buyback(ratio=4.2, ticker="1001")
        b2 = _make_buyback(ratio=10.0, ticker="1002")
        b3 = _make_buyback(ratio=6.0, ticker="1003")

        events = [b1, b2, b3]
        notifiable, _ = filter_and_sort_events(events)

        self.assertEqual(len(notifiable), 3)
        self.assertEqual(notifiable[0].ticker, "1002")  # 10.0%
        self.assertEqual(notifiable[1].ticker, "1003")  # 6.0%
        self.assertEqual(notifiable[2].ticker, "1001")  # 4.2%

    def test_forecast_representative_pct_descending(self):
        """forecast は代表指標降順 (純利益優先)"""
        f1 = _make_forecast(subtype="upward", change_net_pct=20.0, ticker="2001")
        f2 = _make_forecast(subtype="upward", change_net_pct=50.0, ticker="2002")
        f3 = _make_forecast(subtype="upward", change_net_pct=35.0, ticker="2003")

        events = [f1, f2, f3]
        notifiable, _ = filter_and_sort_events(events)

        self.assertEqual(notifiable[0].ticker, "2002")  # 50%
        self.assertEqual(notifiable[1].ticker, "2003")  # 35%
        self.assertEqual(notifiable[2].ticker, "2001")  # 20%

    def test_forecast_priority_order(self):
        """forecast は純利益 > 営業利益 の優先順で代表指標を選択"""
        # f1: 純利益 20% あり → 代表 20%
        f1 = _make_forecast(subtype="upward", change_net_pct=20.0,
                            change_sales_pct=200.0, ticker="2001")
        # f2: 純利益なし、営業利益 100% → 代表 100%
        f2 = _make_forecast(subtype="upward", change_op_pct=100.0,
                            change_sales_pct=300.0, ticker="2002")

        events = [f1, f2]
        notifiable, _ = filter_and_sort_events(events)

        # f2 (代表 100%) > f1 (代表 20%) の順
        self.assertEqual(notifiable[0].ticker, "2002")
        self.assertEqual(notifiable[1].ticker, "2001")

    def test_dividend_category_order(self):
        """dividend: 無配→有配 > 特別配当 > 通常増配"""
        d_resumption = _make_dividend(prev=0, rev=10, ticker="3001")
        d_special = _make_dividend(subtype="special_dividend", prev=100, rev=105, ticker="3002")
        d_normal = _make_dividend(prev=100, rev=150, ticker="3003")

        events = [d_normal, d_special, d_resumption]
        notifiable, _ = filter_and_sort_events(events)

        self.assertEqual(len(notifiable), 3)
        self.assertEqual(notifiable[0].ticker, "3001")  # 復配
        self.assertEqual(notifiable[1].ticker, "3002")  # 特別配当
        self.assertEqual(notifiable[2].ticker, "3003")  # 通常増配 +50%

    def test_dividend_normal_increase_descending(self):
        """通常増配は増配率降順"""
        d1 = _make_dividend(prev=100, rev=120, ticker="3001")  # +20%
        d2 = _make_dividend(prev=50, rev=70, ticker="3002")    # +40%
        d3 = _make_dividend(prev=100, rev=130, ticker="3003")  # +30%

        events = [d1, d2, d3]
        notifiable, _ = filter_and_sort_events(events)

        self.assertEqual(notifiable[0].ticker, "3002")  # +40%
        self.assertEqual(notifiable[1].ticker, "3003")  # +30%
        self.assertEqual(notifiable[2].ticker, "3001")  # +20%

    def test_same_rank_disclosure_datetime_descending(self):
        """同率なら disclosure_datetime の新しい順"""
        b1 = _make_buyback(ratio=5.0, ticker="1001", disclosure_dt="2026-03-20 14:00")
        b2 = _make_buyback(ratio=5.0, ticker="1002", disclosure_dt="2026-03-20 16:00")
        b3 = _make_buyback(ratio=5.0, ticker="1003", disclosure_dt="2026-03-20 15:00")

        events = [b1, b2, b3]
        notifiable, _ = filter_and_sort_events(events)

        self.assertEqual(notifiable[0].ticker, "1002")  # 16:00
        self.assertEqual(notifiable[1].ticker, "1003")  # 15:00
        self.assertEqual(notifiable[2].ticker, "1001")  # 14:00


# ============================================================
# E. フィルタ分離と回帰
# ============================================================
class TestFilterAndSort(unittest.TestCase):

    def test_mixed_events_filtered_correctly(self):
        """通知対象・非対象が正しく分離される"""
        events = [
            _make_buyback(ratio=3.0),           # filtered
            _make_buyback(ratio=5.0),           # notifiable
            _make_forecast(subtype="downward"),  # filtered
            _make_forecast(subtype="upward", change_net_pct=10.0),  # notifiable
            _make_dividend(prev=100, rev=110),  # filtered (+10%)
            _make_dividend(prev=100, rev=130),  # notifiable (+30%)
        ]
        notifiable, filtered = filter_and_sort_events(events)

        self.assertEqual(len(notifiable), 3)
        self.assertEqual(len(filtered), 3)

    def test_all_filtered_returns_empty_notifiable(self):
        """全部非通知の場合、notifiable は空"""
        events = [
            _make_buyback(ratio=1.0),
            _make_forecast(subtype="downward"),
            _make_dividend(prev=100, rev=105),
        ]
        notifiable, filtered = filter_and_sort_events(events)

        self.assertEqual(len(notifiable), 0)
        self.assertEqual(len(filtered), 3)

    def test_empty_events_returns_empty(self):
        notifiable, filtered = filter_and_sort_events([])
        self.assertEqual(len(notifiable), 0)
        self.assertEqual(len(filtered), 0)


# ============================================================
# F. ステータス管理回帰
# ============================================================
class TestStatusManagement(unittest.TestCase):

    def test_filtered_not_marked_notified(self):
        """非通知対象が notified にならない"""
        conn = sqlite3.connect(":memory:")
        ensure_events_table(conn)

        ev = _make_buyback(ratio=2.0)
        upsert_event(conn, ev)

        mark_filtered(conn, ev.event_id)

        row = conn.execute(
            "SELECT status, notified_at FROM events WHERE event_id = ?",
            (ev.event_id,),
        ).fetchone()
        self.assertEqual(row[0], "filtered")
        self.assertIsNone(row[1])  # notified_at は NULL のまま
        conn.close()

    def test_filtered_can_be_renotified(self):
        """filtered ステータスのイベントが再評価で取得可能"""
        conn = sqlite3.connect(":memory:")
        ensure_events_table(conn)

        ev = _make_buyback(ratio=5.0)
        upsert_event(conn, ev)
        mark_filtered(conn, ev.event_id)

        # get_unnotified_events は filtered も取得する
        events = get_unnotified_events(conn)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, ev.event_id)
        conn.close()

    def test_notified_not_overwritten_by_filtered(self):
        """notified 済みが filtered に上書きされない"""
        conn = sqlite3.connect(":memory:")
        ensure_events_table(conn)

        ev = _make_buyback(ratio=5.0)
        upsert_event(conn, ev)
        mark_notified(conn, ev.event_id)

        # filtered に上書きしようとしても notified が維持される
        mark_filtered(conn, ev.event_id)

        row = conn.execute(
            "SELECT status FROM events WHERE event_id = ?",
            (ev.event_id,),
        ).fetchone()
        self.assertEqual(row[0], "notified")
        conn.close()

    def test_fingerprint_duplicate_still_works(self):
        """重複防止が壊れていない"""
        conn = sqlite3.connect(":memory:")
        ensure_events_table(conn)

        ev = _make_buyback(ratio=5.0)
        action1, _ = upsert_event(conn, ev)
        self.assertEqual(action1, "inserted")

        # 同じ fingerprint で再投入
        ev2 = EventRecord(
            ticker=ev.ticker,
            company_name=ev.company_name,
            event_type=ev.event_type,
            subtype=ev.subtype,
            fingerprint=ev.fingerprint,
            extracted_payload_json=ev.extracted_payload_json,
        )
        action2, _ = upsert_event(conn, ev2)
        self.assertIn(action2, ("no_change", "updated"))
        conn.close()


if __name__ == "__main__":
    unittest.main()
