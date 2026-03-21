#!/usr/bin/env python3
"""common_normalizers.py — イベント検知用の共通正規化関数

buyback_extractor.py に存在していた正規化関数を共通モジュールに抽出。
buyback_extractor からも利用可能。
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional


# ============================================================
# 全角→半角 変換
# ============================================================
_ZEN_TO_HAN = str.maketrans(
    "０１２３４５６７８９．，　",
    "0123456789., ",
)


def normalize_jp_number(text: str) -> str:
    """全角数字→半角、カンマ除去、全角スペース→半角"""
    s = text.translate(_ZEN_TO_HAN)
    s = s.replace(",", "").replace("，", "")
    return s.strip()


# ============================================================
# 株数正規化
# ============================================================
_SHARE_RE = re.compile(
    r"([\d,.]+)\s*(?:(億|万|千))?\s*株",
)

_UNIT_MULTI = {"億": 100_000_000, "万": 10_000, "千": 1_000}


def normalize_share_count(text: str) -> Optional[int]:
    """テキストから株数を抽出して int で返す。"""
    s = normalize_jp_number(text)
    m = _SHARE_RE.search(s)
    if not m:
        return None
    num_str = m.group(1).replace(",", "")
    unit = m.group(2) or ""
    try:
        num = float(num_str)
    except ValueError:
        return None
    multiplier = _UNIT_MULTI.get(unit, 1)
    return int(num * multiplier)


# ============================================================
# 金額正規化 → 百万円単位
# ============================================================
_AMOUNT_RE = re.compile(
    r"([\d,.]+)\s*(?:(億|百万|万|千))?\s*円",
)

_AMOUNT_TO_MILLION = {
    "億":   100,
    "百万": 1,
    "万":   0.01,
    "千":   0.001,
}


def normalize_amount_to_million_yen(text: str) -> Optional[float]:
    """テキストから金額を抽出し、百万円単位で返す。"""
    s = normalize_jp_number(text)
    m = _AMOUNT_RE.search(s)
    if not m:
        return None
    num_str = m.group(1).replace(",", "")
    unit = m.group(2) or ""
    try:
        num = float(num_str)
    except ValueError:
        return None

    if unit:
        multiplier = _AMOUNT_TO_MILLION.get(unit, 1)
        return round(num * multiplier, 2)
    else:
        return round(num / 1_000_000, 2)


# ============================================================
# 日付正規化
# ============================================================
_JP_DATE_RE = re.compile(
    r"(?:令和|平成|昭和)?\s*(\d{1,4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)
_SLASH_DATE_RE = re.compile(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})")

_ERA_OFFSET = {"令和": 2018, "平成": 1988, "昭和": 1925}
_ERA_RE = re.compile(r"(令和|平成|昭和)\s*(\d{1,2})")


def normalize_jp_date(text: str) -> Optional[str]:
    """日本語日付 → YYYY-MM-DD"""
    s = normalize_jp_number(text)

    era_m = _ERA_RE.search(s)
    if era_m:
        era_name = era_m.group(1)
        era_year = int(era_m.group(2))
        era_offset = _ERA_OFFSET.get(era_name, 0)
        western = era_offset + era_year
        rest_start = era_m.end()
        if rest_start < len(s) and s[rest_start] == "年":
            rest_start += 1
        s = s[:era_m.start()] + str(western) + "年" + s[rest_start:]

    m = _JP_DATE_RE.search(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    m = _SLASH_DATE_RE.search(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    return None


# ============================================================
# 期間正規化
# ============================================================
_PERIOD_JI_RE = re.compile(
    r"自\s*(.{8,20}?)\s*至\s*(.{8,20})",
)
_PERIOD_KARA_RE = re.compile(
    r"(.{8,30}?)(?:から|～|〜|-)(.{8,30}?)(?:まで|$)",
)


def normalize_period(text: str) -> tuple[Optional[str], Optional[str]]:
    """期間テキスト → (start_date, end_date)"""
    m = _PERIOD_JI_RE.search(text)
    if m:
        start = normalize_jp_date(m.group(1))
        end = normalize_jp_date(m.group(2))
        if start and end:
            return start, end

    m = _PERIOD_KARA_RE.search(text)
    if m:
        start = normalize_jp_date(m.group(1))
        end = normalize_jp_date(m.group(2))
        if start and end:
            return start, end

    # fallback
    m = _PERIOD_JI_RE.search(text)
    if m:
        start = normalize_jp_date(m.group(1))
        end = normalize_jp_date(m.group(2))
        if start or end:
            return start, end
    m = _PERIOD_KARA_RE.search(text)
    if m:
        start = normalize_jp_date(m.group(1))
        end = normalize_jp_date(m.group(2))
        if start or end:
            return start, end

    return None, None


# ============================================================
# 比率正規化
# ============================================================
_PERCENT_RE = re.compile(r"([\d.]+)\s*[%％]")


def normalize_percent(text: str) -> Optional[float]:
    """パーセント値を抽出。2.35% → 2.35"""
    s = normalize_jp_number(text)
    m = _PERCENT_RE.search(s)
    if m:
        try:
            return round(float(m.group(1)), 4)
        except ValueError:
            pass
    return None


# ============================================================
# テキストハッシュ
# ============================================================
def compute_text_hash(text: str) -> str:
    """テキストの SHA-256 先頭16文字"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ============================================================
# アンカーベース値抽出ヘルパー
# ============================================================
def find_near_anchor(text: str, anchors: list[str], window: int = 200) -> str:
    """アンカーキーワードの近傍テキストを取得"""
    for anchor in anchors:
        idx = text.find(anchor)
        if idx >= 0:
            start = idx
            end = min(len(text), idx + len(anchor) + window)
            return text[start:end]
    return ""


# ============================================================
# 数値パーサ（整数・浮動小数）
# ============================================================
_NUMBER_RE = re.compile(r"[-+]?[\d,.]+")


def parse_number(text: str) -> Optional[float]:
    """テキストから最初の数値を抽出"""
    s = normalize_jp_number(text)
    m = _NUMBER_RE.search(s)
    if m:
        try:
            return float(m.group(0).replace(",", ""))
        except ValueError:
            pass
    return None


# ============================================================
# Fingerprint生成
# ============================================================
def compute_fingerprint(*parts: str) -> str:
    """複数文字列パーツからfingerprintを生成する。
    
    event_typeごとの正規化済み主要値を入力として使う。
    """
    combined = "|".join(str(p) for p in parts)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:32]
