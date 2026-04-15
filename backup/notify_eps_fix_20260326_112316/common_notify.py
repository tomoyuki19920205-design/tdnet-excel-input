#!/usr/bin/env python3
"""common_notify.py — イベント検知 通知・表示フォーマット共通基盤

全フォーマット (Discord / Web一覧 / Web詳細) は
build_event_parts() を唯一の元ネタとする。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import requests

from .common_models import EventRecord, EventType
from .notify_rules import is_resumption_dividend, compute_dividend_increase_ratio

logger = logging.getLogger("event_notify")

_MAX_DISCORD_LEN = 1950  # 2000文字制限に余裕
_TAIL = "\n\u200b"  # Discord末尾空行保持用
_SEP = "\u3000\u3000"  # 全角スペース2個 (Discord メッセージ区切り)


def _truncate(text: str, max_len: int = _MAX_DISCORD_LEN) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


# ============================================================
# 共通パーツ構造
# ============================================================
@dataclass
class EventParts:
    """イベント表示に必要な構造化パーツ。全フォーマットの唯一の元ネタ。"""
    emoji: str = "📝"
    label: str = ""               # "上方修正", "自社株買い - 決議", "増配"
    display_name: str = ""        # "トヨタ自動車（7203）"
    metrics: list[str] = field(default_factory=list)  # ["純利益: 12.0億円→15.0億円(+25.0%)", ...]
    period: str = ""              # "2026年3月期 通期"
    extra: list[str] = field(default_factory=list)     # ["特別配当5円", ...]


# ============================================================
# 表示ヘルパー
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


# ============================================================
# build_event_parts — 唯一の元ネタ生成
# ============================================================
def _get_payload(event: EventRecord) -> dict:
    if not event.extracted_payload_json:
        return {}
    try:
        return json.loads(event.extracted_payload_json)
    except (json.JSONDecodeError, TypeError):
        return {}


def build_event_parts(event: EventRecord) -> EventParts:
    """イベントから構造化パーツを生成する（全フォーマットの唯一の元ネタ）。"""
    payload = _get_payload(event)
    disp = f"{event.company_name}（{event.ticker}）" if event.company_name else event.ticker
    parts = EventParts(display_name=disp)

    if event.event_type == EventType.BUYBACK:
        _build_buyback_parts(parts, event, payload)
    elif event.event_type == EventType.FORECAST_REVISION:
        _build_forecast_parts(parts, event, payload)
    elif event.event_type == EventType.DIVIDEND_REVISION:
        _build_dividend_parts(parts, event, payload)
    else:
        parts.emoji = "📄"
        parts.label = event.event_type or "イベント"

    return parts


def _build_buyback_parts(parts: EventParts, event: EventRecord, payload: dict) -> None:
    """自社株買いのパーツ生成"""
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

    parts.emoji = f"{fire}📊"
    parts.label = f"自社株買い - {subtype_ja}"

    # metrics: 発行済%, 金額, 期間 (固定順)
    if ratio is not None:
        parts.metrics.append(f"発行済{ratio:.1f}%")
    amount_limit = payload.get("amount_limit_million_yen")
    if amount_limit:
        parts.metrics.append(f"{_fmt_amount_billion(amount_limit)}")
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")
    if start_date and end_date:
        parts.period = f"{start_date}~{end_date}"


def _build_forecast_parts(parts: EventParts, event: EventRecord, payload: dict) -> None:
    """業績予想修正のパーツ生成"""
    subtype_ja = {
        "upward": "上方修正", "downward": "下方修正",
        "difference": "差異開示", "neutral": "予想修正", "undecided": "予想修正",
    }.get(event.subtype, "予想修正")
    emoji = {"upward": "🔺", "downward": "🔻", "difference": "📋"}.get(event.subtype, "📝")

    parts.emoji = emoji
    parts.label = subtype_ja

    # metrics: 純利益, 営業利益, 経常利益, 売上高 (固定順、最大2つ)
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
            parts.metrics.append(
                f"{label}: {_fmt_amount_billion(prev)}→{_fmt_amount_billion(rev)}({_fmt_pct(pct)})"
            )
            count += 1
        elif rev is not None:
            parts.metrics.append(f"{label}: {_fmt_amount_billion(rev)}")
            count += 1
        if count >= 2:
            break

    # EPS 表示（負値・0.0 でも表示する）
    eps_prev = payload.get("previous_eps")
    eps_rev = payload.get("revised_eps")
    eps_line = ""
    if eps_prev is not None and eps_rev is not None:
        if eps_prev != 0:
            change = (eps_rev / eps_prev - 1) * 100
            eps_line = f"EPS: {eps_prev}円→{eps_rev}円({change:+.1f}%)"
        else:
            eps_line = f"EPS: {eps_prev}円→{eps_rev}円"
        parts.metrics.append(eps_line)
    logger.info(
        f"[forecast_notify] eps_prev={eps_prev} eps_rev={eps_rev} eps_line={eps_line!r}"
    )

    parts.period = payload.get("period_label", "")


def _build_dividend_parts(parts: EventParts, event: EventRecord, payload: dict) -> None:
    """配当修正のパーツ生成"""
    # ラベル・emoji 判定
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

    parts.emoji = emoji
    parts.label = label

    # metrics: 配当額, 特別配当, 記念配当, 利回り (固定順)
    prev = payload.get("previous_dividend_per_share")
    rev = payload.get("revised_dividend_per_share")
    if prev is not None and rev is not None:
        inc_ratio = compute_dividend_increase_ratio(event)
        pct_str = f"(+{inc_ratio * 100:.1f}%)" if inc_ratio is not None else ""
        parts.metrics.append(f"{prev}円→{rev}円{pct_str}")
    elif rev is not None:
        parts.metrics.append(f"→{rev}円")

    special = payload.get("special_dividend_per_share")
    if special:
        parts.extra.append(f"特別配当{special}円")
    commemorative = payload.get("commemorative_dividend_per_share")
    if commemorative:
        parts.extra.append(f"記念配当{commemorative}円")

    closing_price = payload.get("closing_price")
    annual_div = payload.get("annual_total_revised")
    if closing_price and annual_div and closing_price > 0:
        div_yield = annual_div / closing_price * 100
        parts.extra.append(f"利回り{div_yield:.2f}%（終値{closing_price:,.0f}円）")

    period = payload.get("fiscal_period", "")
    basis = payload.get("dividend_basis", "")
    parts.period = f"{period} {basis}".rstrip() if period else ""


# ============================================================
# フォーマッタ: パーツ → 各媒体向け文字列
# ============================================================
def format_event_message(event: EventRecord) -> str:
    """Discord 送信用メッセージを生成（全角スペース区切り1行コンパクト形式）。

    これが Discord と formatted_message の唯一の源泉。
    """
    p = build_event_parts(event)
    segments = [f"{p.emoji}【{p.label}】{p.display_name}"]
    segments.extend(p.metrics)
    segments.extend(p.extra)
    if p.period:
        segments.append(p.period)
    return _truncate(_SEP.join(segments)) + _TAIL


def build_formatted_message(event: EventRecord) -> str:
    """Web詳細画面用メッセージ。Discord と完全一致を保証する薄いラッパー。"""
    msg = format_event_message(event)
    # Discord末尾マーカーを除去
    return msg.rstrip("\n\u200b").strip()


def build_display_title(event: EventRecord) -> str:
    """Web一覧用の短い見出しを生成。"""
    p = build_event_parts(event)
    return f"{p.emoji}【{p.label}】{p.display_name}"


def build_display_summary(event: EventRecord) -> str:
    """Web一覧用のコンパクトサマリを生成。"""
    p = build_event_parts(event)
    segments = list(p.metrics) + list(p.extra)
    if p.period:
        segments.append(p.period)
    if not segments:
        return event.title or ""
    return " / ".join(segments)


# ============================================================
# 後方互換: 旧フォーマッタ (format_event_message 経由)
# ============================================================
def format_buyback_msg(event: EventRecord) -> str:
    """後方互換: 自社株買い通知メッセージ"""
    return format_event_message(event)


def format_forecast_msg(event: EventRecord) -> str:
    """後方互換: 業績予想修正通知メッセージ"""
    return format_event_message(event)


def format_dividend_msg(event: EventRecord) -> str:
    """後方互換: 配当修正通知メッセージ"""
    return format_event_message(event)


# ============================================================
# 送信
# ============================================================
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

