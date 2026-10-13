"""Shared actual/consolidated context guards for financial XBRL facts."""
from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET


_ACTUAL_SCOPE_REJECT_TOKENS = (
    "forecast",
    "estimate",
    "lower",
    "upper",
    "nonconsolidated",
)
_ALLOWED_ENTITY_TOTAL_MEMBERS = {"ConsolidatedMember", "ResultMember"}


def local_name(name: str) -> str:
    if name.startswith("{") and "}" in name:
        return name.split("}", 1)[1]
    return name.split(":")[-1]


def parse_context_metadata(root: ET.Element) -> dict[str, dict[str, Any]]:
    """Parse duration and dimension/member data for every XBRL context."""
    result: dict[str, dict[str, Any]] = {}
    for elem in root.iter():
        if local_name(str(elem.tag)) != "context":
            continue
        context_id = elem.get("id", "")
        if not context_id:
            continue
        start = end = instant = ""
        members: list[str] = []
        dimensions: list[str] = []
        for child in elem.iter():
            child_local = local_name(str(child.tag))
            text = (child.text or "").strip()
            if child_local == "startDate":
                start = text
            elif child_local == "endDate":
                end = text
            elif child_local == "instant":
                instant = text
            elif child_local == "explicitMember":
                dimensions.append(local_name(child.get("dimension", "")))
                if text:
                    members.append(local_name(text))
            elif child_local == "typedMember":
                dimensions.append(local_name(child.get("dimension", "")))
                members.append("__typed_member__")
        result[context_id] = {
            "start": start,
            "end": end,
            "instant": instant,
            "members": members,
            "dimensions": dimensions,
        }
    return result


def is_actual_consolidated_duration_context(
    context_ref: str,
    contexts: dict[str, dict[str, Any]],
) -> bool:
    """Return true only for actual consolidated/entity-total durations.

    Summary facts normally use ``ConsolidatedMember`` and ``ResultMember``.
    Detailed consolidated statements commonly use a dimensionless duration.
    Both forms are valid, while non-consolidated, forecast, instant, segment,
    and other dimensioned contexts are rejected.
    """
    if not context_ref:
        return False
    lowered_ref = context_ref.lower()
    if any(token in lowered_ref for token in _ACTUAL_SCOPE_REJECT_TOKENS):
        return False

    metadata = contexts.get(context_ref)
    if metadata is None:
        # Some synthetic/legacy iXBRL omits context definitions. Keep this
        # fallback compatible with standard detailed-statement duration IDs,
        # but never accept a member context unless it is explicitly
        # consolidated actual/result scope.
        if "Member" in context_ref:
            return (
                "ConsolidatedMember" in context_ref
                and "ResultMember" in context_ref
                and "Segment" not in context_ref
                and "Axis" not in context_ref
            )
        return (
            ("Duration" in context_ref or "Interim" in context_ref)
            and "Segment" not in context_ref
            and "Axis" not in context_ref
        )

    if metadata.get("instant"):
        return False
    if not metadata.get("start") or not metadata.get("end"):
        return False

    members = set(metadata.get("members") or [])
    if any(
        any(token in member.lower() for token in _ACTUAL_SCOPE_REJECT_TOKENS)
        for member in members
    ):
        return False
    if not members:
        return True
    if not members.issubset(_ALLOWED_ENTITY_TOTAL_MEMBERS):
        return False
    return "ConsolidatedMember" in members
