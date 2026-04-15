# ============================================================
# table_scoring.py — Phase B: テーブルスコアリング (Phase 5 強化)
# ============================================================
"""
候補ページ内の各テーブル(行ブロック)に対して
「セグメント表らしさ」をスコアリングする。

Phase 5 追加:
  - 5カテゴリ分離 (header / numeric_layout / segment_row / exclusion / sequence)
  - exclusion penalty 強化
  - weak evidence 判定支援
  - multi-page merge 用テーブル構造情報
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .scoring import normalize_text, is_toc_line
from .header_analysis import normalize_header


# ============================================================
# スコア要素
# ============================================================

_TABLE_SEGMENT_KW: list[tuple[str, float]] = [
    ("報告セグメント", 0.15),
    ("セグメント情報", 0.12),
    ("事業セグメント", 0.12),
    ("セグメント別", 0.10),
]

_TABLE_SALES_KW = [
    "売上高", "売上収益", "営業収益", "事業収益", "収益",
    "外部顧客への売上高", "外部顧客売上高",
    "Net sales", "Revenue", "Sales",
]

_TABLE_PROFIT_KW = [
    "セグメント利益", "セグメント損益", "営業利益", "事業利益",
    "利益又は損失", "利益（損失）", "経常利益", "損益",
    "Operating profit", "Segment profit", "Profit",
]

# Phase 5: 除外テーブル判定キーワード
_EXCLUSION_KW: list[tuple[str, float]] = [
    ("設備投資", -0.15),
    ("減価償却", -0.12),
    ("従業員", -0.12),
    ("キャッシュフロー", -0.15),
    ("キャッシュ・フロー", -0.15),
    ("財政状態", -0.12),
    ("貸借対照表", -0.12),
    ("株主還元", -0.10),
    ("業績予想", -0.12),
    ("配当予想", -0.10),
    ("自己資本", -0.08),
    ("有利子負債", -0.08),
    ("連結損益計算書", -0.30),
    ("四半期連結損益計算書", -0.30),
    ("連結貸借対照表", -0.12),
]

# セグメント表特有の補助語
_AUX_TERMS = ["その他", "調整額", "合計", "消去又は全社", "全社", "消去"]

# ── PL 勘定科目辞書 (行ラベルに出現したら account_like としてカウント) ──
_PL_ACCOUNT_LABELS: list[str] = [
    "売上原価", "売上総利益", "販売費及び一般管理費", "販売費・一般管理費",
    "営業外収益", "営業外費用", "経常利益", "特別利益", "特別損失",
    "税金等調整前", "法人税等", "法人税、住民税及び事業税",
    "当期純利益", "四半期純利益", "親会社株主に帰属する",
    "受取利息", "支払利息", "受取配当金",
    "減価償却費", "人件費", "賃借料", "租税公課",
    "貸倒引当金繰入", "トレーディング損益", "金融収益", "金融費用",
    "純営業収益", "営業総利益", "資金調達費用",
    "受入手数料", "委託手数料", "固定資産売却益", "固定資産除却損",
    "投資有価証券売却", "為替差損", "減損損失",
    "退職給付費用", "賞与引当金繰入", "持分法による投資",
    "負ののれん発生益", "関係会社株式売却",
]

# PL 強共起パターン (これらが3つ以上同時存在 → 強PL判定)
_PL_STRONG_COOCCURRENCE = [
    "売上原価", "売上総利益", "販売費及び一般管理費",
    "営業外収益", "営業外費用", "経常利益",
    "税金等調整前", "法人税等",
]

# 連結PL見出し近傍キーワード (テーブル近傍・テーブル内に出現 → 強減点)
_PL_HEADING_KW: list[tuple[str, float]] = [
    ("連結損益計算書", -0.35),
    ("四半期連結損益計算書", -0.35),
    ("損益計算書", -0.25),
    ("連結損益及び包括利益計算書", -0.30),
]

# セグメント見出し近傍キーワード (加点)
_SEG_HEADING_STRONG_KW: list[tuple[str, float]] = [
    ("セグメント情報", 0.20),
    ("報告セグメント", 0.20),
    ("セグメント別業績", 0.18),
    ("セグメントの業績", 0.18),
    ("セグメントごとの業績", 0.15),
    ("報告セグメントごとの売上高", 0.22),
    ("事業セグメント", 0.15),
]

# ── 事業名パターン (主軸: .*事業/.*部門/.*セグメント, 補助: 地域名等) ──
_SEGMENT_NAME_PRIMARY_RE = re.compile(
    r'.*(事業|部門|セグメント|ビジネス|カンパニー)$'
)
_SEGMENT_NAME_SECONDARY_KW = [
    "国内", "海外", "日本", "北米", "欧州", "アジア", "中国",
    "不動産", "物流", "エネルギー", "生活関連", "環境",
    "金融", "情報", "通信", "建設", "機械", "化学",
]


@dataclass
class TableScore:
    """テーブルスコアリング結果 (Phase 5 拡張 + PL除外)"""
    table_index: int = 0
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    # Phase 5: 5カテゴリ分離
    score_categories: dict[str, float] = field(default_factory=dict)
    reason: str = ""
    start_line: int = 0
    end_line: int = 0
    nearby_text: str = ""
    # Phase 5: テーブル構造情報 (weak evidence / multi-page 用)
    has_sales_header: bool = False
    has_profit_header: bool = False
    segment_like_rows: int = 0
    numeric_col_count: int = 0
    numeric_row_count: int = 0
    data_row_count: int = 0
    aux_term_count: int = 0
    line_count: int = 0
    heading_like: bool = False
    non_segment_type: str = ""  # company_profile, correction_or_notice, narrative_text, pl_table
    # PL除外用
    account_like_rows: int = 0
    segment_name_rows: int = 0
    pl_table_penalty: float = 0.0


def _count_numeric_columns(table_lines: list[str]) -> int:
    """テーブル行群から、数値列数を推定する (行毎の数値トークン数の中央値)。"""
    col_counts: list[int] = []
    for line in table_lines:
        stripped = line.strip()
        if not stripped:
            continue
        num_tokens = re.findall(r'[△▲\-]?\s*[\d,]+(?:\.\d+)?', stripped)
        if num_tokens:
            col_counts.append(len(num_tokens))
    if not col_counts:
        return 0
    col_counts.sort()
    return col_counts[len(col_counts) // 2]  # median


def score_segment_table(
    table_lines: list[str],
    nearby_text: str = "",
    table_index: int = 0,
    start_line: int = 0,
    end_line: int = 0,
) -> TableScore:
    """
    Phase B: テーブル行ブロックのセグメント表スコア (Phase 5 強化)。

    5カテゴリ分離:
      - header_score: sales/profit/segment KW ヘッダー
      - numeric_layout_score: 数値列数/行数/表構造
      - segment_row_score: セグメント名行 / 補助語
      - exclusion_penalty: 非セグメント表の減点
      - sequence_bonus: 三拍子揃いボーナス (条件付き)
    """
    header_score = 0.0
    numeric_layout_score = 0.0
    segment_row_score = 0.0
    exclusion_penalty = 0.0
    sequence_bonus = 0.0

    breakdown: dict[str, float] = {}
    reasons: list[str] = []

    all_text = "\n".join(table_lines)
    all_normalized = normalize_header(all_text)
    context = normalize_text(nearby_text) if nearby_text else ""

    line_count = len(table_lines)

    # ================================================================
    # 1. header_score: セグメント/売上/利益 KW
    # ================================================================

    # セグメント系 KW (テーブル内 + 周辺テキスト)
    for kw, sc in _TABLE_SEGMENT_KW:
        nkw = normalize_text(kw)
        if nkw in all_normalized or nkw in context:
            header_score += sc
            breakdown[f"seg:{kw}"] = sc
            reasons.append(kw)

    # 売上系ヘッダー
    has_sales = False
    for kw in _TABLE_SALES_KW:
        nkw = normalize_header(kw)
        if nkw in all_normalized:
            header_score += 0.12
            breakdown["sales_header"] = 0.12
            reasons.append(f"売上系:{kw}")
            has_sales = True
            break

    # 利益系ヘッダー
    has_profit = False
    for kw in _TABLE_PROFIT_KW:
        nkw = normalize_header(kw)
        if nkw in all_normalized:
            header_score += 0.12
            breakdown["profit_header"] = 0.12
            reasons.append(f"利益系:{kw}")
            has_profit = True
            break

    # ================================================================
    # 2. numeric_layout_score: 数値列数/行数/表構造
    # ================================================================

    # 数値列数推定
    numeric_cols = _count_numeric_columns(table_lines)
    if numeric_cols >= 2:
        bonus = min(0.10, 0.04 * numeric_cols)
        numeric_layout_score += bonus
        breakdown["numeric_cols"] = bonus
        reasons.append(f"数値列{numeric_cols}列")
    elif numeric_cols <= 1 and line_count > 3:
        # 数値列1列のみはセグメント表らしさ低い
        penalty = -0.08
        numeric_layout_score += penalty
        breakdown["single_num_col"] = penalty
        reasons.append("数値列1列(減点)")

    # 数値行数
    num_lines = sum(1 for l in table_lines if re.search(r'[\d,]{3,}', l))
    total = max(line_count, 1)

    if num_lines >= 5:
        numeric_layout_score += 0.10
        breakdown["num_rows_5plus"] = 0.10
        reasons.append(f"数値行{num_lines}行(5+)")
    elif num_lines >= 3:
        numeric_layout_score += 0.05
        breakdown["num_rows_3plus"] = 0.05
        reasons.append(f"数値行{num_lines}行(3+)")

    # 数値密度
    if total > 0 and num_lines / total >= 0.3:
        numeric_layout_score += 0.05
        breakdown["num_density"] = 0.05
        reasons.append(f"数値密度{num_lines}/{total}")

    # 行数が十分
    if line_count >= 10:
        numeric_layout_score += 0.05
        breakdown["sufficient_rows"] = 0.05
        reasons.append(f"行数{line_count}")
    elif line_count < 3:
        numeric_layout_score -= 0.05
        breakdown["too_few_rows"] = -0.05
        reasons.append(f"行数{line_count}(極少)")

    # ================================================================
    # 3. segment_row_score: セグメント名らしい行・補助語
    # ================================================================

    segment_like_rows = 0
    for line in table_lines:
        stripped = line.strip()
        if not stripped:
            continue
        # ラベル + 数値のパターン
        name_match = re.match(r'^([^\d△▲\-－]{2,30})\s+.*[\d,]{3,}', stripped)
        if name_match:
            label = name_match.group(1).strip()
            # スキップラベルでないこと
            skip_kws = ["合計", "調整", "消去", "全社", "計"]
            if not any(sk in label for sk in skip_kws):
                segment_like_rows += 1

    if segment_like_rows >= 2:
        bonus = min(0.15, 0.05 * segment_like_rows)
        segment_row_score += bonus
        breakdown["segment_rows"] = bonus
        reasons.append(f"セグメント名行{segment_like_rows}行")

    # 補助語 (その他 / 調整額 / 合計 / 全社 / 消去)
    aux_count = sum(1 for kw in _AUX_TERMS if kw in all_text)
    if aux_count >= 1:
        bonus = min(0.08, 0.03 * aux_count)
        segment_row_score += bonus
        breakdown["aux_terms"] = bonus
        reasons.append(f"補助語{aux_count}個")

    # ================================================================
    # 4. exclusion_penalty: 非セグメント表の減点
    # ================================================================

    # 除外 KW
    for kw, pen in _EXCLUSION_KW:
        nkw = normalize_text(kw)
        if nkw in all_normalized:
            exclusion_penalty += pen
            breakdown[f"excl:{kw}"] = pen
            reasons.append(f"{kw}(減点)")

    # --- PL 勘定科目除外 ---
    pl_penalty = 0.0
    account_like_count = 0
    seg_name_count = 0
    non_segment_type = ""  # Phase 6: 先に初期化 (UnboundLocalError 防止)

    # 行ラベルごとに account_like / segment_like を分類
    _row_labels: list[str] = []
    for line in table_lines:
        stripped = line.strip()
        if not stripped:
            continue
        name_m = re.match(r'^([^\d△▲\-－]{2,40})', stripped)
        if name_m:
            _row_labels.append(name_m.group(1).strip())

    for lbl in _row_labels:
        # account_like 判定
        for pl_kw in _PL_ACCOUNT_LABELS:
            if pl_kw in lbl:
                account_like_count += 1
                break
        # segment_like 判定 (主軸: .*事業/.*部門 等)
        if _SEGMENT_NAME_PRIMARY_RE.match(lbl):
            seg_name_count += 1
        elif any(skw in lbl for skw in _SEGMENT_NAME_SECONDARY_KW):
            seg_name_count += 1  # 補助 (0.5 相当だが簡素にカウント)

    # PL 強共起パターン判定
    pl_strong_hits = sum(1 for kw in _PL_STRONG_COOCCURRENCE if kw in all_text)
    if pl_strong_hits >= 3:
        # 売上原価+売上総利益+販管費 等が3つ以上 → 強PL判定
        pl_penalty = -0.50
        breakdown["pl_strong_cooccurrence"] = pl_penalty
        reasons.append(f"PL強共起({pl_strong_hits}語)")
        non_segment_type = non_segment_type or "pl_table"
    elif account_like_count >= 5:
        pl_penalty = -0.40
        breakdown["pl_account_heavy"] = pl_penalty
        reasons.append(f"PL勘定科目多数({account_like_count}語)")
        non_segment_type = non_segment_type or "pl_table"
    elif account_like_count >= 3:
        pl_penalty = -0.25
        breakdown["pl_account_moderate"] = pl_penalty
        reasons.append(f"PL勘定科目中程度({account_like_count}語)")

    # PL 見出し近傍 (テーブル内 or nearby に連結損益計算書等)
    combined_text = all_normalized + " " + context
    for kw, pen in _PL_HEADING_KW:
        nkw = normalize_text(kw)
        if nkw in combined_text:
            pl_penalty += pen
            breakdown[f"pl_heading:{kw}"] = pen
            reasons.append(f"{kw}近傍(減点)")
            non_segment_type = non_segment_type or "pl_table"

    # セグメント見出し近傍 (強い加点)
    for kw, bonus_val in _SEG_HEADING_STRONG_KW:
        nkw = normalize_text(kw)
        if nkw in combined_text:
            # 既に _TABLE_SEGMENT_KW でスコアされている場合は追加ボーナスのみ
            extra = bonus_val * 0.5 if f"seg:{kw}" in breakdown else bonus_val
            header_score += extra
            breakdown[f"seg_heading_strong:{kw}"] = extra
            reasons.append(f"{kw}見出し(加点)")
            break  # 最初のヒットのみ

    # segment_like bonus (事業名行が多い)
    if seg_name_count >= 2:
        seg_bonus = min(0.15, 0.05 * seg_name_count)
        segment_row_score += seg_bonus
        breakdown["segment_name_pattern"] = seg_bonus
        reasons.append(f"事業名パターン{seg_name_count}行")

    exclusion_penalty += pl_penalty

    # 目次行
    toc = sum(1 for l in table_lines if is_toc_line(l))
    if toc >= 2:
        penalty = -0.20
        exclusion_penalty += penalty
        breakdown["toc_lines"] = penalty
        reasons.append(f"目次行{toc}行(減点)")

    # 比率中心 (% が多い)
    pct_count = sum(1 for l in table_lines if "%" in l or "％" in l)
    if pct_count > num_lines * 0.5 and num_lines > 0:
        penalty = -0.10
        exclusion_penalty += penalty
        breakdown["ratio_heavy"] = penalty
        reasons.append("比率中心")

    # --- 8. hard negative keywords ---
    _HARD_NEG_KW = [
        "訂正前", "訂正後", "【訂正前】", "【訂正後】",
        "％表示は、対前期増減率", "対前期増減率",
        "延期", "未定", "サイバー攻撃", "発表を延期",
        "影響により",
    ]
    _hard_neg_count = sum(1 for kw in _HARD_NEG_KW if kw in all_text)
    non_segment_type = ""
    if _hard_neg_count >= 2:
        penalty = -0.35
        exclusion_penalty += penalty
        breakdown["hard_negative"] = penalty
        reasons.append(f"hard_negative({_hard_neg_count}語)")
        non_segment_type = "correction_or_notice"
    elif _hard_neg_count == 1:
        penalty = -0.15
        exclusion_penalty += penalty
        breakdown["hard_negative"] = penalty
        reasons.append("hard_negative(1語)")

    # --- 9. company profile table ---
    _PROFILE_KW = ["名称", "設立", "所在地", "代表者", "資本金", "事業内容",
                    "従業員数", "主要な事業", "上場取引所"]
    _profile_hits = sum(1 for kw in _PROFILE_KW if kw in all_text)
    if _profile_hits >= 3:
        penalty = -0.50
        exclusion_penalty += penalty
        breakdown["company_profile"] = penalty
        reasons.append(f"会社概要表({_profile_hits}語)")
        non_segment_type = "company_profile"

    # --- 10. narrative text block (multi-line) ---
    _non_empty_lines = [l.strip() for l in table_lines if l.strip()]
    _avg_line_len = sum(len(l) for l in _non_empty_lines) / max(len(_non_empty_lines), 1)
    _punct_lines = sum(1 for l in _non_empty_lines if "。" in l or "、" in l)
    if (_avg_line_len >= 50 and _punct_lines >= max(2, len(_non_empty_lines) * 0.4)
            and numeric_cols <= 1):
        penalty = -0.45
        exclusion_penalty += penalty
        breakdown["narrative_text"] = penalty
        reasons.append(f"自然文ブロック(avg={_avg_line_len:.0f},punct={_punct_lines})")
        non_segment_type = non_segment_type or "narrative_text"

    # --- 11. ratio-only table guard 強化 ---
    if (pct_count > num_lines * 0.5 and num_lines > 0
            and not has_sales and not has_profit):
        extra_penalty = -0.15
        exclusion_penalty += extra_penalty
        breakdown["ratio_only_guard"] = extra_penalty
        reasons.append("比率表(sales/profit header なし)")

    # --- 12. explanation / deck slide exclusion ---
    _SLIDE_NEG_KW = [
        "事業体制", "組織再編", "持株会社体制", "商号変更", "子会社設立",
        "四半期業績推移", "業績推移", "グラフ", "参考",
        "見通し", "トピックス", "中期経営計画",
    ]
    _slide_hits = sum(1 for kw in _SLIDE_NEG_KW if kw in all_text)
    if _slide_hits >= 1 and not has_sales and not has_profit:
        penalty = -0.35 if _slide_hits >= 2 else -0.20
        exclusion_penalty += penalty
        breakdown["slide_exclusion"] = penalty
        reasons.append(f"説明資料スライド語({_slide_hits}語)")
        non_segment_type = non_segment_type or "explanation_slide"

    # ================================================================
    # 5. sequence_bonus: 三拍子ボーナス (条件付き)
    # ================================================================
    # 売上+利益+(その他/調整額/合計) が揃い、かつ segment 行がある場合のみ
    has_sales_or_profit = has_sales or has_profit
    if has_sales_or_profit and aux_count >= 2 and segment_like_rows >= 2:
        sequence_bonus = 0.10
        breakdown["sequence_bonus"] = 0.10
        reasons.append("三拍子ボーナス(売上/利益+補助語+セグメント行)")

    # ================================================================
    # 6. heading_block_penalty: 見出しブロック減点
    # ================================================================
    heading_penalty = 0.0
    heading_like = False
    # 条件: header_score が高いが数値構造が弱い
    if header_score >= 0.10 and num_lines <= 3 and line_count <= 15:
        heading_penalty = -0.30
        breakdown["heading_block"] = heading_penalty
        reasons.append("見出しブロック(数値行少+短ブロック)")
        heading_like = True
    elif header_score >= 0.10 and numeric_cols <= 1 and num_lines <= 5:
        heading_penalty = -0.20
        breakdown["heading_block"] = heading_penalty
        reasons.append("見出しブロック(数値列1以下+数値行少)")
        heading_like = True
    # 【】や（）で囲まれた見出し語中心
    if not heading_like:
        bracket_lines = sum(
            1 for l in table_lines
            if re.match(r'^\s*[\[\]【】（()）\(\)]', l.strip())
        )
        if bracket_lines >= max(1, line_count * 0.3) and num_lines <= 3:
            # 短い見出しブロック (≤6行) は強く減点
            if line_count <= 6:
                heading_penalty = -0.40
                breakdown["heading_bracket"] = heading_penalty
                reasons.append("見出しブロック(括弧見出し+短ブロック)")
            else:
                heading_penalty = -0.25
                breakdown["heading_bracket"] = heading_penalty
                reasons.append("見出しブロック(括弧見出し中心)")
            heading_like = True

    # ================================================================
    # 7. text_block_penalty: 本文ブロック減点
    # ================================================================
    text_block_penalty = 0.0
    if numeric_cols <= 1:
        # 行ごとのテキスト長の平均を計算
        non_empty = [l.strip() for l in table_lines if l.strip()]
        avg_len = sum(len(l) for l in non_empty) / max(len(non_empty), 1)
        has_punct = any("。" in l or "、" in l for l in non_empty)
        if num_lines <= 2 and avg_len >= 35 and has_punct:
            text_block_penalty = -0.50
            breakdown["text_block"] = text_block_penalty
            reasons.append(f"本文ブロック(num_lines={num_lines},avg_len={avg_len:.0f})")
        elif avg_len >= 40 and has_punct:
            text_block_penalty = -0.40
            breakdown["text_block"] = text_block_penalty
            reasons.append(f"本文ブロック(avg_len={avg_len:.0f}+句読点)")

    # ================================================================
    # 合計
    # ================================================================
    total_score = (
        header_score
        + numeric_layout_score
        + segment_row_score
        + exclusion_penalty
        + sequence_bonus
        + heading_penalty
        + text_block_penalty
    )
    total_score = max(0.0, min(total_score, 1.0))

    score_categories = {
        "header": round(header_score, 3),
        "numeric_layout": round(numeric_layout_score, 3),
        "segment_row": round(segment_row_score, 3),
        "exclusion": round(exclusion_penalty, 3),
        "sequence": round(sequence_bonus, 3),
        "heading": round(heading_penalty, 3),
        "text_block": round(text_block_penalty, 3),
        "pl_penalty": round(pl_penalty, 3),
    }

    return TableScore(
        table_index=table_index,
        score=total_score,
        score_breakdown=breakdown,
        score_categories=score_categories,
        reason="; ".join(reasons),
        start_line=start_line,
        end_line=end_line,
        nearby_text=nearby_text,
        has_sales_header=has_sales,
        has_profit_header=has_profit,
        segment_like_rows=segment_like_rows,
        numeric_col_count=numeric_cols,
        numeric_row_count=num_lines,
        data_row_count=max(0, line_count - 2),  # header 推定2行
        aux_term_count=aux_count,
        line_count=line_count,
        heading_like=heading_like,
        non_segment_type=non_segment_type,
        account_like_rows=account_like_count,
        segment_name_rows=seg_name_count,
        pl_table_penalty=pl_penalty,
    )


# ============================================================
# Weak Evidence 判定
# ============================================================

def is_weak_evidence_table(ts: TableScore) -> bool:
    """
    テーブルスコアが通常閾値未満でも、構造条件を満たせば
    weak evidence として候補に残すべきかを判定する。

    条件 (すべて AND):
      - score >= 0.10
      - numeric_col_count >= 2
      - numeric_row_count >= 3 (= data_rows)
      - segment_like_rows >= 2 OR has_sales_header
      - exclusion penalty が強くない (> -0.15)
    """
    if ts.score < 0.10:
        return False
    if ts.numeric_col_count < 2:
        return False
    if ts.numeric_row_count < 3:
        return False
    # exclusion penalty が -0.10 以下なら排除 (設備投資 -0.15 等)
    if ts.score_categories.get("exclusion", 0) <= -0.10:
        return False
    # sales ヘッダー or segment 行
    if ts.has_sales_header or ts.segment_like_rows >= 2:
        return True
    return False


def is_heading_like_table(ts: TableScore) -> bool:
    """見出しブロック型テーブルかどうかを判定する。

    heading_like フラグに加え、数値構造の弱さを総合判定。
    """
    if ts.heading_like:
        return True
    # header_score だけ高くて numeric が弱い
    if (ts.score_categories.get("header", 0) >= 0.20
            and ts.numeric_row_count <= 3
            and ts.numeric_col_count <= 1):
        return True
    return False


# ============================================================
# テーブル領域検出
# ============================================================

def find_table_regions(lines: list[str]) -> list[tuple[int, int, str]]:
    """
    テキスト行リストからテーブル候補領域を検出する。

    Phase 3.5: 5-pass 検索
      Pass 1: セグメントKW行を起点
      Pass 2: セグメント名・地域名・業種名クラスタ起点 (複数対応)
      Pass 3: 売上/利益ヘッダー起点
      Pass 4: 数値密度ベース (スライドウィンドウ)

    Returns:
        [(start, end, nearby_text), ...]
    """
    import logging as _ftr_logging
    _ftr_logger = _ftr_logging.getLogger("tdnet.find_table_regions")

    _SEGMENT_HEADER_KW = [
        "報告セグメント", "事業セグメント", "セグメント情報",
        "セグメント別", "事業別", "部門別",
        "売上高及び利益", "売上高と利益", "売上収益及び利益",
        "報告セグメントごとの売上高", "セグメントの業績",
        "外部顧客への売上高",
        "セグメント間内部売上高", "セグメント間内部営業収益",
    ]
    _SECTION_END_KW = [
        "連結損益", "連結貸借", "連結キャッシュ", "経営成績",
        "連結損益計算書", "四半期連結損益計算書",
        "連結貸借対照表", "キャッシュ・フローの状況",
        "財政状態",
    ]
    _TOC_INDICATORS = ["…", "・・", "───", "─────"]

    _SEGMENT_NAME_RE = re.compile(r'.*(事業|部門|セグメント|ビジネス|カンパニー)$')
    _REGION_NAMES_SET = {
        "日本", "国内", "海外", "北米", "欧州", "アジア", "中国",
        "東南アジア", "米国", "アメリカ", "ヨーロッパ",
    }
    _INDUSTRY_NAMES_SET = {
        "不動産", "建設", "小売", "物流", "住宅", "金融", "製造",
        "情報", "通信", "サービス", "エネルギー", "化学", "食品",
    }
    _SALES_PROFIT_HEADER_KW = [
        "売上高", "営業利益", "売上収益", "営業収益", "セグメント利益",
        "セグメント損益", "事業利益", "コア営業利益",
        "経常利益", "経常収益",
    ]
    # 数値密度ベース除外キーワード
    _NUMDENS_EXCLUDE_KW = ["%", "％", "構成比", "比率", "件数", "人数", "面積"]

    regions: list[tuple[int, int, str]] = []
    region_passes: list[int] = []  # 各 region がどの pass 由来か
    used: set[int] = set()

    def _expand_region(start_line: int, max_extent: int = 60) -> int:
        """start_line から下方に region を拡張し、終了行を返す"""
        end = min(start_line + max_extent, len(lines))
        blank_count = 0
        for j in range(start_line + 1, len(lines)):
            stripped = lines[j].strip()
            if not stripped:
                blank_count += 1
                if blank_count >= 2:
                    end = j
                    break
            else:
                blank_count = 0
            if any(kw in stripped for kw in _SECTION_END_KW):
                end = j
                break
        return end

    def _add_region(start: int, end: int, pass_no: int) -> bool:
        """重複チェック後に region を追加。追加したら True"""
        # 既存 region と 50% 以上重複していたらスキップ
        new_range = set(range(start, end))
        for rs, re_, _ in regions:
            existing = set(range(rs, re_))
            overlap = len(new_range & existing)
            if overlap > len(new_range) * 0.5:
                return False
        nearby_start = max(0, start - 5)
        nearby = "\n".join(lines[nearby_start:start])
        regions.append((start, end, nearby))
        region_passes.append(pass_no)
        used.update(range(start, end))
        return True

    # ================================================================
    # Pass 1: セグメントKW行を起点
    # ================================================================
    for i, line in enumerate(lines):
        if i in used:
            continue
        if any(ind in line for ind in _TOC_INDICATORS):
            continue
        if not any(kw in line for kw in _SEGMENT_HEADER_KW):
            continue
        start = i
        end = _expand_region(start)
        if _add_region(start, end, 1):
            _ftr_logger.debug(f"[REGION_PASS:1] start={start} end={end} line={line.strip()[:50]}")

    # ================================================================
    # Pass 2: セグメント名・地域名・業種名クラスタ起点 (複数対応)
    # ================================================================
    for i, line in enumerate(lines):
        if i in used:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if any(ind in stripped for ind in _TOC_INDICATORS):
            continue
        # ラベル部分を取り出す
        label_m = re.match(r'^([^\d△▲\-－]{2,30})', stripped)
        if not label_m:
            continue
        label = label_m.group(1).strip()

        is_seg_name = _SEGMENT_NAME_RE.match(label)
        is_region = any(rn in label for rn in _REGION_NAMES_SET)
        is_industry = any(inn in label for inn in _INDUSTRY_NAMES_SET)

        if not (is_seg_name or is_region or is_industry):
            continue

        # この行の近辺に数値行が 3 行以上あるか確認
        context_start = max(0, i - 3)
        context_end = min(len(lines), i + 20)
        num_count = sum(
            1 for j in range(context_start, context_end)
            if re.search(r'[\d,]{3,}', lines[j])
        )
        if num_count < 3:
            continue

        # セグメント名が 3 行以上近辺にあるか確認
        seg_cluster_count = 0
        for j in range(context_start, context_end):
            js = lines[j].strip()
            jm = re.match(r'^([^\d△▲\-－]{2,30})', js)
            if jm:
                jl = jm.group(1).strip()
                if (_SEGMENT_NAME_RE.match(jl)
                        or any(rn in jl for rn in _REGION_NAMES_SET)
                        or any(inn in jl for inn in _INDUSTRY_NAMES_SET)):
                    seg_cluster_count += 1

        if seg_cluster_count < 2:
            continue

        # region 検出
        start = max(0, i - 3)
        end = _expand_region(start, 40)
        if _add_region(start, end, 2):
            _ftr_logger.debug(
                f"[REGION_PASS:2] start={start} end={end} "
                f"label={label} cluster={seg_cluster_count}"
            )

    # ================================================================
    # Pass 3: 売上/利益ヘッダー起点
    # ================================================================
    for i, line in enumerate(lines):
        if i in used:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        # ヘッダー行判定: 売上/利益KWを含み、数値が主でない行
        header_hits = sum(1 for kw in _SALES_PROFIT_HEADER_KW if kw in stripped)
        if header_hits < 1:
            continue
        # 数値が主の行 (データ行) は除外
        num_tokens = re.findall(r'[△▲\-]?[\d,]+(?:\.\d+)?', stripped)
        non_num = re.sub(r'[△▲\-]?[\d,]+(?:\.\d+)?', '', stripped).strip()
        if len(num_tokens) >= 3 and len(non_num) < 10:
            continue  # データ行なのでスキップ

        # ヘッダー行の下に数値行が 3 行以上あるか
        below_num_count = 0
        for j in range(i + 1, min(i + 25, len(lines))):
            if re.search(r'[\d,]{3,}', lines[j]):
                below_num_count += 1
        if below_num_count < 3:
            continue

        start = i
        end = _expand_region(start, 40)
        if _add_region(start, end, 3):
            _ftr_logger.debug(
                f"[REGION_PASS:3] start={start} end={end} "
                f"header_hits={header_hits} below_num={below_num_count}"
            )

    # ================================================================
    # Pass 4: 数値密度ベース (スライドウィンドウ)
    # ================================================================
    # 条件: distinct_numeric_positions >= 2
    #        + (segment_like_rows >= 2 OR region_hits >= 2 OR header_hits >= 1)
    #        + %/構成比 優勢領域は除外
    _WINDOW_SIZE = 20
    _best_density_regions: list[tuple[int, int, float, int, int, int]] = []
    # (start, end, density, seg_rows, region_hits, header_hits)

    for win_start in range(0, max(1, len(lines) - _WINDOW_SIZE + 1), 5):
        win_end = min(win_start + _WINDOW_SIZE, len(lines))
        # 既存 region と重なっていたらスキップ
        win_range = set(range(win_start, win_end))
        if len(win_range & used) > len(win_range) * 0.3:
            continue

        window_lines = lines[win_start:win_end]
        non_blank = [l for l in window_lines if l.strip()]
        if len(non_blank) < 5:
            continue

        # 数値行カウント + distinct_numeric_positions
        rep_num = 0
        num_positions: dict[int, int] = {}
        pct_count = 0
        exclude_kw_count = 0
        for wl in window_lines:
            ws = wl.strip()
            if not ws:
                continue
            # %/除外KW チェック
            if any(ek in ws for ek in _NUMDENS_EXCLUDE_KW):
                exclude_kw_count += 1
            nums = re.findall(r'[△▲\-]?[\d,]+(?:\.\d+)?', ws)
            if len(nums) >= 2:
                rep_num += 1
                for nm in nums:
                    pos_bucket = ws.find(nm) // 10
                    num_positions[pos_bucket] = num_positions.get(pos_bucket, 0) + 1

        distinct_numpos = sum(1 for _cnt in num_positions.values() if _cnt >= 2)

        if rep_num < 4 or distinct_numpos < 2:
            continue

        # %/除外KW が優勢な領域は除外
        if exclude_kw_count > len(non_blank) * 0.3:
            continue

        # 構造チェック: seg_rows, region_hits, header_hits
        win_seg_rows = 0
        win_region_hits = 0
        win_header_hits = 0
        for wl in window_lines:
            ws = wl.strip()
            if not ws:
                continue
            wm = re.match(r'^([^\d△▲\-－]{2,30})', ws)
            if wm:
                wlabel = wm.group(1).strip()
                skip_kws = ["合計", "調整", "消去", "全社", "計"]
                if not any(sk in wlabel for sk in skip_kws):
                    if (_SEGMENT_NAME_RE.match(wlabel)
                            or any(rn in wlabel for rn in _REGION_NAMES_SET)
                            or any(inn in wlabel for inn in _INDUSTRY_NAMES_SET)):
                        win_seg_rows += 1
                    if any(rn in wlabel for rn in _REGION_NAMES_SET):
                        win_region_hits += 1
            # header KW チェック
            if any(kw in ws for kw in _SALES_PROFIT_HEADER_KW):
                win_header_hits += 1

        # gate: seg_rows >= 2 OR region_hits >= 2 OR header_hits >= 1
        if not (win_seg_rows >= 2 or win_region_hits >= 2 or win_header_hits >= 1):
            continue

        density = rep_num / len(non_blank)
        _best_density_regions.append(
            (win_start, win_end, density, win_seg_rows, win_region_hits, win_header_hits)
        )

    # density 降順で上位 3 件を候補化
    _best_density_regions.sort(key=lambda x: x[2], reverse=True)
    for bdr in _best_density_regions[:3]:
        bdr_start, bdr_end = bdr[0], bdr[1]
        if _add_region(bdr_start, bdr_end, 4):
            _ftr_logger.debug(
                f"[REGION_PASS:4] start={bdr_start} end={bdr_end} "
                f"density={bdr[2]:.2f} seg_rows={bdr[3]} "
                f"region_hits={bdr[4]} header_hits={bdr[5]}"
            )

    # ================================================================
    # 同一ページ内の候補数を上位 5 件に制限 (スコア推定不可のため行数で優先)
    # ================================================================
    if len(regions) > 5:
        # region サイズ (行数) 降順で上位 5 件
        indexed = list(enumerate(regions))
        indexed.sort(key=lambda x: (x[1][1] - x[1][0]), reverse=True)
        keep_indices = set(idx for idx, _ in indexed[:5])
        regions = [r for i, r in enumerate(regions) if i in keep_indices]
        region_passes = [p for i, p in enumerate(region_passes) if i in keep_indices]

    # region_pass 情報を trace 用に nearby_text に埋め込む
    enriched_regions: list[tuple[int, int, str]] = []
    for (start, end, nearby), pass_no in zip(regions, region_passes):
        enriched_nearby = f"[REGION_PASS:{pass_no}]\n{nearby}"
        enriched_regions.append((start, end, enriched_nearby))

    return enriched_regions

