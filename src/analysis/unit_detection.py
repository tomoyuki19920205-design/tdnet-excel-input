# ============================================================
# unit_detection.py — 単位検出
# ============================================================
"""
セグメント表から数値のスケール単位を検出する。

検出元の優先順位:
  1. ヘッダーセル内
  2. テーブル直上テキスト
  3. ページ上部テキスト
  4. ページ全体からの弱推定
  5. unknown
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


# ============================================================
# UnitDetectionResult
# ============================================================

@dataclass
class UnitDetectionResult:
    """単位検出結果"""
    unit_raw: str | None = None
    unit_multiplier: int | None = None
    currency: str | None = None
    unit_source: str | None = None     # "header" / "nearby" / "page_top" / "page_body" / None
    confidence: float = 0.0
    matched_pattern: str | None = None


# ============================================================
# 単位パターン定義
# ============================================================

# (pattern, multiplier, currency)
_JP_UNIT_PATTERNS: list[tuple[re.Pattern, int, str, str]] = [
    (re.compile(r'百万米ドル'), 1_000_000, "USD", "百万米ドル"),
    (re.compile(r'千米ドル'), 1_000, "USD", "千米ドル"),
    (re.compile(r'百万ドル'), 1_000_000, "USD", "百万ドル"),
    (re.compile(r'千ドル'), 1_000, "USD", "千ドル"),
    (re.compile(r'百万ユーロ'), 1_000_000, "EUR", "百万ユーロ"),
    (re.compile(r'千ユーロ'), 1_000, "EUR", "千ユーロ"),
    (re.compile(r'百万円'), 1_000_000, "JPY", "百万円"),
    (re.compile(r'億円'), 100_000_000, "JPY", "億円"),
    (re.compile(r'千円'), 1_000, "JPY", "千円"),
    (re.compile(r'(?<![百万千億])円'), 1, "JPY", "円"),
]

_EN_UNIT_PATTERNS: list[tuple[re.Pattern, int, str, str]] = [
    (re.compile(r'millions?\s+of\s+yen', re.IGNORECASE), 1_000_000, "JPY", "millions of yen"),
    (re.compile(r'thousands?\s+of\s+yen', re.IGNORECASE), 1_000, "JPY", "thousands of yen"),
    (re.compile(r'billions?\s+of\s+yen', re.IGNORECASE), 1_000_000_000, "JPY", "billions of yen"),
    (re.compile(r'millions?\s+of\s+(?:us\s*)?dollars?', re.IGNORECASE), 1_000_000, "USD", "millions of dollars"),
    (re.compile(r'thousands?\s+of\s+(?:us\s*)?dollars?', re.IGNORECASE), 1_000, "USD", "thousands of dollars"),
    (re.compile(r'million\s+yen', re.IGNORECASE), 1_000_000, "JPY", "million yen"),
    (re.compile(r'thousand\s+yen', re.IGNORECASE), 1_000, "JPY", "thousand yen"),
]

# 単位を囲むパターン
_WRAPPER_PATTERNS = [
    re.compile(r'[（\(]\s*単位\s*[：:]\s*(.+?)\s*[）\)]'),
    re.compile(r'単位\s*[：:]\s*(.+?)(?:\s|$|[）\)])'),
    re.compile(r'[（\(]\s*(.+?)\s*[）\)]'),
    re.compile(r'Unit\s*:\s*(.+?)(?:\s|$)', re.IGNORECASE),
]


# ============================================================
# 公開関数
# ============================================================

def _normalize_for_unit(text: str) -> str:
    """単位検出用テキスト正規化"""
    return unicodedata.normalize("NFKC", text)


def detect_unit_from_text(text: str) -> UnitDetectionResult:
    """
    テキストから単位を検出する。

    Returns:
        UnitDetectionResult (unit_source は呼び出し側で上書き想定)
    """
    if not text:
        return UnitDetectionResult()

    normalized = _normalize_for_unit(text)

    # まず wrapper パターン (「単位：百万円」等) を探す
    for wrapper in _WRAPPER_PATTERNS:
        m = wrapper.search(normalized)
        if m:
            inner = m.group(1).strip()
            result = _match_unit_text(inner)
            if result.unit_multiplier is not None:
                result.confidence = min(result.confidence + 0.1, 1.0)  # wrapper明示ボーナス
                return result

    # wrapper なしで直接パターンマッチ
    return _match_unit_text(normalized)


def _match_unit_text(text: str) -> UnitDetectionResult:
    """テキスト内から単位パターンをマッチ"""
    # 英語パターン優先 (より具体的)
    for pat, multi, curr, raw in _EN_UNIT_PATTERNS:
        if pat.search(text):
            return UnitDetectionResult(
                unit_raw=raw, unit_multiplier=multi, currency=curr,
                confidence=0.8, matched_pattern=pat.pattern,
            )

    # 日本語パターン
    for pat, multi, curr, raw in _JP_UNIT_PATTERNS:
        if pat.search(text):
            return UnitDetectionResult(
                unit_raw=raw, unit_multiplier=multi, currency=curr,
                confidence=0.8, matched_pattern=pat.pattern,
            )

    return UnitDetectionResult()


def detect_unit_for_table(
    page_text: str,
    table_headers: list[str],
    nearby_text: str | None = None,
) -> UnitDetectionResult:
    """
    テーブル用の単位検出。複数ソースを優先順位で探索。

    Args:
        page_text: ページ全体テキスト
        table_headers: テーブルのヘッダー行リスト
        nearby_text: テーブル直上のテキスト
    """
    candidates: list[UnitDetectionResult] = []

    # 1. ヘッダーセル内
    for header in table_headers:
        r = detect_unit_from_text(header)
        if r.unit_multiplier is not None:
            r.unit_source = "header"
            r.confidence = min(r.confidence + 0.1, 1.0)
            candidates.append(r)

    # 2. テーブル直上テキスト
    if nearby_text:
        r = detect_unit_from_text(nearby_text)
        if r.unit_multiplier is not None:
            r.unit_source = "nearby"
            candidates.append(r)

    # 3. ページ上部 (先頭20行)
    if page_text:
        page_lines = page_text.split("\n")
        top_text = "\n".join(page_lines[:20])
        r = detect_unit_from_text(top_text)
        if r.unit_multiplier is not None:
            r.unit_source = "page_top"
            r.confidence = max(r.confidence - 0.1, 0.3)
            candidates.append(r)

    # 4. ページ全体 (弱推定)
    if page_text and not candidates:
        r = detect_unit_from_text(page_text)
        if r.unit_multiplier is not None:
            r.unit_source = "page_body"
            r.confidence = max(r.confidence - 0.2, 0.2)
            candidates.append(r)

    return merge_unit_candidates(candidates)


def merge_unit_candidates(
    candidates: list[UnitDetectionResult],
) -> UnitDetectionResult:
    """
    複数の単位候補を統合し、最高 confidence のものを返す。

    同じ単位が複数ソースで見つかった場合は confidence を上げる。
    """
    if not candidates:
        return UnitDetectionResult()

    # confidence 最大を採用
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    best = candidates[0]

    # 同じ単位が複数ソースで確認された場合ボーナス
    same_unit_count = sum(
        1 for c in candidates[1:]
        if c.unit_multiplier == best.unit_multiplier and c.currency == best.currency
    )
    if same_unit_count > 0:
        best.confidence = min(best.confidence + 0.05 * same_unit_count, 1.0)

    return best
