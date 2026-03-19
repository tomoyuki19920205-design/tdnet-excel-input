# ============================================================
# scoring.py — 抽出エンジン スコアリングユーティリティ
# ============================================================
"""
ルールベース + スコアリング方式の汎用ユーティリティ。

設計思想:
  - 「当たり / 外れ」ではなく「候補を集めて score で選ぶ」
  - 各判定は SemanticScore を返し、説明可能性を担保
  - 閾値はモジュール定数として定義（チューニング容易）
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


# ============================================================
# SemanticScore — 全スコアの基底
# ============================================================

@dataclass
class SemanticScore:
    """
    意味判定のスコア結果。

    Attributes:
        role: 判定されたロール ("sales", "profit", "toc", "skip" 等)
        score: 0.0 〜 1.0 のスコア
        matched_keyword: マッチしたキーワード
        reason: スコアの理由 (説明可能性)
    """
    role: str = ""
    score: float = 0.0
    matched_keyword: str = ""
    reason: str = ""


# ============================================================
# テキスト正規化 (共通)
# ============================================================

def normalize_text(text: str) -> str:
    """
    テキストの汎用正規化。
    - NFKC正規化 (全半角統一)
    - 空白除去
    """
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r'\s+', '', text)
    return text


# ============================================================
# 目次判定 (TOC Detection)
# ============================================================

_TOC_INDICATORS = ["…", "・・", "───", "─────", "......", "----"]
_TOC_KEYWORDS = ["目次", "目　次", "もくじ", "INDEX", "Contents"]


def score_toc_line(text: str) -> SemanticScore:
    """
    テキスト行が目次行かどうかを判定するスコア。

    Returns:
        SemanticScore (role="toc", score=0.0〜1.0)
    """
    score = 0.0
    reasons = []

    normalized = normalize_text(text)

    # 目次キーワード
    for kw in _TOC_KEYWORDS:
        nkw = normalize_text(kw)
        if nkw in normalized:
            score += 0.5
            reasons.append(f"目次キーワード '{kw}'")
            break

    # ドットリーダー / ダッシュ
    for ind in _TOC_INDICATORS:
        if ind in text:
            score += 0.4
            reasons.append(f"目次インジケータ '{ind}'")
            break

    # ページ番号パターン (末尾に数字)
    if re.search(r'\d{1,3}\s*$', text.strip()):
        score += 0.1
        reasons.append("末尾ページ番号")

    score = min(score, 1.0)
    matched = reasons[0] if reasons else ""

    return SemanticScore(
        role="toc",
        score=score,
        matched_keyword=matched,
        reason="; ".join(reasons),
    )


def is_toc_line(text: str, threshold: float = 0.3) -> bool:
    """目次行判定 (閾値ベース)。既存ロジックの代替として利用可能。"""
    return score_toc_line(text).score >= threshold


# ============================================================
# 行ロール判定 (Row Role Detection)
# ============================================================

# スキップ行キーワード (完全一致)
_SKIP_EXACT = {
    "合計", "総計", "計", "調整額", "消去", "消去又は全社",
    "全社", "配賦不能", "セグメント間", "内部取引",
}

# スキップ行キーワード (部分一致)
_SKIP_PARTIAL = ["合計", "調整", "消去", "全社", "配賦不能", "セグメント間", "内部取引"]

# 合計行キーワード
_TOTAL_KEYWORDS = ["合計", "総計", "計", "Total", "TOTAL"]


def score_row_role(text: str) -> dict[str, float]:
    """
    行テキストのロールをスコアリング。

    Returns:
        {"segment_name": float, "total": float, "adjustment": float, "skip": float}
    """
    stripped = text.strip()
    normalized = normalize_text(stripped)
    scores: dict[str, float] = {
        "segment_name": 0.0,
        "total": 0.0,
        "adjustment": 0.0,
        "skip": 0.0,
    }

    # --- 合計行判定 ---
    for kw in _TOTAL_KEYWORDS:
        nkw = normalize_text(kw)
        if nkw in normalized:
            scores["total"] = 0.9
            scores["skip"] = 0.8
            return scores

    # --- スキップ行 (完全一致) ---
    if stripped in _SKIP_EXACT:
        scores["skip"] = 1.0
        if "調整" in stripped:
            scores["adjustment"] = 0.9
        return scores

    # --- スキップ行 (部分一致、短い行のみ) ---
    for p in _SKIP_PARTIAL:
        if p in stripped and len(stripped) <= len(p) + 6:
            scores["skip"] = 0.7
            if "調整" in p:
                scores["adjustment"] = 0.8
            return scores

    # --- セグメント名候補 (スキップでない + 適度な長さ) ---
    if 2 <= len(stripped) <= 30:
        scores["segment_name"] = 0.6

    return scores


# ============================================================
# セグメント表スコア
# ============================================================

_SEGMENT_TABLE_KEYWORDS = [
    "報告セグメント", "事業セグメント", "セグメント情報",
    "セグメント別", "事業別", "部門別",
]


def score_table_candidate(
    lines: list[str],
    start: int,
    end: int,
    table_type: str = "segment",
) -> SemanticScore:
    """
    行範囲がセグメント表 / 受注表候補かをスコアリング。

    Args:
        lines: テキスト全行
        start: 開始行index
        end: 終了行index
        table_type: "segment" or "order"
    """
    score = 0.0
    reasons = []
    region = lines[start:end]
    region_text = "\n".join(region)

    if table_type == "segment":
        # セグメント表キーワード
        for kw in _SEGMENT_TABLE_KEYWORDS:
            if kw in region_text:
                score += 0.3
                reasons.append(f"セグメントKW '{kw}'")
                break

        # 売上・利益列ヘッダー
        if any(kw in region_text for kw in ["売上", "収益", "Sales", "Revenue"]):
            score += 0.2
            reasons.append("売上系ヘッダあり")
        if any(kw in region_text for kw in ["利益", "損益", "Profit", "Income"]):
            score += 0.2
            reasons.append("利益系ヘッダあり")

        # 数値密度
        num_count = sum(
            1 for line in region
            if re.search(r'[\d,]{3,}', line)
        )
        if num_count >= 3:
            score += 0.2
            reasons.append(f"数値行{num_count}行")

        # 目次減点
        toc_count = sum(1 for line in region if is_toc_line(line))
        if toc_count >= 2:
            score -= 0.3
            reasons.append(f"目次行{toc_count}行（減点）")

    elif table_type == "order":
        order_kws = ["受注高", "受注残", "受注額", "繰越工事", "手持工事"]
        for kw in order_kws:
            if kw in region_text:
                score += 0.3
                reasons.append(f"受注KW '{kw}'")

    score = max(0.0, min(score, 1.0))
    return SemanticScore(
        role=table_type,
        score=score,
        matched_keyword=reasons[0] if reasons else "",
        reason="; ".join(reasons),
    )
