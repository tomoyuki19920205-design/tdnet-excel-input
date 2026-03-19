# ============================================================
# segment_name_normalizer.py — セグメント名正規化
# ============================================================
"""
抽出したセグメント名を raw と normalized に分離する。
表記ゆれ (事業/部門/関連/セグメント、英語、空白) を吸収。

設計:
  Phase 1: 基本文字整形
  Phase 2: 同義語辞書
  Phase 3: ticker別alias (将来hook)
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


# ============================================================
# SegmentNameNormalizationResult
# ============================================================

@dataclass
class SegmentNameNormalizationResult:
    """セグメント名正規化の結果"""
    raw_name: str = ""
    normalized_name: str = ""
    normalize_rule: str | None = None
    confidence: float = 1.0


# ============================================================
# Phase 1: 基本文字整形
# ============================================================

# 末尾で除去する接尾辞 (長い順に並べて貪欲マッチ)
_SUFFIX_REMOVE = [
    "セグメント",
    "事業部門",
    "関連事業",
    "事業",
    "部門",
    "関連",
]

# 注記的パターン (除去対象)
_NOTE_PATTERNS = [
    re.compile(r'[※＊\*]\s*\d*$'),      # ※1, *2
    re.compile(r'\s*（注\d*）$'),          # （注1）
    re.compile(r'\s*\(注\d*\)$'),          # (注1)
]


def _normalize_chars(name: str) -> str:
    """基本文字整形"""
    # NFKC正規化
    name = unicodedata.normalize("NFKC", name)
    # 改行→空白
    name = name.replace("\n", " ").replace("\r", " ")
    # 連続空白圧縮
    name = re.sub(r'\s+', ' ', name)
    # strip
    name = name.strip()
    return name


def _remove_note_markers(name: str) -> str:
    """注記記号を除去"""
    for pat in _NOTE_PATTERNS:
        name = pat.sub("", name)
    return name.strip()


def _remove_suffix(name: str) -> tuple[str, str | None]:
    """末尾の事業/部門/関連/セグメントを除去"""
    for suffix in _SUFFIX_REMOVE:
        if name.endswith(suffix) and len(name) > len(suffix):
            stripped = name[:-len(suffix)].strip()
            if stripped:  # 空にならないこと
                return stripped, f"suffix_remove:{suffix}"
    return name, None


# ============================================================
# Phase 2: 同義語辞書
# ============================================================

# (aliases, canonical) — 最初にマッチした canonical に正規化
_SYNONYM_MAP: list[tuple[list[str], str]] = [
    (["automotive", "オートモーティブ", "車載", "自動車関連", "自動車"], "自動車"),
    (["electronics", "エレクトロニクス", "電子"], "電子"),
    (["housing", "ハウジング", "住宅"], "住宅"),
    (["industrial materials", "素材", "マテリアル"], "素材"),
    (["infrastructure", "インフラストラクチャー", "インフラ", "社会インフラ"], "社会インフラ"),
    (["it services", "itサービス", "情報システム", "システム"], "ITサービス"),
    (["real estate", "不動産"], "不動産"),
    (["construction", "建設", "建築"], "建設"),
    (["chemicals", "ケミカル", "化学"], "化学"),
    (["energy", "エネルギー"], "エネルギー"),
    (["logistics", "ロジスティクス", "物流"], "物流"),
    (["retail", "リテール", "小売"], "小売"),
    (["financial services", "ファイナンシャルサービス", "金融"], "金融"),
    (["healthcare", "ヘルスケア", "医療"], "医療"),
]


def _apply_synonyms(name: str) -> tuple[str, str | None]:
    """同義語辞書で正規化"""
    name_lower = name.lower().strip()
    for aliases, canonical in _SYNONYM_MAP:
        for alias in aliases:
            if name_lower == alias.lower():
                return canonical, f"synonym:{alias}→{canonical}"
    return name, None


# ============================================================
# 公開関数
# ============================================================

def normalize_segment_name(
    name: str,
    ticker: str | None = None,
    alias_map: dict[str, str] | None = None,
) -> SegmentNameNormalizationResult:
    """
    セグメント名を正規化する。

    Args:
        name: 生のセグメント名
        ticker: 企業コード (将来の company-specific alias 用)
        alias_map: ユーザー定義の alias dict (将来拡張用)
    """
    raw = name
    rules: list[str] = []
    conf = 1.0

    # Phase 1: 基本文字整形
    normalized = _normalize_chars(name)
    if normalized != raw.strip():
        rules.append("char_normalize")

    # 注記記号除去
    cleaned = _remove_note_markers(normalized)
    if cleaned != normalized:
        rules.append("note_remove")
        normalized = cleaned

    # 末尾接尾辞除去
    stripped, rule = _remove_suffix(normalized)
    if rule:
        rules.append(rule)
        normalized = stripped

    # Phase 2: 同義語辞書
    synced, rule = _apply_synonyms(normalized)
    if rule:
        rules.append(rule)
        normalized = synced

    # Phase 3: ticker-specific alias (将来hook)
    if alias_map and normalized in alias_map:
        prev = normalized
        normalized = alias_map[normalized]
        rules.append(f"alias:{prev}→{normalized}")

    # 空にならない保証
    if not normalized:
        normalized = raw.strip() or "UNKNOWN"
        conf = 0.3

    return SegmentNameNormalizationResult(
        raw_name=raw,
        normalized_name=normalized,
        normalize_rule="; ".join(rules) if rules else None,
        confidence=conf,
    )


def normalize_segment_names(
    names: list[str],
    ticker: str | None = None,
) -> list[SegmentNameNormalizationResult]:
    """複数セグメント名を一括正規化"""
    return [normalize_segment_name(n, ticker) for n in names]
