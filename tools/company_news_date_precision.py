"""Offline date-precision contract for Company News compatibility planning.

This module is deliberately not wired into the production validator, database,
API, or viewer.  It gives saved-artifact replays a precise representation for
publication instants, calendar dates, and unknown values without fabricating a
time or timezone.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from lib.news_monitor import NewsValidationError, normalize_publication_fields, parse_datetime


PRECISIONS = frozenset({"datetime", "date", "unknown"})


class DatePrecisionError(ValueError):
    pass


def normalize_document_date(value: Any) -> str | None:
    """Map a legacy missing value to null and validate a known calendar date.

    The returned value is for the proposed offline representation.  The current
    Strategy v5 schema still requires a string and therefore does not accept
    the returned ``None``.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise DatePrecisionError("document date must be YYYY-MM-DD or null")
    candidate = value.strip()
    try:
        parsed = date.fromisoformat(candidate)
    except ValueError as exc:
        raise DatePrecisionError("document date must be YYYY-MM-DD or null") from exc
    if parsed.isoformat() != candidate:
        raise DatePrecisionError("document date must be canonical YYYY-MM-DD")
    return candidate


def classify_publication(value: Any) -> dict[str, str | None]:
    """Return the proposed three-state publication representation."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return {"published_at": None, "published_date": None,
                "published_precision": "unknown"}
    if isinstance(value, str):
        candidate = value.strip()
        try:
            parsed_date = date.fromisoformat(candidate)
        except ValueError:
            parsed_date = None
        if parsed_date is not None and parsed_date.isoformat() == candidate:
            return {"published_at": None, "published_date": candidate,
                    "published_precision": "date"}
    try:
        exact = parse_datetime(value, "published_at")
    except NewsValidationError as exc:
        raise DatePrecisionError(str(exc)) from exc
    assert exact is not None
    source_date = str(value).strip()[:10]
    return {"published_at": exact,
            "published_date": source_date if len(source_date) == 10 else None,
            "published_precision": "datetime"}


def validate_publication_record(record: dict[str, Any]) -> dict[str, str | None]:
    """Validate the proposed representation without supplying missing facts."""
    try:
        return normalize_publication_fields(record, "publication", allow_legacy=False)
    except NewsValidationError as exc:
        raise DatePrecisionError(str(exc)) from exc


def publication_sort_key(record: dict[str, Any], stable_id: str) -> tuple[str, int, str, str]:
    """Return an ascending key; callers sort descending for newest-first views.

    Exact instants rank ahead of date-only records on the same calendar date.
    Unknown records sort last.  ``stable_id`` makes date-only ties deterministic
    without implying an intraday order.
    """
    value = validate_publication_record(record)
    # Exact legacy rows may not have a safely backfilled published_date.  Their
    # stored instant still supplies a transient ordering anchor without writing
    # a calendar date back to the row.
    anchor = value["published_date"] or (value["published_at"][:10] if value["published_at"] else "0001-01-01")
    rank = {"unknown": 0, "date": 1, "datetime": 2}[value["published_precision"]]
    return anchor, rank, value["published_at"] or "", stable_id


def publication_dedupe_component(record: dict[str, Any]) -> str:
    """Keep precision in dedupe material so date-only is not an exact instant."""
    value = validate_publication_record(record)
    precision = value["published_precision"]
    if precision == "datetime":
        return f"datetime:{value['published_at']}"
    if precision == "date":
        return f"date:{value['published_date']}"
    return "unknown"


def project_document_date(value: Any, search_from: str, search_to: str) -> dict[str, Any]:
    """Classify a saved document date without claiming window membership."""
    start = date.fromisoformat(search_from)
    end = date.fromisoformat(search_to)
    normalized = normalize_document_date(value)
    if normalized is None:
        return {"date": None, "date_precision": "unknown", "window_status": "unknown",
                "eligible_as_window_candidate": False}
    parsed = date.fromisoformat(normalized)
    in_window = start <= parsed <= end
    return {"date": normalized, "date_precision": "date",
            "window_status": "in_window" if in_window else "outside_window",
            "eligible_as_window_candidate": in_window}
