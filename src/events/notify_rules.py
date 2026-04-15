#!/usr/bin/env python3
"""notify_rules.py — イベント通知判定・ソートルール

通知条件:
- buyback: ratio_to_outstanding >= 4.0%
- dividend_revision: 20%以上増配 / 無配→有配 / 特別配当 / 記念配当
- forecast_revision: subtype == 'upward' のみ

通知順:
1. イベント種別優先度: buyback > forecast_revision(upward) > dividend_revision
2. イベント内強度 (降順)
3. disclosure_datetime (新しい順)
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .common_models import EventRecord, EventType

logger = logging.getLogger("notify_rules")

# ============================================================
# ペイロード取得ヘルパー
# ============================================================

def _get_payload(event: EventRecord) -> dict[str, Any]:
    """extracted_payload_json をパースして dict を返す"""
    if not event.extracted_payload_json:
        return {}
    try:
        return json.loads(event.extracted_payload_json)
    except (json.JSONDecodeError, TypeError):
        return {}


# ============================================================
# 通知判定: should_notify_event
# ============================================================

def should_notify_event(event: EventRecord) -> bool:
    """イベントが Discord 通知対象かどうかを判定する。

    DB保存は別途行われている前提。ここでは通知段階のフィルタのみ。
    """
    if event.event_type == EventType.BUYBACK:
        return _should_notify_buyback(event)
    elif event.event_type == EventType.FORECAST_REVISION:
        return _should_notify_forecast(event)
    elif event.event_type == EventType.DIVIDEND_REVISION:
        return _should_notify_dividend(event)
    return False


def _should_notify_buyback(event: EventRecord) -> bool:
    """自社株買い: ratio_to_outstanding >= 4.0% のみ通知"""
    payload = _get_payload(event)
    ratio = payload.get("ratio_to_outstanding")
    if ratio is None:
        return False
    try:
        return float(ratio) >= 4.0
    except (ValueError, TypeError):
        return False


def _should_notify_forecast(event: EventRecord) -> bool:
    """上方修正: subtype == 'upward' のみ通知"""
    return event.subtype == "upward"


def _should_notify_dividend(event: EventRecord) -> bool:
    """増配通知判定:
    - special_dividend / commemorative_dividend → 常に通知
    - 無配→有配 (previous == 0, revised > 0) → 通知
    - 通常増配: previous > 0 かつ increase_ratio >= 0.20 → 通知
    - 金額未抽出 (rev is None): subtype が 'decrease' 以外なら通知
      (配当修正PDFが検知されたが金額抽出できなかった場合の簡易通知)
    - 減配 (decrease) → 非通知
    """
    # 減配は通知しない
    if event.subtype == "decrease":
        return False

    # 特別配当・記念配当は subtype で判定
    if event.subtype in ("special_dividend", "commemorative_dividend"):
        return True

    # increase / その他の subtype は金額ベースで判定
    payload = _get_payload(event)
    prev = payload.get("previous_dividend_per_share")
    rev = payload.get("revised_dividend_per_share")

    if rev is None:
        # 金額未抽出: 配当修正タイトルが検知されたこと自体を通知
        # (decrease は上で除外済み)
        return True

    try:
        prev_f = float(prev) if prev is not None else None
        rev_f = float(rev)
    except (ValueError, TypeError):
        return False

    # 無配→有配
    if (prev_f is None or prev_f == 0) and rev_f > 0:
        return True

    # 通常増配: +20%以上
    if prev_f is not None and prev_f > 0 and rev_f > prev_f:
        increase_ratio = (rev_f - prev_f) / prev_f
        return increase_ratio >= 0.20

    return False


# ============================================================
# 増配率計算ヘルパー (文面表示にも使う)
# ============================================================

def compute_dividend_increase_ratio(event: EventRecord) -> float | None:
    """増配率を計算。計算不可の場合は None を返す。"""
    payload = _get_payload(event)
    prev = payload.get("previous_dividend_per_share")
    rev = payload.get("revised_dividend_per_share")
    if prev is None or rev is None:
        return None
    try:
        prev_f = float(prev)
        rev_f = float(rev)
    except (ValueError, TypeError):
        return None
    if prev_f <= 0:
        return None  # 無配→有配は ratio では表現できない
    return (rev_f - prev_f) / prev_f


def is_resumption_dividend(event: EventRecord) -> bool:
    """無配→有配 (復配) かどうかを判定"""
    payload = _get_payload(event)
    prev = payload.get("previous_dividend_per_share")
    rev = payload.get("revised_dividend_per_share")
    if rev is None:
        return False
    try:
        prev_f = float(prev) if prev is not None else 0.0
        rev_f = float(rev)
    except (ValueError, TypeError):
        return False
    return prev_f == 0.0 and rev_f > 0


# ============================================================
# ソートキー: get_notification_sort_key
# ============================================================

# イベント種別の優先順位 (小さいほど優先)
_EVENT_TYPE_PRIORITY = {
    EventType.BUYBACK: 0,
    EventType.FORECAST_REVISION: 1,
    EventType.DIVIDEND_REVISION: 2,
}

# 配当カテゴリ優先順位 (小さいほど優先)
_DIVIDEND_CATEGORY_RESUMPTION = 0       # 無配→有配
_DIVIDEND_CATEGORY_SPECIAL = 1          # 特別配当/記念配当
_DIVIDEND_CATEGORY_NORMAL_INCREASE = 2  # 通常増配


def get_notification_sort_key(event: EventRecord) -> tuple:
    """通知順ソートキーを返す。

    タプルを自然ソート (昇順) すると仕様どおりの優先順になる。

    Returns:
        (type_priority, strength_key, datetime_key)
    """
    type_priority = _EVENT_TYPE_PRIORITY.get(event.event_type, 99)
    strength_key = _compute_strength_key(event)
    # disclosure_datetime: 新しい順 → 降順にするため負のソート
    # 文字列比較でも ISO形式なら問題ないが、負にできないため逆順文字列を使う
    dt_key = event.disclosure_datetime or ""
    # 新しい順: 文字列の逆ソート用に complement をとる
    # 簡易的に、文字列を反転ソートするため先頭に "-" をつけてタプル比較のtie-breakにする
    # → タプルの最後に文字列を降順で並べるため、reverseの代わりにビット反転相当が必要
    # 実装上は sorted(..., key=...) で呼ぶので、datetime を逆順にする
    # 方法: 大きい文字列ほど先に来るように、全文字を反転する
    dt_sort = "".join(chr(0xFFFF - ord(c)) for c in dt_key) if dt_key else ""

    return (type_priority, strength_key, dt_sort)


def _compute_strength_key(event: EventRecord) -> tuple:
    """イベント内強度キー (タプル、昇順ソート前提で小さいほど強い)"""
    payload = _get_payload(event)

    if event.event_type == EventType.BUYBACK:
        ratio = payload.get("ratio_to_outstanding")
        if ratio is not None:
            try:
                return (-float(ratio),)  # ratio降順
            except (ValueError, TypeError):
                pass
        return (0.0,)

    elif event.event_type == EventType.FORECAST_REVISION:
        # 優先順位付きで代表指標を1つ選ぶ
        representative_pct = _get_forecast_representative_pct(payload)
        return (-representative_pct,)

    elif event.event_type == EventType.DIVIDEND_REVISION:
        return _compute_dividend_strength_key(event, payload)

    return (0.0,)


def _get_forecast_representative_pct(payload: dict) -> float:
    """業績予想修正の代表変化率を取得。

    優先順位:
    1. change_net_income_pct (純利益)
    2. change_op_pct (営業利益)
    3. change_ordinary_pct (経常利益)
    4. change_sales_pct (売上高)

    最初に取得できた指標を代表値として返す。
    """
    for key in [
        "change_net_income_pct",
        "change_op_pct",
        "change_ordinary_pct",
        "change_sales_pct",
    ]:
        val = payload.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                continue
    return 0.0


def _compute_dividend_strength_key(event: EventRecord, payload: dict) -> tuple:
    """配当ソートキー: (カテゴリ優先順位, -増配率)

    カテゴリ優先順位:
    0: 無配→有配 (復配)
    1: 特別配当/記念配当
    2: 通常増配 (率の降順)
    """
    # 無配→有配チェック
    if is_resumption_dividend(event):
        return (_DIVIDEND_CATEGORY_RESUMPTION, 0.0)

    # 特別配当/記念配当
    if event.subtype in ("special_dividend", "commemorative_dividend"):
        return (_DIVIDEND_CATEGORY_SPECIAL, 0.0)

    # 通常増配: 増配率降順
    ratio = compute_dividend_increase_ratio(event)
    neg_ratio = -ratio if ratio is not None else 0.0
    return (_DIVIDEND_CATEGORY_NORMAL_INCREASE, neg_ratio)


# ============================================================
# フィルタ + ソート一括適用
# ============================================================

def filter_and_sort_events(events: list[EventRecord]) -> tuple[list[EventRecord], list[EventRecord]]:
    """イベントリストを通知判定しフィルタ・ソートする。

    Returns:
        (notifiable, filtered) - 通知対象リスト(ソート済み), 非通知リスト
    """
    notifiable = []
    filtered = []
    for ev in events:
        if should_notify_event(ev):
            notifiable.append(ev)
        else:
            filtered.append(ev)

    notifiable.sort(key=get_notification_sort_key)
    return notifiable, filtered
