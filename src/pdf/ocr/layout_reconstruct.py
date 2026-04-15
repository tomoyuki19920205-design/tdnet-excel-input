# ============================================================
# layout_reconstruct.py — OCRテキストのアンカーベース範囲切り出し
# ============================================================
from __future__ import annotations

import re

from .text_normalize import normalize_ocr_text


def split_to_lines(text: str) -> list[str]:
    """OCRテキストを正規化して行分割する。"""
    normalized = normalize_ocr_text(text)
    return [line for line in normalized.splitlines() if line.strip()]


def find_anchor_region(
    lines: list[str],
    anchor_keywords: list[str],
    before: int = 5,
    after: int = 20,
) -> list[str] | None:
    """
    アンカーキーワードを含む行を検出し、その周辺行を返す。

    Args:
        lines: テキスト行リスト
        anchor_keywords: 検出キーワードリスト
        before: アンカー行の何行前から含めるか
        after: アンカー行の何行後まで含めるか

    Returns:
        アンカー周辺の行リスト、見つからなければNone
    """
    for i, line in enumerate(lines):
        for kw in anchor_keywords:
            if kw in line:
                start = max(0, i - before)
                end = min(len(lines), i + after + 1)
                return lines[start:end]
    return None


def find_all_anchor_regions(
    lines: list[str],
    anchor_keywords: list[str],
    before: int = 5,
    after: int = 20,
) -> list[tuple[list[str], int, str]]:
    """
    アンカーキーワードを含む全行を検出し、各周辺regionを返す。

    Returns:
        [(region行リスト, アンカー行index, マッチしたキーワード), ...]
    """
    results: list[tuple[list[str], int, str]] = []
    seen_indices: set[int] = set()

    for i, line in enumerate(lines):
        for kw in anchor_keywords:
            if kw in line and i not in seen_indices:
                start = max(0, i - before)
                end = min(len(lines), i + after + 1)
                results.append((lines[start:end], i, kw))
                seen_indices.add(i)
                break  # 同一行で複数KWマッチしても1回

    return results


def filter_data_lines(lines: list[str], max_length: int = 100) -> list[str]:
    """
    データ行をフィルタリングする。
    - 長文（>max_length文字）は除外
    - 数字を含む行を優先して返す
    """
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if len(stripped) > max_length:
            continue
        # 数字を含む行のみ
        if re.search(r"\d", stripped):
            result.append(stripped)
    return result
