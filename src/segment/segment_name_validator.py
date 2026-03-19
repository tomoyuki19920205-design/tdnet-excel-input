"""セグメント名バリデーター

行レベルでセグメント名の妥当性を判定する。
3層構造:
  Layer 1: Deny list — PL/BS/CF勘定科目、既知のゴミパターン
  Layer 2: Shape rule — 長さ、句読点、助詞終わり、数字のみ等
  Layer 3: Allow signal — 事業/部門/製品等のpositive pattern

PIPELINE_SPEC §7
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.segment.normalize import normalize_segment_name, classify_special_row


# ============================================================
# 判定結果
# ============================================================

class InvalidReason(str, Enum):
    """セグメント名が無効な理由。"""
    VALID = ""
    PL_ACCOUNT = "pl_account"
    BS_ITEM = "bs_item"
    CF_ITEM = "cf_item"
    FRAGMENT = "fragment"
    TOO_SHORT = "too_short"
    HEADER_LABEL = "header_label"
    UNIT_ROW = "unit_row"
    NUMERIC_ONLY = "numeric_only"
    PUNCTUATION = "punctuation"
    PARTICLE_ENDING = "particle_ending"
    PARENTHESIS_ONLY = "parenthesis_only"


class RowType(str, Enum):
    """行の種別。"""
    SEGMENT = "segment"               # 通常セグメント (canonical 対象)
    ADJUSTMENT = "adjustment"         # 調整額
    TOTAL = "total"                   # 合計/計
    CORPORATE = "corporate"           # 全社/共通
    OTHER_SPECIAL = "other_special"   # その他/報告セグメント等メタラベル
    INVALID = "invalid"               # 不正行 (除外対象)


@dataclass
class SegmentNameValidation:
    """セグメント名バリデーション結果。"""
    name: str
    normalized_name: str
    is_valid: bool
    invalid_reason: InvalidReason
    row_type: RowType
    confidence: float  # 0.0-1.0
    matched_rule: str  # どのルールでマッチしたか (debug用)


# ============================================================
# Layer 1: Deny list
# ============================================================

# PL勘定科目
_PL_DENY: set[str] = {
    "売上原価",
    "販売費及び一般管理費",
    "販売費・一般管理費",
    "営業利益",
    "営業損失",
    "経常利益",
    "経常損失",
    "税金等調整前当期純利益",
    "税金等調整前当期純損失",
    "税金等調整前四半期純利益",
    "税金等調整前四半期純損失",
    "税金等調整前中間純利益",
    "税金等調整前中間純損失",
    "法人税等",
    "法人税、住民税及び事業税",
    "法人税等調整額",
    "受取利息",
    "支払利息",
    "受取配当金",
    "減価償却費",
    "のれん償却額",
    "当期純利益",
    "当期純損失",
    "四半期純利益",
    "四半期純損失",
    "中間純利益",
    "中間純損失",
    "営業外収益",
    "営業外費用",
    "特別利益",
    "特別損失",
    "人件費",
    "売上総利益",
    "売上総損失",
    "親会社株主に帰属する当期純利益",
    "親会社株主に帰属する四半期純利益",
    "親会社株主に帰属する中間純利益",
    "非支配株主に帰属する当期純利益",
    "非支配株主に帰属する四半期純利益",
    "非支配株主に帰属する中間純利益",
    "包括利益",
    "その他の包括利益",
    "その他の包括利益合計",
    "貸倒引当金繰入額",
    "貸倒引当金戻入額",
    "為替差益",
    "為替差損",
    "投資有価証券売却益",
    "投資有価証券売却損",
    "固定資産売却益",
    "固定資産売却損",
    "固定資産除却損",
    "減損損失",
    "契約解約損",
}

# PL部分一致パターン (完全一致で漏れるバリエーション対応)
_PL_DENY_PARTIAL: list[str] = [
    "税金等調整前",
    "親会社株主に帰属する",
    "非支配株主に帰属する",
    "法人税",
    "事業税",
    "住民税",
    "純利益",     # 中間純利益又は中間純損失 等のバリエーション対応
    "純損失",
]

# BS項目
_BS_DENY: set[str] = {
    "資産合計",
    "負債合計",
    "純資産合計",
    "資産",
    "負債",
    "純資産",
    "流動資産",
    "固定資産",
    "流動負債",
    "固定負債",
    "株主資本",
    "自己資本",
    "有利子負債",
    "総資産",
    "利益剰余金",
    "資本金",
    "資本剰余金",
}

# CF項目
_CF_DENY: set[str] = {
    "キャッシュフロー",
    "営業活動によるキャッシュフロー",
    "投資活動によるキャッシュフロー",
    "財務活動によるキャッシュフロー",
    "現金及び現金同等物",
    "フリーキャッシュフロー",
}

# ヘッダー/メタラベル
_HEADER_DENY: set[str] = {
    "売上高",
    "売上",
    "売上収益",
    "営業収益",
    "収益",
    "外部顧客への売上高",
    "セグメント間内部売上高",
    "セグメント間内部営業収益",
    "セグメント利益",
    "セグメント損益",
    "百万円",
    "千円",
    "円",
    "単位",
    "前年同期",
    "前年同期比",
    "増減",
    "増減率",
    "構成比",
    "前期",
    "当期",
    "金額",
    "比率",
}

# 単位行パターン
_UNIT_DENY: set[str] = {
    "百万円",
    "千円",
    "億円",
    "万円",
}

# 補助行 (invalidではないが通常セグメントではない)
_AUXILIARY_LABELS: set[str] = {
    "合計",
    "計",
    "総計",
    "連結",
    "調整額",
    "消去",
    "消去又は全社",
    "全社",
    "全社・消去",
    "配賦不能",
    "セグメント間",
    "内部取引",
    "その他",
    "報告セグメント",
    "事業セグメント",
    "セグメント情報",
}


# ============================================================
# Layer 2: Shape rules
# ============================================================

# 数字のみ
_NUMERIC_ONLY_RE = re.compile(r"^[\d,.，、\s△▲\-－]+$")

# 括弧のみ / 括弧+助詞的断片
_PAREN_ONLY_RE = re.compile(r"^[()（）\[\]「」『』【】\s]+$")

# 句読点を含む (文章の可能性が高い)
_HAS_SENTENCE_PUNCT = re.compile(r"[。、．，]")

# 助詞終わり (文章断片の可能性)
_PARTICLE_ENDING_RE = re.compile(
    r"[はがをにへでとのもやかけばなら]$"
)

# 「(自」「前第」「当社は」のような文章断片
_FRAGMENT_PATTERNS: list[re.Pattern] = [
    re.compile(r"^(自\s|至\s)"),       # (自 2025年... の断片
    re.compile(r"^(前第|当第)"),        # 前第1四半期...
    re.compile(r"^(（注）|注\s*[)）])"),  # (注)
    re.compile(r"^(当社|弊社|連結)は"),
    re.compile(r"(の結果|において|について|による|に関する)$"),
    re.compile(r"^(ムは|の内部)"),       # 実データで確認された断片
]

# 正規化前テキストで追加チェックするフラグメントパターン
_FRAGMENT_PATTERNS_RAW: list[re.Pattern] = [
    re.compile(r"^円[\(（(]同"),         # 「円(同」 — normalize で括弧が消えるため元テキストでチェック
]


# ============================================================
# Layer 3: Allow signal
# ============================================================

# セグメントらしいパターン
_ALLOW_PATTERNS: list[re.Pattern] = [
    re.compile(r"事業$"),              # ○○事業
    re.compile(r"部門$"),              # ○○部門
    re.compile(r"関連$"),              # ○○関連
    re.compile(r"製品$"),              # ○○製品
    re.compile(r"サービス$"),           # ○○サービス
    re.compile(r"セグメント$"),         # (正規化後に消えるが念のため)
    re.compile(r"ビジネス$"),           # ○○ビジネス
    re.compile(r"カンパニー$"),         # ○○カンパニー
    re.compile(r"グループ$"),           # ○○グループ
    re.compile(r"(?<!内)部$"),          # ○○部 (「内部」は除外)
    re.compile(r"事業部$"),            # ○○事業部
    re.compile(r"国内"),              # 国内○○
    re.compile(r"海外"),              # 海外○○
    re.compile(r"(日本|北米|欧州|アジア|中国|米国|アメリカ)"),  # 地域セグメント
]


# ============================================================
# メインバリデーション
# ============================================================

def validate_segment_name(name: str) -> SegmentNameValidation:
    """セグメント名の妥当性を3層で判定する。

    Layer 1: Deny list (PL/BS/CF勘定科目、ヘッダー)
    Layer 2: Shape rule (長さ、句読点、助詞、数字のみ等)
    Layer 3: Allow signal (事業/部門/製品等のpositive)

    Returns:
        SegmentNameValidation
    """
    if not name:
        return SegmentNameValidation(
            name="", normalized_name="", is_valid=False,
            invalid_reason=InvalidReason.TOO_SHORT,
            row_type=RowType.INVALID, confidence=1.0,
            matched_rule="empty",
        )

    # 正規化
    normalized = normalize_segment_name(name)
    if not normalized:
        return SegmentNameValidation(
            name=name, normalized_name="", is_valid=False,
            invalid_reason=InvalidReason.TOO_SHORT,
            row_type=RowType.INVALID, confidence=1.0,
            matched_rule="normalized_empty",
        )

    # NFKC正規化 (deny list との比較用)
    nfkc = unicodedata.normalize("NFKC", normalized)

    # --- 補助行判定 (invalidではないが通常セグメントではない) ---
    special = classify_special_row(normalized)
    if special != "ordinary_segment":
        row_type_map = {
            "adjustment": RowType.ADJUSTMENT,
            "corporate": RowType.CORPORATE,
            "total": RowType.TOTAL,
            "other": RowType.OTHER_SPECIAL,
        }
        return SegmentNameValidation(
            name=name, normalized_name=normalized, is_valid=True,
            invalid_reason=InvalidReason.VALID,
            row_type=row_type_map.get(special, RowType.OTHER_SPECIAL),
            confidence=0.9,
            matched_rule=f"special_row:{special}",
        )

    # 補助行 exact match (classify_special_row で漏れるケース)
    if nfkc in _AUXILIARY_LABELS:
        return SegmentNameValidation(
            name=name, normalized_name=normalized, is_valid=True,
            invalid_reason=InvalidReason.VALID,
            row_type=RowType.OTHER_SPECIAL,
            confidence=0.9,
            matched_rule=f"auxiliary_label:{nfkc}",
        )

    # ============ Layer 1: Deny list ============

    # PL勘定科目 (完全一致)
    if nfkc in _PL_DENY:
        return SegmentNameValidation(
            name=name, normalized_name=normalized, is_valid=False,
            invalid_reason=InvalidReason.PL_ACCOUNT,
            row_type=RowType.INVALID, confidence=1.0,
            matched_rule=f"pl_deny_exact:{nfkc}",
        )

    # PL勘定科目 (部分一致)
    for pattern in _PL_DENY_PARTIAL:
        if pattern in nfkc:
            return SegmentNameValidation(
                name=name, normalized_name=normalized, is_valid=False,
                invalid_reason=InvalidReason.PL_ACCOUNT,
                row_type=RowType.INVALID, confidence=0.95,
                matched_rule=f"pl_deny_partial:{pattern}",
            )

    # BS項目
    if nfkc in _BS_DENY:
        return SegmentNameValidation(
            name=name, normalized_name=normalized, is_valid=False,
            invalid_reason=InvalidReason.BS_ITEM,
            row_type=RowType.INVALID, confidence=1.0,
            matched_rule=f"bs_deny:{nfkc}",
        )

    # CF項目
    if nfkc in _CF_DENY:
        return SegmentNameValidation(
            name=name, normalized_name=normalized, is_valid=False,
            invalid_reason=InvalidReason.CF_ITEM,
            row_type=RowType.INVALID, confidence=1.0,
            matched_rule=f"cf_deny:{nfkc}",
        )

    # ヘッダー/メタラベル
    if nfkc in _HEADER_DENY:
        return SegmentNameValidation(
            name=name, normalized_name=normalized, is_valid=False,
            invalid_reason=InvalidReason.HEADER_LABEL,
            row_type=RowType.INVALID, confidence=1.0,
            matched_rule=f"header_deny:{nfkc}",
        )

    # 単位行
    if nfkc in _UNIT_DENY:
        return SegmentNameValidation(
            name=name, normalized_name=normalized, is_valid=False,
            invalid_reason=InvalidReason.UNIT_ROW,
            row_type=RowType.INVALID, confidence=1.0,
            matched_rule=f"unit_deny:{nfkc}",
        )

    # ============ Layer 2: Shape rules ============

    # 長さ1文字
    if len(nfkc) <= 1:
        return SegmentNameValidation(
            name=name, normalized_name=normalized, is_valid=False,
            invalid_reason=InvalidReason.TOO_SHORT,
            row_type=RowType.INVALID, confidence=1.0,
            matched_rule="too_short",
        )

    # 数字のみ
    if _NUMERIC_ONLY_RE.match(nfkc):
        return SegmentNameValidation(
            name=name, normalized_name=normalized, is_valid=False,
            invalid_reason=InvalidReason.NUMERIC_ONLY,
            row_type=RowType.INVALID, confidence=1.0,
            matched_rule="numeric_only",
        )

    # 括弧のみ
    if _PAREN_ONLY_RE.match(nfkc):
        return SegmentNameValidation(
            name=name, normalized_name=normalized, is_valid=False,
            invalid_reason=InvalidReason.PARENTHESIS_ONLY,
            row_type=RowType.INVALID, confidence=1.0,
            matched_rule="parenthesis_only",
        )

    # --- Allow signal check (Layer 3 を先にチェック: allow なら shape rule をスキップ) ---
    has_allow = False
    for pattern in _ALLOW_PATTERNS:
        if pattern.search(nfkc):
            has_allow = True
            break

    # 句読点を含む (allow signal がある場合はスキップ)
    if not has_allow and _HAS_SENTENCE_PUNCT.search(nfkc):
        return SegmentNameValidation(
            name=name, normalized_name=normalized, is_valid=False,
            invalid_reason=InvalidReason.PUNCTUATION,
            row_type=RowType.INVALID, confidence=0.9,
            matched_rule="has_punctuation",
        )

    # 文章断片パターン (正規化後テキスト)
    if not has_allow:
        for frag_re in _FRAGMENT_PATTERNS:
            if frag_re.search(nfkc):
                return SegmentNameValidation(
                    name=name, normalized_name=normalized, is_valid=False,
                    invalid_reason=InvalidReason.FRAGMENT,
                    row_type=RowType.INVALID, confidence=0.85,
                    matched_rule=f"fragment:{frag_re.pattern}",
                )

    # 文章断片パターン (正規化前テキスト — normalize で消える文字を含むパターン)
    if not has_allow:
        name_nfkc = unicodedata.normalize("NFKC", name.strip())
        for frag_re in _FRAGMENT_PATTERNS_RAW:
            if frag_re.search(name_nfkc):
                return SegmentNameValidation(
                    name=name, normalized_name=normalized, is_valid=False,
                    invalid_reason=InvalidReason.FRAGMENT,
                    row_type=RowType.INVALID, confidence=0.85,
                    matched_rule=f"fragment_raw:{frag_re.pattern}",
                )

    # 助詞終わり (allow signal がある場合はスキップ)
    if not has_allow and _PARTICLE_ENDING_RE.search(nfkc):
        # 2文字以下の助詞終わりは高確信度でNG
        if len(nfkc) <= 3:
            return SegmentNameValidation(
                name=name, normalized_name=normalized, is_valid=False,
                invalid_reason=InvalidReason.PARTICLE_ENDING,
                row_type=RowType.INVALID, confidence=0.9,
                matched_rule="particle_ending_short",
            )

    # ============ Layer 3: Allow signal → 最終判定 ============

    # allow signal なしで短すぎる名前はフラグメントとして排除
    if not has_allow and len(nfkc) <= 3:
        return SegmentNameValidation(
            name=name, normalized_name=normalized, is_valid=False,
            invalid_reason=InvalidReason.FRAGMENT,
            row_type=RowType.INVALID, confidence=0.8,
            matched_rule="too_short_no_signal",
        )

    # confidence 決定:
    #   allow signal あり → 0.8
    #   カタカナ語4文字以上 (セグメント名に多い) → 0.7
    #   その他 → 0.6
    if has_allow:
        confidence = 0.8
    elif len(nfkc) >= 4 and re.search(r'[\u30A0-\u30FF]{4,}', nfkc):
        confidence = 0.7
    else:
        confidence = 0.6

    return SegmentNameValidation(
        name=name, normalized_name=normalized, is_valid=True,
        invalid_reason=InvalidReason.VALID,
        row_type=RowType.SEGMENT,
        confidence=confidence,
        matched_rule="allow_signal" if has_allow else "default_accept",
    )


def validate_segment_names(names: list[str]) -> list[SegmentNameValidation]:
    """複数のセグメント名を一括バリデーション。"""
    return [validate_segment_name(n) for n in names]
