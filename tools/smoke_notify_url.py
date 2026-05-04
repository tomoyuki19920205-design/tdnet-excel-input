#!/usr/bin/env python3
"""smoke_notify_url.py — forecast_revision 通知本文 URL 付きスモークテスト

確認内容:
  - format_forecast_msg() が開示 URL を末尾に表示すること
  - 金額・EPS が正常フォーマットされること
  - e+N 異常値が "---" になること

実行:
    cd "C:\\Users\\takuy\\OneDrive\\tdnet-excel-input"
    python -X utf8 .\\tools\\smoke_notify_url.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# プロジェクトルートを sys.path に追加
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.events.common_models import EventRecord, EventType
from src.events.common_notify import format_forecast_msg

# ─────────────────────────────────────────────
# ケース 1: 月島HD（6332）上方修正 + URL あり
# ─────────────────────────────────────────────
case1 = EventRecord(
    event_id="smoke-6332",
    source_doc_id="140120260423508751",
    ticker="6332",
    company_name="月島HD",
    event_type=EventType.FORECAST_REVISION,
    subtype="upward",
    doc_url="https://www.release.tdnet.info/inbs/140120260423508751.pdf",
    extracted_payload_json=json.dumps({
        "period_label": "2026年3月期 通期",
        "previous_sales":      144000.0,
        "revised_sales":       149000.0,
        "change_sales_pct":    3.5,
        "previous_op":         9500.0,
        "revised_op":          9800.0,
        "change_op_pct":       3.2,
        "previous_ordinary":   10500.0,
        "revised_ordinary":    11000.0,
        "change_ordinary_pct": 4.8,
        "previous_net_income": 15000.0,
        "revised_net_income":  16900.0,
        "change_net_income_pct": 12.7,
        "previous_eps":        380.64,
        "revised_eps":         412.43,
    }, ensure_ascii=False),
)

# ─────────────────────────────────────────────
# ケース 2: URL なし（通知本文に URL 行を出さない)
# ─────────────────────────────────────────────
case2 = EventRecord(
    event_id="smoke-no-url",
    source_doc_id="dummy",
    ticker="9999",
    company_name="テスト会社",
    event_type=EventType.FORECAST_REVISION,
    subtype="upward",
    doc_url="",
    extracted_payload_json=json.dumps({
        "period_label": "2026年3月期 通期",
        "previous_net_income": 500.0,
        "revised_net_income":  700.0,
        "change_net_income_pct": 40.0,
        "previous_eps": 100.0,
        "revised_eps":  140.0,
    }, ensure_ascii=False),
)

# ─────────────────────────────────────────────
# ケース 3: Phase1 e+N 異常値 → "---" に変換確認
# ─────────────────────────────────────────────
case3 = EventRecord(
    event_id="smoke-eN",
    source_doc_id="dummy2",
    ticker="546A",
    company_name="テスト異常値社",
    event_type=EventType.FORECAST_REVISION,
    subtype="upward",
    doc_url="https://www.release.tdnet.info/inbs/140120260422508408.pdf",
    extracted_payload_json=json.dumps({
        "period_label": "2026年3月期 通期",
        "previous_sales":      2.700006900590036e+20,   # e+N 異常値
        "revised_sales":       270000.0,
        "previous_net_income": None,
        "revised_net_income":  1500.0,
        "previous_eps":        None,
        "revised_eps":         150.53,
    }, ensure_ascii=False),
)


def _check(label: str, msg: str, must_contain: list[str], must_not_contain: list[str]) -> bool:
    ok = True
    print(f"\n{'='*60}")
    print(f"【{label}】")
    print(msg)
    for s in must_contain:
        if s in msg:
            print(f"  ✅ 含む: {s!r}")
        else:
            print(f"  ❌ 欠落: {s!r}")
            ok = False
    for s in must_not_contain:
        if s not in msg:
            print(f"  ✅ 含まない: {s!r}")
        else:
            print(f"  ❌ 含むべきでない: {s!r}")
            ok = False
    return ok


all_pass = True

all_pass &= _check(
    "ケース1: 月島HD 上方修正 + URL",
    format_forecast_msg(case1),
    must_contain=[
        "月島HD",
        "6332",
        "上方修正",
        "億円",
        "EPS",
        "380.64円",
        "412.43円",
        "開示: https://www.release.tdnet.info/inbs/140120260423508751.pdf",
    ],
    must_not_contain=["e+20", "e+18"],
)

all_pass &= _check(
    "ケース2: URL なし",
    format_forecast_msg(case2),
    must_contain=["テスト会社", "上方修正", "EPS"],
    must_not_contain=["開示:"],
)

all_pass &= _check(
    "ケース3: e+N 異常値 → ---",
    format_forecast_msg(case3),
    must_contain=[
        "開示: https://www.release.tdnet.info/inbs/140120260422508408.pdf",
        "---",
    ],
    must_not_contain=["e+20", "2700000000"],
)

print(f"\n{'='*60}")
if all_pass:
    print("✅ ALL CASES PASS")
    sys.exit(0)
else:
    print("❌ SOME CASES FAILED")
    sys.exit(1)
