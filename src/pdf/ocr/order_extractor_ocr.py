# ============================================================
# order_extractor_ocr.py — OCRテキストから受注情報抽出
# ============================================================
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .layout_reconstruct import split_to_lines, find_anchor_region, filter_data_lines
from .text_normalize import extract_numbers, normalize_ocr_text

logger = logging.getLogger("tdnet")

# 受注アンカーキーワード
_ORDER_ANCHORS = [
    "受注高",
    "受注額",
    "新規受注",
    "受注工事高",
    "受注残高",
    "受注残",
    "手持工事高",
    "繰越工事高",
]

# メトリクス→キーワードマッピング
_METRIC_KEYWORDS = {
    "orders_total": ["受注高", "受注額", "新規受注", "受注工事高"],
    "backlog_total": ["受注残高", "受注残", "手持工事高", "手持ち工事高"],
    "carryover_construction_total": ["繰越工事高", "繰越高", "次期繰越工事高"],
}

# 単位検出パターン
_SCALE_PATTERNS = [
    re.compile(r"[（(]単位[：:]?\s*(百万円|億円|千円|円)[）)]"),
    re.compile(r"単位[：:]\s*(百万円|億円|千円|円)"),
    re.compile(r"[（(](百万円|億円|千円)[）)]"),
]


@dataclass
class OcrOrderMetric:
    """OCRから抽出された受注メトリクス1件"""
    metric_name: str
    value: int
    raw_value: int
    unit: str
    raw_text: str = ""


@dataclass
class OcrOrderResult:
    """OCR受注抽出結果"""
    metrics: list[OcrOrderMetric] = field(default_factory=list)
    success: bool = False
    reason: str = ""


def _detect_scale(text: str) -> str:
    """テキストから単位を検出"""
    for pattern in _SCALE_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    return "百万円"


def _normalize_value(value: int, scale_str: str) -> int:
    """百万円に単位統一"""
    if scale_str == "億円":
        return value * 100
    elif scale_str == "千円":
        return value // 1000 if abs(value) >= 1000 else value
    elif scale_str == "円":
        return value // 1_000_000 if abs(value) >= 1_000_000 else value
    return value  # 百万円はそのまま


def extract_orders_from_ocr_text(ocr_text: str) -> OcrOrderResult:
    """
    OCRテキストから受注・受注残情報を抽出する。

    アンカーベース:
    1. 受注関連キーワードを含む行を検出
    2. 前後10行を切り出し
    3. メトリクスごとにキーワード行から数値抽出

    成功条件: 受注または受注残1件以上
    """
    lines = split_to_lines(ocr_text)
    if not lines:
        return OcrOrderResult(reason="no_lines")

    # アンカー検出（前5行、後10行）
    region = find_anchor_region(lines, _ORDER_ANCHORS, before=5, after=10)
    if region is None:
        logger.info("[order-ocr] no anchor found")
        return OcrOrderResult(reason="no_order_anchor")

    logger.info(f"[order-ocr] anchor found, region={len(region)} lines")

    region_text = "\n".join(region)
    scale_str = _detect_scale(region_text)

    metrics: list[OcrOrderMetric] = []

    for metric_name, keywords in _METRIC_KEYWORDS.items():
        for line in region:
            matched_kw = None
            for kw in keywords:
                if kw in line:
                    matched_kw = kw
                    break

            if matched_kw is None:
                continue

            # キーワード部分を除去して数値抽出
            text_after_kw = line.replace(matched_kw, "", 1)
            nums = extract_numbers(text_after_kw)
            if not nums:
                # 行全体から試行
                nums = extract_numbers(line)
                if not nums:
                    continue

            # 合計行を優先: "合計" "計" を含む行があるか
            raw_value = nums[0]
            normalized = _normalize_value(raw_value, scale_str)

            metrics.append(OcrOrderMetric(
                metric_name=metric_name,
                value=normalized,
                raw_value=raw_value,
                unit=scale_str,
                raw_text=line,
            ))
            logger.info(f"[order-ocr] value extracted: {metric_name}={normalized}")
            break  # 最初にマッチしたキーワード行で確定

    if not metrics:
        logger.info("[order-ocr] no metrics extracted")
        return OcrOrderResult(reason="no_extractable_values")

    logger.info(f"[order-ocr] extracted {len(metrics)} metrics")
    return OcrOrderResult(metrics=metrics, success=True)
