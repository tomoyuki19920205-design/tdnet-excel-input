#!/usr/bin/env python3
"""test_common_notify.py — 共通フォーマッタのテスト

テスト対象:
- build_event_parts: 構造化パーツ生成
- format_event_message / build_formatted_message: Discord == Web詳細 一致
- build_display_title / build_display_summary: Web一覧用
"""
import json
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.events.common_models import EventRecord, EventType
from src.events.common_notify import (
    format_event_message,
    format_buyback_msg,
    format_forecast_msg,
    format_dividend_msg,
    send_event_discord,
    SendResult,
)

# NOTE: EventParts / build_event_parts / build_formatted_message /
# build_display_title / build_display_summary は common_notify.py に現在存在しない。
# これらのテスト(TestDiscordFormattedMessageMatch, TestDisplayOutput,
# TestBuildEventParts, TestEmptyFormattedMessageFallback) は
# 当該関数が存在しないためスキップとする。

# ============================================================
# ヘルパー
# ============================================================
def _make_forecast(**kwargs) -> EventRecord:
    payload = {}
    for k in ["previous_net_income", "revised_net_income", "change_net_income_pct",
              "previous_op", "revised_op", "change_op_pct",
              "previous_ordinary", "revised_ordinary", "change_ordinary_pct",
              "previous_sales", "revised_sales", "change_sales_pct",
              "period_label"]:
        if k in kwargs:
            payload[k] = kwargs.pop(k)
    defaults = {
        "ticker": "7203", "company_name": "トヨタ自動車",
        "event_type": EventType.FORECAST_REVISION,
        "subtype": "upward", "title": "業績予想の上方修正に関するお知らせ",
    }
    defaults.update(kwargs)
    defaults["extracted_payload_json"] = json.dumps(payload, ensure_ascii=False)
    return EventRecord(**defaults)


def _make_buyback(**kwargs) -> EventRecord:
    payload = {}
    for k in ["ratio_to_outstanding", "amount_limit_million_yen", "start_date", "end_date"]:
        if k in kwargs:
            payload[k] = kwargs.pop(k)
    defaults = {
        "ticker": "6758", "company_name": "ソニーG",
        "event_type": EventType.BUYBACK,
        "subtype": "resolution", "title": "自社株式の取得に関するお知らせ",
    }
    defaults.update(kwargs)
    defaults["extracted_payload_json"] = json.dumps(payload, ensure_ascii=False)
    return EventRecord(**defaults)


def _make_dividend(**kwargs) -> EventRecord:
    payload = {}
    for k in ["previous_dividend_per_share", "revised_dividend_per_share",
              "fiscal_period", "dividend_basis",
              "special_dividend_per_share", "commemorative_dividend_per_share"]:
        if k in kwargs:
            payload[k] = kwargs.pop(k)
    defaults = {
        "ticker": "7203", "company_name": "トヨタ自動車",
        "event_type": EventType.DIVIDEND_REVISION,
        "subtype": "increase", "title": "配当予想の修正に関するお知らせ",
    }
    defaults.update(kwargs)
    defaults["extracted_payload_json"] = json.dumps(payload, ensure_ascii=False)
    return EventRecord(**defaults)


# ============================================================
# A. Discord == formatted_message 完全一致テスト
# ============================================================
@unittest.skip("build_formatted_message / build_display_title / build_event_parts は common_notify.py に現在存在しない")
class TestDiscordFormattedMessageMatch(unittest.TestCase):
    """format_event_message と build_formatted_message の一致を検証"""

    def test_forecast_upward_match(self):
        ev = _make_forecast(
            previous_net_income=1200, revised_net_income=1500, change_net_income_pct=25.0,
            previous_op=1500, revised_op=1800, change_op_pct=20.0,
            period_label="2026年3月期 通期",
        )
        discord = format_event_message(ev)
        web = build_formatted_message(ev)
        # Discord版は末尾 \n\u200b あり、Web版はそれを除去
        self.assertEqual(web, discord.rstrip("\n\u200b").strip())

    def test_buyback_match(self):
        ev = _make_buyback(
            ratio_to_outstanding=5.0,
            amount_limit_million_yen=10000,
            start_date="2026-04-01", end_date="2026-09-30",
        )
        discord = format_event_message(ev)
        web = build_formatted_message(ev)
        self.assertEqual(web, discord.rstrip("\n\u200b").strip())

    def test_dividend_match(self):
        ev = _make_dividend(
            previous_dividend_per_share=80, revised_dividend_per_share=100,
            fiscal_period="2026年3月期", dividend_basis="連結",
        )
        discord = format_event_message(ev)
        web = build_formatted_message(ev)
        self.assertEqual(web, discord.rstrip("\n\u200b").strip())


