#!/usr/bin/env python3
"""common_notify.py — イベント検知 Discord 通知"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from .common_models import EventRecord, EventType

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
    # 1,000,000,000 百万円 = 1,000兆円超は e+N 等の異常値とみなす
    if abs(val_million) >= 1_000_000_000:
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


def _format_eps_value(val: float | None) -> str | None:
    """EPS値を円単位でフォーマット。整数なら小数なし、小数ありなら表示。"""
    if val is None:
        return None
    if val == int(val):
        return f"{int(val)}円"
    # 過剰桁を出さない: 小数点以下の有効桁をそのまま
    formatted = f"{val:g}円"
    return formatted


def _format_eps_change(prev: float | None, revised: float | None) -> str | None:
    """EPS修正行をフォーマット。両方あるときだけ返す。

    Returns: 'EPS: 100円→150円(+50.0%)' or None
    """
    if prev is None or revised is None:
        return None
    prev_str = _format_eps_value(prev)
    rev_str = _format_eps_value(revised)
    # prev==0 の場合は % を省略
    if prev == 0:
        return f"EPS: {prev_str}→{rev_str}"
    pct = (revised - prev) / abs(prev) * 100
    sign = "+" if pct > 0 else ""
    return f"EPS: {prev_str}→{rev_str}({sign}{pct:.1f}%)"


def _fmt_amount_yen(val_million: float | None) -> str:
    """百万円 → 円単位整数表示（例: 2,000,000,000円）"""
    if val_million is None:
        return "---"
    if abs(val_million) >= 1_000_000_000:
        return "---"
    yen = int(val_million * 1_000_000)
    return f"{yen:,}円"


def _fmt_shares_exact(val: int | None) -> str:
    """株数 → カンマ区切り株表示（例: 1,000,000株）"""
    if val is None:
        return "---"
    return f"{val:,}株"


def _fmt_date_ja(date_str: str | None) -> str:
    """YYYY-MM-DD → YYYY年M月D日"""
    if not date_str:
        return "---"
    try:
        parts = date_str.split("-")
        if len(parts) == 3:
            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
            return f"{y}年{m}月{d}日"
    except Exception:
        pass
    return date_str


def _format_buyback_new_program(payload: dict, disp: str) -> str:
    """NEW_PROGRAM 通知文（取得枠決議）"""
    lines = [f"📊【自社株買い - 取得枠決議】{disp}"]

    shares = payload.get("shares_limit")
    if shares is not None:
        lines.append(f"株数上限: {_fmt_shares_exact(shares)}")

    amt = payload.get("amount_limit_million_yen")
    if amt is not None:
        lines.append(f"金額上限: {_fmt_amount_yen(amt)}")

    ratio = payload.get("ratio_to_outstanding")
    if ratio is not None:
        lines.append(f"割合: {ratio:.2f}%")

    start = payload.get("start_date")
    end = payload.get("end_date")
    if start and end:
        lines.append(f"取得期間: {_fmt_date_ja(start)}〜{_fmt_date_ja(end)}")
    elif start:
        lines.append(f"取得開始: {_fmt_date_ja(start)}")

    method = payload.get("acquisition_method")
    if method:
        method_ja = {
            "market_purchase": "市場買付",
            "tostnet": "ToSTNeT",
            "off_auction": "立会外取引",
        }.get(method, method)
        lines.append(f"取得方法: {method_ja}")

    cancelled = payload.get("shares_cancelled")
    if cancelled is not None:
        lines.append(f"消却: {_fmt_shares_exact(cancelled)}")

    return "\n".join(lines)


def _format_buyback_tostnet(payload: dict, disp: str) -> str:
    """TOSTNET 通知文（立会外買付取引）"""
    lines = [f"📊【自社株買い - ToSTNeT】{disp}"]

    shares = payload.get("shares_limit")
    if shares is not None:
        lines.append(f"株数上限: {_fmt_shares_exact(shares)}")

    amt = payload.get("amount_limit_million_yen")
    if amt is not None:
        lines.append(f"金額上限: {_fmt_amount_yen(amt)}")

    ratio = payload.get("ratio_to_outstanding")
    if ratio is not None:
        lines.append(f"割合: {ratio:.2f}%")

    start = payload.get("start_date")
    if start:
        lines.append(f"買付日時: {_fmt_date_ja(start)}")

    # ToSTNeT は単価情報があれば表示（amount / shares から算出）
    # 現在 BuybackEvent に price フィールドがないため、
    # extracted_json の raw_amount_text から価格を参照する代わりに
    # amount / shares で推算（あれば表示）
    if shares and amt and shares > 0:
        price_per_share = int(amt * 1_000_000 / shares)
        lines.append(f"買付価格(概算): {price_per_share:,}円")

    return "\n".join(lines)


def format_buyback_msg(event: EventRecord) -> str:
    """自社株買いイベントの通知メッセージ

    subtype が new_program / tostnet に応じて別フォーマットを使用。
    """
    payload = {}
    if event.extracted_payload_json:
        try:
            payload = json.loads(event.extracted_payload_json)
        except (json.JSONDecodeError, TypeError):
            pass

    disp = f"{event.company_name}（{event.ticker}）" if event.company_name else event.ticker
    subtype = event.subtype or ""

    if subtype == "tostnet":
        body = _format_buyback_tostnet(payload, disp)
    else:
        # new_program またはその他
        body = _format_buyback_new_program(payload, disp)

    return _truncate(body) + _TAIL


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

    # EPS行（利益指標とは独立した別枠）
    eps_prev = payload.get("previous_eps")
    eps_rev  = payload.get("revised_eps")
    # 異常値ガード: abs > 10000 なら通知文に出さない (DB保存値が壊れていても安全)
    _EPS_NOTIFY_MAX = 10_000
    if eps_prev is not None and abs(eps_prev) > _EPS_NOTIFY_MAX:
        eps_prev = None
    if eps_rev is not None and abs(eps_rev) > _EPS_NOTIFY_MAX:
        eps_rev = None
    eps_line = _format_eps_change(eps_prev, eps_rev)
    if eps_line:
        parts.append(eps_line)

    # 配当行（dividend_annual_total_revised がある場合のみ追記）
    div_prev = payload.get("dividend_annual_total_previous")
    div_rev  = payload.get("dividend_annual_total_revised")
    if div_rev is not None:
        def _fmt_div(v: float) -> str:
            return f"{int(v)}円" if v == int(v) else f"{v:g}円"
        if div_prev is not None and div_prev != 0:
            pct  = (div_rev - div_prev) / abs(div_prev) * 100
            sign = "+" if pct > 0 else ""
            parts.append(f"配当: {_fmt_div(div_prev)}→{_fmt_div(div_rev)}({sign}{pct:.1f}%)")
        elif div_prev is not None:
            parts.append(f"配当: {_fmt_div(div_prev)}→{_fmt_div(div_rev)}")
        else:
            parts.append(f"配当: {_fmt_div(div_rev)}")

    if period:
        parts.append(period)

    # 開示URL（event.doc_url を優先、なければ payload["doc_url"] を fallback）
    url = event.doc_url or payload.get("doc_url", "") or ""
    body = _truncate("\u3000\u3000".join(parts))
    if url:
        return body + "\n" + f"開示: {url}" + _TAIL
    return body + _TAIL


def _fmt_div_amount(val: float) -> str:
    """配当額を表示用文字列に変換（整数なら .0 なし）"""
    if val == int(val):
        return f"{int(val)}円"
    return f"{val:g}円"


def format_dividend_msg(event: EventRecord) -> str:
    """配当修正イベントの通知メッセージ（1行コンパクト形式）"""
    payload = {}
    if event.extracted_payload_json:
        try:
            payload = json.loads(event.extracted_payload_json)
        except (json.JSONDecodeError, TypeError):
            pass

    disp = f"{event.company_name}（{event.ticker}）" if event.company_name else event.ticker
    subtype_ja = {
        "increase": "増配", "decrease": "減配",
        "special_dividend": "特別配当", "commemorative_dividend": "記念配当",
        "maintain": "据え置き", "undecided": "配当予想修正",
    }.get(event.subtype, "配当予想修正")
    emoji = {"increase": "💰", "decrease": "📉", "special_dividend": "🎁",
             "commemorative_dividend": "🎉"}.get(event.subtype, "💵")

    # 1行: ヘッダー + 配当額 + 利回り + 対象期(末尾)
    period = payload.get("fiscal_period", "")
    basis = payload.get("dividend_basis", "")
    parts = [f"{emoji}【{subtype_ja}】{disp}"]

    prev = payload.get("previous_dividend_per_share")
    rev = payload.get("revised_dividend_per_share")

    # 配当額修正行: {区分}配当: {前回}円→{今回}円({増減率})
    if rev is not None:
        basis_label = basis if basis else ""
        prev_str = _fmt_div_amount(prev) if prev is not None else "---"

        # 増減率の算出
        pct_str = ""
        if prev is not None and prev != 0:
            delta = payload.get("delta_dividend_per_share")
            if delta is not None:
                pct = delta / abs(prev) * 100
            else:
                pct = (rev - prev) / abs(prev) * 100
            sign = "+" if pct > 0 else ""
            pct_str = f"({sign}{pct:.1f}%)"

        parts.append(f"{basis_label}配当: {prev_str}→{_fmt_div_amount(rev)}{pct_str}")
    # rev が None の場合は配当額行なし（タイトルのみにフォールバック）

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
        sent_at = datetime.now(timezone(timedelta(hours=9)))
        logger.info(f"discord_sent_at: {sent_at.isoformat()}")
        if event.first_seen_at:
            try:
                first_seen = datetime.fromisoformat(event.first_seen_at)
                diff = (sent_at - first_seen).total_seconds()
                logger.info(f"detect_to_notify_sec: {diff:.2f}")
            except Exception:
                pass
        logger.info(
            f"[NOTIFY] sent event_id={event.event_id[:12]} "
            f"type={event.event_type} ticker={event.ticker}"
        )
        return True
    except Exception as e:
        logger.error(f"[NOTIFY] Discord send failed: {e}")
        return False
