"""Company Viewer notification eligibility rules.

Correction disclosures must still be downloaded, parsed, and merged into the
canonical financial data.  They are excluded only at the notification
persistence boundary.
"""
from __future__ import annotations

import re
import unicodedata


_COLLAPSIBLE_WHITESPACE = re.compile(r"\s+")


def normalize_notification_title(title: str | None) -> str:
    """Normalize harmless title variants before notification classification."""
    normalized = unicodedata.normalize("NFKC", str(title or ""))
    return _COLLAPSIBLE_WHITESPACE.sub(" ", normalized).strip()


def is_correction_disclosure_title(title: str | None) -> bool:
    """Return True only for correction/partial-change disclosure titles.

    ``修正`` is deliberately not a marker: forecast and dividend revisions are
    normal important notifications.  NFKC makes full/half-width parentheses
    and spaces equivalent before matching.
    """
    normalized = normalize_notification_title(title)
    return "訂正" in normalized or "一部変更" in normalized