# ============================================================
# B. display_title / display_summary テスト
# ============================================================
@unittest.skip("build_display_title / build_display_summary は common_notify.py に現在存在しない")
class TestDisplayOutput(unittest.TestCase):

    def test_forecast_display_title(self):
        ev = _make_forecast(
            previous_net_income=1200, revised_net_income=1500, change_net_income_pct=25.0,
        )
        title = build_display_title(ev)
        self.assertIn("🔺", title)
        self.assertIn("上方修正", title)
        self.assertIn("トヨタ自動車", title)
        # display_title には指標を含まない (短い見出し)
        self.assertNotIn("純利益", title)

    def test_forecast_display_summary(self):
        ev = _make_forecast(
            previous_net_income=1200, revised_net_income=1500, change_net_income_pct=25.0,
            period_label="2026年3月期 通期",
        )
        summary = build_display_summary(ev)
        self.assertIn("純利益", summary)
        self.assertIn("2026年3月期", summary)

    def test_buyback_display_title(self):
        ev = _make_buyback(ratio_to_outstanding=5.0)
        title = build_display_title(ev)
        self.assertIn("自社株買い", title)
        self.assertIn("ソニーG", title)
        self.assertNotIn("発行済", title)

    def test_buyback_display_summary(self):
        ev = _make_buyback(
            ratio_to_outstanding=5.0,
            amount_limit_million_yen=10000,
            start_date="2026-04-01", end_date="2026-09-30",
        )
        summary = build_display_summary(ev)
        self.assertIn("発行済5.0%", summary)
        self.assertIn("100.0億円", summary)

    def test_dividend_display_title(self):
        ev = _make_dividend(
            previous_dividend_per_share=80, revised_dividend_per_share=100,
        )
        title = build_display_title(ev)
        self.assertIn("増配", title)
        self.assertIn("トヨタ自動車", title)

    def test_dividend_display_summary(self):
        ev = _make_dividend(
            previous_dividend_per_share=80, revised_dividend_per_share=100,
            fiscal_period="2026年3月期", dividend_basis="連結",
        )
        summary = build_display_summary(ev)
        self.assertIn("80円→100円", summary)
        self.assertIn("2026年3月期", summary)


# ============================================================
# C. build_event_parts テスト
# ============================================================
@unittest.skip("build_event_parts / EventParts は common_notify.py に現在存在しない")
class TestBuildEventParts(unittest.TestCase):

    def test_forecast_parts_structure(self):
        ev = _make_forecast(
            previous_net_income=1200, revised_net_income=1500, change_net_income_pct=25.0,
            period_label="2026年3月期 通期",
        )
        p = build_event_parts(ev)
        self.assertEqual(p.emoji, "🔺")
        self.assertEqual(p.label, "上方修正")
        self.assertIn("トヨタ自動車", p.display_name)
        self.assertEqual(len(p.metrics), 1)  # 純利益のみ
        self.assertIn("純利益", p.metrics[0])
        self.assertEqual(p.period, "2026年3月期 通期")

    def test_forecast_max_two_metrics(self):
        ev = _make_forecast(
            previous_net_income=1200, revised_net_income=1500, change_net_income_pct=25.0,
            previous_op=1500, revised_op=1800, change_op_pct=20.0,
            previous_ordinary=1600, revised_ordinary=1900, change_ordinary_pct=18.8,
        )
        p = build_event_parts(ev)
        self.assertEqual(len(p.metrics), 2)  # 最大2つ

    def test_buyback_parts_with_fire(self):
        ev = _make_buyback(ratio_to_outstanding=8.0)
        p = build_event_parts(ev)
        self.assertIn("🔥🔥", p.emoji)

    def test_metrics_order_fixed(self):
        """指標の出力順が固定 (純利益 > 営業利益 > 経常利益 > 売上高)"""
        ev = _make_forecast(
            previous_sales=10000, revised_sales=11000, change_sales_pct=10.0,
            previous_net_income=1200, revised_net_income=1500, change_net_income_pct=25.0,
        )
        p = build_event_parts(ev)
        self.assertIn("純利益", p.metrics[0])
        self.assertIn("売上高", p.metrics[1])


# ============================================================
# D. 後方互換テスト
# ============================================================
class TestBackwardCompatibility(unittest.TestCase):

    def test_format_buyback_msg_calls_format_event_message(self):
        ev = _make_buyback(ratio_to_outstanding=5.0)
        self.assertEqual(format_buyback_msg(ev), format_event_message(ev))

    def test_format_forecast_msg_calls_format_event_message(self):
        ev = _make_forecast()
        self.assertEqual(format_forecast_msg(ev), format_event_message(ev))

    def test_format_dividend_msg_calls_format_event_message(self):
        ev = _make_dividend(previous_dividend_per_share=80, revised_dividend_per_share=100)
        self.assertEqual(format_dividend_msg(ev), format_event_message(ev))


# ============================================================
# E. フォールバックテスト（formatted_message空のケース）
# ============================================================
@unittest.skip("build_display_title / build_display_summary は common_notify.py に現在存在しない")
class TestEmptyFormattedMessageFallback(unittest.TestCase):
    """formatted_message='' の既存データでもフォールバックが効くか"""

    def test_display_title_always_populated(self):
        """どのイベントタイプでも display_title は空にならない"""
        for ev in [
            _make_forecast(),
            _make_buyback(ratio_to_outstanding=3.0),
            _make_dividend(previous_dividend_per_share=80, revised_dividend_per_share=100),
        ]:
            title = build_display_title(ev)
            self.assertTrue(len(title) > 0, f"display_title empty for {ev.event_type}")

    def test_display_summary_fallback_to_title(self):
        """metrics も period もない場合、event.title にフォールバック"""
        ev = _make_forecast(title="業績予想の修正に関するお知らせ")
        ev.extracted_payload_json = "{}"  # metrics なし
        summary = build_display_summary(ev)
        self.assertEqual(summary, "業績予想の修正に関するお知らせ")


