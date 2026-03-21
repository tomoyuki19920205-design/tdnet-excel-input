#!/usr/bin/env python3
"""common_notify.py — イベント検知 Discord 通知"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import requests

from .common_models import EventRecord, EventType
from .notify_rules import is_resumption_dividend, compute_dividend_increase_ratio

logger = logging.getLogger("event_notify")

_MAX_DISCORD_LEN = 1950  # 2000文字制限に余裕
_TAIL = "\n\u200b"  # Discord末尾空行保持用


def _truncate(text: str, max_len: int = _MAX_DISCORD_LEN) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


# ============================================================
# フォーマッタ
# ============================================================

def _fmt_amount_billion(val_million: float | None) -> str:
    """百万円 → 表示用（億円 or 百万円）"""
    if val_million is None:
        return "---"
    if abs(val_million) >= 100:
        return f"{val_million / 100:.1f}億円"
    return f"{val_million:.0f}百万円"


def _fmt_shares(val: int | None) -> str:
    """株数 → 表示用（万株 or 株）"""
    if val is None:
        return "---"
    if val >= 10_000:
        return f"{val / 10_000:.1f}万株"
    return f"{val:,}株"


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return "---"
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.1f}%"


def format_buyback_msg(event: EventRecord) -> str:
    """自社株買いイベントの通知メッセージ（1行コンパクト形式）"""
    payload = {}
    if event.extracted_payload_json:
        try:
            payload = json.loads(event.extracted_payload_json)
        except (json.JSONDecodeError, TypeError):
            pass

    disp = f"{event.company_name}（{event.ticker}）" if event.company_name else event.ticker
    subtype_ja = {
        "resolution": "決議", "completion": "完了", "result": "結果",
        "cancellation": "消却", "status": "状況",
    }.get(event.subtype, event.subtype)

    ratio = payload.get("ratio_to_outstanding")
    fire = ""
    if ratio is not None and ratio >= 8.0:
        fire = "🔥🔥 "
    elif ratio is not None and ratio >= 4.0:
        fire = "🔥 "

    # 1行: ヘッダー + 発行済% + 金額 + 期間
    parts = [f"{fire}📊【自社株買い - {subtype_ja}】{disp}"]

    if ratio is not None:
        parts.append(f"発行済{ratio:.1f}%")
    amount_limit = payload.get("amount_limit_million_yen")
    if amount_limit:
        parts.append(f"{_fmt_amount_billion(amount_limit)}")
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")
    if start_date and end_date:
        parts.append(f"{start_date}~{end_date}")

    return _truncate("\u3000\u3000".join(parts)) + _TAIL


def format_forecast_msg(event: EventRecord) -> str:
    """業績予想修正イベントの通知メッセージ（1行コンパクト形式）"""
    payload = {}
    if event.extracted_payload_json:
        try:
            payload = json.loads(event.extracted_payload_json)
        except (json.JSONDecodeError, TypeError):
            pass

    disp = f"{event.company_name}（{event.ticker}）" if event.company_name else event.ticker
    subtype_ja = {
        "upward": "上方修正", "downward": "下方修正",
        "difference": "差異開示", "neutral": "予想修正", "undecided": "予想修正",
    }.get(event.subtype, "予想修正")
    emoji = {"upward": "🔺", "downward": "🔻", "difference": "📋"}.get(event.subtype, "📝")

    # 1行: ヘッダー + 指標 + 対象期(末尾)
    period = payload.get("period_label", "")
    parts = [f"{emoji}【{subtype_ja}】{disp}"]

    count = 0
    for label, key_prev, key_rev, key_pct in [
        ("純利益", "previous_net_income", "revised_net_income", "change_net_income_pct"),
        ("営業利益", "previous_op", "revised_op", "change_op_pct"),
        ("経常利益", "previous_ordinary", "revised_ordinary", "change_ordinary_pct"),
        ("売上高", "previous_sales", "revised_sales", "change_sales_pct"),
    ]:
        prev = payload.get(key_prev)
        rev = payload.get(key_rev)
        pct = payload.get(key_pct)
        if rev is not None and prev is not None:
            parts.append(f"{label}: {_fmt_amount_billion(prev)}→{_fmt_amount_billion(rev)}({_fmt_pct(pct)})")
            count += 1
        elif rev is not None:
            parts.append(f"{label}: {_fmt_amount_billion(rev)}")
            count += 1
        if count >= 2:
            break

    if period:
        parts.append(period)

    return _truncate("\u3000\u3000".join(parts)) + _TAIL


def format_dividend_msg(event: EventRecord) -> str:
    """配当修正イベントの通知メッセージ（1行コンパクト形式）"""
    payload = {}
    if event.extracted_payload_json:
        try:
            payload = json.loads(event.extracted_payload_json)
        except (json.JSONDecodeError, TypeError):
            pass

    disp = f"{event.company_name}（{event.ticker}）" if event.company_name else event.ticker

    # 復配判定
    if is_resumption_dividend(event):
        label = "復配"
        emoji = "💰"
    elif event.subtype == "special_dividend":
        label = "特別配当"
        emoji = "🎁"
    elif event.subtype == "commemorative_dividend":
        label = "記念配当"
        emoji = "🎉"
    elif event.subtype == "increase":
        label = "増配"
        emoji = "💰"
    elif event.subtype == "decrease":
        label = "減配"
        emoji = "📉"
    else:
        label = {"maintain": "据え置き", "undecided": "配当予想修正"}.get(event.subtype, "配当予想修正")
        emoji = "💵"

    # 1行: ヘッダー + 配当額 + 増配率 + 利回り + 対象期(末尾)
    period = payload.get("fiscal_period", "")
    basis = payload.get("dividend_basis", "")
    parts = [f"{emoji}【{label}】{disp}"]

    prev = payload.get("previous_dividend_per_share")
    rev = payload.get("revised_dividend_per_share")
    if prev is not None and rev is not None:
        inc_ratio = compute_dividend_increase_ratio(event)
        if inc_ratio is not None:
            pct_str = f"(+{inc_ratio * 100:.1f}%)"
        else:
            pct_str = ""
        parts.append(f"{prev}円→{rev}円{pct_str}")
    elif rev is not None:
        parts.append(f"→{rev}円")

    special = payload.get("special_dividend_per_share")
    if special:
        parts.append(f"特別配当{special}円")
    commemorative = payload.get("commemorative_dividend_per_share")
    if commemorative:
        parts.append(f"記念配当{commemorative}円")

    closing_price = payload.get("closing_price")
    annual_div = payload.get("annual_total_revised")
    if closing_price and annual_div and closing_price > 0:
        div_yield = annual_div / closing_price * 100
        parts.append(f"利回り{div_yield:.2f}%（終値{closing_price:,.0f}円）")

    if period:
        parts.append(f"{period} {basis}".rstrip())

    return _truncate("\u3000\u3000".join(parts)) + _TAIL


# ============================================================
# 送信
# ============================================================
_FORMATTERS = {
    EventType.BUYBACK: format_buyback_msg,
    EventType.FORECAST_REVISION: format_forecast_msg,
    EventType.DIVIDEND_REVISION: format_dividend_msg,
}


def format_event_message(event: EventRecord) -> str:
    """イベント種別に応じた通知メッセージを生成"""
    formatter = _FORMATTERS.get(event.event_type)
    if formatter:
        return formatter(event)
    return f"【{event.event_type}】{event.ticker} {event.title[:80]}"


def send_event_discord(
    webhook_url: str,
    event: EventRecord,
    dry_run: bool = False,
) -> bool:
    """Discord にイベント通知を送信する。

    Returns
    -------
    bool
        送信成功 or dry-run
    """
    msg = format_event_message(event)

    if dry_run:
        logger.info(f"[DRY-RUN] Discord通知:\n{msg}")
        print(f"[DRY-RUN] Discord通知:\n{msg}")
        return True

    try:
        r = requests.post(webhook_url, json={"content": msg}, timeout=10)
        r.raise_for_status()
        logger.info(
            f"[NOTIFY] sent event_id={event.event_id[:12]} "
            f"type={event.event_type} ticker={event.ticker}"
        )
        return True
    except Exception as e:
        logger.error(f"[NOTIFY] Discord send failed: {e}")
        return False
