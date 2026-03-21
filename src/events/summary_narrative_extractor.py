#!/usr/bin/env python3
"""summary_narrative_extractor.py — 経営成績の概況から増減理由を抽出

PDF/HTML の「経営成績の概況」セクションからキーワード後の増減理由テキストを切り出す。
AIは箇条書き整形のみに使用し、数値生成は禁止。

抽出ルール:
  1. 「経営成績」「業績の概況」等のセクション特定
  2. 冒頭マクロ環境説明はスキップ
  3. キーワード以降を優先抽出:
     - このような環境下 / このような状況下 / この結果 / その結果
  4. 増減理由が書かれた文のみ切り出し
  5. セグメント別の説明文も同様に抽出
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("summary_narrative")

# ============================================================
# データモデル
# ============================================================

@dataclass
class NarrativeData:
    """抽出された増減理由テキスト"""
    company_reason: str = ""              # 全社の増減理由テキスト
    segment_reasons: dict[str, str] = field(default_factory=dict)  # {セグメント名: 理由}
    raw_section: str = ""                 # 生の経営概況セクション（デバッグ用）

    @property
    def has_reason(self) -> bool:
        return bool(self.company_reason.strip())


# ============================================================
# セクション検出
# ============================================================

# 経営成績の概況セクションを示すキーワード
_SECTION_START_PATTERNS = [
    re.compile(r"(?:１|1)[.．、\s]*経営成績"),
    re.compile(r"経営成績(?:等)?の概況"),
    re.compile(r"経営成績に関する説明"),
    re.compile(r"業績の概況"),
    re.compile(r"連結業績の概況"),
    re.compile(r"当(?:四半期|期)の概況"),
]

# セクション終了を示すパターン
_SECTION_END_PATTERNS = [
    re.compile(r"(?:２|2)[.．、\s]*財政状態"),
    re.compile(r"財政状態(?:の概況|に関する)"),
    re.compile(r"(?:２|2)[.．、\s]*(?:今後|通期|次期)"),
    re.compile(r"(?:３|3)[.．、\s]*"),
    re.compile(r"キャッシュ・フロー"),
    re.compile(r"配当の状況"),
]

# セグメントセクション検出
_SEGMENT_SECTION_PATTERNS = [
    re.compile(r"セグメント(?:別?の|ごとの)?(?:業績|概況|状況)"),
    re.compile(r"事業(?:別|の)(?:概況|状況)"),
    re.compile(r"セグメント情報"),
    re.compile(r"報告セグメント"),
]

# ============================================================
# キーワード抽出
# ============================================================

# 増減理由が始まるキーワード（冒頭マクロ環境説明後）
_REASON_TRIGGER_KEYWORDS = [
    "このような環境下",
    "このような状況下",
    "このような経営環境",
    "このような事業環境",
    "こうした環境下",
    "こうした状況下",
    "この結果",
    "その結果",
    "以上の結果",
    "これらの結果",
    "これにより",
    "この結果当",  # "この結果当四半期" のパターン
]

# 不要な定型文パターン（削除対象）
_BOILERPLATE_PATTERNS = [
    re.compile(r"当社グループは.*?に取り組んで(?:まいりました|おります|きました)。?"),
    re.compile(r"引き続き.*?に努めて(?:まいります|おります)。?"),
    re.compile(r"今後(?:とも|につきましても).*?(?:努めて|推進して)(?:まいります|まいる所存です)。?"),
]

# 除外すべき文に含まれるキーワード
_EXCLUDE_KEYWORDS = [
    "1株当たり", "１株当たり", "1口当たり", "１口当たり",
    "（注）", "（注１）", "（注２）", "（注1）", "（注2）",
    "(注)", "(注1)", "(注2)", "注1）", "注2）",
    "顧客所在地", "所在地で", "に分類",
    "償却期間", "のれん償却額",
    "物件数は", "拠点数は", "件数は",
]

# 優先的に抽出すべき増減要因キーワード（スコアリング用）
_PRIORITY_KEYWORDS = [
    "好調", "堅調", "伸長", "拡大", "増加", "増収", "増益",
    "低下", "減少", "悪化", "減収", "減益",
    "改善", "回復",
    "販売", "受注", "稼働", "出荷",
    "原価", "コスト", "原材料",
    "価格改定", "値上げ", "価格転嫁",
    "為替", "円安", "円高",
    "需要", "市場", "競争",
    "販促", "広告", "販管費",
    "新規連結", "M&A", "買収",
    "減損", "一過性", "特損", "特益",
    "黒字転換", "赤字転落",
    "利益率", "採算", "効率",
]


def _find_section(text: str, patterns: list[re.Pattern]) -> int:
    """テキスト中でパターンに最初にマッチする位置を返す（-1 = 未検出）"""
    for pattern in patterns:
        m = pattern.search(text)
        if m:
            return m.start()
    return -1


def _extract_overview_section(text: str) -> str:
    """経営成績の概況セクションを切り出す（目次をスキップ）"""
    # 全てのマッチ位置を収集
    all_starts = []
    for pattern in _SECTION_START_PATTERNS:
        for m in pattern.finditer(text):
            all_starts.append(m.start())
    
    if not all_starts:
        return ""
    
    all_starts.sort()
    
    # 目次判定: セクション開始後50文字以内に「…」や「─」が含まれる場合は目次
    def _is_toc_entry(pos: int) -> bool:
        snippet = text[pos:pos + 80]
        return bool(re.search(r"[…‥─━]+|……+|\.{3,}", snippet))
    
    # 目次でない最初のセクション開始を採用
    start = -1
    for pos in all_starts:
        if not _is_toc_entry(pos):
            start = pos
            break
    
    if start < 0:
        # 全て目次だった場合: 最後のマッチを採用（本文は通常後半にある）
        start = all_starts[-1]
    
    remaining = text[start:]

    # セクション終了位置（先頭200文字をスキップしてから検索）
    end = _find_section(remaining[200:], _SECTION_END_PATTERNS)
    if end >= 0:
        remaining = remaining[:end + 200]

    # 最大5000文字（セグメント説明も含むように拡大）
    return remaining[:5000]


def _should_exclude_sentence(sent: str) -> bool:
    """除外すべき文かどうか判定"""
    if any(kw in sent for kw in _EXCLUDE_KEYWORDS):
        return True
    # 金額だけの説明文を除外 (「◯◯は△△百万円」パターンで、要因キーワードなし)
    if re.search(r"\d{1,3}(?:,\d{3})+百万円", sent):
        if not any(kw in sent for kw in _PRIORITY_KEYWORDS):
            return True
    return False


def _score_sentence(sent: str) -> int:
    """増減要因としての優先度スコア（高いほど良い）"""
    score = 0
    for kw in _PRIORITY_KEYWORDS:
        if kw in sent:
            score += 1
    return score


def _filter_and_rank_sentences(text: str, max_sentences: int = 5) -> str:
    """文を分割→除外→スコア順で選別"""
    sentences = re.split(r"[。\n]", text)
    candidates = []
    for sent in sentences:
        sent = sent.strip()
        if not sent or len(sent) < 8:
            continue
        if _should_exclude_sentence(sent):
            continue
        score = _score_sentence(sent)
        candidates.append((score, sent))
    
    # スコア降順、同スコアなら出現順
    candidates.sort(key=lambda x: x[0], reverse=True)
    selected = [s for _, s in candidates[:max_sentences]]
    return "。".join(selected)


def _extract_reason_after_keyword(section_text: str) -> str:
    """キーワード以降の増減理由テキストを抽出"""
    best_pos = -1
    best_keyword = ""

    for keyword in _REASON_TRIGGER_KEYWORDS:
        pos = section_text.find(keyword)
        if pos >= 0:
            if best_pos < 0 or pos < best_pos:
                best_pos = pos
                best_keyword = keyword

    if best_pos < 0:
        # キーワードが見つからない場合、増減に関する文を直接探す
        return _extract_change_sentences(section_text)

    # キーワードの含まれる文の先頭から取得
    reason_text = section_text[best_pos:]

    # 最大1200文字に拡大（フィルタ前）
    reason_text = reason_text[:1200]

    # 不要定型文を除去
    for pattern in _BOILERPLATE_PATTERNS:
        reason_text = pattern.sub("", reason_text)

    # 文単位でフィルタ＆ランキング
    return _filter_and_rank_sentences(reason_text)


def _extract_change_sentences(text: str) -> str:
    """増減に関する文を直接探す（キーワードが見つからない場合のフォールバック）"""
    return _filter_and_rank_sentences(text, max_sentences=5)


# ============================================================
# セグメント理由抽出
# ============================================================

def _extract_segment_reasons(text: str) -> dict[str, str]:
    """セグメント別の増減理由を抽出"""
    seg_start = _find_section(text, _SEGMENT_SECTION_PATTERNS)
    if seg_start < 0:
        return {}

    seg_text = text[seg_start:]

    # セグメント名を検出（「◆ セグメント名」「（セグメント名）」「■ セグメント名」等）
    seg_patterns = [
        re.compile(r"[◆◇■□●○▶▷★☆①②③④⑤⑥⑦⑧⑨⑩]\s*(.+?)(?:\s*事業)?(?:\n|$)"),
        re.compile(r"[（(](.+?)(?:\s*事業)?[）)](?:\s*セグメント)?"),
        re.compile(r"[「『](.+?)(?:\s*事業)?[」』]"),
    ]

    segments: dict[str, str] = {}
    # 簡易パース: 段落ごとにセグメント名を検出して対応テキストを格納
    paragraphs = re.split(r"\n\s*\n", seg_text[:3000])

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # セグメント名の検出
        seg_name = None
        for pattern in seg_patterns:
            m = pattern.search(para[:100])  # 段落冒頭100文字から検索
            if m:
                seg_name = m.group(1).strip()
                break

        if seg_name and len(seg_name) < 30:
            # 増減理由の文を抽出
            reason = _extract_reason_after_keyword(para)
            if not reason:
                reason = _extract_change_sentences(para)
            if reason:
                segments[seg_name] = reason[:300]

    return segments


# ============================================================
# メインエントリ
# ============================================================

def extract_narrative(
    text: str,
    title: str = "",
) -> NarrativeData:
    """テキストから経営概況の増減理由を抽出する。

    Parameters
    ----------
    text : 開示文書のテキスト（PDFまたはHTMLから抽出済み）
    title : 開示タイトル

    Returns
    -------
    NarrativeData
    """
    result = NarrativeData()

    if not text or len(text) < 50:
        return result

    # 経営概況セクション切り出し
    section = _extract_overview_section(text)
    if not section:
        logger.info("[NARRATIVE] 経営成績の概況セクション未検出")
        return result

    result.raw_section = section

    # 全社増減理由
    company_reason = _extract_reason_after_keyword(section)
    result.company_reason = company_reason

    # セグメント理由
    result.segment_reasons = _extract_segment_reasons(text)

    if result.has_reason:
        logger.info(
            f"[NARRATIVE] 抽出成功: company_reason_len={len(result.company_reason)} "
            f"segments={list(result.segment_reasons.keys())}"
        )
    else:
        logger.info("[NARRATIVE] 増減理由のキーワードが見つかりませんでした")

    return result
