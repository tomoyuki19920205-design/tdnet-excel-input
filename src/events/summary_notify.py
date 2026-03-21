#!/usr/bin/env python3
"""summary_notify.py — AI要約の Discord 通知

Webhook 1本で運用。needs_review の場合はレビュー文面に分岐する。
"""
from __future__ import annotations

import logging

import requests

from .summary_models import AISummary

logger = logging.getLogger("summary_notify")

_MAX_DISCORD_LEN = 1950

# トーン絵文字マッピング
_TONE_EMOJI = {
    "positive": "📈",
    "negative": "📉",
    "neutral": "➖",
    "mixed": "🔄",
    "cautious": "⚠️",
}

_TONE_JA = {
    "positive": "ポジティブ",
    "negative": "ネガティブ",
    "neutral": "ニュートラル",
    "mixed": "強弱混在",
    "cautious": "慎重",
}


def format_summary_message(summary: AISummary) -> str:
    """AI要約から Discord メッセージを生成する"""
    disp = (
        f"{summary.company_name}（{summary.ticker}）"
        if summary.company_name
        else summary.ticker
    )

    if summary.needs_review:
        return _format_review_message(summary, disp)
    else:
        return _format_flash_message(summary, disp)


def _format_flash_message(summary: AISummary, display_name: str) -> str:
    """速報要約メッセージ"""
    tone_emoji = _TONE_EMOJI.get(summary.tone, "📋")
    tone_ja = _TONE_JA.get(summary.tone, summary.tone)

    lines = [
        f"🤖【AI速報要約】{display_name}",
        f"📌 {summary.headline}",
    ]

    for bullet in summary.bullets:
        lines.append(f"・ {bullet}")

    lines.append(f"{tone_emoji} トーン: {tone_ja}")

    msg = "\n".join(lines)
    if len(msg) > _MAX_DISCORD_LEN:
        msg = msg[:_MAX_DISCORD_LEN - 3] + "..."
    return msg


def _format_review_message(summary: AISummary, display_name: str) -> str:
    """要レビュー通知メッセージ"""
    lines = [
        f"⚠️【AI要約レビュー依頼】{display_name}",
        f"📌 {summary.headline}",
    ]

    for bullet in summary.bullets:
        lines.append(f"・ {bullet}")

    lines.append("")
    lines.append(f"🔍 タイトル: {summary.title[:100]}")
    lines.append("📋 情報不足のため確認が必要です")

    msg = "\n".join(lines)
    if len(msg) > _MAX_DISCORD_LEN:
        msg = msg[:_MAX_DISCORD_LEN - 3] + "..."
    return msg


def send_summary_discord(
    webhook_url: str,
    summary: AISummary,
    dry_run: bool = False,
) -> bool:
    """Discord に AI要約通知を送信する。

    Returns
    -------
    bool
        送信成功 or dry-run
    """
    msg = format_summary_message(summary)

    if dry_run:
        logger.info(f"[DRY-RUN] AI要約通知:\n{msg}")
        print(f"[DRY-RUN] AI要約通知:\n{msg}")
        return True

    if not webhook_url:
        logger.warning("[NOTIFY] webhook_url が空のためスキップ")
        return False

    try:
        r = requests.post(webhook_url, json={"content": msg}, timeout=10)
        r.raise_for_status()
        logger.info(
            f"[NOTIFY] AI要約送信 summary_id={summary.summary_id[:12]} "
            f"ticker={summary.ticker} review={summary.needs_review}"
        )
        return True
    except Exception as e:
        logger.error(f"[NOTIFY] Discord送信失敗: {e}")
        return False


# ============================================================
# V2: 決算短信向け通知フォーマット
# ============================================================

def format_earnings_message(
    ticker: str,
    company_name: str,
    summary_line: str,
    segment_lines: str,
    company_reasons: list[str],
    segment_reasons: list[dict],
    title: str = "",
) -> str:
    """V2 決算短信通知メッセージを組み立てる。

    Parameters
    ----------
    ticker : 銘柄コード
    company_name : 会社名
    summary_line : "売上 YOY +12.3%　　営業利益 YOY +25.4%"
    segment_lines : セグメント行（複数行 or 空文字）
    company_reasons : 全社増減理由の箇条書きリスト
    segment_reasons : [{segment_name, reason}, ...]
    title : 開示タイトル
    """
    disp = f"{company_name}（{ticker}）" if company_name else ticker
    if title:
        disp += f"　{title[:60]}"

    lines = [
        f"📊 {disp}",
        summary_line,
    ]

    if segment_lines:
        lines.append("")
        lines.append(f"```\n{segment_lines}\n```")

    if company_reasons:
        lines.append("")
        lines.append("■ 増減理由（全社）")
        for reason in company_reasons[:3]:
            lines.append(f"・{reason}")

    if segment_reasons:
        lines.append("")
        lines.append("■ 増減理由（セグメント）")
        for sr in segment_reasons:
            lines.append(f"・{sr.get('segment_name', '')}：{sr.get('reason', '')}")

    msg = "\n".join(lines)
    if len(msg) > _MAX_DISCORD_LEN:
        msg = msg[:_MAX_DISCORD_LEN - 3] + "..."
    return msg


def send_earnings_discord(
    webhook_url: str,
    message: str,
    dry_run: bool = False,
) -> bool:
    """V2 決算短信通知を Discord に送信する。"""
    if dry_run:
        logger.info(f"[DRY-RUN] V2通知:\n{message}")
        print(f"[DRY-RUN] V2通知:\n{message}")
        return True

    if not webhook_url:
        logger.warning("[NOTIFY] webhook_url が空のためスキップ")
        return False

    try:
        r = requests.post(webhook_url, json={"content": message}, timeout=10)
        r.raise_for_status()
        logger.info("[NOTIFY] V2通知送信成功")
        return True
    except Exception as e:
        logger.error(f"[NOTIFY] V2 Discord送信失敗: {e}")
        return False
