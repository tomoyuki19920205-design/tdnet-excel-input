#!/usr/bin/env python3
# ============================================================
# section_diff.py — セクション差分抽出 + キーワードタグ
# ============================================================
"""
同一 normalized section 同士を文単位で比較し、
added / removed / changed の差分候補を抽出する。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher


@dataclass
class DiffResult:
    """差分抽出結果"""
    section_name: str
    added_sentences: list[str] = field(default_factory=list)
    removed_sentences: list[str] = field(default_factory=list)
    changed_pairs: list[tuple[str, str]] = field(default_factory=list)
    unchanged_count: int = 0
    diff_score: float = 0.0   # 0.0=同一, 1.0=完全変化
    keywords: list[str] = field(default_factory=list)


# ================================================================
# 日本語文分割
# ================================================================

def split_into_sentences_ja(text: str) -> list[str]:
    """日本語テキストを文単位に分割"""
    # 「。」で分割し、空文を除外
    raw = re.split(r"(?<=。)", text)
    sentences: list[str] = []
    for s in raw:
        s = s.strip()
        if s and len(s) > 5:  # 極端に短い断片は除外
            sentences.append(s)
    return sentences


# ================================================================
# ノイズ除去ヘルパー
# ================================================================

def _normalize_for_compare(text: str) -> str:
    """比較用にテキストを正規化（数値・日付・句読点差を吸収）"""
    t = text
    # 日付パターンを先に置換（数値置換より前）
    t = re.sub(r"(令和|平成)\d+年\d+月\d*日?期?", "DATE", t)
    t = re.sub(r"20\d{2}年\d+月\d*日?期?", "DATE", t)
    # 数値を一律 "NUM" に置換
    t = re.sub(r"[\d,\.]+", "NUM", t)
    # 全角半角統一
    t = t.replace("，", ",").replace("．", ".").replace("　", " ")
    # 句読点差
    t = t.replace("、", "").replace("。", "").replace(",", "").replace(".", "")
    # 連続空白
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ================================================================
# 差分計算
# ================================================================

_SIMILARITY_THRESHOLD = 0.6   # これ以上ならchanged扱い
_NOISE_THRESHOLD = 0.95       # これ以上なら同一扱い


def diff_sections(
    prev_text: str,
    curr_text: str,
    section_name: str = "",
) -> DiffResult:
    """
    前回/今回のセクションテキストを文単位で比較し差分を返す。
    """
    prev_sents = split_into_sentences_ja(prev_text)
    curr_sents = split_into_sentences_ja(curr_text)

    # 正規化版で比較
    prev_norm = [_normalize_for_compare(s) for s in prev_sents]
    curr_norm = [_normalize_for_compare(s) for s in curr_sents]

    matched_prev: set[int] = set()
    matched_curr: set[int] = set()
    changed_pairs: list[tuple[str, str]] = []
    unchanged = 0

    # 完全一致を先にマッチ
    for i, pn in enumerate(prev_norm):
        for j, cn in enumerate(curr_norm):
            if j in matched_curr:
                continue
            if pn == cn:
                matched_prev.add(i)
                matched_curr.add(j)
                unchanged += 1
                break

    # 類似マッチ（changed検出）
    for i, pn in enumerate(prev_norm):
        if i in matched_prev:
            continue
        best_j = -1
        best_ratio = 0.0
        for j, cn in enumerate(curr_norm):
            if j in matched_curr:
                continue
            ratio = SequenceMatcher(None, pn, cn).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_j = j

        if best_ratio >= _NOISE_THRESHOLD:
            # ほぼ同一 → 同一扱い
            matched_prev.add(i)
            matched_curr.add(best_j)
            unchanged += 1
        elif best_ratio >= _SIMILARITY_THRESHOLD:
            matched_prev.add(i)
            matched_curr.add(best_j)
            changed_pairs.append((prev_sents[i], curr_sents[best_j]))

    # 未マッチ → added / removed
    removed = [prev_sents[i] for i in range(len(prev_sents)) if i not in matched_prev]
    added = [curr_sents[j] for j in range(len(curr_sents)) if j not in matched_curr]

    total = max(len(prev_sents), len(curr_sents), 1)
    diff_score = 1.0 - (unchanged / total)

    # キーワードタグ付け
    all_diff_text = " ".join(added + removed + [p[1] for p in changed_pairs])
    keywords = tag_diff_keywords(all_diff_text)

    return DiffResult(
        section_name=section_name,
        added_sentences=added,
        removed_sentences=removed,
        changed_pairs=changed_pairs,
        unchanged_count=unchanged,
        diff_score=round(diff_score, 3),
        keywords=keywords,
    )


# ================================================================
# キーワードタグ付け
# ================================================================

_KEYWORD_CATEGORIES: list[str] = [
    "需要", "受注", "在庫調整", "価格改定",
    "円安", "円高", "為替", "原材料高", "原材料", "人件費",
    "減損", "設備投資", "研究開発",
    "米国関税", "関税", "中国需要", "中国",
    "半導体", "自動車", "生成AI", "AI",
    "不透明感", "先行き不透明", "不確実性",
    "回復", "低調", "堅調", "軟調",
    "想定を下回", "想定を上回", "上振れ", "下振れ",
    "据え置き", "上方修正", "下方修正",
    "増収", "減収", "増益", "減益",
    "構造改革", "事業再編", "合理化",
    "配当", "自社株買い",
]


def tag_diff_keywords(text: str) -> list[str]:
    """テキストに含まれる重要キーワードをタグ付け"""
    found: list[str] = []
    for kw in _KEYWORD_CATEGORIES:
        if kw in text:
            found.append(kw)
    return found
