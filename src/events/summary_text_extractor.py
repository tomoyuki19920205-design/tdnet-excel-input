#!/usr/bin/env python3
"""summary_text_extractor.py — AI要約用の入力テキスト構築

既存イベント抽出の構造化データを優先し、本文キーワード近傍は補助的に使用する。
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("summary_text_extractor")

# ============================================================
# 本文から抽出するセクションのアンカーキーワード
# ============================================================
_SECTION_ANCHORS = {
    "業績ハイライト": [
        "売上高", "営業利益", "経常利益", "当期純利益",
        "前回発表予想", "今回修正予想", "増減額", "増減率",
    ],
    "会社予想": [
        "通期業績予想", "業績予想の修正", "連結業績予想",
        "個別業績予想", "見通し",
    ],
    "配当情報": [
        "配当", "配当金", "1株当たり配当金", "期末配当",
        "中間配当", "年間配当",
    ],
    "特別損益": [
        "特別損失", "特別利益", "減損損失", "固定資産除却損",
        "投資有価証券売却益",
    ],
    "セグメント": [
        "セグメント", "事業別", "部門別",
    ],
}


def _extract_near_keywords(text: str, keywords: list[str], window: int = 200) -> str:
    """キーワード近傍のテキストを抽出する"""
    found_ranges: list[tuple[int, int]] = []
    for kw in keywords:
        idx = text.find(kw)
        if idx >= 0:
            start = max(0, idx - 30)
            end = min(len(text), idx + len(kw) + window)
            found_ranges.append((start, end))

    if not found_ranges:
        return ""

    # オーバーラップ範囲をマージ
    found_ranges.sort()
    merged = [found_ranges[0]]
    for start, end in found_ranges[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    parts = [text[s:e].strip() for s, e in merged]
    return "\n".join(parts)


def _format_structured_data(event_type: str, subtype: str, payload: dict) -> str:
    """構造化データから要約用テキストを生成する"""
    lines: list[str] = []

    if event_type == "buyback":
        lines.append(f"[自社株買い: {subtype}]")
        if payload.get("shares_limit"):
            lines.append(f"取得上限株数: {payload['shares_limit']:,}株")
        if payload.get("amount_limit_million_yen"):
            amt = payload["amount_limit_million_yen"]
            lines.append(f"取得上限金額: {amt:.0f}百万円")
        if payload.get("start_date"):
            lines.append(f"取得期間: {payload.get('start_date', '')} - {payload.get('end_date', '')}")
        if payload.get("shares_acquired"):
            lines.append(f"取得済株数: {payload['shares_acquired']:,}株")
        if payload.get("amount_acquired_million_yen"):
            lines.append(f"取得済金額: {payload['amount_acquired_million_yen']:.0f}百万円")

    elif event_type == "forecast_revision":
        lines.append(f"[業績予想修正: {subtype}]")
        if payload.get("period_label"):
            lines.append(f"対象期: {payload['period_label']}")
        for label, key_prev, key_rev, key_pct in [
            ("売上高", "previous_sales", "revised_sales", "change_sales_pct"),
            ("営業利益", "previous_op", "revised_op", "change_op_pct"),
            ("経常利益", "previous_ordinary", "revised_ordinary", "change_ordinary_pct"),
            ("純利益", "previous_net_income", "revised_net_income", "change_net_income_pct"),
        ]:
            prev = payload.get(key_prev)
            rev = payload.get(key_rev)
            pct = payload.get(key_pct)
            if rev is not None:
                parts = [f"{label}:"]
                if prev is not None:
                    parts.append(f"{prev}→{rev}")
                else:
                    parts.append(f"{rev}")
                if pct is not None:
                    sign = "+" if pct > 0 else ""
                    parts.append(f"({sign}{pct:.1f}%)")
                lines.append(" ".join(parts))

    elif event_type == "dividend_revision":
        lines.append(f"[配当修正: {subtype}]")
        if payload.get("fiscal_period"):
            lines.append(f"対象期: {payload['fiscal_period']} {payload.get('dividend_basis', '')}")
        prev = payload.get("previous_dividend_per_share")
        rev = payload.get("revised_dividend_per_share")
        if prev is not None and rev is not None:
            delta = payload.get("delta_dividend_per_share")
            delta_str = f" ({'+' if delta and delta > 0 else ''}{delta}円)" if delta is not None else ""
            lines.append(f"配当: {prev}円→{rev}円{delta_str}")
        if payload.get("special_dividend_per_share"):
            lines.append(f"特別配当: {payload['special_dividend_per_share']}円")
        if payload.get("commemorative_dividend_per_share"):
            lines.append(f"記念配当: {payload['commemorative_dividend_per_share']}円")

    return "\n".join(lines)


def extract_summary_input(
    title: str,
    text_body: str = "",
    event_type: str = "",
    subtype: str = "",
    extracted_payload_json: str = "",
    max_chars: int = 2000,
) -> str:
    """AI要約用の入力テキストを構築する。

    構造化データを優先し、本文キーワード近傍は補助として使用する。

    Parameters
    ----------
    title : str
        開示タイトル
    text_body : str
        開示本文（オプション）
    event_type : str
        イベント種別
    subtype : str
        サブタイプ
    extracted_payload_json : str
        イベント抽出済みの構造化データ (JSON文字列)
    max_chars : int
        最大文字数制限

    Returns
    -------
    str
        AI要約用の入力テキスト
    """
    parts: list[str] = []

    # 1. タイトル（常に含める）
    parts.append(f"【タイトル】{title}")

    # 2. 構造化データ（最優先）
    if extracted_payload_json:
        try:
            payload = json.loads(extracted_payload_json)
            structured = _format_structured_data(event_type, subtype, payload)
            if structured:
                parts.append(f"\n【構造化データ】\n{structured}")
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"extracted_payload_json parse error: {e}")

    # 3. 本文キーワード近傍（補助、構造化データが不足している場合のみ）
    if text_body:
        remaining_budget = max_chars - sum(len(p) for p in parts) - 100
        if remaining_budget > 200:
            body_parts: list[str] = []
            for section_name, keywords in _SECTION_ANCHORS.items():
                extracted = _extract_near_keywords(text_body, keywords, window=150)
                if extracted:
                    body_parts.append(f"\n【{section_name}】\n{extracted}")

            # 予算内で追加
            for bp in body_parts:
                if remaining_budget - len(bp) > 0:
                    parts.append(bp)
                    remaining_budget -= len(bp)
                else:
                    break

    # 文字数制限
    result = "\n".join(parts)
    if len(result) > max_chars:
        result = result[:max_chars - 3] + "..."

    return result
