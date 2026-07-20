"""抽出結果バリデーター

開示レベルで抽出結果を success / partial / quarantine に判定する。
segment_name_validator と連携し、行レベルの品質を集計して
開示全体の品質を判定する。

PIPELINE_SPEC §8
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from src.segment.segment_name_validator import (
    validate_segment_name,
    SegmentNameValidation,
    InvalidReason,
    RowType,
)


# ============================================================
# Enum 定義
# ============================================================

class ExtractionStatus(str, Enum):
    """抽出結果のステータス。"""
    SUCCESS = "success"
    PARTIAL = "partial"
    QUARANTINE = "quarantine"


class HardFailReason(str, Enum):
    """quarantine の hard fail 理由。"""
    NONE = ""                                       # success / partial
    TOO_FEW_VALID_SEGMENTS = "too_few_valid_segments"
    TOO_FEW_SALES = "too_few_sales"
    NO_PROFIT = "no_profit"
    HIGH_INVALID_RATIO = "high_invalid_ratio"
    NARRATIVE_CONTAMINATION = "narrative_contamination"
    ACCOUNT_LIKE_DOMINANT = "account_like_dominant"
    NO_RECORDS = "no_records"


class SoftFailReason(str, Enum):
    """partial の soft fail 理由。"""
    NONE = ""
    WEAK_PROFIT = "weak_profit"                     # profit < 要件だが sales は OK
    BORDERLINE_INVALID_RATIO = "borderline_invalid_ratio"
    LOW_CONFIDENCE_NAMES = "low_confidence_names"
    UNDISCLOSED_SEGMENT_SALES = "undisclosed_segment_sales"


# ============================================================
# 結果データクラス
# ============================================================

@dataclass
class ExtractionValidationResult:
    """抽出結果のバリデーション結果。"""
    status: ExtractionStatus
    confidence: float                        # 0.0-1.0
    reason: str                              # 人間可読な判定理由
    hard_fail_reason: HardFailReason         # quarantine 時の具体的理由
    raw_segment_count: int                   # バリデーション前のレコード数
    valid_segment_count: int                 # is_valid=True & row_type=SEGMENT
    invalid_segment_count: int               # is_valid=False
    sales_non_null_count: int                # segment_sales が非NULL のレコード数
    profit_non_null_count: int               # segment_profit が非NULL のレコード数
    invalid_names: list[str]                 # invalid と判定されたセグメント名一覧
    account_like_ratio: float                # PL/BS/CF勘定科目として拒否された行の割合
    narrative_contamination: bool            # 叙述文混入の有無
    # 内部詳細 (デバッグ・ベンチマーク用)
    validations: list[SegmentNameValidation] = field(default_factory=list, repr=False)


# ============================================================
# 閾値定数
# ============================================================

# Success 最低条件
MIN_VALID_SEGMENTS = 2
MIN_SALES_NON_NULL = 2
MIN_PROFIT_NON_NULL = 1
MAX_INVALID_RATIO = 0.3          # invalid / total < 0.3
MAX_ACCOUNT_LIKE_RATIO = 0.5     # account-like / total < 0.5

# Feature flag: XBRL single-segment 例外 (default off, 調査後に有効化判断)
_ENABLE_SINGLE_SEGMENT_EXCEPTION = False

# Partial 条件
PARTIAL_MIN_VALID_SEGMENTS = 2
PARTIAL_MIN_SALES_NON_NULL = 2

# Narrative contamination 検出
_NARRATIVE_RE = re.compile(
    r"(において|については|に関して|を実施|することとしました|の結果|と考えて|によるもの|させていただき)"
)
_MIN_NARRATIVE_RATIO = 0.3       # 30% 以上の行に叙述文パターン → contamination


# ============================================================
# メインバリデーション
# ============================================================

def validate_extraction_result(
    segment_records: list[dict],
    source: str = "unknown",
) -> ExtractionValidationResult:
    """抽出結果を success / partial / quarantine に判定する。

    Args:
        segment_records: 抽出されたセグメントレコードのリスト。
            各 dict は最低限 ``segment_name`` キーを持つ。
            ``segment_sales``, ``segment_profit`` キーがあれば
            sales/profit の非NULL カウントに使用。
        source: 抽出元。"xbrl" | "html" | "pdf" | "unknown"

    Returns:
        ExtractionValidationResult
    """
    # --- 空レコード ---
    if not segment_records:
        return ExtractionValidationResult(
            status=ExtractionStatus.QUARANTINE,
            confidence=1.0,
            reason=f"[{source}] レコードなし",
            hard_fail_reason=HardFailReason.NO_RECORDS,
            raw_segment_count=0,
            valid_segment_count=0,
            invalid_segment_count=0,
            sales_non_null_count=0,
            profit_non_null_count=0,
            invalid_names=[],
            account_like_ratio=0.0,
            narrative_contamination=False,
        )

    raw_count = len(segment_records)

    # --- 行レベルバリデーション ---
    validations: list[SegmentNameValidation] = []
    for rec in segment_records:
        name = rec.get("segment_name", "") or ""
        validations.append(validate_segment_name(name))

    # --- XBRL source 限定: too_short_no_signal 免除 ---
    # XBRL member 由来の短い英字略称 (D2C 等) を救済する。
    # 条件: source=xbrl, matched_rule=too_short_no_signal,
    #        英字主体 2-4文字, sales/profitあり,
    #        account-like/narrative/header ではない,
    #        記号単独・ローマ数字単独ではない。
    _ROMAN_ONLY = re.compile(r'^[IVXLCDM]+$')
    _SYMBOL_ONLY = re.compile(r'^[^A-Za-z0-9]+$')
    if source == "xbrl":
        for i, v in enumerate(validations):
            if v.matched_rule != "too_short_no_signal":
                continue
            nm = v.normalized_name or v.name
            # 英字主体 2-4文字
            if not (2 <= len(nm) <= 4):
                continue
            if not re.match(r'^[A-Za-z0-9&]+$', nm):
                continue
            # ローマ数字単独・記号単独を除外
            if _ROMAN_ONLY.match(nm.upper()):
                continue
            if _SYMBOL_ONLY.match(nm):
                continue
            # sales or profit fact があるか
            rec = segment_records[i]
            has_fact = (
                _is_non_null(rec.get("segment_sales"))
                or _is_non_null(rec.get("segment_profit"))
            )
            if not has_fact:
                continue
            # 免除: valid に復帰
            validations[i] = SegmentNameValidation(
                name=v.name,
                normalized_name=v.normalized_name,
                is_valid=True,
                invalid_reason=InvalidReason.VALID,
                row_type=RowType.SEGMENT,
                confidence=0.65,
                matched_rule="xbrl_short_abbreviation_exempt",
            )

    # --- 集計 ---
    valid_segments = [v for v in validations if v.is_valid and v.row_type == RowType.SEGMENT]
    invalid_entries = [v for v in validations if not v.is_valid]
    account_like = [
        v for v in invalid_entries
        if v.invalid_reason in (
            InvalidReason.PL_ACCOUNT,
            InvalidReason.BS_ITEM,
            InvalidReason.CF_ITEM,
            InvalidReason.HEADER_LABEL,
        )
    ]

    valid_segment_count = len(valid_segments)
    invalid_count = len(invalid_entries)
    account_like_count = len(account_like)

    invalid_ratio = invalid_count / raw_count if raw_count > 0 else 0.0
    account_like_ratio = account_like_count / raw_count if raw_count > 0 else 0.0

    invalid_names = [v.name for v in invalid_entries]

    # Sales / Profit カウント
    sales_non_null = sum(
        1 for rec in segment_records
        if _is_non_null(rec.get("segment_sales"))
    )
    profit_non_null = sum(
        1 for rec in segment_records
        if _is_non_null(rec.get("segment_profit"))
    )

    # Narrative contamination 検出
    narrative_hit_count = sum(
        1 for rec in segment_records
        if _NARRATIVE_RE.search(rec.get("segment_name", "") or "")
    )
    narrative_contamination = (
        narrative_hit_count / raw_count >= _MIN_NARRATIVE_RATIO
        if raw_count > 0 else False
    )

    valid_records = [
        rec for rec, validation in zip(segment_records, validations)
        if validation.is_valid and validation.row_type == RowType.SEGMENT
    ]
    undisclosed_sales_contract = _has_verified_undisclosed_segment_sales(
        valid_records,
        source=source,
        invalid_count=invalid_count,
    )

    # --- 判定ロジック ---
    status, confidence, reason, hard_fail, soft_fail = _determine_status(
        raw_count=raw_count,
        valid_segment_count=valid_segment_count,
        invalid_count=invalid_count,
        invalid_ratio=invalid_ratio,
        account_like_ratio=account_like_ratio,
        sales_non_null=sales_non_null,
        profit_non_null=profit_non_null,
        narrative_contamination=narrative_contamination,
        source=source,
        undisclosed_sales_contract=undisclosed_sales_contract,
    )

    return ExtractionValidationResult(
        status=status,
        confidence=confidence,
        reason=reason,
        hard_fail_reason=hard_fail,
        raw_segment_count=raw_count,
        valid_segment_count=valid_segment_count,
        invalid_segment_count=invalid_count,
        sales_non_null_count=sales_non_null,
        profit_non_null_count=profit_non_null,
        invalid_names=invalid_names,
        account_like_ratio=account_like_ratio,
        narrative_contamination=narrative_contamination,
        validations=validations,
    )


# ============================================================
# 判定ロジック (内部)
# ============================================================

def _determine_status(
    *,
    raw_count: int,
    valid_segment_count: int,
    invalid_count: int,
    invalid_ratio: float,
    account_like_ratio: float,
    sales_non_null: int,
    profit_non_null: int,
    narrative_contamination: bool,
    source: str,
    undisclosed_sales_contract: bool = False,
) -> tuple[ExtractionStatus, float, str, HardFailReason, SoftFailReason]:
    """success / partial / quarantine を判定する。

    Quarantine 判定は**最も致命的な理由**を hard_fail_reason に記録。
    Partial 判定は soft_fail_reason に記録。

    Returns:
        (status, confidence, reason, hard_fail_reason, soft_fail_reason)
    """

    # ======== pdf_compat: v1 互換モード (DEPRECATED — TDNET XBRL only 方針により未使用) ========
    # worker_v2 では PDF path が disabled のため呼ばれない。
    # 外部参照がある場合に備え残置。
    if source == "pdf_compat":
        # v1 では segment_name_validator 未使用のため valid/invalid 区別なし
        # レコードがあり sales >= 1 なら ok
        if sales_non_null >= 1:
            return (
                ExtractionStatus.SUCCESS, 0.7,
                f"[pdf_compat] v1互換 (valid={valid_segment_count}, sales={sales_non_null})",
                HardFailReason.NONE,
                SoftFailReason.NONE,
            )
        # sales = 0 → quarantine
        return (
            ExtractionStatus.QUARANTINE, 0.9,
            f"[pdf_compat] v1互換だが品質不足 (valid={valid_segment_count}, sales={sales_non_null})",
            HardFailReason.TOO_FEW_SALES,
            SoftFailReason.NONE,
        )


    # ======== Quarantine: hard fail チェック ========

    # (1) narrative contamination
    if narrative_contamination:
        return (
            ExtractionStatus.QUARANTINE, 0.95,
            f"[{source}] 叙述文混入検出",
            HardFailReason.NARRATIVE_CONTAMINATION,
            SoftFailReason.NONE,
        )

    # (2) account-like 行が支配的
    if account_like_ratio >= MAX_ACCOUNT_LIKE_RATIO:
        return (
            ExtractionStatus.QUARANTINE, 0.95,
            f"[{source}] PL/BS/CF勘定科目行が{account_like_ratio:.0%}を占有",
            HardFailReason.ACCOUNT_LIKE_DOMINANT,
            SoftFailReason.NONE,
        )

    # (3) valid_segment_count < 2
    if valid_segment_count < MIN_VALID_SEGMENTS:
        # XBRL single-segment 例外 (feature flag: default off)
        if (
            _ENABLE_SINGLE_SEGMENT_EXCEPTION
            and source == "xbrl"
            and valid_segment_count == 1
            and sales_non_null >= 1
            and account_like_ratio < MAX_ACCOUNT_LIKE_RATIO
            and not narrative_contamination
        ):
            return (
                ExtractionStatus.PARTIAL, 0.6,
                f"[{source}] single-segment 許容 (valid=1, sales={sales_non_null})",
                HardFailReason.NONE,
                SoftFailReason.LOW_CONFIDENCE_NAMES,
            )
        return (
            ExtractionStatus.QUARANTINE, 0.9,
            f"[{source}] 有効セグメント数不足 ({valid_segment_count} < {MIN_VALID_SEGMENTS})",
            HardFailReason.TOO_FEW_VALID_SEGMENTS,
            SoftFailReason.NONE,
        )

    # (4) sales_non_null < 2
    if sales_non_null < MIN_SALES_NON_NULL:
        if undisclosed_sales_contract:
            return (
                ExtractionStatus.PARTIAL, 0.9,
                f"[{source}] reportable segment sales explicitly undisclosed; reconciliation verified",
                HardFailReason.NONE,
                SoftFailReason.UNDISCLOSED_SEGMENT_SALES,
            )
        return (
            ExtractionStatus.QUARANTINE, 0.9,
            f"[{source}] 売上非NULLセグメント不足 ({sales_non_null} < {MIN_SALES_NON_NULL})",
            HardFailReason.TOO_FEW_SALES,
            SoftFailReason.NONE,
        )

    # (5) invalid_ratio >= 0.3
    if invalid_ratio >= MAX_INVALID_RATIO:
        return (
            ExtractionStatus.QUARANTINE, 0.85,
            f"[{source}] 不正行比率超過 ({invalid_ratio:.0%} >= {MAX_INVALID_RATIO:.0%})",
            HardFailReason.HIGH_INVALID_RATIO,
            SoftFailReason.NONE,
        )

    # ======== Success vs Partial ========

    # profit_non_null が足りない場合は partial
    if profit_non_null < MIN_PROFIT_NON_NULL:
        return (
            ExtractionStatus.PARTIAL, 0.6,
            f"[{source}] 利益非NULLセグメント不足 ({profit_non_null} < {MIN_PROFIT_NON_NULL})",
            HardFailReason.NONE,
            SoftFailReason.WEAK_PROFIT,
        )

    # invalid_ratio が高め (0.2-0.3) → partial
    if invalid_ratio >= 0.2:
        return (
            ExtractionStatus.PARTIAL, 0.65,
            f"[{source}] 不正行比率がやや高い ({invalid_ratio:.0%})",
            HardFailReason.NONE,
            SoftFailReason.BORDERLINE_INVALID_RATIO,
        )

    # ======== Success ========

    # source による confidence 調整
    source_confidence = {
        "xbrl": 0.95,
        "html": 0.85,
        "pdf": 0.75,
    }
    base_confidence = source_confidence.get(source, 0.7)

    # invalid_ratio による微調整
    if invalid_ratio > 0.1:
        base_confidence -= 0.05

    return (
        ExtractionStatus.SUCCESS, base_confidence,
        f"[{source}] success (valid={valid_segment_count}, sales={sales_non_null}, profit={profit_non_null})",
        HardFailReason.NONE,
        SoftFailReason.NONE,
    )


# ============================================================
# ヘルパー
# ============================================================

def _is_non_null(value) -> bool:
    """値が非NULL (None, 0, 空文字列 以外) かどうか。"""
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip() != ""
    return True


def _has_verified_undisclosed_segment_sales(
    valid_records: list[dict], *, source: str, invalid_count: int
) -> bool:
    """Strictly recognize reportable segments whose sales are explicitly nil.

    This is deliberately evidence-driven.  It does not relax the global sales
    threshold and cannot be activated by a missing parser value alone.
    """
    if source != "xbrl" or invalid_count or len(valid_records) < 2:
        return False
    if len({(r.get("period"), r.get("quarter")) for r in valid_records}) != 1:
        return False
    if any(r.get("_segment_period_role") != "current" for r in valid_records):
        return False
    if any(r.get("_segment_member_kind") != "reportable" for r in valid_records):
        return False
    if len({str(r.get("segment_name") or "").strip() for r in valid_records}) != len(valid_records):
        return False
    missing = [r for r in valid_records if not _is_non_null(r.get("segment_sales"))]
    present = [r for r in valid_records if _is_non_null(r.get("segment_sales"))]
    if not missing or not present:
        return False
    if any(not _is_non_null(r.get("segment_profit")) for r in missing):
        return False
    if any(
        r.get("_sales_fact_explicit_nil") is not True
        or not r.get("_sales_fact_names")
        for r in missing
    ):
        return False
    if any(r.get("_sales_reconciliation_verified") is not True for r in valid_records):
        return False
    totals = {r.get("_reportable_sales_total_raw") for r in valid_records}
    consolidated = {r.get("_consolidated_sales_raw") for r in valid_records}
    if len(totals) != 1 or len(consolidated) != 1 or None in totals or None in consolidated:
        return False
    return totals == consolidated
