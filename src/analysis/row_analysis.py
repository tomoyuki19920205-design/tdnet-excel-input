# ============================================================
# row_analysis.py — Phase E: 行ロール分類
# ============================================================
"""
セグメント表の各行に role を付与する。

v2 Phase 2: role 詳細化 — subtotal / elimination 追加、
is_reportable_segment フラグ導入。

方針:
  - corporate / adjustment / elimination / note / total / subtotal → is_reportable_segment=False
  - segment → True
  - other → False (今回方針。docs に明記)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .header_analysis import normalize_header, _NUM_PATTERN


# ============================================================
# RowRole (Phase 2 拡張)
# ============================================================

class RowRole:
    """行ロール定数"""
    SEGMENT = "segment"
    SUBTOTAL = "subtotal"
    TOTAL = "total"
    ADJUSTMENT = "adjustment"
    CORPORATE = "corporate"
    ELIMINATION = "elimination"
    OTHER = "other"
    NOTE = "note"
    BLANK = "blank"
    HEADER = "header"
    UNKNOWN = "unknown"

    # 後方互換
    SEGMENT_ITEM = SEGMENT

    SKIP_ROLES = {TOTAL, SUBTOTAL, ADJUSTMENT, CORPORATE, ELIMINATION,
                  OTHER, NOTE, BLANK, HEADER}

    NON_REPORTABLE_ROLES = {TOTAL, SUBTOTAL, ADJUSTMENT, CORPORATE,
                            ELIMINATION, OTHER, NOTE, BLANK, HEADER, UNKNOWN}


# ============================================================
# キーワード
# ============================================================

_TOTAL_KW = ["合計", "総計", "連結", "Total", "TOTAL", "Consolidated"]
_SUBTOTAL_KW = ["小計", "計", "報告セグメント計", "事業セグメント計"]

_ADJUSTMENT_KW = [
    "調整額", "調整", "連結調整",
]

_ELIMINATION_KW = [
    "消去", "消去又は全社", "セグメント間",
    "セグメント間取引消去", "セグメント間消去",
    "内部取引消去", "全社・消去",
]

_CORPORATE_KW = [
    "全社", "全社共通", "配賦不能", "本社",
    "グループ本社", "共通費",
]

_OTHER_KW = [
    "その他",
]


# ============================================================
# RowClassification (Phase 2 拡張)
# ============================================================

@dataclass
class RowClassification:
    """1行の分類結果"""
    row_index: int = 0
    role: str = ""
    score: float = 0.0
    label: str = ""
    is_extractable: bool = False       # 後方互換
    is_reportable_segment: bool = False # Phase 2 追加
    reason: str = ""


@dataclass
class RowAnalysisResult:
    """全行の分類結果"""
    rows: list[RowClassification] = field(default_factory=list)

    @property
    def segment_rows(self) -> list[RowClassification]:
        return [r for r in self.rows if r.role == RowRole.SEGMENT and r.is_extractable]

    @property
    def reportable_rows(self) -> list[RowClassification]:
        return [r for r in self.rows if r.is_reportable_segment]

    @property
    def total_rows(self) -> list[RowClassification]:
        return [r for r in self.rows if r.role == RowRole.TOTAL]

    @property
    def skip_rows(self) -> list[RowClassification]:
        return [r for r in self.rows if r.role in RowRole.SKIP_ROLES]

    @property
    def extractable_count(self) -> int:
        return len(self.segment_rows)

    @property
    def non_reportable_count(self) -> int:
        return sum(1 for r in self.rows if not r.is_reportable_segment and r.role != RowRole.BLANK)


# ============================================================
# classify_rows
# ============================================================

def classify_rows(
    lines: list[str],
    label_col_idx: int = 0,
    header_band_height: int = 1,
) -> RowAnalysisResult:
    """
    Phase E: 行ロール分類 (Phase 2)。

    Args:
        lines: テーブル全行のリスト
        label_col_idx: ラベル列のインデックス (通常0)
        header_band_height: ヘッダーバンドの高さ
    """
    results: list[RowClassification] = []

    for i, line in enumerate(lines):
        stripped = line.strip()

        # --- 空行 ---
        if not stripped:
            results.append(RowClassification(
                row_index=i, role=RowRole.BLANK, label="",
                is_extractable=False, is_reportable_segment=False,
                reason="空行",
            ))
            continue

        # --- ヘッダー行 ---
        if i < header_band_height:
            results.append(RowClassification(
                row_index=i, role=RowRole.HEADER, label=stripped,
                is_extractable=False, is_reportable_segment=False,
                reason=f"ヘッダーバンド内(行{i})",
            ))
            continue

        # ラベル部分を抽出
        name_match = re.match(r'^([^\d△▲\-－]*)', stripped)
        label = name_match.group(1).strip() if name_match else ""
        label_normalized = normalize_header(label)

        # 数値トークン
        nums = _NUM_PATTERN.findall(stripped)
        has_nums = len(nums) > 0

        cls = _classify_single_row(i, stripped, label, label_normalized, has_nums, len(nums))
        results.append(cls)

    return RowAnalysisResult(rows=results)


def _classify_single_row(
    idx: int,
    stripped: str,
    label: str,
    label_normalized: str,
    has_nums: bool,
    num_count: int,
) -> RowClassification:
    """単一行の分類"""

    # --- 消去系 (調整より先に判定 — 「消去又は全社」を正しく分類) ---
    for kw in _ELIMINATION_KW:
        nkw = normalize_header(kw)
        if nkw in label_normalized and len(label) <= len(kw) + 8:
            return RowClassification(
                row_index=idx, role=RowRole.ELIMINATION, label=label,
                is_extractable=False, is_reportable_segment=False,
                score=0.8, reason=f"消去KW '{kw}'",
            )

    # --- 合計行 ---
    for kw in _TOTAL_KW:
        nkw = normalize_header(kw)
        if nkw == label_normalized or (nkw in label_normalized and len(label) <= len(kw) + 4):
            return RowClassification(
                row_index=idx, role=RowRole.TOTAL, label=label,
                is_extractable=False, is_reportable_segment=False,
                score=0.9, reason=f"合計KW '{kw}'",
            )

    # --- 小計行 ---
    for kw in _SUBTOTAL_KW:
        nkw = normalize_header(kw)
        if nkw == label_normalized or (nkw in label_normalized and len(label) <= len(kw) + 6):
            return RowClassification(
                row_index=idx, role=RowRole.SUBTOTAL, label=label,
                is_extractable=False, is_reportable_segment=False,
                score=0.85, reason=f"小計KW '{kw}'",
            )

    # --- 調整額行 ---
    for kw in _ADJUSTMENT_KW:
        nkw = normalize_header(kw)
        if nkw in label_normalized and len(label) <= len(kw) + 8:
            return RowClassification(
                row_index=idx, role=RowRole.ADJUSTMENT, label=label,
                is_extractable=False, is_reportable_segment=False,
                score=0.8, reason=f"調整KW '{kw}'",
            )

    # --- 全社行 ---
    for kw in _CORPORATE_KW:
        nkw = normalize_header(kw)
        if nkw in label_normalized and len(label) <= len(kw) + 8:
            return RowClassification(
                row_index=idx, role=RowRole.CORPORATE, label=label,
                is_extractable=False, is_reportable_segment=False,
                score=0.8, reason=f"全社KW '{kw}'",
            )

    # --- その他行 (抽出対象として扱う — ユーザー要件) ---
    for kw in _OTHER_KW:
        nkw = normalize_header(kw)
        if nkw == label_normalized:
            return RowClassification(
                row_index=idx, role=RowRole.SEGMENT, label=label,
                is_extractable=True, is_reportable_segment=True,
                score=0.6, reason=f"その他セグメント '{kw}'",
            )

    # --- 注記行 ---
    if not has_nums and (len(stripped) > 40 or stripped.count("（") >= 2
                         or stripped.startswith("注") or stripped.startswith("※")
                         or stripped.startswith("（注") or stripped.startswith("(注")):
        return RowClassification(
            row_index=idx, role=RowRole.NOTE, label=label,
            is_extractable=False, is_reportable_segment=False,
            score=0.5, reason="注記行",
        )

    # --- 数値なし + ラベル短い ---
    if not has_nums:
        return RowClassification(
            row_index=idx, role=RowRole.UNKNOWN, label=label,
            is_extractable=False, is_reportable_segment=False,
            score=0.1, reason="数値なし",
        )

    # --- セグメント名候補 ---
    if has_nums and label and len(label) >= 2:
        return RowClassification(
            row_index=idx, role=RowRole.SEGMENT, label=label,
            is_extractable=True, is_reportable_segment=True,
            score=0.7, reason=f"セグメント名候補(ラベル:{label}, 数値{num_count}個)",
        )

    # --- 不明 ---
    return RowClassification(
        row_index=idx, role=RowRole.UNKNOWN, label=label,
        is_extractable=False, is_reportable_segment=False,
        score=0.2, reason=f"ラベル不足('{label}')",
    )
