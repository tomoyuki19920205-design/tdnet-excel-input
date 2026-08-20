"""Semantic classification for procedural interim-review completion disclosures.

The classifier deliberately operates on disclosure intent rather than one exact
title.  A review-completion marker is procedural unless the same title also
advertises a substantive correction or revision.  Substantive disclosures must
continue through the existing earnings/forecast/dividend pipelines.
"""
from __future__ import annotations

import re
import unicodedata


PROCEDURAL_REVIEW_COMPLETION = "procedural_review_completion"


def normalize_disclosure_title(title: str) -> str:
    """Return a width/whitespace/line-break invariant title."""
    normalized = unicodedata.normalize("NFKC", title or "").lower()
    return re.sub(r"\s+", "", normalized)


_REVIEW_COMPLETION_PATTERNS = (
    re.compile(r"(?:公認会計士等|監査法人)?(?:による)?期中レビュー(?:の)?完了"),
    re.compile(r"期中レビュー完了"),
)

# A title carrying one of these signals is not a procedural-only disclosure.
# "開示事項の変更" is intentionally conservative: until the extracted diff is
# known, retaining a potentially material notification is safer than hiding it.
_MATERIAL_CHANGE_PATTERNS = (
    re.compile(r"訂正|修正|correction|revision"),
    re.compile(r"(?:業績|予想|配当|eps|1株|一株|売上|収益|利益|数値|決算).{0,12}(?:変更|差異)"),
    re.compile(r"(?:変更|差異).{0,12}(?:業績|予想|配当|eps|1株|一株|売上|収益|利益|数値|決算)"),
)

_AMBIGUOUS_CHANGE_PATTERNS = (
    re.compile(r"開示事項(?:の)?変更"),
    re.compile(r"(?:一部|内容)(?:の)?変更"),
)


def has_review_completion_marker(title: str) -> bool:
    normalized = normalize_disclosure_title(title)
    return any(pattern.search(normalized) for pattern in _REVIEW_COMPLETION_PATTERNS)


def has_material_change_marker(title: str) -> bool:
    normalized = normalize_disclosure_title(title)
    return any(pattern.search(normalized) for pattern in _MATERIAL_CHANGE_PATTERNS)


def has_ambiguous_change_marker(title: str) -> bool:
    """Return true when a title says 'changed' without naming a financial item."""
    normalized = normalize_disclosure_title(title)
    return any(pattern.search(normalized) for pattern in _AMBIGUOUS_CHANGE_PATTERNS)


def classify_procedural_review_completion(title: str) -> str | None:
    """Classify a procedural-only review completion, or return ``None``.

    ``None`` means either this is not a review-completion disclosure, or the
    title also carries a material-change signal and must follow an existing
    investor-notification route.
    """
    if not has_review_completion_marker(title):
        return None
    if has_material_change_marker(title):
        return None
    # Generic "disclosure changed" wording needs extracted-value comparison;
    # it must not be discarded by the early title-only classifier.
    if has_ambiguous_change_marker(title):
        return None
    return PROCEDURAL_REVIEW_COMPLETION


def should_suppress_earnings_notification(title: str) -> bool:
    """Final notification-candidate policy for review-completion disclosures."""
    return classify_procedural_review_completion(title) is not None


_FINANCIAL_COMPARE_FIELDS = (
    "sales_value",
    "gross_profit_value",
    "op_value",
    "ordinary_profit_value",
    "profit_before_tax_value",
    "net_income_value",
    "eps_value",
    "guidance_sales",
    "guidance_op",
    "guidance_eps",
    "dividend_forecast",
)


def _same_number(left, right) -> bool:
    try:
        return abs(float(left) - float(right)) <= max(1e-9, abs(float(left)) * 1e-12)
    except (TypeError, ValueError):
        return left == right


def should_suppress_after_financial_comparison(
    title: str,
    previous: dict | None,
    current: dict | None,
) -> bool:
    """Final two-stage policy for ambiguous review-completion revisions.

    Procedural-only titles are suppressed immediately.  Explicit financial
    corrections/revisions are never suppressed.  Generic "disclosure changed"
    titles are suppressed only when at least one comparable financial value is
    available and every comparable actual/forecast/dividend value is unchanged.
    Missing comparison evidence fails open (notification retained).
    """
    if not has_review_completion_marker(title):
        return False
    if has_material_change_marker(title):
        return False
    if not has_ambiguous_change_marker(title):
        return True
    if not previous or not current:
        return False

    compared = 0
    for field in _FINANCIAL_COMPARE_FIELDS:
        before = previous.get(field)
        after = current.get(field)
        if before is None or after is None:
            continue
        compared += 1
        if not _same_number(before, after):
            return False
    return compared > 0
