"""Canonicalization rules for J-Quants forecast revision histories."""
from __future__ import annotations

from collections import defaultdict
import re
from typing import Any, Iterable, Mapping

from lib.jquants_values import parse_optional_boolean


CORRECTION_DISCLOSURE_ITEM_CODES = frozenset({"11323", "11741"})
RETRACTION_TITLE_TOKENS = ("撤回", "取消", "取り下げ", "取下げ")
_CORRECTION_TITLE = re.compile(r"(?:再?訂正|数値データ(?:再)?訂正)")
_FORECAST_TITLE_TOKENS = ("業績予想", "業績見通し")


def metadata_role(metadata: Mapping[str, Any] | None) -> str:
    """Return original/correction/retraction from TDnet disclosure metadata."""
    if not metadata:
        return "original"
    title = str(metadata.get("title") or metadata.get("Title") or "")
    disc_status = str(metadata.get("disc_status") or metadata.get("DiscStatus") or "").lower()
    rev_no = str(metadata.get("rev_no") or metadata.get("RevNo") or "1")
    items = metadata.get("disc_items") or metadata.get("DiscItems") or []
    item_codes = {str(value) for value in items}
    if (
        item_codes & CORRECTION_DISCLOSURE_ITEM_CODES
        or _CORRECTION_TITLE.search(title)
        or disc_status == "revision"
        or (rev_no.isdigit() and int(rev_no) > 1)
    ):
        return "correction"
    return "original"


def is_forecast_retraction(
    raw: Mapping[str, Any], metadata: Mapping[str, Any] | None, prefix: str
) -> bool:
    """Return true only when the forecast itself was withdrawn.

    A generic ``取り下げ`` title is insufficient because a disclosure can revise
    earnings while withdrawing only a medium-term plan. J-Quants represents a
    withdrawn forecast with no values for that forecast scope, so both title
    intent and an empty value vector are required.
    """
    if not metadata:
        return False
    title = str(metadata.get("title") or metadata.get("Title") or "")
    if not any(token in title for token in _FORECAST_TITLE_TOKENS):
        return False
    if not any(token in title for token in RETRACTION_TITLE_TOKENS):
        return False
    keys = (
        f"{prefix}Sales", f"{prefix}NCSales", f"{prefix}OP", f"{prefix}NCOP",
        f"{prefix}OdP", f"{prefix}NCOdP", f"{prefix}NP", f"{prefix}NCNP",
        f"{prefix}EPS", f"{prefix}NCEPS",
    )
    return not any(raw.get(key) not in (None, "") for key in keys)


def statement_identity(raw: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Identify repeated/corrected versions of one financial statement."""
    document_type = str(raw.get("DocType") or "")
    if "FinancialStatements" not in document_type or "ForecastRevision" in document_type:
        return None
    return (
        str(raw.get("Code") or raw.get("LocalCode") or ""),
        document_type,
        str(raw.get("CurPerSt") or ""),
        str(raw.get("CurPerEn") or ""),
        str(raw.get("CurFYEn") or ""),
    )


def canonicalize_statement_rows(
    rows: Iterable[dict[str, Any]],
    metadata_by_disclosure: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse corrected/review-completion copies into the original statement.

    The latest XBRL values are authoritative, while the earliest disclosure ID
    and timestamp remain the economic event anchor. A retracted statement is
    removed from the chain.
    """
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    passthrough: list[dict[str, Any]] = []
    for raw in rows:
        identity = statement_identity(raw)
        if identity is None:
            passthrough.append(raw)
        else:
            groups[identity].append(raw)

    result = list(passthrough)
    for members in groups.values():
        ordered = sorted(members, key=_raw_order_key)
        active: dict[str, Any] | None = None
        anchor: dict[str, Any] | None = None
        source_ids: list[str] = []
        for raw in ordered:
            disclosure_id = str(raw.get("DiscNo") or "")
            source_ids.append(disclosure_id)
            if anchor is None:
                anchor = raw
            active = raw
        if active is None or anchor is None:
            continue
        canonical = dict(active)
        canonical["DiscNo"] = anchor.get("DiscNo")
        canonical["DiscDate"] = anchor.get("DiscDate")
        canonical["DiscTime"] = anchor.get("DiscTime")
        canonical["_canonical_source_disclosure_ids"] = tuple(source_ids)
        canonical["_canonical_statement"] = True
        canonical["_suppress_revision_event"] = (
            metadata_role(metadata_by_disclosure.get(str(anchor.get("DiscNo") or "")))
            != "original"
        )
        canonical["_retrospective_restatement"] = parse_optional_boolean(
            active.get("RetroRst")
        )
        result.append(canonical)
    return sorted(result, key=_raw_order_key)


def canonical_forecast_anchors(
    rows: Iterable[dict[str, Any]],
    metadata_by_disclosure: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[tuple[str, str, str], str], set[str]]:
    """Map correction records to the prior original and identify retractions.

    Keys are ``(correction disclosure ID, target FY, prefix)``. Corrections do
    not become new events; their values replace the immediately preceding
    original forecast-revision disclosure for the same ticker/target/prefix.
    """
    latest: dict[tuple[str, str, str, str], str] = {}
    latest_by_scope: dict[tuple[str, str, str], str] = {}
    anchors: dict[tuple[str, str, str], str] = {}
    retracted: set[str] = set()
    for raw in sorted(rows, key=_raw_order_key):
        disclosure_id = str(raw.get("DiscNo") or "")
        ticker = str(raw.get("Code") or raw.get("LocalCode") or "")
        document_type = str(raw.get("DocType") or "")
        role = metadata_role(metadata_by_disclosure.get(disclosure_id))
        targets = [(str(raw.get("CurFYEn") or ""), "F")]
        next_target = str(raw.get("NxtFYEn") or "")
        if next_target:
            targets.append((next_target, "NxF"))
        for target, prefix in targets:
            if not target:
                continue
            key = (ticker, document_type, target, prefix)
            scope = (ticker, target, prefix)
            if is_forecast_retraction(
                raw, metadata_by_disclosure.get(disclosure_id), prefix
            ):
                prior = latest_by_scope.pop(scope, None)
                if prior:
                    retracted.add(prior)
                latest.pop(key, None)
            elif role == "original":
                latest[key] = disclosure_id
                latest_by_scope[scope] = disclosure_id
            elif role == "correction":
                if key in latest:
                    anchors[(disclosure_id, target, prefix)] = latest[key]
    return anchors, retracted


def _raw_order_key(raw: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(raw.get("DiscDate") or ""),
        str(raw.get("DiscTime") or ""),
        str(raw.get("DiscNo") or ""),
    )
