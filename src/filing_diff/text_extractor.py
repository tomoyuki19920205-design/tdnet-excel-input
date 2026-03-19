#!/usr/bin/env python3
# ============================================================
# text_extractor.py — PDF本文抽出 + 見出し分割
# ============================================================
"""
決算短信PDFから全ページのテキストを抽出し、
定性情報セクション（経営成績の概況等）を見出し単位に分割する。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import pdfplumber

logger = logging.getLogger("filing_diff")


@dataclass
class Section:
    """分割されたセクション"""
    section_name_raw: str
    section_name_normalized: str
    section_order: int
    section_text: str


@dataclass
class ExtractedTextResult:
    """本文抽出結果"""
    raw_text: str
    cleaned_text: str
    sections: list[Section]
    source_type: str          # "pdf"
    extraction_status: str    # "ok" / "empty" / "error"
    extraction_note: str = ""


# ================================================================
# 見出し正規化マッピング
# ================================================================

_SECTION_PATTERNS: list[tuple[str, list[str]]] = [
    ("operating_results", [
        "経営成績に関する説明",
        "経営成績の概況",
        "当四半期の経営成績の概況",
        "当四半期決算に関する定性的情報",
        "当四半期の業績の概況",
        "連結業績の概況",
        "業績の概況",
        "経営成績の概要",
        "経営成績についての分析",
        "経営成績等の概況",
        "当期の経営成績の概況",
    ]),
    ("financial_position", [
        "財政状態に関する説明",
        "財政状態の分析",
        "財政状態",
        "資産、負債及び純資産の状況",
    ]),
    ("cash_flow", [
        "キャッシュ・フローに関する説明",
        "キャッシュ・フローの状況",
        "キャッシュフローの状況",
        "キャッシュ・フロー",
    ]),
    ("guidance", [
        "業績予想に関する説明",
        "今後の見通し",
        "通期の見通し",
        "連結業績予想",
        "業績予想",
        "次期の見通し",
        "今後の経営方針",
    ]),
    ("going_concern", [
        "継続企業の前提",
        "ゴーイングコンサーン",
    ]),
    ("segment", [
        "セグメントの概況",
        "セグメント情報",
        "セグメントごとの経営成績",
        "セグメント別の業績",
        "事業セグメント",
    ]),
    ("significant_events", [
        "重要事象等",
        "重要な後発事象",
        "特記事項",
    ]),
]


def normalize_section_name(raw_heading: str) -> str:
    """見出しテキストを正規化されたセクション名に変換"""
    clean = raw_heading.strip()
    # 先頭の番号・記号を除去
    clean = re.sub(r"^[\d０-９]+[\.\．\)）]\s*", "", clean)
    clean = re.sub(r"^[（(]\d+[)）]\s*", "", clean)
    clean = re.sub(r"^[①②③④⑤⑥⑦⑧⑨⑩]\s*", "", clean)

    for normalized, patterns in _SECTION_PATTERNS:
        for p in patterns:
            if p in clean:
                return normalized
    return "other"


# ================================================================
# PDF テキスト抽出
# ================================================================

def extract_full_text_from_pdf(pdf_path: str) -> str:
    """PDFの全ページからテキストを抽出"""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            parts: list[str] = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    parts.append(text)
            return "\n".join(parts)
    except Exception as e:
        logger.error(f"[TEXT] PDF読み込みエラー: {e}")
        return ""


def clean_text(raw: str) -> str:
    """抽出テキストのクリーニング"""
    text = raw
    # 連続空白を1つに
    text = re.sub(r"[ \t]+", " ", text)
    # 3つ以上の連続改行を2つに
    text = re.sub(r"\n{3,}", "\n\n", text)
    # ページヘッダ/フッタ的なパターン除去
    text = re.sub(r"^[\-－―]+\s*\d+\s*[\-－―]+\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*-\s*\d+\s*-\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


# ================================================================
# 見出し分割
# ================================================================

# 見出し行の検出パターン
_HEADING_RE = re.compile(
    r"^[\d０-９]*[\.\．\)）]?\s*"
    r"[（(]?\d*[)）]?\s*"
    r"("
    + "|".join(
        re.escape(p)
        for _, patterns in _SECTION_PATTERNS
        for p in patterns
    )
    + r")"
)


def split_into_sections(cleaned_text: str) -> list[Section]:
    """
    クリーニング済みテキストを見出し単位に分割する。

    見出し行を検出し、次の見出しまでのテキストを1セクションとする。
    """
    lines = cleaned_text.split("\n")
    sections: list[Section] = []
    current_heading: str | None = None
    current_lines: list[str] = []
    order = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            current_lines.append("")
            continue

        # 見出し行判定
        matched = _match_heading(stripped)
        if matched:
            # 前のセクションを保存
            if current_heading is not None:
                section_text = "\n".join(current_lines).strip()
                if section_text:
                    sections.append(Section(
                        section_name_raw=current_heading,
                        section_name_normalized=normalize_section_name(current_heading),
                        section_order=order,
                        section_text=section_text,
                    ))
                    order += 1
            current_heading = matched
            current_lines = []
        else:
            current_lines.append(line)

    # 最後のセクション
    if current_heading is not None:
        section_text = "\n".join(current_lines).strip()
        if section_text:
            sections.append(Section(
                section_name_raw=current_heading,
                section_name_normalized=normalize_section_name(current_heading),
                section_order=order,
                section_text=section_text,
            ))

    return sections


def _match_heading(line: str) -> str | None:
    """
    行が見出しパターンに一致するか判定。一致すれば見出し名を返す。

    見出し行の基準:
    - パターンキーワードを含む
    - 行が短い（80文字未満）
    - パターンが行の大部分を占める（50%以上）
      → 「通期の見通しは据え置きます。」のような本文行を除外
    """
    stripped = line.strip()
    if len(stripped) > 80:
        return None
    for _, patterns in _SECTION_PATTERNS:
        for p in patterns:
            if p in stripped:
                # パターンが行の大部分を占めるか確認
                # 見出し行は通常 "1. 経営成績に関する説明" のように短い
                ratio = len(p) / max(len(stripped), 1)
                if ratio >= 0.5:
                    return line
    return None


# ================================================================
# エントリポイント
# ================================================================

def extract_disclosure_text(pdf_path: str) -> ExtractedTextResult:
    """
    PDFファイルから本文を抽出し、セクション分割まで行う。
    """
    raw = extract_full_text_from_pdf(pdf_path)
    if not raw.strip():
        return ExtractedTextResult(
            raw_text="",
            cleaned_text="",
            sections=[],
            source_type="pdf",
            extraction_status="empty",
            extraction_note="テキスト抽出不可",
        )

    cleaned = clean_text(raw)
    sections = split_into_sections(cleaned)

    return ExtractedTextResult(
        raw_text=raw,
        cleaned_text=cleaned,
        sections=sections,
        source_type="pdf",
        extraction_status="ok",
        extraction_note=f"{len(sections)} sections found",
    )
