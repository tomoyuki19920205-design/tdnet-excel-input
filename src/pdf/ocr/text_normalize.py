# ============================================================
# text_normalize.py — OCRテキスト正規化ユーティリティ
# ============================================================
from __future__ import annotations

import re
import unicodedata


def normalize_ocr_text(text: str) -> str:
    """
    OCRテキストの正規化。
    - 全角英数→半角
    - 全角スペース→半角
    - カンマ除去（数値内）
    - 連続空白の圧縮
    """
    # 全角英数字・記号を半角に変換（カタカナは保持）
    text = unicodedata.normalize("NFKC", text)
    # 全角スペースを半角に
    text = text.replace("\u3000", " ")
    # 連続空白を1つに
    text = re.sub(r" {2,}", " ", text)
    return text


def normalize_number_str(raw: str) -> int | None:
    """
    OCRテキストから数値を正規化して整数に変換。
    - △▲ → 負数
    - カンマ除去
    - 全角数字は NFKC で半角化済み前提
    """
    s = raw.strip()
    if not s:
        return None

    negative = False
    if s.startswith(("△", "▲", "－", "-")):
        negative = True
        s = s.lstrip("△▲－-").strip()

    # カンマ除去
    s = s.replace(",", "")

    # 小数点対応（切り捨て）
    if "." in s:
        s = s.split(".")[0]

    if not s or not s.isdigit():
        return None

    val = int(s)
    return -val if negative else val


# 数値パターン（△▲付き、カンマ区切り対応）
_NUMBER_PATTERN = re.compile(r"[△▲\-－]?\d[\d,]*(?:\.\d+)?")


def extract_numbers(text: str) -> list[int]:
    """
    テキストから数値をすべて抽出する。
    100未満の数値は除外（YoY%等と判別）。
    """
    matches = _NUMBER_PATTERN.findall(text)
    results: list[int] = []
    for raw in matches:
        val = normalize_number_str(raw)
        if val is not None and abs(val) >= 100:
            results.append(val)
    return results
