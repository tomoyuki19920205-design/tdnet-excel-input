"""Evidence-backed issuer-total market capitalisation for NY Top20 packets."""
from __future__ import annotations

from datetime import date
from math import isfinite
from typing import Any
from urllib.parse import urlsplit


class IssuerCapError(ValueError):
    pass


def _date(value: Any, field: str, session: date) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise IssuerCapError(f"{field} needs ISO date") from exc
    if parsed > session:
        raise IssuerCapError(f"{field} is after market session")
    return parsed.isoformat()


def _url(value: Any, field: str) -> str:
    if not isinstance(value, str) or urlsplit(value).scheme != "https" or not urlsplit(value).netloc:
        raise IssuerCapError(f"{field} needs an HTTPS source URL")
    return value


def _positive(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
        raise IssuerCapError(f"{field} must be positive")
    return float(value)


def issuer_cap_from_evidence(
    ticker: str, close: float, session: date, evidence: dict[str, Any],
    official_action: dict[str, Any] | None = None,
) -> tuple[float | None, str, list[dict[str, Any]], dict[str, Any]]:
    """Calculate cap only from dated, sourced class counts and quote conversion.

    A missing count cannot be inferred by dividing a vendor cap by the quote.
    """
    if not isinstance(evidence, dict):
        raise IssuerCapError(f"{ticker}: issuer evidence must be an object")
    status = evidence.get("status")
    if status == "unavailable":
        missing = evidence.get("missing_items")
        if not isinstance(missing, list) or not missing or any(not isinstance(item, str) or not item.strip() for item in missing):
            raise IssuerCapError(f"{ticker}: unavailable needs specific missing items")
        attempts = evidence.get("attempted_sources")
        if not isinstance(attempts, list) or len(attempts) < 2:
            raise IssuerCapError(f"{ticker}: unavailable needs at least two actual source attempts")
        for index, attempt in enumerate(attempts):
            if not isinstance(attempt, dict):
                raise IssuerCapError(f"{ticker}: attempt {index} must be an object")
            _url(attempt.get("url"), f"{ticker}.attempted_sources[{index}].url")
            if not isinstance(attempt.get("outcome"), str) or not attempt["outcome"].strip():
                raise IssuerCapError(f"{ticker}: attempt {index} needs an outcome")
        if evidence.get("components"):
            raise IssuerCapError(f"{ticker}: unavailable cannot carry partial components")
        return None, "unavailable", [], evidence
    if status != "verified":
        raise IssuerCapError(f"{ticker}: status must be verified or unavailable")

    effective = None
    if official_action is not None:
        if official_action.get("symbol") != ticker:
            raise IssuerCapError(f"{ticker}: split notice symbol conflicts with issuer evidence")
        effective = date.fromisoformat(official_action["effective_session_date"])

    coverage = evidence.get("class_coverage")
    if not isinstance(coverage, dict):
        raise IssuerCapError(f"{ticker}: complete class coverage is required")
    _url(coverage.get("source_url"), f"{ticker}.class_coverage.source_url")
    coverage_date = date.fromisoformat(_date(coverage.get("as_of"), f"{ticker}.class_coverage.as_of", session))
    if effective is not None and effective <= session and coverage_date < effective:
        raise IssuerCapError(f"{ticker}: class coverage predates split")
    if not isinstance(coverage.get("basis"), str) or not coverage["basis"].strip():
        raise IssuerCapError(f"{ticker}: class_coverage needs an explicit basis")
    names = coverage.get("classes")
    if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n.strip() for n in names):
        raise IssuerCapError(f"{ticker}: class_coverage.classes must list economic classes")
    if len(set(names)) != len(names):
        raise IssuerCapError(f"{ticker}: duplicate economic class")
    if len(names) > 1:
        _url(coverage.get("economic_equivalence_source_url"), f"{ticker}.class_coverage.economic_equivalence_source_url")
    components = evidence.get("components")
    if not isinstance(components, list) or len(components) != len(names):
        raise IssuerCapError(f"{ticker}: every economic class needs one component")
    result: list[dict[str, Any]] = []
    total = 0.0
    for index, component in enumerate(components):
        if not isinstance(component, dict) or component.get("class") != names[index]:
            raise IssuerCapError(f"{ticker}: component {index} does not match class coverage")
        shares = _positive(component.get("shares_outstanding"), f"{ticker}.shares_outstanding")
        if not shares.is_integer():
            raise IssuerCapError(f"{ticker}: shares_outstanding must be an integer")
        count_date = date.fromisoformat(_date(component.get("share_count_as_of"), f"{ticker}.share_count_as_of", session))
        if effective is not None and effective <= session and count_date < effective:
            raise IssuerCapError(f"{ticker}: share count predates split; post-action class count is required")
        _url(component.get("share_count_source_url"), f"{ticker}.share_count_source_url")
        ratio = _positive(component.get("quoted_security_ratio"), f"{ticker}.quoted_security_ratio")
        if ratio != 1:
            _url(component.get("quoted_security_ratio_source_url"), f"{ticker}.quoted_security_ratio_source_url")
        # A separate listed class can use its own documented close. Otherwise
        # the packet's regular-session close is converted to underlying units.
        if "price" in component:
            price = _positive(component["price"], f"{ticker}.price")
            _url(component.get("price_source_url"), f"{ticker}.price_source_url")
            _date(component.get("price_as_of"), f"{ticker}.price_as_of", session)
            if component["price_as_of"] != session.isoformat():
                raise IssuerCapError(f"{ticker}: price date differs from market session")
            basis = "separate_class_regular_close"
        else:
            price = close / ratio
            basis = "quoted_regular_close_divided_by_security_ratio"
        row = {**component, "price": price, "price_as_of": session.isoformat(), "pricing_basis": basis}
        result.append(row)
        total += price * shares
    if len(result) == 1:
        method = "issuer_total_ads" if result[0]["quoted_security_ratio"] != 1 else "issuer_total_single_class"
    else:
        method = "issuer_total_dual_class"
    if not isfinite(total) or total <= 0:
        raise IssuerCapError(f"{ticker}: invalid issuer-total value")
    return total, method, result, evidence
