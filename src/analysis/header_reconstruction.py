#!/usr/bin/env python3
"""
header_reconstruction.py — 分割ヘッダー復元

PDF 表抽出後の列ヘッダーを横連結・縦連結・辞書照合で復元し、
sales/profit 列の特定率を改善する。

設計原則:
  1. 純粋関数ベース (副作用なし)
  2. 生セル値を破壊しない (復元は別フィールド)
  3. 決定的 (deterministic)
  4. ルールベース優先 (ブラックボックス化しない)
  5. ログで「何が連結されたか」追跡可能
  6. 誤爆回避優先
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from .header_analysis import _NUM_PATTERN, normalize_header

# ============================================================
# Feature Flag
# ============================================================

ENABLE_SPLIT_HEADER_RECONSTRUCTION = True

# ============================================================
# Data Models
# ============================================================

@dataclass
class MetricScore:
    """構造化メトリクスコア — なぜその判定になったか追跡可能"""
    metric_type: str                   # "sales" | "profit" | "exclusion"
    total_score: float = 0.0
    exact_match: bool = False
    normalized_match: bool = False
    matched_terms: list[str] = field(default_factory=list)
    penalties: list[str] = field(default_factory=list)
    base_score: float = 0.0


@dataclass
class ReconstructionCandidate:
    """連結候補"""
    parts: list[str]
    result: str
    merge_type: str       # "horizontal" | "vertical" | "hybrid"
    positions: list = field(default_factory=list)  # col/row indices
    score: float = 0.0
    matched_dict_term: str = ""


@dataclass
class ReconstructedColumnHeader:
    """列ごとの復元結果"""
    col_idx: int
    original_fragments: list[str] = field(default_factory=list)
    normalized_fragments: list[str] = field(default_factory=list)
    reconstructed_text: str = ""
    merge_type: str | None = None       # "horizontal" | "vertical" | None (raw)
    confidence: float = 0.0
    applied_steps: list[dict] = field(default_factory=list)


@dataclass
class ReconstructionResult:
    """復元全体の結果"""
    column_headers: list[ReconstructedColumnHeader] = field(default_factory=list)
    reconstructed_headers: list[str] = field(default_factory=list)   # 便利accessor
    steps: list[dict] = field(default_factory=list)
    candidates: list[ReconstructionCandidate] = field(default_factory=list)
    original_matrix: list[list[str]] = field(default_factory=list)
    normalized_matrix: list[list[str]] = field(default_factory=list)
    feature_enabled: bool = True


# ============================================================
# Domain Vocabulary Dictionaries
# ============================================================

# --- sales系 (exact match 用) ---
_SALES_DICT: list[tuple[str, float]] = [
    ("売上高", 100),
    ("売上収益", 100),
    ("営業収益", 95),
    ("収益", 80),
    ("売上", 80),
    ("純売上高", 95),
    ("総売上高", 95),
    ("純収益", 85),
    ("正味収入保険料", 90),
    ("経常収益", 85),
    # English
    ("NetSales", 100),
    ("Revenue", 90),
    ("Sales", 85),
    ("NetRevenue", 95),
    ("TotalRevenue", 95),
    ("OperatingRevenue", 90),
    ("GrossPremiums", 75),
]

# --- profit系 (exact match 用) ---
_PROFIT_DICT: list[tuple[str, float]] = [
    ("営業利益", 100),
    ("セグメント利益", 100),
    ("事業利益", 95),
    ("経常利益", 95),
    ("税引前利益", 90),
    ("当期純利益", 85),
    ("コア営業利益", 98),
    ("セグメント損益", 100),
    ("セグメント利益又は損失", 100),
    ("利益又は損失", 95),
    ("営業損失", 90),
    ("経常損失", 90),
    ("親会社株主に帰属する当期純利益", 85),
    # English
    ("OperatingProfit", 100),
    ("OperatingIncome", 100),
    ("SegmentProfit", 100),
    ("SegmentIncome", 100),
    ("OrdinaryProfit", 95),
    ("OrdinaryIncome", 95),
    ("CoreOperatingProfit", 98),
    ("CoreOperatingIncome", 98),
    ("AdjustedOperatingIncome", 90),
    ("AdjustedOperatingProfit", 90),
    ("ProfitBeforeTax", 90),
    ("SegmentProfitOrLoss", 100),
    ("ProfitOrLoss", 95),
]

# --- 除外語 (これが含まれると penalty) ---
_EXCLUSION_TERMS: list[tuple[str, float]] = [
    ("利益率", 50),
    ("営業利益率", 50),
    ("前年同期比", 40),
    ("前年比", 40),
    ("増減額", 40),
    ("増減率", 50),
    ("構成比", 40),
    ("前期比", 40),
    ("マージン", 50),
    ("利益剰余金", 50),
    ("包括利益", 45),
    ("Margin", 50),
    ("Ratio", 40),
    ("YoY", 40),
    ("Change", 30),
    ("Variance", 30),
    ("収益性", 30),
    ("売上総利益", 30),    # gross profit ≠ segment profit
]

# --- 弱化語 (単体では metric にならない) ---
_WEAK_TERMS: set[str] = {
    "百万円", "億円", "千円", "円", "％", "%",
    "注", "Notes", "Note",
    "単位", "Unit",
    "調整額", "消去又は全社", "消去", "全社",
    "合計", "計", "小計",
}

# --- 期間修飾語 ---
_PERIOD_MODIFIERS: list[str] = [
    "当期", "前期", "当第", "第1四半期", "第2四半期", "第3四半期", "第4四半期",
    "累計", "通期", "予想", "実績", "前年同期", "当四半期",
    "上半期", "下半期", "中間期",
]
_PERIOD_RE = re.compile(
    r'(?:当(?:第\d)?四半期|第[1-4]四半期(?:累計)?|前期|当期|通期|予想|実績|前年同期|上半期|下半期|中間期)\s*'
)

# --- 補完語 (下段に来やすい) ---
_COMPLEMENT_SUFFIXES: set[str] = {
    "高", "利益", "損失", "収益", "費", "額", "益", "損益",
    "Income", "Profit", "Loss", "Revenue", "Sales",
}


# ============================================================
# Cell-Level Functions
# ============================================================

_EMPTY_CELL_VALUES: set[str] = {"", "-", "－", "―", "—", "|"}
_DASH_LINE_RE = re.compile(r'^[-─━┃│┐┘┌└]+$')
_DATE_LIKE_RE = re.compile(r'^\d{4}[/\-]\d{1,2}(?:[/\-]\d{1,2})?$')


def normalize_header_cell(text: str) -> str:
    """セル単位の正規化。

    - strip
    - NFKC
    - 改行→空白
    - 全角空白除去
    - 日本語テキスト中の空白除去
    - 罫線由来文字除去
    - △▲ は保持
    """
    text = text.strip()
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\n", " ").replace("\r", "")
    text = text.replace("\u3000", "")  # 全角空白
    # 日本語文字を含む場合はすべてのスペースを除去
    if re.search(r'[\u3000-\u9FFF\uF900-\uFAFF]', text):
        text = re.sub(r'\s+', '', text)
    else:
        text = re.sub(r'\s+', ' ', text)
    # 罫線系文字除去 (表枠残骸)
    text = re.sub(r'[─━┃│┐┘┌└┬┴┼╋]', '', text)
    return text.strip()


def is_empty_cell(text: str) -> bool:
    """空セルか判定。"""
    normalized = normalize_header_cell(text)
    if normalized in _EMPTY_CELL_VALUES:
        return True
    if _DASH_LINE_RE.fullmatch(normalized):
        return True
    return False


def is_numeric_cell(text: str) -> bool:
    """数値セルか判定。日付形式 (2025/3) も数値扱い。"""
    normalized = normalize_header_cell(text)
    if not normalized:
        return False
    if _NUM_PATTERN.fullmatch(normalized):
        return True
    if _DATE_LIKE_RE.fullmatch(normalized):
        return True
    return False


def is_unit_cell(text: str) -> bool:
    """単位セルか判定。"""
    normalized = normalize_header_cell(text)
    if not normalized:
        return False
    clean = re.sub(r'[（()）]', '', normalized)
    return clean in {"百万円", "億円", "千円", "円", "％", "%",
                     "Millionsofyen", "Billionsofyen", "Thousandsofyen",
                     "単位百万円", "単位千円", "単位億円"}


def is_period_modifier(text: str) -> bool:
    """期間修飾語のみで構成されるか判定。"""
    normalized = normalize_header_cell(text)
    if not normalized:
        return False
    stripped = _PERIOD_RE.sub('', normalized).strip()
    return stripped == ""


def strip_period_modifiers(text: str) -> str:
    """期間修飾語を除去してメトリック本体を取り出す。"""
    return _PERIOD_RE.sub('', text).strip()


# ============================================================
# Scoring
# ============================================================

def score_metric_header(text: str) -> dict[str, MetricScore]:
    """ヘッダーテキストに対して sales/profit/exclusion スコアを付与。

    Returns:
        {"sales": MetricScore, "profit": MetricScore, "exclusion": MetricScore}
    """
    # 正規化 (空白除去版 — 辞書照合用)
    normalized = normalize_header(text)
    normalized_lower = normalized.lower()
    # 期間修飾語を除去した版
    stripped = strip_period_modifiers(normalized)
    stripped_lower = stripped.lower()

    sales = MetricScore(metric_type="sales")
    profit = MetricScore(metric_type="profit")
    exclusion = MetricScore(metric_type="exclusion")

    # --- Exclusion check first ---
    for term, penalty in _EXCLUSION_TERMS:
        nterm = normalize_header(term)
        nterm_l = nterm.lower()
        if nterm_l in normalized_lower:
            exclusion.total_score += penalty
            exclusion.matched_terms.append(term)
            if nterm_l == normalized_lower:
                exclusion.exact_match = True

    # --- Sales scoring ---
    for term, base in _SALES_DICT:
        nterm = normalize_header(term)
        nterm_l = nterm.lower()
        if nterm_l == normalized_lower or nterm_l == stripped_lower:
            # exact match
            sales.exact_match = True
            sales.base_score = max(sales.base_score, base)
            sales.matched_terms.append(term)
        elif nterm_l in normalized_lower:
            # substring match
            sales.normalized_match = True
            score = base * 0.6  # keyword match は 60%
            sales.base_score = max(sales.base_score, score)
            sales.matched_terms.append(term)

    sales.total_score = sales.base_score
    # exclusion penalty 適用
    if exclusion.total_score > 0 and not sales.exact_match:
        sales.total_score = max(0, sales.total_score - exclusion.total_score)
        sales.penalties.append(f"exclusion:-{exclusion.total_score}")

    # --- Profit scoring ---
    for term, base in _PROFIT_DICT:
        nterm = normalize_header(term)
        nterm_l = nterm.lower()
        if nterm_l == normalized_lower or nterm_l == stripped_lower:
            profit.exact_match = True
            profit.base_score = max(profit.base_score, base)
            profit.matched_terms.append(term)
        elif nterm_l in normalized_lower:
            profit.normalized_match = True
            score = base * 0.6
            profit.base_score = max(profit.base_score, score)
            profit.matched_terms.append(term)

    profit.total_score = profit.base_score
    if exclusion.total_score > 0 and not profit.exact_match:
        profit.total_score = max(0, profit.total_score - exclusion.total_score)
        profit.penalties.append(f"exclusion:-{exclusion.total_score}")

    # --- 「率」penalty ---
    if "率" in normalized:
        if not sales.exact_match:
            sales.total_score = min(sales.total_score, 15)
            sales.penalties.append("ratio_suffix")
        if not profit.exact_match:
            profit.total_score = min(profit.total_score, 15)
            profit.penalties.append("ratio_suffix")

    return {"sales": sales, "profit": profit, "exclusion": exclusion}


# ============================================================
# Reconstruction: Horizontal (同一行内の隣接セル連結)
# ============================================================

def reconstruct_horizontal(
    row: list[str],
    row_idx: int = 0,
) -> list[ReconstructionCandidate]:
    """同一ヘッダー行の隣接2〜4セルを連結して候補生成。

    条件:
      - 間に空セル1個まで跨ぎ可
      - 数値セル含む場合は除外
      - 単位セルは連結対象外
      - 期間セルだけの連結は candidate にしない
    """
    candidates: list[ReconstructionCandidate] = []
    normalized_row = [normalize_header_cell(c) for c in row]
    n = len(normalized_row)

    # 各セル単体のスコアを先に計算 (over-merging 防止用)
    cell_scores: list[float] = []
    for c in normalized_row:
        if is_empty_cell(c) or is_unit_cell(c) or is_numeric_cell(c):
            cell_scores.append(0)
        else:
            cs = score_metric_header(c)
            cell_scores.append(max(cs["sales"].total_score, cs["profit"].total_score))

    for width in range(2, min(5, n + 1)):  # 2〜4セル
        for start in range(n - width + 1):
            window = normalized_row[start:start + width]
            raw_window = row[start:start + width]

            # 数値セル混在チェック
            if any(is_numeric_cell(c) for c in window):
                continue
            # 単位セル混在チェック
            if any(is_unit_cell(c) for c in window):
                continue

            # Over-merging 防止: 個別セルが完成品 (exact match >=90) なら連結しない
            # 「売上」(80) は fragment → 連結OK、「売上高」(100) は完成品 → 連結拒否
            window_scores = cell_scores[start:start + width]
            if any(s >= 90 for s in window_scores):
                continue

            # 非空セルを収集 (空セルは跨ぎカウント)
            parts: list[str] = []
            empty_count = 0
            for c in window:
                if is_empty_cell(c):
                    empty_count += 1
                    if empty_count > 1:
                        break
                else:
                    parts.append(c)

            if empty_count > 1:
                continue
            if len(parts) < 2:
                continue

            # 期間語だけの連結は候補にしない
            if all(is_period_modifier(p) for p in parts):
                continue

            merged = "".join(parts)
            # 辞書照合でスコア付与
            scores = score_metric_header(merged)
            best_score = max(scores["sales"].total_score, scores["profit"].total_score)

            # 連結後のスコアが個別セルの最高値を超えない場合は不採用
            # ただし連結後が exact match なら許可 (「親会社株主に帰属する当期純利益」等)
            max_individual = max(window_scores) if window_scores else 0
            merged_is_exact = scores["sales"].exact_match or scores["profit"].exact_match
            if best_score < max_individual and not merged_is_exact:
                continue
            if best_score <= max_individual and not merged_is_exact:
                continue

            if best_score >= 50:  # 辞書ヒット閾値
                metric_type = "sales" if scores["sales"].total_score >= scores["profit"].total_score else "profit"
                matched_term = scores[metric_type].matched_terms[0] if scores[metric_type].matched_terms else ""
                candidates.append(ReconstructionCandidate(
                    parts=parts,
                    result=merged,
                    merge_type="horizontal",
                    positions=[{"row": row_idx, "cols": list(range(start, start + width))}],
                    score=best_score,
                    matched_dict_term=matched_term,
                ))

    return candidates


# ============================================================
# Reconstruction: Vertical (同一列の上下セル連結)
# ============================================================

def reconstruct_vertical(
    matrix: list[list[str]],
) -> list[ReconstructionCandidate]:
    """同一列の上下2〜3セルを連結して候補生成。

    条件:
      - 下段が補完語の場合は優先度高
      - 上段/下段の片方が空なら片側のみ
      - 数値セル混在は除外
    """
    candidates: list[ReconstructionCandidate] = []
    if len(matrix) < 2:
        return candidates

    # 正規化
    norm_matrix = []
    for row in matrix:
        norm_matrix.append([normalize_header_cell(c) for c in row])

    max_cols = max(len(r) for r in norm_matrix) if norm_matrix else 0

    for col_idx in range(max_cols):
        col_vals: list[tuple[int, str]] = []
        for row_idx, row in enumerate(norm_matrix):
            val = row[col_idx] if col_idx < len(row) else ""
            if not is_empty_cell(val) and not is_unit_cell(val) and not is_numeric_cell(val):
                col_vals.append((row_idx, val))

        # 2〜3セル連結
        for width in range(2, min(4, len(col_vals) + 1)):
            for start in range(len(col_vals) - width + 1):
                parts = [v[1] for v in col_vals[start:start + width]]
                row_indices = [v[0] for v in col_vals[start:start + width]]

                # 期間語だけの連結は候補にしない
                if all(is_period_modifier(p) for p in parts):
                    continue

                merged = "".join(parts)
                scores = score_metric_header(merged)
                best_score = max(scores["sales"].total_score, scores["profit"].total_score)

                # 補完語ボーナス
                last_part = parts[-1]
                if last_part in _COMPLEMENT_SUFFIXES:
                    best_score = min(best_score * 1.2, 100)

                if best_score >= 50:
                    metric_type = "sales" if scores["sales"].total_score >= scores["profit"].total_score else "profit"
                    matched_term = scores[metric_type].matched_terms[0] if scores[metric_type].matched_terms else ""
                    candidates.append(ReconstructionCandidate(
                        parts=parts,
                        result=merged,
                        merge_type="vertical",
                        positions=[{"col": col_idx, "rows": row_indices}],
                        score=best_score,
                        matched_dict_term=matched_term,
                    ))

    return candidates


# ============================================================
# Candidate Resolution (2段目: 競合解消 → 列ごと最終決定)
# ============================================================

def generate_reconstruction_candidates(
    matrix: list[list[str]],
) -> list[ReconstructionCandidate]:
    """全候補を生成 (horizontal + vertical)。"""
    all_candidates: list[ReconstructionCandidate] = []

    # Horizontal
    for row_idx, row in enumerate(matrix):
        all_candidates.extend(reconstruct_horizontal(row, row_idx))

    # Vertical
    all_candidates.extend(reconstruct_vertical(matrix))

    # スコア降順
    all_candidates.sort(key=lambda c: -c.score)
    return all_candidates


def resolve_reconstruction_candidates(
    matrix: list[list[str]],
    candidates: list[ReconstructionCandidate],
) -> list[ReconstructedColumnHeader]:
    """候補から列ごとに最も妥当なものを採用。

    列にまだ候補がなければ raw header を保持。
    """
    max_cols = max(len(r) for r in matrix) if matrix else 0
    resolved: dict[int, ReconstructedColumnHeader] = {}

    # 候補からの採用 (高スコア順)
    used_cols: set[int] = set()

    for cand in candidates:
        # 候補がカバーする列を特定
        covered_cols: list[int] = []
        if cand.merge_type == "horizontal":
            for pos in cand.positions:
                covered_cols.extend(pos.get("cols", []))
        elif cand.merge_type == "vertical":
            for pos in cand.positions:
                covered_cols.append(pos.get("col", -1))

        # 既に解決済みの列とは競合
        if any(c in used_cols for c in covered_cols):
            continue

        # 代表列 (最初の列) に割り当て
        primary_col = covered_cols[0] if covered_cols else 0

        # original fragments 収集
        original_frags = cand.parts
        norm_frags = [normalize_header_cell(p) for p in cand.parts]

        resolved[primary_col] = ReconstructedColumnHeader(
            col_idx=primary_col,
            original_fragments=original_frags,
            normalized_fragments=norm_frags,
            reconstructed_text=cand.result,
            merge_type=cand.merge_type,
            confidence=cand.score / 100.0,
            applied_steps=[{
                "type": cand.merge_type,
                "parts": cand.parts,
                "result": cand.result,
                "score": cand.score,
                "dict_term": cand.matched_dict_term,
            }],
        )
        for c in covered_cols:
            used_cols.add(c)

    # 未解決列は raw header で埋める
    for col_idx in range(max_cols):
        if col_idx not in resolved:
            # 各行から当該列のセルを取得
            frags = []
            for row in matrix:
                if col_idx < len(row):
                    val = normalize_header_cell(row[col_idx])
                    if val and not is_empty_cell(val) and not is_unit_cell(val):
                        frags.append(val)
            text = "".join(frags) if frags else ""
            resolved[col_idx] = ReconstructedColumnHeader(
                col_idx=col_idx,
                original_fragments=frags,
                normalized_fragments=[normalize_header_cell(f) for f in frags],
                reconstructed_text=text,
                merge_type=None,
                confidence=0.0,
                applied_steps=[],
            )

    # 列番号順に整列
    return [resolved[i] for i in sorted(resolved.keys())]


# ============================================================
# Main Entry Point
# ============================================================

def build_reconstructed_headers(
    header_matrix: list[list[str]],
    *,
    enable_reconstruction: bool | None = None,
) -> ReconstructionResult:
    """ヘッダーマトリクスから復元ヘッダーを構築。

    Args:
        header_matrix: header 行ごとのセルリスト (2D)
            例: [["売上", "営業"], ["高", "利益"]]
        enable_reconstruction: feature flag override (None → global)

    Returns:
        ReconstructionResult with column_headers, steps, candidates
    """
    enabled = enable_reconstruction if enable_reconstruction is not None else ENABLE_SPLIT_HEADER_RECONSTRUCTION

    result = ReconstructionResult(
        original_matrix=[row[:] for row in header_matrix],
        feature_enabled=enabled,
    )

    if not header_matrix:
        return result

    # 正規化マトリクス
    norm_matrix: list[list[str]] = []
    for row in header_matrix:
        norm_row = [normalize_header_cell(c) for c in row]
        # 単位セル・注記を除去
        norm_row_clean = [c if not is_unit_cell(c) else "" for c in norm_row]
        norm_matrix.append(norm_row_clean)
    result.normalized_matrix = norm_matrix

    if not enabled:
        # 復元無効時は正規化のみでカラムヘッダー生成
        max_cols = max(len(r) for r in norm_matrix) if norm_matrix else 0
        for col_idx in range(max_cols):
            frags = []
            for row in norm_matrix:
                if col_idx < len(row) and row[col_idx]:
                    frags.append(row[col_idx])
            text = "".join(frags)
            result.column_headers.append(ReconstructedColumnHeader(
                col_idx=col_idx,
                original_fragments=frags,
                reconstructed_text=text,
            ))
        result.reconstructed_headers = [ch.reconstructed_text for ch in result.column_headers]
        return result

    # 候補生成
    candidates = generate_reconstruction_candidates(norm_matrix)
    result.candidates = candidates

    # 候補解決
    column_headers = resolve_reconstruction_candidates(norm_matrix, candidates)
    result.column_headers = column_headers
    result.reconstructed_headers = [ch.reconstructed_text for ch in column_headers]

    # steps ログ
    for ch in column_headers:
        for step in ch.applied_steps:
            result.steps.append(step)

    return result


# ============================================================
# Convenience: legacy compatibility wrapper
# ============================================================

def reconstruct_from_lines(
    header_lines: list[str],
    *,
    enable_reconstruction: bool | None = None,
) -> ReconstructionResult:
    """テキスト行リストからヘッダーマトリクスを構築して復元。

    既存の segment_detection_v2.py との互換向け。
    header_lines は「2space+ or tab」で分割してマトリクス化。
    さらに、単一トークンが複数のmetric語を含む場合は
    単一スペースで再分割する。
    """
    import re as _re
    from .header_analysis import _is_unit_or_note_line

    # 単位行除去 + 自然文除去
    _NARRATIVE_KW = [
        "こうした中", "当社グループ", "詳細については", "ご覧ください",
        "ページをご覧", "をベースとした", "については", "以下のとおり",
        "報告セグメントの利益は", "セグメント間の内部",
        "なお、", "また、", "ただし、",
    ]
    effective_lines: list[str] = []
    for line in header_lines:
        if _is_unit_or_note_line(line):
            continue
        # 自然文判定: 長文 + 句読点 → ヘッダーではない
        _stripped = line.strip()
        if len(_stripped) >= 40 and ("。" in _stripped or "、" in _stripped):
            continue
        # narrative キーワードを含む → 除外
        if any(kw in _stripped for kw in _NARRATIVE_KW):
            continue
        # 数値のみヘッダー → 除外 (例: '15000', '1,234', '2025')
        _tokens_check = _re.split(r'\s{2,}|\t', _stripped)
        _tokens_check = [t.strip() for t in _tokens_check if t.strip()]
        if _tokens_check and all(
            _re.fullmatch(r'[△▲\-]?\s*[\d,]+(?:\.\d+)?%?', t) for t in _tokens_check
        ):
            continue
        effective_lines.append(line)
    if not effective_lines:
        effective_lines = header_lines

    # トークン化
    matrix: list[list[str]] = []
    for line in effective_lines:
        tokens = _re.split(r'\s{2,}|\t', line.strip())
        tokens = [t.strip() for t in tokens if t.strip()]
        # multi-metric token 分割
        expanded = _expand_multi_metric_tokens(tokens)
        matrix.append(expanded)

    return build_reconstructed_headers(
        matrix,
        enable_reconstruction=enable_reconstruction,
    )


# metric 語セット (分割判定用 — 正規化後で照合)
_METRIC_SPLIT_TERMS: set[str] = set()
for _kw, _sc in _SALES_DICT + _PROFIT_DICT:
    _METRIC_SPLIT_TERMS.add(normalize_header_cell(_kw))


def _expand_multi_metric_tokens(tokens: list[str]) -> list[str]:
    """単一スペースで区切られた複数 metric 語を展開する。

    例: '売上高 営業利益 経常利益 当期純利益'
      → ['売上高', '営業利益', '経常利益', '当期純利益']

    展開条件:
      - 単一スペースで分割した各パーツの多数 (>=50%) が
        metric 辞書に正規化マッチする
      - パーツが 2 個以上
      - そうでなければ元のまま
    """
    result: list[str] = []
    for token in tokens:
        # 単一スペースで分割可能か
        parts = token.split()
        if len(parts) >= 2:
            # 各パーツが metric に該当するかチェック
            metric_hits = 0
            for part in parts:
                norm_part = normalize_header_cell(part)
                if norm_part in _METRIC_SPLIT_TERMS:
                    metric_hits += 1
                elif is_period_modifier(norm_part):
                    # 期間修飾語は metric とみなす (「前期 売上高」etc)
                    metric_hits += 1
            if metric_hits >= 2 and metric_hits / len(parts) >= 0.5:
                result.extend(parts)
                continue
        result.append(token)
    return result


# ============================================================
# descriptive_only_header 判定
# ============================================================

# 説明見出しパターン — これらだけで構成される header は列ラベルではない
_DESCRIPTIVE_HEADER_PATTERNS: list[re.Pattern] = [
    re.compile(r'^\s*[（(]\s*\d+\s*[）)]\s*報告セグメント', re.IGNORECASE),
    re.compile(r'^\s*[（(]\s*\d+\s*[）)]\s*セグメント情報', re.IGNORECASE),
    re.compile(r'^\s*[（(]\s*\d+\s*[）)]\s*$'),  # 節番号のみ
    re.compile(r'報告セグメント(に関する)?情報'),
    re.compile(r'セグメント情報'),
    re.compile(r'報告セグメントの概要'),
    re.compile(r'収益及び業績は以下のとおり'),
    re.compile(r'利益は.*をベース'),
    re.compile(r'以下のとおりであります'),
    re.compile(r'以下のとおりです'),
    re.compile(r'セグメント間の内部'),
    re.compile(r'^\s*[（(]注[）)]\s*'),
    re.compile(r'^\s*注[)）]\s*'),
]

# 実表の列ラベル語 — これらが含まれる行は descriptive ではない
_REAL_COLUMN_LABEL_KW = [
    "売上高", "売上収益", "営業収益", "経常収益", "revenue",
    "営業利益", "セグメント利益", "事業利益", "経常利益",
    "コア営業利益", "EBITDA", "segment profit",
    "セグメント損益", "利益又は損失",
    "セグメント資産", "調整額", "減価償却費", "設備投資",
]


def is_descriptive_segment_header(texts: list[str]) -> bool:
    """ヘッダー行群が説明見出しだけで構成されているか判定する。

    True の場合、そのヘッダーをそのまま列ロール判定に渡すべきではない。

    判定ロジック:
    - 全行が descriptive パターンまたは空行にマッチ
    - かつ実表の列ラベル語が独立行として含まれない
      (長文中の言及は除外 — "営業利益をベースとした" は列ラベルではない)
    """
    if not texts:
        return True

    non_empty = [t.strip() for t in texts if t.strip()]
    if not non_empty:
        return True

    # 実表の列ラベル語が独立行 (短文) に含まれるなら descriptive ではない
    for line in non_empty:
        # 長文 (句読点含む) は文中言及なのでスキップ
        if len(line) >= 20 and ("。" in line or "、" in line):
            continue
        # descriptive パターンにマッチする行は文中言及
        _is_desc_line = any(pat.search(line) for pat in _DESCRIPTIVE_HEADER_PATTERNS)
        if _is_desc_line:
            continue
        line_lower = line.lower()
        for kw in _REAL_COLUMN_LABEL_KW:
            if kw.lower() in line_lower:
                return False

    # 全行が descriptive パターンまたは自然文(句読点含む長文)にマッチするか
    for line in non_empty:
        is_desc = False
        # descriptive パターンチェック
        for pat in _DESCRIPTIVE_HEADER_PATTERNS:
            if pat.search(line):
                is_desc = True
                break
        # 自然文チェック: 句読点含む長文
        if not is_desc and len(line) >= 30 and ("。" in line or "、" in line):
            is_desc = True
        # 短い節番号 ((1), (2))
        if not is_desc and re.fullmatch(r'\s*[（(]\s*\d+\s*[）)]\s*', line):
            is_desc = True
        # descriptive でもなく、数値行でもなく、短い token なら unknown → descriptive 扱い
        if not is_desc and len(line) < 10 and not re.search(r'\d', line):
            is_desc = True
        if not is_desc:
            return False

    return True


# ============================================================
# header row splitting for role detection
# ============================================================

# 期間/比較 secondary パターン
_PERIOD_COMPARISON_PATTERNS: list[re.Pattern] = [
    re.compile(r'\d+月\d+日に終了した'),
    re.compile(r'\d+年\s*\d+月\s*\d+日'),  # 2024年12月31日
    re.compile(r'^\s*\d{4}年\s*$'),  # 2024年
    re.compile(r'^\s*\d{4}年\s+\d{4}年'),  # 2024年 2025年
    re.compile(r'前年同期比'),
    re.compile(r'対前期'),
    re.compile(r'^\s*増減\s*$'),
    re.compile(r'^\s*増減率\s*$'),
    re.compile(r'^\s*増減\s+増減率'),
    re.compile(r'^\s*前期\s+当期'),
    re.compile(r'^\s*前年度?\s+当年度?'),
    re.compile(r'自\s*\d{4}年', re.IGNORECASE),
    re.compile(r'至\s*\d{4}年', re.IGNORECASE),
]


def split_header_rows_for_role_detection(
    header_rows: list[str],
) -> dict[str, list[str]]:
    """ヘッダー行を primary (メトリクス行) と secondary (期間/比較行) に分離。

    Returns:
        {"primary": [...], "secondary": [...]}
    """
    primary: list[str] = []
    secondary: list[str] = []

    for row in header_rows:
        stripped = row.strip()
        if not stripped:
            continue

        is_secondary = False
        for pat in _PERIOD_COMPARISON_PATTERNS:
            if pat.search(stripped):
                is_secondary = True
                break

        # 実表のメトリクス語を含む場合は primary 優先
        if is_secondary:
            stripped_lower = stripped.lower()
            for kw in _REAL_COLUMN_LABEL_KW:
                if kw.lower() in stripped_lower:
                    is_secondary = False
                    break

        if is_secondary:
            secondary.append(row)
        else:
            primary.append(row)

    # primary が空でも fallback しない — D-0 側で判定する
    return {"primary": primary, "secondary": secondary}


