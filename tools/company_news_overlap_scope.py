"""Python authority for TDNET/IR Nightly overlap in Company News."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.company_ir_document_contract import classify_ir_nightly_asset


OUT_OF_SCOPE_IR_NIGHTLY = "OUT_OF_SCOPE_IR_NIGHTLY"
OUT_OF_SCOPE_TDNET = "OUT_OF_SCOPE_TDNET"


class OverlapScopeError(ValueError):
    pass


@dataclass(frozen=True)
class IrNightlyCoverageRecord:
    covered: bool
    asset_type: str | None
    authority: str

    def validate(self) -> "IrNightlyCoverageRecord":
        if self.authority not in {"IR_NIGHTLY_CLASSIFIER", "IR_NIGHTLY_INVENTORY"}:
            raise OverlapScopeError("invalid IR Nightly coverage authority")
        if self.covered != (self.asset_type in {"earnings_material", "earnings_video"}):
            raise OverlapScopeError("IR Nightly coverage record is inconsistent")
        return self


def ir_nightly_coverage(title: str, url: str, *,
                        record: IrNightlyCoverageRecord | None = None) -> IrNightlyCoverageRecord:
    if record is not None:
        return record.validate()
    asset_type = classify_ir_nightly_asset(title, url)
    return IrNightlyCoverageRecord(asset_type is not None, asset_type,
                                   "IR_NIGHTLY_CLASSIFIER")


def classify_news_monitor_scope(title: str, url: str, *, tdnet_covered: bool = False,
                                nightly_record: IrNightlyCoverageRecord | None = None,
                                in_scope_label: str = "IN_SCOPE_COMPANY_WEB") -> dict[str, Any]:
    if tdnet_covered:
        return {"scope_label": OUT_OF_SCOPE_TDNET,
                "scope_reason": "Equivalent disclosure is covered by TDNET.",
                "body_fetch_required": False, "candidate_allowed": False}
    nightly = ir_nightly_coverage(title, url, record=nightly_record)
    if nightly.covered:
        return {
            "scope_label": OUT_OF_SCOPE_IR_NIGHTLY,
            "scope_reason": (
                f"{nightly.asset_type} is covered by IR Nightly "
                f"({nightly.authority})."),
            "body_fetch_required": False,
            "candidate_allowed": False,
        }
    return {"scope_label": in_scope_label,
            "scope_reason": "No TDNET or IR Nightly covered-document overlap established.",
            "body_fetch_required": True, "candidate_allowed": True}


def assert_incremental_ir_candidate_allowed(document: Mapping[str, Any]) -> None:
    nightly = ir_nightly_coverage(str(document.get("title") or ""),
                                  str(document.get("url") or ""))
    if (document.get("scope_label") == "IN_SCOPE_INCREMENTAL_IR"
            and nightly.covered):
        raise OverlapScopeError(
            "incremental_ir candidate conflicts with IR_NIGHTLY_COVERED_DOCUMENT")


def apply_ir_nightly_overlap(response: Mapping[str, Any]) -> dict[str, Any]:
    """Exclude Nightly-covered documents before candidate/final selection."""
    repaired = copy.deepcopy(response)
    strategy = repaired.get("strategy_telemetry")
    payload = repaired.get("research_result")
    if not isinstance(strategy, dict) or not isinstance(payload, dict):
        raise OverlapScopeError("canonical response is incomplete")
    documents = strategy.get("documents")
    candidates = strategy.get("candidate_provenance")
    items = payload.get("items")
    if not isinstance(documents, list) or not isinstance(candidates, list) or not isinstance(items, list):
        raise OverlapScopeError("canonical document/candidate state is incomplete")

    excluded_urls: set[str] = set()
    for document in documents:
        assert_incremental_ir_candidate_allowed(document)
        decision = classify_news_monitor_scope(
            str(document.get("title") or ""), str(document.get("url") or ""),
            tdnet_covered=document.get("scope_label") == OUT_OF_SCOPE_TDNET,
            in_scope_label=str(document.get("scope_label") or "IN_SCOPE_COMPANY_WEB"),
        )
        if decision["scope_label"] != OUT_OF_SCOPE_IR_NIGHTLY:
            continue
        document.update(
            scope_label=OUT_OF_SCOPE_IR_NIGHTLY,
            scope_reason=decision["scope_reason"],
            material=False,
            body_status="SKIPPED",
            attempt_evidence=None,
            incremental_information=False,
            incremental_information_evidence=None,
        )
        excluded_urls.add(str(document.get("url") or ""))

    for candidate in candidates:
        if candidate.get("document_url") in excluded_urls:
            candidate.update(
                selected_for_output=False,
                selection_reason="Python overlap authority: covered by IR Nightly.",
                source_url=None, headline=None, economic_transmission=None, event_key=None,
            )
    payload["items"] = [item for item in items if item.get("source_url") not in excluded_urls]
    strategy["final_event_count"] = sum(
        row.get("selected_for_output") is True for row in candidates)
    return repaired


def remove_properly_excluded_document_gaps(
        documents: Sequence[Mapping[str, Any]],
        gaps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    excluded = {str(row.get("url")) for row in documents
                if row.get("scope_label") == OUT_OF_SCOPE_IR_NIGHTLY}
    return [dict(gap) for gap in gaps
            if not (gap.get("gap_type", "document_access") == "document_access"
                    and str(gap.get("url")) in excluded)]


def classify_xj_inventory_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    candidates = entry.get("canonical_document_candidates") or []
    url = str(candidates[0].get("url") if candidates and isinstance(candidates[0], Mapping) else "")
    decision = classify_news_monitor_scope(str(entry.get("title") or ""), url)
    return {**copy.deepcopy(dict(entry)), **decision,
            "inventory_retained": True,
            "llm_materiality_required": decision["candidate_allowed"]}
