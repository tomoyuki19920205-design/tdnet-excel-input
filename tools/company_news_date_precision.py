"""Offline date-precision contract for Company News compatibility planning.

This module is deliberately not wired into the production validator, database,
API, or viewer.  It gives saved-artifact replays a precise representation for
publication instants, calendar dates, and unknown values without fabricating a
time or timezone.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from lib.news_monitor import NewsValidationError, parse_datetime


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
    return {"published_at": exact,
            "published_date": datetime.fromisoformat(exact).date().isoformat(),
            "published_precision": "datetime"}


def validate_publication_record(record: dict[str, Any]) -> dict[str, str | None]:
    """Validate the proposed representation without supplying missing facts."""
    if not isinstance(record, dict) or record.get("published_precision") not in PRECISIONS:
        raise DatePrecisionError("published_precision must be datetime, date, or unknown")
    precision = record["published_precision"]
    published_at = record.get("published_at")
    published_date = normalize_document_date(record.get("published_date"))
    if precision == "unknown":
        if published_at is not None or published_date is not None:
            raise DatePrecisionError("unknown precision cannot carry a publication value")
    elif precision == "date":
        if published_at is not None or published_date is None:
            raise DatePrecisionError("date precision requires only published_date")
    else:
        if published_at is None or published_date is None:
            raise DatePrecisionError("datetime precision requires published_at and published_date")
        exact = classify_publication(published_at)
        if exact["published_precision"] != "datetime" or exact["published_date"] != published_date:
            raise DatePrecisionError("published_at and published_date disagree")
        published_at = exact["published_at"]
    return {"published_at": published_at, "published_date": published_date,
            "published_precision": precision}


def publication_sort_key(record: dict[str, Any], stable_id: str) -> tuple[str, int, str, str]:
    """Return an ascending key; callers sort descending for newest-first views.

    Exact instants rank ahead of date-only records on the same calendar date.
    Unknown records sort last.  ``stable_id`` makes date-only ties deterministic
    without implying an intraday order.
    """
    value = validate_publication_record(record)
    anchor = value["published_date"] or "0001-01-01"
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
