"""Strict normalization for actual consolidated J-Quants financial details."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Iterable


PBT_DETAIL_KEYS: tuple[str, ...] = (
    "Profit (loss) before tax from continuing operations (IFRS)",
    "Profit before tax (IFRS)",
    "Profit (loss) before income taxes",
    "Income (loss) before income taxes",
    "Income before income taxes",
)

_ACTUAL_CONSOLIDATED_DOCUMENT = re.compile(
    r"^(1Q|2Q|3Q|FY)FinancialStatements_Consolidated_([A-Za-z0-9]+)$"
)
_VALID_QUARTERS = {"1Q", "2Q", "3Q", "FY"}


@dataclass(frozen=True)
class ActualConsolidatedPBT:
    code: str
    disclosure_date: str
    disclosure_time: str
    disclosure_number: str
    disclosure_datetime: str
    document_type: str
    accounting_standard: str
    fiscal_year_end: str
    quarter: str
    pbt_field: str
    raw_value_jpy: int
    actual_scope: str = "actual_consolidated"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _exact_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number != number.to_integral_value():
        return None
    return int(number)


def actual_consolidated_document_info(document_type: str) -> tuple[str, str] | None:
    match = _ACTUAL_CONSOLIDATED_DOCUMENT.fullmatch(document_type or "")
    if not match:
        return None
    return match.group(1), match.group(2)


def extract_pbt_from_fs(fs: Any) -> tuple[str, int] | None:
    """Extract one exact actual PBT PL item; never use NC or forecast fields."""
    if not isinstance(fs, dict):
        return None
    matches: list[tuple[str, int]] = []
    for field in PBT_DETAIL_KEYS:
        if field not in fs:
            continue
        value = _exact_int(fs[field])
        if value is not None:
            matches.append((field, value))
    if not matches:
        return None
    if len({value for _, value in matches}) != 1:
        raise ValueError(f"conflicting exact PBT fields: {matches}")
    return matches[0]


def normalize_actual_consolidated_pbt(
    detail: dict[str, Any],
    summary: dict[str, Any],
    *,
    expected_code: str,
) -> ActualConsolidatedPBT | None:
    """Join one details row to its J-Quants summary metadata and normalize PBT."""
    if detail.get("Code") != expected_code or summary.get("Code") != expected_code:
        return None
    if str(detail.get("DiscNo") or "") != str(summary.get("DiscNo") or ""):
        return None
    document_type = str(detail.get("DocType") or "")
    if document_type != str(summary.get("DocType") or ""):
        return None
    document_info = actual_consolidated_document_info(document_type)
    if document_info is None:
        return None
    document_quarter, accounting_standard = document_info
    quarter = str(summary.get("CurPerType") or "")
    fiscal_year_end = str(summary.get("CurFYEn") or "")
    if quarter not in _VALID_QUARTERS or quarter != document_quarter or not fiscal_year_end:
        return None
    pbt = extract_pbt_from_fs(detail.get("FS"))
    if pbt is None:
        return None
    field, raw_value = pbt
    disclosure_date = str(detail.get("DiscDate") or "")
    disclosure_time = str(detail.get("DiscTime") or "00:00:00")
    try:
        parsed = datetime.fromisoformat(f"{disclosure_date}T{disclosure_time}")
    except ValueError:
        return None
    return ActualConsolidatedPBT(
        code=expected_code,
        disclosure_date=disclosure_date,
        disclosure_time=disclosure_time,
        disclosure_number=str(detail.get("DiscNo") or ""),
        disclosure_datetime=parsed.isoformat() + "+09:00",
        document_type=document_type,
        accounting_standard=accounting_standard,
        fiscal_year_end=fiscal_year_end,
        quarter=quarter,
        pbt_field=field,
        raw_value_jpy=raw_value,
    )


def select_latest_effective_pbt(
    details: Iterable[dict[str, Any]],
    summaries_by_disclosure: dict[str, dict[str, Any]],
    *,
    expected_code: str,
) -> tuple[list[ActualConsolidatedPBT], list[dict[str, Any]]]:
    """Select the latest actual consolidated filing for each fiscal period.

    The latest eligible filing is selected before PBT extraction. Therefore an
    explicit missing PBT in a correction cannot silently fall back to an older
    disclosure.
    """
    eligible: dict[tuple[str, str], tuple[tuple[str, str, str], dict[str, Any], dict[str, Any]]] = {}
    audit: list[dict[str, Any]] = []
    for detail in details:
        disc_no = str(detail.get("DiscNo") or "")
        summary = summaries_by_disclosure.get(disc_no)
        reason = None
        if detail.get("Code") != expected_code:
            reason = "code_mismatch"
        elif summary is None:
            reason = "summary_metadata_missing"
        elif summary.get("Code") != expected_code:
            reason = "summary_code_mismatch"
        elif detail.get("DocType") != summary.get("DocType"):
            reason = "document_type_mismatch"
        else:
            info = actual_consolidated_document_info(str(detail.get("DocType") or ""))
            if info is None:
                reason = "not_actual_consolidated_financial_statements"
            elif summary.get("CurPerType") != info[0] or not summary.get("CurFYEn"):
                reason = "period_metadata_mismatch"
        if reason:
            audit.append({"disclosure_number": disc_no, "status": "rejected", "reason": reason})
            continue
        key = (str(summary["CurFYEn"]), str(summary["CurPerType"]))
        order = (
            str(detail.get("DiscDate") or ""),
            str(detail.get("DiscTime") or ""),
            disc_no,
        )
        previous = eligible.get(key)
        if previous is None or order > previous[0]:
            eligible[key] = (order, detail, summary)

    selected: list[ActualConsolidatedPBT] = []
    for key, (_, detail, summary) in sorted(eligible.items()):
        record = normalize_actual_consolidated_pbt(
            detail, summary, expected_code=expected_code
        )
        if record is None:
            audit.append({
                "period": key[0],
                "quarter": key[1],
                "disclosure_number": detail.get("DiscNo"),
                "status": "rejected",
                "reason": "latest_effective_has_no_exact_pbt",
            })
            continue
        selected.append(record)
        audit.append({
            "period": record.fiscal_year_end,
            "quarter": record.quarter,
            "disclosure_number": record.disclosure_number,
            "status": "selected",
            "pbt_field": record.pbt_field,
            "raw_value_jpy": record.raw_value_jpy,
        })
    return selected, audit
