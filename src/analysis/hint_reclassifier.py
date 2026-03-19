"""hint_reclassifier.py — candidate_guard reject 後の review_hint 再分類

detail_breakdown_guard / invalid_structure で reject されたもののうち、
実態が「セグメント表なし会社の narrative page」を正しく再分類する。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReclassifyResult:
    """再分類結果"""
    final_hint: str
    final_reason: str
    reclassified: bool
    basis: str  # 再分類の根拠 (短い説明)


def reclassify_candidate_failure(
    raw_reason: str,
    raw_hint: str,
    valid_segment: int,
    narrative: int,
    garbage: int,
    detail_breakdown: int,
    bs_cf: int,
    pl_account: int,
    total_or_metric: int,
    has_sales_header: bool,
    has_profit_header: bool,
) -> ReclassifyResult:
    """
    candidate_guard reject 後の review_hint を再分類する。

    対象: detail_breakdown_guard / invalid_structure のみを再分類。
    他の reject reason (narrative_guard, pl_guard, bs_cf_guard 等) はそのまま返す。

    再分類ルール:
    1. no_segment_narrative_page:
       - valid=0 かつ (narrative+garbage)>=3 かつ header 弱い
       - valid<=1 かつ (narrative+garbage) が dominant かつ header なし
    2. pdf_segment_like_but_invalid_structure (維持):
       - valid>=2 (親行あり) → 直せば救えそう
       - detail_breakdown が dominant → 構造問題
       - header あり → table structure がある
    """
    # 再分類対象外: detail_breakdown_guard / invalid_structure 以外はそのまま返す
    if raw_reason not in ("detail_breakdown_guard", "invalid_structure"):
        return ReclassifyResult(
            final_hint=raw_hint,
            final_reason=raw_reason,
            reclassified=False,
            basis="not_target",
        )

    noise = narrative + garbage
    has_header = has_sales_header or has_profit_header

    # ── Rule 1: 明確な表なし narrative page ──
    # valid=0, noise dominant, header なし → 表がない会社の説明文ページ
    if valid_segment == 0 and noise >= 3 and not has_header:
        return ReclassifyResult(
            final_hint="pdf_no_segment_narrative_page",
            final_reason="no_segment_narrative_page",
            reclassified=True,
            basis=f"valid=0,noise={noise},no_header",
        )

    # ── Rule 2: valid わずかだが noise 支配的、header なし ──
    if valid_segment <= 1 and noise >= 3 and noise > (valid_segment + detail_breakdown) and not has_header:
        return ReclassifyResult(
            final_hint="pdf_no_segment_narrative_page",
            final_reason="no_segment_narrative_page",
            reclassified=True,
            basis=f"valid={valid_segment},noise={noise}>structured,no_header",
        )

    # ── Rule 3: valid=0 かつ header あるが noise 支配的 ──
    # header はあるが valid が全くない → 弱い表候補
    if valid_segment == 0 and noise >= 5 and has_header:
        return ReclassifyResult(
            final_hint="pdf_no_segment_narrative_page",
            final_reason="no_segment_narrative_page",
            reclassified=True,
            basis=f"valid=0,noise={noise},header_present_but_no_segments",
        )

    # ── 維持: 親行あり (valid>=2) → 直せば救えそうな構造問題 ──
    # ── 維持: detail dominant → 構造的な表問題 ──
    # ── 維持: header あり + valid >= 1 → table structure がある ──
    return ReclassifyResult(
        final_hint=raw_hint,
        final_reason=raw_reason,
        reclassified=False,
        basis=f"valid={valid_segment},detail={detail_breakdown},header={'Y' if has_header else 'N'}",
    )
