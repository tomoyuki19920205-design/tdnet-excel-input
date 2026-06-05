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
    """自社株買い通知判定（最終防衛線）

    [D-0] subtype ガード: new_program または tostnet のみ通知。
          それ以外（resolution/status/result/cancellation 等の旧 subtype）は
          classify_buyback_subtype() で "ignore" になったものが到達しないはずだが
          万一到達しても遮断する。

    [D-1] extraction_confidence >= 0.50

    [D-2] new_program:
          株数上限 / 金額上限 / 比率 / 取得期間 のうち 2項目以上取れていること
    [D-3] tostnet:
          (株数上限 or 金額上限) かつ 買付日(start_date) が取れていること
    """
    payload = _get_payload(event)

    # [D-0] subtype ガード
    subtype = event.subtype or ""
    if subtype not in ("new_program", "tostnet"):
        logger.debug(
            f"[NOTIFY] buyback subtype-guard blocked: "
            f"subtype={subtype!r} ticker={event.ticker}"
        )
        return False

    # [D-1] confidence ガード
    conf = payload.get("extraction_confidence")
    try:
        if conf is None or float(conf) < 0.50:
            return False
    except (ValueError, TypeError):
        return False

    # [D-1.5] 発行済株式数比 >= 4.0% ガード (new_program / tostnet 共通)
    #   ratio_to_outstanding: 既存 BuybackEvent フィールド（% 単位）
    #   None の場合も通知しない（PDF未記載 & 補完なし）
    _ratio = payload.get("ratio_to_outstanding")
    try:
        _ratio_f = float(_ratio) if _ratio is not None else None
    except (ValueError, TypeError):
        _ratio_f = None
    if _ratio_f is None or _ratio_f < 4.0:
        logger.debug(
            f"[NOTIFY] buyback ratio-guard blocked: "
            f"ratio={_ratio_f} subtype={subtype} ticker={event.ticker}"
        )
        return False

    if subtype == "new_program":
        # [D-2] new_program: 株数上限/金額上限/比率/期間 のうち 2項目以上
        _NP_FIELDS = (
            "shares_limit",
            "amount_limit_million_yen",
            "ratio_to_outstanding",
            "start_date",
            "end_date",
        )
        filled = sum(1 for f in _NP_FIELDS if payload.get(f) is not None)
        if filled < 2:
            logger.debug(
                f"[NOTIFY] new_program field-guard blocked: "
                f"filled={filled} ticker={event.ticker}"
            )
            return False
        return True

    elif subtype == "tostnet":
        # [D-3] tostnet: (株数 or 金額) + 買付日
        has_amount = (
            payload.get("shares_limit") is not None
            or payload.get("amount_limit_million_yen") is not None
        )
        has_date = payload.get("start_date") is not None
        if not (has_amount and has_date):
            logger.debug(
                f"[NOTIFY] tostnet field-guard blocked: "
                f"has_amount={has_amount} has_date={has_date} ticker={event.ticker}"
            )
            return False
        return True

    return False



def _should_notify_forecast(event: EventRecord) -> bool:
    """上方修正・差異通知フィルタ

    - upward: 無条件で True
    - difference: 営業利益差異率 (change_op_pct) の絶対値 >= 20.0% のみ True
                  change_op_pct が欠損の場合は安全側で True（数値未抽出で重要案件を落とさない）
    - downward / neutral / undecided: False
    """
    subtype = event.subtype or ""
    if subtype == "upward":
        return True
    if subtype == "difference":
        payload = _get_payload(event)
        op_pct = payload.get("change_op_pct")
        if op_pct is None:
            # 欠損時は安全側で通知する
            logger.debug(
                f"[NOTIFY] forecast difference: change_op_pct missing → notify True "
                f"ticker={event.ticker}"
            )
            return True
        try:
            if abs(float(op_pct)) >= 20.0:
                return True
            logger.debug(
                f"[NOTIFY] forecast difference: abs(change_op_pct)={abs(float(op_pct)):.1f}% < 20% → skip "
                f"ticker={event.ticker}"
            )
            return False
        except (ValueError, TypeError):
            return True  # 変換失敗も安全側
    return False


def _should_notify_dividend(event: EventRecord) -> bool:
    """増配通知判定（厳格版）:

    通知条件: 以下をすべて満たす場合のみ True
      1. prev_dividend が正の数値 (>0)
      2. revised_dividend が正の数値 (>0)
      3. revised_dividend > prev_dividend
      4. 増加率 >= 20.0%

    非通知:
      - 減配 / 据え置き
      - prev=None / rev=None / "---" / 空文字 / 0
      - 無配→有配 (prev=0 → rev>0): 今回は通知しない。将来の別イベントで対応。
      - 未定→有配: 同上
      - +20%未満の小幅増配

    将来拡張:
      - special_dividend / commemorative_dividend: 今回は数値なしブロックと同条件
    """
    payload = _get_payload(event)
    prev = payload.get("previous_dividend_per_share")
    rev  = payload.get("revised_dividend_per_share")

    # [G1] prev / rev が None / 空 / "---" → ブロック
    _INVALID = (None, "", "---")
    if prev in _INVALID or rev in _INVALID:
        logger.debug(
            "[NOTIFY] dividend null-guard: prev=%r rev=%r subtype=%s ticker=%s",
            prev, rev, event.subtype, event.ticker,
        )
        return False

    # [G2] float 変換
    try:
        prev_f = float(prev)
        rev_f  = float(rev)
    except (ValueError, TypeError):
        return False

    # [G3] prev が 0 以下 → 無配・未定は今回は通知対象外
    if prev_f <= 0:
        logger.debug(
            "[NOTIFY] dividend prev<=0 blocked: prev=%s ticker=%s",
            prev_f, event.ticker,
        )
        return False

    # [G4] rev が 0 以下
    if rev_f <= 0:
        return False

    # [G5] rev <= prev → 据え置き / 減配
    if rev_f <= prev_f:
        return False

    # [G6] 増加率 >= 20.0%
    increase_pct = (rev_f - prev_f) / prev_f * 100
    if increase_pct < 20.0:
        logger.debug(
            "[NOTIFY] dividend pct-guard: pct=%.1f%% ticker=%s",
            increase_pct, event.ticker,
        )
        return False

    return True


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
