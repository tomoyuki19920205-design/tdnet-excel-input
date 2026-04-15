# ============================================================
# column_analysis.py — Phase D: 列ロール分類
# ============================================================
"""
セグメント表の各列に role (売上/利益/比率/ラベル等) を
スコアベースで付与する。

v2 Phase 2: 利益taxonomy強化 — profit を operating_profit_like / segment_profit_like /
ordinary_profit_like / pretax_like / net_income_like に詳細化。
margin_like / assets_like / depreciation_like / capex_like / other_metric を追加。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .header_analysis import (
    normalize_header,
    score_header_role,
    detect_numeric_columns,
    HeaderRole,
    _NUM_PATTERN,
)


# ============================================================
# ColumnRole (taxonomy 拡張)
# ============================================================

class ColumnRole:
    """列ロール定数 (Phase 2 taxonomy + sales 細分化)"""
    SEGMENT_LABEL = "segment_label"
    SALES = "sales"
    EXTERNAL_SALES = "external_sales"       # 外部顧客への売上高
    INTERNAL_SALES = "internal_sales"       # セグメント間内部売上
    TOTAL_SALES_LIKE = "total_sales_like"   # 計 (外部+内部の合計)
    # --- profit taxonomy ---
    OPERATING_PROFIT = "operating_profit"       # 後方互換
    SEGMENT_PROFIT = "segment_profit"           # 後方互換
    ORDINARY_PROFIT = "ordinary_profit"         # 後方互換
    OPERATING_PROFIT_LIKE = "operating_profit_like"
    SEGMENT_PROFIT_LIKE = "segment_profit_like"
    ORDINARY_PROFIT_LIKE = "ordinary_profit_like"
    PRETAX_LIKE = "pretax_like"
    NET_INCOME_LIKE = "net_income_like"
    # --- non-profit metrics ---
    MARGIN_LIKE = "margin_like"
    ASSETS_LIKE = "assets_like"
    DEPRECIATION_LIKE = "depreciation_like"
    CAPEX_LIKE = "capex_like"
    OTHER_METRIC = "other_metric"
    # --- general ---
    RATIO = "ratio"
    YOY = "yoy"
    NOTES = "notes"
    UNKNOWN = "unknown"

    # 採用可能profit_like (セグメントテーブル判定で高スコア)
    ADOPTABLE_PROFIT_ROLES = {
        OPERATING_PROFIT, SEGMENT_PROFIT, ORDINARY_PROFIT,
        OPERATING_PROFIT_LIKE, SEGMENT_PROFIT_LIKE,
        ORDINARY_PROFIT_LIKE,
    }
    # 全profit roles (弱いものも含む)
    ALL_PROFIT_ROLES = ADOPTABLE_PROFIT_ROLES | {PRETAX_LIKE, NET_INCOME_LIKE}

    # 全sales roles (外部/内部/計含む)
    ALL_SALES_ROLES = {SALES, EXTERNAL_SALES, TOTAL_SALES_LIKE}
    # 内部売上は sales 本命にしない
    ALL_SALES_ROLES_WITH_INTERNAL = ALL_SALES_ROLES | {INTERNAL_SALES}

    # 後方互換の PROFIT_ROLES
    PROFIT_ROLES = {OPERATING_PROFIT, SEGMENT_PROFIT, ORDINARY_PROFIT,
                    OPERATING_PROFIT_LIKE, SEGMENT_PROFIT_LIKE,
                    ORDINARY_PROFIT_LIKE, PRETAX_LIKE, NET_INCOME_LIKE}


# ============================================================
# 利益 taxonomy キーワード
# ============================================================

_OPERATING_PROFIT_LIKE_KW: list[tuple[str, float]] = [
    ("営業利益", 1.0),
    ("営業損失", 0.9),
    ("事業利益", 0.9),
    ("事業損失", 0.85),
    ("コア営業利益", 0.95),
    ("Operatingprofit", 1.0),
    ("Operatingincome", 1.0),
    ("Coreoperatingprofit", 0.95),
    ("Adjustedoperatingprofit", 0.9),
    # Phase 5 追加
    ("CoreOperatingIncome", 0.95),
    ("AdjustedOperatingIncome", 0.9),
]

_SEGMENT_PROFIT_LIKE_KW: list[tuple[str, float]] = [
    ("セグメント利益", 1.0),
    ("セグメント損益", 1.0),
    ("セグメント利益又は損失", 1.0),
    ("利益又は損失", 0.95),
    ("利益(損失)", 0.9),
    ("利益（損失）", 0.9),
    ("利益", 0.35),
    ("損益", 0.7),
    ("損失", 0.5),
    ("Segmentprofit", 1.0),
    ("Segmentincome", 1.0),
    ("Segmentprofitorloss", 1.0),
    ("Profitorloss", 0.9),
    # Phase 5 追加
    ("Adjustedprofit", 0.85),
    # Phase C partial 補完追加
    ("セグメント損失", 0.9),
    ("利益損失", 0.7),
    # Phase 2-2 追加
    ("事業損益", 0.85),
    ("EBITDA", 0.9),
    # Phase 3: weak_header 追加
    ("部門損益", 0.7),
    ("調整後利益", 0.7),
]

_ORDINARY_PROFIT_LIKE_KW: list[tuple[str, float]] = [
    ("経常利益", 1.0),
    ("経常損失", 0.9),
    ("Ordinaryincome", 1.0),
    ("Ordinaryprofit", 1.0),
]

_PRETAX_LIKE_KW: list[tuple[str, float]] = [
    ("税引前利益", 1.0),
    ("税前利益", 0.9),
    ("Profitbeforetax", 1.0),
    ("Incomebeforetax", 1.0),
]

_NET_INCOME_LIKE_KW: list[tuple[str, float]] = [
    ("当期純利益", 1.0),
    ("純利益", 0.9),
    ("Netincome", 1.0),
    ("Netprofit", 0.9),
    ("Profit", 0.5),
]

_MARGIN_LIKE_KW: list[tuple[str, float]] = [
    ("利益率", 1.0),
    ("営業利益率", 1.0),
    ("マージン", 1.0),
    ("Margin", 1.0),
    ("Profitmargin", 1.0),
]

_ASSETS_LIKE_KW: list[tuple[str, float]] = [
    ("セグメント資産", 1.0),
    ("資産", 0.8),
    ("総資産", 0.9),
    ("Segmentassets", 1.0),
    ("Totalassets", 0.9),
    ("Assets", 0.7),
]

_DEPRECIATION_LIKE_KW: list[tuple[str, float]] = [
    ("減価償却費", 1.0),
    ("償却費", 0.8),
    ("Depreciation", 1.0),
    ("Depreciationandamortization", 1.0),
]

_CAPEX_LIKE_KW: list[tuple[str, float]] = [
    ("設備投資", 1.0),
    ("設備投資額", 1.0),
    ("資本的支出", 0.9),
    ("Capitalexpenditure", 1.0),
    ("Capex", 1.0),
]


# ============================================================
# Sales 細分化キーワード
# ============================================================

_EXTERNAL_SALES_KW: list[tuple[str, float]] = [
    ("外部顧客への売上高", 1.0),
    ("外部顧客への売上収益", 1.0),
    ("外部顧客売上高", 1.0),
    ("外部売上高", 0.95),
    ("外部売上", 0.9),
    ("外部顧客売上", 0.95),
    ("Revenuetoexternalcustomers", 1.0),
    ("Salestoexternalcustomers", 1.0),
    ("Externalrevenue", 0.9),
    ("Externalsales", 0.9),
    # Phase 2-2 追加
    ("セグメント売上高", 0.95),
    ("営業収益", 0.85),
    ("経常収益", 0.4),  # 低スコア補助
    # Phase 3: weak_header 追加
    ("収入", 0.3),
    ("営業収入", 0.5),
    ("外部収入", 0.6),
    ("セグメント収益", 0.7),
    ("外部顧客への売上収益", 0.95),
]

_INTERNAL_SALES_KW: list[tuple[str, float]] = [
    ("セグメント間の内部売上高", 1.0),
    ("セグメント間内部売上", 1.0),
    ("内部売上高", 0.9),
    ("内部売上", 0.85),
    ("内部取引", 0.7),
    ("セグメント間売上", 0.85),
    ("Intersegmentsales", 1.0),
    ("Intersegmentrevenue", 1.0),
    ("Internalsales", 0.9),
]

_TOTAL_SALES_LIKE_KW: list[tuple[str, float]] = [
    ("計", 0.6),  # 「計」単独は弱め (セグメント表文脈で加点)
    ("合計", 0.4),  # 合計は行ラベル向きなので更に弱め
    ("小計", 0.3),
    ("Total", 0.5),
    ("Subtotal", 0.3),
]


def _score_taxonomy(text: str) -> dict[str, float]:
    """列 taxonomy スコアリング (sales 細分化対応)"""
    nh = normalize_header(text)
    scores: dict[str, float] = {
        ColumnRole.SALES: 0.0,
        ColumnRole.EXTERNAL_SALES: 0.0,
        ColumnRole.INTERNAL_SALES: 0.0,
        ColumnRole.TOTAL_SALES_LIKE: 0.0,
        ColumnRole.OPERATING_PROFIT_LIKE: 0.0,
        ColumnRole.SEGMENT_PROFIT_LIKE: 0.0,
        ColumnRole.ORDINARY_PROFIT_LIKE: 0.0,
        ColumnRole.PRETAX_LIKE: 0.0,
        ColumnRole.NET_INCOME_LIKE: 0.0,
        ColumnRole.MARGIN_LIKE: 0.0,
        ColumnRole.ASSETS_LIKE: 0.0,
        ColumnRole.DEPRECIATION_LIKE: 0.0,
        ColumnRole.CAPEX_LIKE: 0.0,
        ColumnRole.SEGMENT_LABEL: 0.0,
        ColumnRole.RATIO: 0.0,
        ColumnRole.YOY: 0.0,
        ColumnRole.UNKNOWN: 0.0,
    }

    def _best(kws: list[tuple[str, float]]) -> float:
        best = 0.0
        for kw, s in kws:
            nkw = normalize_header(kw)
            if nkw in nh:
                best = max(best, s)
        return best

    scores[ColumnRole.OPERATING_PROFIT_LIKE] = _best(_OPERATING_PROFIT_LIKE_KW)
    scores[ColumnRole.SEGMENT_PROFIT_LIKE] = _best(_SEGMENT_PROFIT_LIKE_KW)
    scores[ColumnRole.ORDINARY_PROFIT_LIKE] = _best(_ORDINARY_PROFIT_LIKE_KW)
    scores[ColumnRole.PRETAX_LIKE] = _best(_PRETAX_LIKE_KW)
    scores[ColumnRole.NET_INCOME_LIKE] = _best(_NET_INCOME_LIKE_KW)
    scores[ColumnRole.MARGIN_LIKE] = _best(_MARGIN_LIKE_KW)
    scores[ColumnRole.ASSETS_LIKE] = _best(_ASSETS_LIKE_KW)
    scores[ColumnRole.DEPRECIATION_LIKE] = _best(_DEPRECIATION_LIKE_KW)
    scores[ColumnRole.CAPEX_LIKE] = _best(_CAPEX_LIKE_KW)

    # Sales 細分化
    scores[ColumnRole.EXTERNAL_SALES] = _best(_EXTERNAL_SALES_KW)
    scores[ColumnRole.INTERNAL_SALES] = _best(_INTERNAL_SALES_KW)
    scores[ColumnRole.TOTAL_SALES_LIKE] = _best(_TOTAL_SALES_LIKE_KW)

    # Sales は既存 header_analysis から (後方互換)
    header_scores = score_header_role(text)
    base_sales = header_scores.get("sales", 0.0)

    # 内部売上が検出された場合は base_sales を抑制
    # (「セグメント間の内部売上高」に「売上高」が含まれるため高スコアを返すのを防ぐ)
    if scores[ColumnRole.INTERNAL_SALES] > 0:
        base_sales = min(base_sales, scores[ColumnRole.INTERNAL_SALES] * 0.3)

    scores[ColumnRole.SALES] = base_sales

    # 外部売上が検出されたら SALES も高くする
    if scores[ColumnRole.EXTERNAL_SALES] > 0:
        scores[ColumnRole.SALES] = max(scores[ColumnRole.SALES], scores[ColumnRole.EXTERNAL_SALES])

    # 「計」がセグメント表文脈(他に外部/内部売上列がない単独ヘッダー)の場合は SALES 候補
    if scores[ColumnRole.TOTAL_SALES_LIKE] > 0 and scores[ColumnRole.EXTERNAL_SALES] == 0:
        scores[ColumnRole.SALES] = max(scores[ColumnRole.SALES], scores[ColumnRole.TOTAL_SALES_LIKE])

    # 内部売上のみが SALES に寄与する場合は低スコアに抑制
    if scores[ColumnRole.INTERNAL_SALES] > 0 and scores[ColumnRole.EXTERNAL_SALES] == 0:
        scores[ColumnRole.SALES] = max(scores[ColumnRole.SALES], scores[ColumnRole.INTERNAL_SALES] * 0.3)

    scores[ColumnRole.RATIO] = header_scores.get("ratio", 0.0)

    # 競合解消: margin_like が高い場合 profit 系を減点
    if scores[ColumnRole.MARGIN_LIKE] > 0:
        if "率" in nh or "margin" in nh.lower():
            for role in [ColumnRole.OPERATING_PROFIT_LIKE, ColumnRole.SEGMENT_PROFIT_LIKE,
                         ColumnRole.ORDINARY_PROFIT_LIKE]:
                scores[role] = min(scores[role], 0.1)

    # セグメント利益 > 営業利益 (セグメント表コンテキスト)
    if scores[ColumnRole.SEGMENT_PROFIT_LIKE] > 0 and "セグメント" in nh:
        scores[ColumnRole.OPERATING_PROFIT_LIKE] = min(
            scores[ColumnRole.OPERATING_PROFIT_LIKE], 0.3
        )

    return scores


# ============================================================
# ColumnAnalysisResult
# ============================================================

@dataclass
class ColumnAnalysisResult:
    """列分類の結果"""
    column_roles: list[str] = field(default_factory=list)
    role_score_breakdown: list[dict[str, float]] = field(default_factory=list)
    best_sales_col: int | None = None
    best_profit_col: int | None = None
    label_col_candidates: list[int] = field(default_factory=list)
    confidence: float = 0.0

    @property
    def has_sales(self) -> bool:
        return self.best_sales_col is not None

    @property
    def has_profit(self) -> bool:
        return self.best_profit_col is not None

    @property
    def profit_role(self) -> str:
        if self.best_profit_col is not None and self.best_profit_col < len(self.column_roles):
            return self.column_roles[self.best_profit_col]
        return ""


# ============================================================
# Phase 3: 列スコアリング関数
# ============================================================

# 除外ヘッダーキーワード (ratio/count/quantity 列を sales/profit から除外)
_COL_EXCLUDE_KW = ["構成比", "割合", "件数", "数量", "比率", "増減率",
                   "前年比", "前年同期比", "前期比", "率", "%", "％",
                   "margin", "ratio"]

_COL_SALES_PREFER_KW = ["売上", "収益", "営業収益", "売上高", "売上収益",
                         "セグメント売上", "収入", "営業収入", "外部", "Revenue",
                         "Sales", "Income"]

_COL_PROFIT_PREFER_KW = ["利益", "損益", "損失", "セグメント利益", "営業利益",
                          "事業利益", "EBITDA", "部門損益", "調整後利益",
                          "Profit", "OperatingProfit"]

import logging as _col_logging
_col_logger = _col_logging.getLogger("tdnet.column_scoring")


def _compute_column_score(
    col_idx: int,
    data_rows: list[list[str]],
    all_scores: list[dict[str, float]],
    all_roles: list[str],
    header: str | list[str],
) -> dict:
    """
    Phase 3: 各数値列に sales/profit としてのスコアを算出する。

    Returns:
        {"col": col_idx, "sales_score": float, "profit_score": float,
         "fill_rate": float, "magnitude": float, ...}
    """
    hdr = header[col_idx] if isinstance(header, list) and col_idx < len(header) else (
        header if isinstance(header, str) else ""
    )
    hdr_lower = hdr.replace(" ", "").lower() if hdr else ""

    # --- 値収集 ---
    vals: list[float] = []
    total_cells = 0
    pct_count = 0
    decimal_count = 0
    for row in data_rows:
        if col_idx < len(row):
            cell = row[col_idx].strip()
            if not cell:
                continue
            total_cells += 1
            if "%" in cell or "％" in cell:
                pct_count += 1
            if "." in cell:
                decimal_count += 1
            clean = cell.replace(",", "").replace("△", "-").replace("▲", "-").replace("－", "-")
            try:
                vals.append(float(clean))
            except ValueError:
                pass

    fill_rate = len(vals) / max(total_cells, 1) if total_cells > 0 else 0
    abs_vals = sorted(abs(v) for v in vals) if vals else []
    median = abs_vals[len(abs_vals) // 2] if abs_vals else 0

    # --- ratio penalty ---
    ratio_penalty = 0.0
    if total_cells > 0:
        if pct_count / total_cells >= 0.3:
            ratio_penalty = 1.0
        elif decimal_count / total_cells >= 0.5 and median < 200:
            ratio_penalty = 0.8
        elif median < 10 and abs_vals and max(abs_vals) < 200:
            ratio_penalty = 0.6
    if any(ex in hdr_lower for ex in _COL_EXCLUDE_KW):
        ratio_penalty = max(ratio_penalty, 1.0)

    # --- count penalty (件数/数量系) ---
    count_penalty = 0.0
    _count_kw = ["件数", "数量", "人数", "台数", "店舗数", "count", "units"]
    if any(ck in hdr_lower for ck in _count_kw):
        count_penalty = 1.0

    # --- magnitude_score (正規化: 値が大きいほど sales 候補) ---
    magnitude_score = 0.0
    if median > 0:
        import math
        magnitude_score = min(math.log10(median + 1) / 6.0, 1.0)  # 1M → 1.0

    # --- header_sales_score ---
    header_sales_score = 0.0
    if any(kw.lower() in hdr_lower for kw in _COL_SALES_PREFER_KW if kw):
        header_sales_score = 1.0

    # --- header_profit_score ---
    header_profit_score = 0.0
    if any(kw.lower() in hdr_lower for kw in _COL_PROFIT_PREFER_KW if kw):
        header_profit_score = 1.0

    # --- taxonomy score からの補助 ---
    if col_idx < len(all_scores):
        sc = all_scores[col_idx]
        for role in ColumnRole.ALL_SALES_ROLES:
            if sc.get(role, 0) > 0.3:
                header_sales_score = max(header_sales_score, 0.8)
        for role in ColumnRole.ALL_PROFIT_ROLES:
            if sc.get(role, 0) > 0.3:
                header_profit_score = max(header_profit_score, 0.8)

    # --- variance_score (適度なばらつきがあれば加点) ---
    variance_score = 0.0
    if len(abs_vals) >= 3:
        mean_v = sum(abs_vals) / len(abs_vals)
        if mean_v > 0:
            cv = (sum((v - mean_v)**2 for v in abs_vals) / len(abs_vals))**0.5 / mean_v
            variance_score = min(cv, 1.0)  # coefficient of variation

    # --- sales_score 合算 ---
    sales_score = (
        header_sales_score * 5
        + fill_rate * 2
        + magnitude_score * 2
        + variance_score * 1
        - ratio_penalty * 3
        - count_penalty * 2
    )

    # --- profit_score 合算 ---
    profit_score = (
        header_profit_score * 5
        + fill_rate * 2
        + variance_score * 1
        - ratio_penalty * 3
        - count_penalty * 2
    )

    _col_logger.debug(
        f"[COLSCORE] col={col_idx} sales={sales_score:.1f} profit={profit_score:.1f} "
        f"fill={fill_rate:.2f} mag={magnitude_score:.2f} var={variance_score:.2f} "
        f"ratio_pen={ratio_penalty:.1f} count_pen={count_penalty:.1f} "
        f"hdr={hdr!r}"
    )

    return {
        "col": col_idx,
        "sales_score": sales_score,
        "profit_score": profit_score,
        "fill_rate": fill_rate,
        "magnitude": magnitude_score,
        "variance": variance_score,
        "ratio_penalty": ratio_penalty,
        "count_penalty": count_penalty,
        "header": hdr,
    }


# ============================================================
# classify_columns
# ============================================================

def classify_columns(
    data_rows: list[list[str]],
    headers: list[str],
) -> ColumnAnalysisResult:
    """
    Phase D: 列ロール分類 (taxonomy 拡張版)。

    各列にスコアを付けて最高スコア列を sales/profit として採用。
    """
    n_cols = max(
        max((len(row) for row in data_rows), default=0),
        len(headers),
    )

    if n_cols == 0:
        return ColumnAnalysisResult()

    all_scores: list[dict[str, float]] = []
    all_roles: list[str] = []

    for col_idx in range(n_cols):
        # taxonomy スコア (ヘッダーベース)
        if col_idx < len(headers):
            scores = _score_taxonomy(headers[col_idx])
            # ヘッダー重み 60%
            for k in scores:
                scores[k] *= 0.6

            # YoY 判定
            nh = normalize_header(headers[col_idx])
            if any(kw in nh for kw in ["前年比", "前年同期比", "増減率", "増減額", "YoY"]):
                scores[ColumnRole.YOY] = scores.get(ColumnRole.YOY, 0) + 0.6
                scores[ColumnRole.RATIO] = scores.get(ColumnRole.RATIO, 0) + 0.3
        else:
            scores = {k: 0.0 for k in [
                ColumnRole.SALES, ColumnRole.OPERATING_PROFIT_LIKE,
                ColumnRole.SEGMENT_PROFIT_LIKE, ColumnRole.ORDINARY_PROFIT_LIKE,
                ColumnRole.PRETAX_LIKE, ColumnRole.NET_INCOME_LIKE,
                ColumnRole.MARGIN_LIKE, ColumnRole.ASSETS_LIKE,
                ColumnRole.DEPRECIATION_LIKE, ColumnRole.CAPEX_LIKE,
                ColumnRole.SEGMENT_LABEL, ColumnRole.RATIO, ColumnRole.YOY,
                ColumnRole.UNKNOWN,
            ]}

        # データ特性スコア
        col_values = [row[col_idx].strip() if col_idx < len(row) else "" for row in data_rows]
        non_empty = [v for v in col_values if v]

        if not non_empty:
            scores[ColumnRole.UNKNOWN] = 1.0
            all_scores.append(scores)
            all_roles.append(ColumnRole.UNKNOWN)
            continue

        total = len(non_empty)
        num_count = sum(1 for v in non_empty if _NUM_PATTERN.fullmatch(v))
        pct_count = sum(1 for v in non_empty if "%" in v or "％" in v)
        decimal_count = sum(1 for v in non_empty if "." in v)
        text_count = total - num_count
        num_ratio = num_count / total
        text_ratio = text_count / total

        # ラベル列
        if text_ratio >= 0.6:
            scores[ColumnRole.SEGMENT_LABEL] = scores.get(ColumnRole.SEGMENT_LABEL, 0) + 0.5
            for role in ColumnRole.ALL_PROFIT_ROLES | {ColumnRole.SALES, ColumnRole.MARGIN_LIKE}:
                if role in scores:
                    scores[role] *= 0.3

        # 数値列加点
        if num_ratio >= 0.5:
            for role in ColumnRole.ALL_PROFIT_ROLES | {ColumnRole.SALES}:
                if role in scores:
                    scores[role] += 0.15

        # 比率列
        if pct_count / total >= 0.3 or (decimal_count / total >= 0.5 and num_ratio >= 0.5):
            scores[ColumnRole.RATIO] = scores.get(ColumnRole.RATIO, 0) + 0.4
            scores[ColumnRole.MARGIN_LIKE] = scores.get(ColumnRole.MARGIN_LIKE, 0) + 0.3
            for role in ColumnRole.ALL_PROFIT_ROLES | {ColumnRole.SALES}:
                if role in scores:
                    scores[role] = min(scores[role], 0.1)

        # best role
        best_role = max(scores, key=lambda r: scores[r])
        if scores[best_role] < 0.1:
            best_role = ColumnRole.UNKNOWN

        all_scores.append(scores)
        all_roles.append(best_role)

    # best sales / profit 列 (外部売上優先、計をfallback)
    best_sales_col = None
    best_sales_score = 0.0
    best_sales_tier = 99  # 0=external, 1=sales, 2=total, 3=internal
    best_profit_col = None
    best_profit_score = 0.0
    best_profit_role = ""
    label_cols: list[int] = []

    for col_idx in range(n_cols):
        sc = all_scores[col_idx]

        # Sales 優先順: external_sales > sales > total_sales > internal_sales
        for tier, role in [
            (0, ColumnRole.EXTERNAL_SALES),
            (1, ColumnRole.SALES),
            (2, ColumnRole.TOTAL_SALES_LIKE),
            (3, ColumnRole.INTERNAL_SALES),
        ]:
            s = sc.get(role, 0)
            if s > 0.15 and (tier < best_sales_tier or (tier == best_sales_tier and s > best_sales_score)):
                best_sales_score = s
                best_sales_col = col_idx
                best_sales_tier = tier

        # 全profit rolesの最大 (adoptable 優先)
        for role in ColumnRole.ADOPTABLE_PROFIT_ROLES:
            if sc.get(role, 0) > best_profit_score:
                best_profit_score = sc[role]
                best_profit_col = col_idx
                best_profit_role = role
        # 弱いprofit roles も 50%スコアで考慮
        for role in {ColumnRole.PRETAX_LIKE, ColumnRole.NET_INCOME_LIKE}:
            weighted = sc.get(role, 0) * 0.5
            if weighted > best_profit_score:
                best_profit_score = weighted
                best_profit_col = col_idx
                best_profit_role = role

        if sc.get(ColumnRole.SEGMENT_LABEL, 0) >= 0.3:
            label_cols.append(col_idx)

    if best_sales_score < 0.15:
        best_sales_col = None
    if best_profit_score < 0.2:
        best_profit_col = None

    # --- Phase 3: sales 隣接利益昇格 ---
    # sales 列が見つかった場合、隣接列(±3)で弱い profit スコアを持つ列を昇格
    if best_sales_col is not None and best_profit_col is None:
        for offset in [1, -1, 2, -2, 3, -3]:
            adj_idx = best_sales_col + offset
            if 0 <= adj_idx < n_cols:
                adj_sc = all_scores[adj_idx]
                # Phase 5: ratio/yoy/margin/assets は profit 昇格対象から除外
                adj_role = all_roles[adj_idx] if adj_idx < len(all_roles) else ""
                if adj_role in (ColumnRole.RATIO, ColumnRole.YOY, ColumnRole.MARGIN_LIKE,
                                ColumnRole.ASSETS_LIKE, ColumnRole.DEPRECIATION_LIKE,
                                ColumnRole.CAPEX_LIKE, ColumnRole.SEGMENT_LABEL):
                    continue
                # unknown 列は緩い閾値 (0.05) を使用
                threshold = 0.05 if adj_role in (ColumnRole.UNKNOWN, "") else 0.1
                for role in ColumnRole.ALL_PROFIT_ROLES:
                    if adj_sc.get(role, 0) >= threshold:
                        best_profit_col = adj_idx
                        best_profit_score = adj_sc[role]
                        best_profit_role = role
                        break
            if best_profit_col is not None:
                break

    # --- Phase 5: sales があるかつ segment_like_rows >= 2 のとき、「利益」単独を昇格 ---
    if best_sales_col is not None and best_profit_col is None:
        # segment_like_rows をチェック
        seg_rows = 0
        for row in data_rows:
            if row and len(row) > 0:
                label = row[0].strip() if row[0] else ""
                if label and len(label) >= 2 and not _NUM_PATTERN.fullmatch(label):
                    skip_kws = ["合計", "調整", "消去", "全社", "計"]
                    if not any(sk in label for sk in skip_kws):
                        seg_rows += 1
        if seg_rows >= 2:
            # 全列をスキャンして「利益」スコア >= 0.2 の列を探す
            for ci in range(n_cols):
                if ci == best_sales_col:
                    continue
                sc = all_scores[ci]
                r = all_roles[ci] if ci < len(all_roles) else ""
                if r in (ColumnRole.RATIO, ColumnRole.YOY, ColumnRole.MARGIN_LIKE,
                         ColumnRole.ASSETS_LIKE, ColumnRole.SEGMENT_LABEL):
                    continue
                for role in ColumnRole.ALL_PROFIT_ROLES:
                    if sc.get(role, 0) >= 0.15:  # 「利益」単独(0.35*0.6=0.21) でも拾える
                        best_profit_col = ci
                        best_profit_score = sc[role]
                        best_profit_role = role
                        break
                if best_profit_col is not None:
                    break

    # --- Phase 5: sales/profit ペア推定 — 値大小分布による補助 (structure gate 付き) ---
    if best_sales_col is not None and best_profit_col is None and n_cols >= 2:
        # structure gate: segment_like_rows >= 2 が必要
        seg_rows_for_gate = 0
        for row in data_rows:
            if row and len(row) > 0:
                label = row[0].strip() if row[0] else ""
                if label and len(label) >= 2 and not _NUM_PATTERN.fullmatch(label):
                    skip_kws = ["合計", "調整", "消去", "全社", "計"]
                    if not any(sk in label for sk in skip_kws):
                        seg_rows_for_gate += 1
        if seg_rows_for_gate >= 2:
            # sales 列の中央値を計算
            sales_vals = []
            for row in data_rows:
                if best_sales_col < len(row):
                    cell = row[best_sales_col].strip().replace(",", "").replace("△", "-").replace("▲", "-")
                    try:
                        sales_vals.append(abs(float(cell)))
                    except ValueError:
                        pass
            if sales_vals:
                sales_vals.sort()
                sales_median = sales_vals[len(sales_vals) // 2]
                # ratio/yoy/margin/assets 以外の数値列で、値が sales の 1/3 〜 1/50 の列を profit 推定
                for ci in range(n_cols):
                    if ci == best_sales_col:
                        continue
                    r = all_roles[ci] if ci < len(all_roles) else ""
                    if r in (ColumnRole.RATIO, ColumnRole.YOY, ColumnRole.MARGIN_LIKE,
                             ColumnRole.ASSETS_LIKE, ColumnRole.SEGMENT_LABEL,
                             ColumnRole.CAPEX_LIKE, ColumnRole.DEPRECIATION_LIKE):
                        continue
                    ci_vals = []
                    for row in data_rows:
                        if ci < len(row):
                            cell = row[ci].strip().replace(",", "").replace("△", "-").replace("▲", "-")
                            try:
                                ci_vals.append(abs(float(cell)))
                            except ValueError:
                                pass
                    if ci_vals:
                        ci_vals.sort()
                        ci_median = ci_vals[len(ci_vals) // 2]
                        if sales_median > 0 and 0.02 <= ci_median / sales_median <= 0.5:
                            best_profit_col = ci
                            best_profit_score = 0.2
                            best_profit_role = ColumnRole.SEGMENT_PROFIT_LIKE
                            break

    # --- Phase 3: 列スコアリングベースの sales/profit 推定 (structure gate 付き) ---
    # MIN_SCORE / MIN_MARGIN で誤確定を防止
    _COL_MIN_SCORE = 1.5   # sales/profit 確定に必要な最低スコア
    _COL_MIN_MARGIN = 0.5  # 1位と2位の差がこれ以上必要

    if best_sales_col is None and best_profit_col is None and n_cols >= 2:
        # Phase 5: structure gate — segment_like_rows >= 2 が必要
        seg_rows_gate2 = 0
        for row in data_rows:
            if row and len(row) > 0:
                label = row[0].strip() if row[0] else ""
                if label and len(label) >= 2 and not _NUM_PATTERN.fullmatch(label):
                    skip_kws = ["合計", "調整", "消去", "全社", "計"]
                    if not any(sk in label for sk in skip_kws):
                        seg_rows_gate2 += 1
        if seg_rows_gate2 < 2:
            pass  # gate 不満 — 推定しない
        else:
            num_col_indices = [i for i in range(n_cols)
                              if all_roles[i] not in (ColumnRole.SEGMENT_LABEL, ColumnRole.RATIO,
                                                      ColumnRole.YOY, ColumnRole.NOTES,
                                                      ColumnRole.MARGIN_LIKE, ColumnRole.ASSETS_LIKE,
                                                      ColumnRole.DEPRECIATION_LIKE, ColumnRole.CAPEX_LIKE)]
            if len(num_col_indices) >= 2:
                # 各列に sales/profit スコアを付与
                col_scores: list[dict] = []
                for ci in num_col_indices:
                    info = _compute_column_score(ci, data_rows, all_scores, all_roles,
                                                headers if ci < len(headers) else "")
                    col_scores.append(info)

                # sales 確定: score 最大 + margin 条件
                col_scores.sort(key=lambda x: x["sales_score"], reverse=True)
                if len(col_scores) >= 2:
                    best_s = col_scores[0]
                    second_s = col_scores[1]
                    if (best_s["sales_score"] >= _COL_MIN_SCORE
                            and (best_s["sales_score"] - second_s["sales_score"]) >= _COL_MIN_MARGIN):
                        best_sales_col = best_s["col"]
                        best_sales_score = 0.25
                elif len(col_scores) == 1 and col_scores[0]["sales_score"] >= _COL_MIN_SCORE:
                    best_sales_col = col_scores[0]["col"]
                    best_sales_score = 0.25

                # profit 確定: sales と同一列禁止 + margin 条件
                profit_candidates = [c for c in col_scores if c["col"] != best_sales_col]
                profit_candidates.sort(key=lambda x: x["profit_score"], reverse=True)
                if len(profit_candidates) >= 2:
                    best_p = profit_candidates[0]
                    second_p = profit_candidates[1]
                    if (best_p["profit_score"] >= _COL_MIN_SCORE
                            and (best_p["profit_score"] - second_p["profit_score"]) >= _COL_MIN_MARGIN):
                        best_profit_col = best_p["col"]
                        best_profit_score = 0.2
                        best_profit_role = ColumnRole.SEGMENT_PROFIT_LIKE
                elif len(profit_candidates) == 1 and profit_candidates[0]["profit_score"] >= _COL_MIN_SCORE:
                    best_profit_col = profit_candidates[0]["col"]
                    best_profit_score = 0.2
                    best_profit_role = ColumnRole.SEGMENT_PROFIT_LIKE

    if best_profit_col is not None and best_profit_role:
        all_roles[best_profit_col] = best_profit_role

    confidence = 0.0
    if best_sales_col is not None:
        confidence += min(best_sales_score, 0.5)
    if best_profit_col is not None:
        confidence += min(best_profit_score, 0.5)

    return ColumnAnalysisResult(
        column_roles=all_roles,
        role_score_breakdown=all_scores,
        best_sales_col=best_sales_col,
        best_profit_col=best_profit_col,
        label_col_candidates=label_cols,
        confidence=confidence,
    )
