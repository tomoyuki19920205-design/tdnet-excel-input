#!/usr/bin/env python3
"""smoke_send_forecast_revision_discord.py — forecast_revision Discord 送信スモークテスト

上方修正通知（月島HD 6332）のダミーを Discord Webhook へ1件テスト送信する。
既存 DB・通知済みフラグは一切変更しない。

実行:
    cd "C:\\Users\\takuy\\OneDrive\\tdnet-excel-input"
    python -X utf8 .\\tools\\smoke_send_forecast_revision_discord.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

# ── プロジェクトルートを sys.path に追加 ──────────────────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── .env 読み込み ───────────────────────────────────────────────────
_env_path = ROOT / ".env"
if _env_path.exists():
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from src.events.common_models import EventRecord, EventType
from src.events.common_notify import format_forecast_msg

# ── Webhook URL チェック ────────────────────────────────────────────
webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
if not webhook_url:
    raise SystemExit("[ERROR] DISCORD_WEBHOOK_URL が設定されていません (.env を確認)")

# ── ダミーイベント生成（月島HD 6332 上方修正）───────────────────────
# extracted_payload_json のキーは format_forecast_msg() が参照する実仕様に合わせる
_payload = {
    "period_label":            "2026年3月期 通期",
    "previous_sales":          144000.0,          # 百万円
    "revised_sales":           149000.0,
    "change_sales_pct":        3.5,
    "previous_op":             9500.0,
    "revised_op":              9800.0,
    "change_op_pct":           3.2,
    "previous_ordinary":       10500.0,
    "revised_ordinary":        11000.0,
    "change_ordinary_pct":     4.8,
    "previous_net_income":     15000.0,
    "revised_net_income":      16900.0,
    "change_net_income_pct":   12.7,
    "previous_eps":            380.64,
    "revised_eps":             412.43,
}

event = EventRecord(
    event_id="smoke-discord-6332",
    source_doc_id="140120260423508751",
    ticker="6332",
    company_name="月島HD",
    title="業績予想の修正に関するお知らせ",
    event_type=EventType.FORECAST_REVISION,
    subtype="upward",
    doc_url="https://www.release.tdnet.info/inbs/140120260423508751.pdf",
    extracted_payload_json=json.dumps(_payload, ensure_ascii=False),
)

# ── メッセージ生成 ──────────────────────────────────────────────────
message = format_forecast_msg(event)

print("─" * 60)
print("送信メッセージプレビュー:")
print(message)
print("─" * 60)

# 成功条件チェック（送信前に確認）
_url_line = "開示: https://www.release.tdnet.info/inbs/140120260423508751.pdf"
if _url_line not in message:
    raise SystemExit(f"[ERROR] 開示URLが通知本文に含まれていません:\n{message}")

# ── Discord Webhook 送信 ────────────────────────────────────────────
print("Discord に送信中...")
try:
    res = requests.post(webhook_url, json={"content": message}, timeout=15)
except requests.RequestException as e:
    raise SystemExit(f"[ERROR] HTTP リクエスト失敗: {e}")

print(f"discord_status={res.status_code}")

if res.status_code >= 300:
    print(res.text[:500])
    raise SystemExit(f"[ERROR] Discord 送信失敗 status={res.status_code}")

print("✅ Discord forecast revision smoke sent OK")