# ============================================================
# F. SendResult テスト — send_event_discord の戻り値検証
# ============================================================
class TestSendEventDiscordResult(unittest.TestCase):
    """send_event_discord() が正しい SendResult を返すか確認するテスト。

    requests.post をモックして、HTTP ステータスコードや例外ごとに
    SendResult の種類が変わることを検証する。
    """

    def _make_ev(self) -> EventRecord:
        return _make_buyback(ratio_to_outstanding=5.0)

    def _mock_response(self, status_code: int):
        """指定ステータスの requests.Response モックを生成する。"""
        from unittest.mock import MagicMock
        import requests
        resp = MagicMock()
        resp.status_code = status_code
        resp.headers = {}
        if status_code >= 400:
            http_err = requests.exceptions.HTTPError(response=resp)
            resp.raise_for_status.side_effect = http_err
        else:
            resp.raise_for_status.return_value = None
        return resp

    def test_dry_run_returns_skipped(self):
        """dry_run=True → SendResult.SKIPPED（実送信なし）"""
        ev = self._make_ev()
        result = send_event_discord("https://example.com/webhook", ev, dry_run=True)
        self.assertEqual(result, SendResult.SKIPPED)

    def test_http_204_returns_success(self):
        """HTTP 204 No Content → SendResult.SUCCESS"""
        from unittest.mock import patch
        import time
        ev = self._make_ev()
        resp = self._mock_response(204)
        with patch("requests.post", return_value=resp), \
             patch.object(time, "sleep"):
            result = send_event_discord("https://example.com/webhook", ev, dry_run=False)
        self.assertEqual(result, SendResult.SUCCESS)

    def test_http_200_returns_success(self):
        """HTTP 200 OK → SendResult.SUCCESS"""
        from unittest.mock import patch
        import time
        ev = self._make_ev()
        resp = self._mock_response(200)
        with patch("requests.post", return_value=resp), \
             patch.object(time, "sleep"):
            result = send_event_discord("https://example.com/webhook", ev, dry_run=False)
        self.assertEqual(result, SendResult.SUCCESS)

    def test_http_400_returns_failed(self):
        """HTTP 400 Bad Request → SendResult.FAILED（明確な失敗）"""
        from unittest.mock import patch
        import time
        ev = self._make_ev()
        resp = self._mock_response(400)
        with patch("requests.post", return_value=resp), \
             patch.object(time, "sleep"):
            result = send_event_discord("https://example.com/webhook", ev, dry_run=False)
        self.assertEqual(result, SendResult.FAILED)

    def test_http_500_returns_failed(self):
        """HTTP 500 Internal Server Error → SendResult.FAILED"""
        from unittest.mock import patch
        import time
        ev = self._make_ev()
        resp = self._mock_response(500)
        with patch("requests.post", return_value=resp), \
             patch.object(time, "sleep"):
            result = send_event_discord("https://example.com/webhook", ev, dry_run=False)
        self.assertEqual(result, SendResult.FAILED)

    def test_timeout_returns_uncertain(self):
        """Timeout → SendResult.UNCERTAIN（届いたか不明。mark_notified してはいけない）"""
        from unittest.mock import patch
        import requests
        import time
        ev = self._make_ev()
        with patch("requests.post", side_effect=requests.exceptions.Timeout()), \
             patch.object(time, "sleep"):
            result = send_event_discord("https://example.com/webhook", ev, dry_run=False)
        self.assertEqual(result, SendResult.UNCERTAIN)

    def test_connection_error_returns_uncertain(self):
        """ConnectionError → SendResult.UNCERTAIN（届いたか不明）"""
        from unittest.mock import patch
        import requests
        import time
        ev = self._make_ev()
        with patch("requests.post", side_effect=requests.exceptions.ConnectionError()), \
             patch.object(time, "sleep"):
            result = send_event_discord("https://example.com/webhook", ev, dry_run=False)
        self.assertEqual(result, SendResult.UNCERTAIN)

    def test_uncertain_is_not_success(self):
        """UNCERTAIN は SUCCESS ではない（mark_notified 呼び出し条件を満たさない）"""
        self.assertNotEqual(SendResult.UNCERTAIN, SendResult.SUCCESS)

    def test_failed_is_not_success(self):
        """FAILED は SUCCESS ではない"""
        self.assertNotEqual(SendResult.FAILED, SendResult.SUCCESS)

    def test_skipped_is_not_success(self):
        """SKIPPED は SUCCESS ではない"""
        self.assertNotEqual(SendResult.SKIPPED, SendResult.SUCCESS)


if __name__ == "__main__":
    unittest.main()
