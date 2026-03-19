"""toc_detection.py — 短信 PDF の目次（TOC）ページ / テーブル候補を検出

TOC ページの特徴:
- 「（セグメント情報等の注記）…………… 10」のような行パターン
- 括弧 + 省略記号（……, ・・・, ....）+ 小整数（ページ番号）
- 大きな財務数値がない
- 注記キーワード（セグメント情報、重要な後発事象、等）が密集
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# ── regex patterns ──
# 括弧付き注記タイトル + dotted leader + ページ番号
_RE_TOC_PAREN = re.compile(
    r'^\s*[（(].+[)）]\s*[.…・\s]{2,}\s*(\d{1,3})\s*$'
)
# 注記タイトルらしいテキスト + dotted leader + ページ番号 (やや広め)
_RE_TOC_GENERAL = re.compile(
    r'^.{2,40}\s*[.…・]{3,}\s*(\d{1,3})\s*$'
)
# dotted leader (省略記号) を含む行
_RE_DOTTED_LEADER = re.compile(r'[.…・]{3,}')
# 行末の小整数
_RE_TRAILING_INT = re.compile(r'(\d{1,3})\s*$')

# ── TOC 注記キーワード ──
TOC_NOTE_KEYWORDS = [
    "セグメント情報",
    "継続企業の前提",
    "キャッシュ・フロー",
    "重要な後発事象",
    "株主資本",
    "収益認識",
    "注記事項",
    "注記",
    "企業結合",
    "偶発債務",
    "重要な会計方針",
    "連結損益",
    "連結貸借",
    "連結財務",
    "四半期連結",
    "追加情報",
    "補足情報",
    "1株当たり",
    "重要な契約",
    "金融商品",
    "税効果",
    "退職給付",
    "減損損失",
    "配当",
]


@dataclass
class TocLineResult:
    """1行の TOC 判定結果"""
    is_toc_line: bool = False
    is_dotted_leader: bool = False
    trailing_page_number: int | None = None
    has_note_keyword: bool = False


@dataclass
class TocPageResult:
    """ページ単位の TOC 判定結果"""
    is_toc_page: bool = False
    toc_line_count: int = 0
    toc_score: float = 0.0
    dotted_leader_count: int = 0
    page_number_like_count: int = 0
    note_keyword_count: int = 0
    has_mokuji_heading: bool = False
    large_financial_values: int = 0
    line_results: list[TocLineResult] = field(default_factory=list)


@dataclass
class TocCandidateResult:
    """candidate テーブル単位の TOC 判定結果"""
    is_toc_candidate: bool = False
    toc_line_count: int = 0
    toc_line_ratio: float = 0.0
    dotted_leader_count: int = 0
    page_number_like_count: int = 0
    total_rows: int = 0
    reject_reason: str = ""


def classify_toc_line(line: str) -> TocLineResult:
    """1行を TOC 行として判定する。"""
    result = TocLineResult()
    stripped = line.strip()
    if not stripped:
        return result

    # dotted leader 検出
    if _RE_DOTTED_LEADER.search(stripped):
        result.is_dotted_leader = True

    # 行末小整数 (ページ番号候補)
    m_trail = _RE_TRAILING_INT.search(stripped)
    if m_trail:
        val = int(m_trail.group(1))
        if 1 <= val <= 50:
            result.trailing_page_number = val

    # 注記キーワード
    for kw in TOC_NOTE_KEYWORDS:
        if kw in stripped:
            result.has_note_keyword = True
            break

    # TOC line pattern (括弧付き)
    if _RE_TOC_PAREN.match(stripped):
        result.is_toc_line = True
    # TOC line pattern (一般): dotted leader + trailing int + note keyword
    elif _RE_TOC_GENERAL.match(stripped) and result.has_note_keyword:
        result.is_toc_line = True
    # dotted leader + trailing page number → TOC line
    elif result.is_dotted_leader and result.trailing_page_number is not None:
        result.is_toc_line = True

    return result


def detect_toc_page(lines: list[str]) -> TocPageResult:
    """ページ全体のテキスト行から TOC ページかどうかを判定する。"""
    result = TocPageResult()

    # 大きな財務数値を検出 (参考用)
    _re_large_num = re.compile(r'[\d,]{4,}')

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        lr = classify_toc_line(stripped)
        result.line_results.append(lr)

        if lr.is_toc_line:
            result.toc_line_count += 1
        if lr.is_dotted_leader:
            result.dotted_leader_count += 1
        if lr.trailing_page_number is not None:
            result.page_number_like_count += 1
        if lr.has_note_keyword:
            result.note_keyword_count += 1

        # 大きな数値 (4桁以上カンマ含む) → 財務数値指標
        large_nums = _re_large_num.findall(stripped)
        for n in large_nums:
            clean = n.replace(',', '')
            if len(clean) >= 4:
                result.large_financial_values += 1

    # 「目次」見出し
    full_text = '\n'.join(lines)
    if re.search(r'目\s*次', full_text):
        result.has_mokuji_heading = True

    # TOC score 計算
    score = 0.0
    if result.toc_line_count >= 5:
        score += 0.5
    elif result.toc_line_count >= 3:
        score += 0.3
    elif result.toc_line_count >= 2:
        score += 0.15

    if result.dotted_leader_count >= 3:
        score += 0.15
    if result.note_keyword_count >= 3:
        score += 0.15
    if result.has_mokuji_heading:
        score += 0.2

    # 大きな財務数値がないことで加点
    if result.large_financial_values == 0 and result.toc_line_count >= 2:
        score += 0.1

    # 減点: 大きな財務数値が多い → TOC ではなく真の表の可能性
    if result.large_financial_values >= 3:
        score -= 0.3

    result.toc_score = max(0.0, min(1.0, score))
    result.is_toc_page = result.toc_score >= 0.3 and result.toc_line_count >= 3

    return result


def detect_toc_candidate(table_lines: list[str]) -> TocCandidateResult:
    """candidate テーブルの行群から TOC テーブルか判定する。"""
    result = TocCandidateResult()

    non_empty = [l for l in table_lines if l.strip()]
    result.total_rows = len(non_empty)
    if result.total_rows == 0:
        return result

    for line in non_empty:
        lr = classify_toc_line(line.strip())
        if lr.is_toc_line:
            result.toc_line_count += 1
        if lr.is_dotted_leader:
            result.dotted_leader_count += 1
        if lr.trailing_page_number is not None:
            result.page_number_like_count += 1

    result.toc_line_ratio = result.toc_line_count / result.total_rows

    # reject 判定
    if result.toc_line_count >= 3:
        result.is_toc_candidate = True
        result.reject_reason = "toc_lines_dominant"
    elif result.toc_line_ratio >= 0.4 and result.toc_line_count >= 2:
        result.is_toc_candidate = True
        result.reject_reason = "toc_line_ratio_high"
    elif result.dotted_leader_count >= 3 and result.page_number_like_count >= 3:
        result.is_toc_candidate = True
        result.reject_reason = "dotted_leader_with_page_numbers"

    return result
