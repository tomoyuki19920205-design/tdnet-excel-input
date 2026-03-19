# ============================================================
# page_scoring.py — Phase A: ページスコアリング (Phase 4 強化)
# ============================================================
"""
各PDFページに対して「セグメント表を含みそうか」を採点する。
上位スコアのページだけを後段の表解析に回す。

Phase 4 追加:
  - キーワード大幅追加 (カテゴリ別/用途別 etc.)
  - 減点強化 (従業員/キャッシュフロー/財政状態 etc.)
  - 行パターン加点 (地域名/業種名/数値テーブル構造)
  - page sequence boost (前後ページ連続性)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .scoring import SemanticScore, normalize_text, is_toc_line


# ============================================================
# スコア要素
# ============================================================

_SEGMENT_PAGE_KEYWORDS: list[tuple[str, float]] = [
    ("報告セグメント", 0.20),
    ("事業セグメント", 0.18),
    ("セグメント情報", 0.18),
    ("セグメント別", 0.15),
    ("事業別", 0.12),
    ("部門別", 0.10),
    ("報告セグメントごと", 0.18),
    ("地域別", 0.10),
    ("所在地別", 0.10),
    ("製品別", 0.10),
    ("セグメントに関する情報", 0.15),
    ("セグメント損益", 0.15),
    ("business segment", 0.12),
    ("segment information", 0.12),
    ("operating segments", 0.12),
    # Phase 3 追加
    ("reportable segments", 0.12),
    ("セグメントの概要", 0.15),
    ("セグメント別業績", 0.15),
    ("セグメント業績", 0.15),
    ("事業別業績", 0.12),
    ("部門別業績", 0.10),
    # Phase 4 追加
    ("カテゴリ別", 0.10),
    ("用途別", 0.10),
    ("事業ごと", 0.10),
    ("地域ごと", 0.10),
    ("製品ごと", 0.10),
    ("報告別", 0.08),
    ("segment results", 0.12),
    ("reportable segment", 0.12),
    ("business overview", 0.08),
    ("地域別業績", 0.10),
    ("製品別業績", 0.10),
    ("セグメント利益", 0.10),
    ("セグメント売上", 0.10),
]

_FINANCIAL_KEYWORDS: list[tuple[str, float]] = [
    ("売上高", 0.10),
    ("営業利益", 0.10),
    ("セグメント利益", 0.12),
    ("営業収益", 0.08),
    ("事業利益", 0.08),
    ("損益", 0.06),
    ("利益又は損失", 0.10),
    ("経常利益", 0.06),
    # Phase 4 追加
    ("売上収益", 0.10),
    ("コア営業利益", 0.08),
]

_DEDUCTION_KEYWORDS: list[tuple[str, float]] = [
    ("目次", -0.30),
    ("目　次", -0.30),
    ("Q&A", -0.20),
    ("設備投資", -0.10),
    ("株主還元", -0.15),
    ("配当", -0.10),
    ("自己株式", -0.10),
    # Phase 3 追加
    ("業績予想", -0.12),
    ("計画比", -0.08),
    ("注記事項", -0.08),
    ("会計方針", -0.10),
    ("継続企業の前提", -0.10),
    # Phase 4 追加
    ("配当予想", -0.10),
    ("従業員", -0.08),
    ("キャッシュフロー", -0.10),
    ("キャッシュ・フロー", -0.10),
    ("財政状態", -0.08),
    ("貸借対照表", -0.10),
    ("バランスシート", -0.10),
    # Phase 6 追加: PL/BS/CF ページ減点強化
    ("連結損益計算書", -0.25),
    ("四半期連結損益計算書", -0.25),
    ("連結貸借対照表", -0.15),
    ("経営成績の概況", -0.10),
    ("業績の概況", -0.08),
    ("連結損益及び包括利益計算書", -0.20),
]

# Phase 4: 行名に出現するとセグメント表の可能性が高い語
_REGION_NAMES = {
    "日本", "国内", "海外", "北米", "欧州", "アジア", "中国",
    "東南アジア", "米国", "アメリカ", "ヨーロッパ",
    "Japan", "Asia", "Europe", "Americas", "North America",
}

_INDUSTRY_NAMES = {
    "不動産", "建設", "小売", "物流", "住宅", "金融", "製造",
    "情報", "通信", "サービス", "エネルギー", "化学", "食品",
    "医薬", "機械", "電子", "自動車", "鉄鋼", "繊維",
}


@dataclass
class PageScore:
    """ページスコアリング結果"""
    page_no: int
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    reason: str = ""


def score_segment_page(
    page_text: str,
    page_no: int,
    *,
    tables: list | None = None,
) -> PageScore:
    """
    Phase A: ページテキストのセグメント表含有スコア。

    Args:
        page_text: ページの全テキスト
        page_no: ページ番号 (0-based)
        tables: pdfplumber tables (数表密度判定用、optional)

    Returns:
        PageScore
    """
    score = 0.0
    breakdown: dict[str, float] = {}
    reasons: list[str] = []

    normalized = normalize_text(page_text)

    # --- セグメント系キーワード ---
    for kw, sc in _SEGMENT_PAGE_KEYWORDS:
        nkw = normalize_text(kw)
        if nkw in normalized:
            score += sc
            breakdown[f"kw:{kw}"] = sc
            reasons.append(kw)

    # --- 財務キーワード ---
    for kw, sc in _FINANCIAL_KEYWORDS:
        nkw = normalize_text(kw)
        if nkw in normalized:
            score += sc
            breakdown[f"fin:{kw}"] = sc
            reasons.append(kw)

    # --- 減点 ---
    for kw, sc in _DEDUCTION_KEYWORDS:
        nkw = normalize_text(kw)
        if nkw in normalized:
            score += sc  # sc is negative
            breakdown[f"ded:{kw}"] = sc
            reasons.append(f"{kw}(減点)")

    # --- 減点: 目次行多い (TOC ページ強除外) ---
    lines = page_text.split("\n")
    toc_count = sum(1 for line in lines if is_toc_line(line))
    if toc_count >= 5:
        penalty = -0.60
        score += penalty
        breakdown["toc_lines"] = penalty
        reasons.append(f"目次行{toc_count}行(強)")
    elif toc_count >= 3:
        penalty = -0.40
        score += penalty
        breakdown["toc_lines"] = penalty
        reasons.append(f"目次行{toc_count}行")

    # --- 数値行カウント ---
    num_lines = sum(1 for line in lines if re.search(r'[\d,]{3,}', line))
    total_lines = max(len(lines), 1)
    num_density = num_lines / total_lines

    # --- 減点: 数表密度が低い ---
    if num_density < 0.1 and total_lines > 5:
        penalty = -0.10
        score += penalty
        breakdown["low_num_density"] = penalty
        reasons.append(f"数値密度低({num_density:.1%})")

    # --- 加点: 数表ありページ ---
    if tables and len(tables) > 0:
        bonus = 0.05 * min(len(tables), 3)
        score += bonus
        breakdown["tables_count"] = bonus
        reasons.append(f"表{len(tables)}個")

    # --- 加点: 調整額/全社/消去 (セグメント表特有) ---
    for kw in ["調整額", "全社", "消去"]:
        if kw in page_text:
            score += 0.05
            breakdown[f"seg_adj:{kw}"] = 0.05
            reasons.append(kw)
            break  # 1回のみ

    # --- 加点: 売上+利益キーワード共存 ---
    has_sales_kw = any(
        normalize_text(kw) in normalized
        for kw, _ in _FINANCIAL_KEYWORDS
        if "売上" in kw or "収益" in kw
    )
    has_profit_kw = any(
        normalize_text(kw) in normalized
        for kw, _ in _FINANCIAL_KEYWORDS
        if "利益" in kw or "損益" in kw
    )
    if has_sales_kw and has_profit_kw:
        bonus = 0.08
        score += bonus
        breakdown["sales_profit_coexist"] = bonus
        reasons.append("売上+利益共存")

    # --- 加点: 数値行が多い (セグメント表らしさ) ---
    if num_lines >= 3:
        bonus = min(0.10, num_lines * 0.02)
        score += bonus
        breakdown["num_rows"] = bonus
        reasons.append(f"数値行{num_lines}行")

    # --- Phase 4: 行パターン分析 ---
    _EXCLUDE_ROW_NAMES = {"その他", "調整額", "合計", "計", "全社", "消去"}
    segment_like_rows = 0
    region_hits = 0
    industry_hits = 0
    multi_num_col_rows = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # セグメント名らしい行 (数値で始まらない)
        if not re.match(r'^[\d,△▲\-]', stripped):
            first_word = re.split(r'\s{2,}|\t', stripped)[0].strip()
            if first_word and first_word not in _EXCLUDE_ROW_NAMES and len(first_word) <= 20:
                segment_like_rows += 1
            # 地域名/業種名チェック
            for rname in _REGION_NAMES:
                if rname in stripped:
                    region_hits += 1
                    break
            for iname in _INDUSTRY_NAMES:
                if iname in stripped:
                    industry_hits += 1
                    break

        # 複数数値列パターン (2つ以上の数値トークンが並ぶ行)
        num_tokens = re.findall(r'[△▲\-]?[\d,]+(?:\.\d+)?', stripped)
        if len(num_tokens) >= 2:
            multi_num_col_rows += 1

    # セグメント名行加点
    if segment_like_rows >= 3:
        bonus = min(0.08, segment_like_rows * 0.02)
        score += bonus
        breakdown["segment_name_rows"] = bonus
        reasons.append(f"セグメント名{segment_like_rows}行")

    # 地域名加点
    if region_hits >= 2:
        bonus = min(0.10, region_hits * 0.03)
        score += bonus
        breakdown["region_names"] = bonus
        reasons.append(f"地域名{region_hits}件")

    # 業種名加点
    if industry_hits >= 2:
        bonus = min(0.08, industry_hits * 0.02)
        score += bonus
        breakdown["industry_names"] = bonus
        reasons.append(f"業種名{industry_hits}件")

    # 複数列数値テーブル構造加点
    if multi_num_col_rows >= 3:
        bonus = min(0.12, multi_num_col_rows * 0.02)
        score += bonus
        breakdown["multi_num_table"] = bonus
        reasons.append(f"数値テーブル{multi_num_col_rows}行")

    score = max(0.0, min(score, 1.0))

    return PageScore(
        page_no=page_no,
        score=score,
        score_breakdown=breakdown,
        reason="; ".join(reasons),
    )


# ============================================================
# page sequence boost
# ============================================================

def apply_sequence_boost(
    page_scores: list[PageScore],
    *,
    boost_1: float = 0.06,
    boost_2: float = 0.03,
) -> list[PageScore]:
    """
    前後ページの連続性を考慮してスコアを補正する。

    隣接ページ (±1) に候補がある場合は boost_1 を加点、
    ±2 ページなら boost_2 を加点。

    Args:
        page_scores: 全ページスコア (page_no 順)
        boost_1: ±1 ページ加点量
        boost_2: ±2 ページ加点量

    Returns:
        補正後のスコアに更新された page_scores (同一リスト)
    """
    if len(page_scores) < 2:
        return page_scores

    n = len(page_scores)
    raw_scores = [ps.score for ps in page_scores]

    for i in range(n):
        adj_boost = 0.0
        adj_reasons: list[str] = []

        # ±1 ページ
        for offset in [-1, 1]:
            j = i + offset
            if 0 <= j < n and raw_scores[j] > 0.05:
                contrib = min(raw_scores[j] * 0.4, boost_1)
                adj_boost += contrib
                adj_reasons.append(f"pg{page_scores[j].page_no}±1")

        # ±2 ページ
        for offset in [-2, 2]:
            j = i + offset
            if 0 <= j < n and raw_scores[j] > 0.10:
                contrib = min(raw_scores[j] * 0.2, boost_2)
                adj_boost += contrib
                adj_reasons.append(f"pg{page_scores[j].page_no}±2")

        if adj_boost > 0:
            page_scores[i].score = min(1.0, page_scores[i].score + adj_boost)
            page_scores[i].score_breakdown["sequence_boost"] = adj_boost
            if adj_reasons:
                page_scores[i].reason += f"; seq({','.join(adj_reasons)})"

    return page_scores


def rank_candidate_pages(
    page_scores: list[PageScore],
    top_n: int = 5,
    min_score: float = 0.15,
) -> list[PageScore]:
    """
    候補ページを上位Nページに絞る。

    Args:
        page_scores: 全ページのスコア
        top_n: 上位何ページまで
        min_score: 最低スコア

    Returns:
        スコア降順の候補ページリスト
    """
    filtered = [p for p in page_scores if p.score >= min_score]
    filtered.sort(key=lambda p: p.score, reverse=True)
    return filtered[:top_n]
