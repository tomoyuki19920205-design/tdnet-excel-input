#!/usr/bin/env python3
"""smoke_send_dividend_revision_discord.py

配当通知文字列の単体確認スクリプト。
Discord送信・DB接続は一切しない。

使い方:
    cd C:\\Users\\takuy\\OneDrive\\tdnet-excel-input
    python tools/smoke_send_dividend_revision_discord.py
"""
from __future__ import annotations

import json
import sys
import os

# src/ を import パスに追加
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from events.common_models import EventRecord, EventType
from events.common_notify import format_dividend_msg

# ============================================================
# テストケース定義
# ============================================================
_CASES = [
    {
        "label": "サンテック（増配）",
        "company_name": "サンテック",
        "ticker": "1960",
        "subtype": "increase",
        "payload": {
            "fiscal_period": "2026年3月期",
            "dividend_basis": "期末",
            "previous_dividend_per_share": 30.0,
            "revised_dividend_per_share": 45.0,
            "delta_dividend_per_share": 15.0,
        },
    },
    {
        "label": "積水化成（配当予想修正）",
        "company_name": "積水化成",
        "ticker": "4228",
        "subtype": "undecided",
        "payload": {
            "fiscal_period": "2026年3月期",
            "dividend_basis": "期末",
            "previous_dividend_per_share": 80.0,
            "revised_dividend_per_share": 100.0,
            "delta_dividend_per_share": 20.0,
        },
    },
    {
        "label": "ティラド（増配）",
        "company_name": "ティラド",
        "ticker": "7236",
        "subtype": "increase",
        "payload": {
            "fiscal_period": "2026年3月期",
            "dividend_basis": "期末",
            "previous_dividend_per_share": 40.0,
            "revised_dividend_per_share": 50.0,
            "delta_dividend_per_share": 10.0,
        },
    },
    {
        "label": "アマノ（配当予想修正）",
        "company_name": "アマノ",
        "ticker": "6436",
        "subtype": "undecided",
        "payload": {
            "fiscal_period": "2025年3月期",
            "dividend_basis": "期末",
            "previous_dividend_per_share": 60.0,
            "revised_dividend_per_share": 70.0,
            "delta_dividend_per_share": 10.0,
        },
    },
    {
        "label": "前回値なし（新規配当）",
        "company_name": "テスト商事",
        "ticker": "9999",
        "subtype": "increase",
        "payload": {
            "fiscal_period": "2026年3月期",
            "dividend_basis": "期末",
            "previous_dividend_per_share": None,
            "revised_dividend_per_share": 30.0,
            "delta_dividend_per_share": None,
        },
    },
    {
        "label": "前回値0（無配→復配）",
        "company_name": "復配電機",
        "ticker": "8888",
        "subtype": "increase",
        "payload": {
            "fiscal_period": "2026年3月期",
            "dividend_basis": "期末",
            "previous_dividend_per_share": 0.0,
            "revised_dividend_per_share": 20.0,
            "delta_dividend_per_share": 20.0,
        },
    },
]


def _make_event(case: dict) -> EventRecord:
    """テストケースから EventRecord を生成する"""
    # None を含む payload も JSON 化する
    payload_json = json.dumps(case["payload"], ensure_ascii=False)
    return EventRecord(
        ticker=case["ticker"],
        company_name=case["company_name"],
        event_type=EventType.DIVIDEND_REVISION,
        subtype=case["subtype"],
        title=f"配当予想の修正に関するお知らせ",
        extracted_payload_json=payload_json,
    )


def main() -> None:
    print("=" * 60)
    print("配当通知フォーマット 単体確認")
    print("=" * 60)

    all_ok = True
    for case in _CASES:
        ev = _make_event(case)
        msg = format_dividend_msg(ev)

        # 末尾の制御文字を除去して表示
        display = msg.strip()

        print(f"\n[{case['label']}]")
        print(display)

        # 金額行の存在チェック
        basis = case["payload"].get("dividend_basis", "")
        rev = case["payload"].get("revised_dividend_per_share")
        if rev is not None:
            expected_fragment = f"{basis}配当:" if basis else "配当:"
            if expected_fragment not in display:
                print(f"  ⚠️  WARNING: '{expected_fragment}' が見つかりません")
                all_ok = False
            else:
                print(f"  ✅ '{expected_fragment}' を確認")

    print("\n" + "=" * 60)
    print("結果: " + ("✅ 全ケース通過" if all_ok else "⚠️  一部ケースで問題あり"))
    print("=" * 60)


if __name__ == "__main__":
    main()
