"""Strict extraction of actual consolidated PL facts from TDnet Summary XBRL."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import io
from typing import Any
from xml.etree import ElementTree as ET
import zipfile

from src.xbrl_clean import read_xbrl_bytes
from src.xbrl_context_scope import (
    is_actual_consolidated_duration_context,
    local_name,
    parse_context_metadata,
)


_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "sales": ("NetSales", "Revenue", "RevenueIFRS", "SalesIFRS"),
    "operating_profit": (
        "OperatingIncome", "OperatingProfit", "OperatingIncomeIFRS",
        "OperatingProfitLossIFRS",
    ),
    "ordinary_profit": ("OrdinaryIncome",),
    "profit_before_tax": (
        "ProfitBeforeTaxIFRS", "ProfitLossBeforeTaxIFRS",
        "IncomeBeforeIncomeTaxes",
    ),
    "net_income": (
        "ProfitAttributableToOwnersOfParent",
        "ProfitLossAttributableToOwnersOfParentIFRS",
        "NetIncome",
    ),
}


@dataclass(frozen=True)
class SummaryActualFact:
    metric: str
    value_jpy: int
    qname: str
    local_name: str
    namespace: str
    context: str
    period_start: str
    period_end: str
    members: tuple[str, ...]
    dimensions: tuple[str, ...]
    unit_ref: str
    scale: int
    source_file: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _namespace_map(xml: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    for _, item in ET.iterparse(io.BytesIO(xml), events=("start-ns",)):
        prefix, uri = item
        result[prefix or ""] = uri
    return result


def _scaled_integer(text: str, scale: str, sign: str) -> int | None:
    try:
        number = Decimal(text.replace(",", "").strip())
        multiplier = Decimal(10) ** int(scale or "0")
    except (InvalidOperation, ValueError):
        return None
    if sign == "-":
        number = -number
    result = number * multiplier
    if not result.is_finite() or result != result.to_integral_value():
        return None
    return int(result)


def _context_matches_quarter(context: str, quarter: str) -> bool:
    markers = {
        "1Q": ("AccumulatedQ1", "FirstQuarter"),
        "2Q": ("AccumulatedQ2", "SecondQuarter", "Interim"),
        "3Q": ("AccumulatedQ3", "ThirdQuarter"),
        "FY": ("CurrentYearDuration", "AnnualMember", "YearEndMember"),
    }
    return any(marker in context for marker in markers.get(quarter, ()))


def extract_summary_actuals_from_zip_bytes(
    raw: bytes,
    *,
    expected_quarter: str,
    expected_period_start: str | None = None,
    expected_period_end: str | None = None,
) -> dict[str, SummaryActualFact]:
    """Extract one actual consolidated Summary fact per canonical PL metric."""
    if not raw.startswith(b"PK\x03\x04"):
        raise ValueError("source is not an XBRL ZIP package")
    aliases = {
        alias: (metric, priority)
        for metric, names in _METRIC_ALIASES.items()
        for priority, alias in enumerate(names)
    }
    candidates: dict[str, list[tuple[int, SummaryActualFact]]] = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = sorted(
            name for name in archive.namelist()
            if "/summary/" in name.lower()
            and name.lower().endswith((".htm", ".html", ".xbrl", ".xml"))
        )
        if not names:
            raise ValueError("TDnet Summary XBRL is missing")
        for name in names:
            xml_text = read_xbrl_bytes(archive.read(name))
            xml = xml_text.encode("utf-8")
            root = ET.fromstring(xml_text)
            contexts = parse_context_metadata(root)
            namespaces = _namespace_map(xml)
            reverse_namespaces = {uri: prefix for prefix, uri in namespaces.items()}
            for elem in root.iter():
                tag_local = local_name(str(elem.tag))
                if tag_local == "nonFraction":
                    qname = str(elem.get("name") or "")
                    concept_local = local_name(qname)
                else:
                    concept_local = tag_local
                    namespace_uri = ""
                    if str(elem.tag).startswith("{"):
                        namespace_uri = str(elem.tag).split("}", 1)[0][1:]
                    prefix = reverse_namespaces.get(namespace_uri, "")
                    qname = f"{prefix}:{concept_local}" if prefix else concept_local
                mapping = aliases.get(concept_local)
                if mapping is None:
                    continue
                context = str(elem.get("contextRef") or "")
                if not _context_matches_quarter(context, expected_quarter):
                    continue
                if not is_actual_consolidated_duration_context(context, contexts):
                    continue
                metadata = contexts.get(context) or {}
                if (
                    expected_period_start is not None
                    and str(metadata.get("start") or "") != expected_period_start
                ):
                    continue
                if (
                    expected_period_end is not None
                    and str(metadata.get("end") or "") != expected_period_end
                ):
                    continue
                text = "".join(elem.itertext()).strip()
                value = _scaled_integer(
                    text, str(elem.get("scale") or ""), str(elem.get("sign") or "")
                )
                if value is None:
                    continue
                metric, priority = mapping
                prefix = qname.split(":", 1)[0] if ":" in qname else ""
                fact = SummaryActualFact(
                    metric=metric,
                    value_jpy=value,
                    qname=qname,
                    local_name=concept_local,
                    namespace=namespaces.get(prefix, ""),
                    context=context,
                    period_start=str(metadata.get("start") or ""),
                    period_end=str(metadata.get("end") or ""),
                    members=tuple(str(v) for v in metadata.get("members") or ()),
                    dimensions=tuple(str(v) for v in metadata.get("dimensions") or ()),
                    unit_ref=str(elem.get("unitRef") or ""),
                    scale=int(str(elem.get("scale") or "0")),
                    source_file=name,
                )
                candidates.setdefault(metric, []).append((priority, fact))
    selected: dict[str, SummaryActualFact] = {}
    for metric, values in candidates.items():
        best_priority = min(priority for priority, _ in values)
        best = [fact for priority, fact in values if priority == best_priority]
        if len({fact.value_jpy for fact in best}) != 1:
            raise ValueError(f"conflicting Summary actual facts for {metric}")
        selected[metric] = sorted(
            best, key=lambda fact: (fact.qname, fact.context, fact.source_file)
        )[0]
    return selected
