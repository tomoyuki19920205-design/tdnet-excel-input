from __future__ import annotations

import datetime

import pytest
from bs4 import BeautifulSoup

from src.segment.extraction_result_validator import (
    ExtractionStatus,
    HardFailReason,
    validate_extraction_result,
)
from src.segment.xbrl_segment_extractor import (
    _extract_ixbrl_segment_data,
    _parse_context_periods,
)


SEGMENTS = (
    "MarketingBusiness",
    "FinancialServicesBusiness",
    "InvestmentBusiness",
)


def _context(role: str, segment: str) -> str:
    if role == "current":
        prefix, start, end = "CurrentYTDDuration", "2025-01-01", "2025-09-30"
    else:
        prefix, start, end = "Prior1YTDDuration", "2024-01-01", "2024-09-30"
    context_id = f"{prefix}_tse-qcedjpfr-23890{segment}ReportableSegmentsMember"
    return f"""
    <xbrli:context id="{context_id}">
      <xbrli:entity><xbrli:identifier>23890</xbrli:identifier></xbrli:entity>
      <xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period>
      <xbrli:scenario>
        <xbrldi:explicitMember dimension="jpcrp_cor:OperatingSegmentsAxis">tse-qcedjpfr-23890:{segment}ReportableSegmentsMember</xbrldi:explicitMember>
      </xbrli:scenario>
    </xbrli:context>
    """


def _fact(concept: str, role: str, segment: str, value: int) -> str:
    prefix = "CurrentYTDDuration" if role == "current" else "Prior1YTDDuration"
    context_id = f"{prefix}_tse-qcedjpfr-23890{segment}ReportableSegmentsMember"
    return f'<ix:nonFraction name="tse-qcedjpfr-23890:{concept}" contextRef="{context_id}" unitRef="JPY">{value}</ix:nonFraction>'


def _document(*, include_revenue2: bool = True, include_external: bool = True):
    parts = []
    for role, sales_base, profit_base in (("current", 8_000, 1_000), ("previous", 7_000, 900)):
        for index, segment in enumerate(SEGMENTS):
            parts.append(_context(role, segment))
            if include_revenue2:
                parts.append(_fact("Revenue2", role, segment, sales_base + index * 100))
            if include_external:
                parts.append(_fact("RevenuesFromExternalCustomers2", role, segment, sales_base + index * 100 - 10))
            parts.append(_fact("OperatingIncome", role, segment, profit_base + index * 10))
    return BeautifulSoup("<html>" + "".join(parts) + "</html>", "html.parser")


def _records(rows):
    return [
        {
            "segment_name": name,
            "segment_sales": data["sales"],
            "segment_profit": data["profit"],
            "_segment_member_kind": data["segment_member_kind"],
        }
        for (name, _role), data in rows.items()
    ]


def test_company_revenue2_is_selected_over_external_customer_variant():
    soup = _document()
    rows = _extract_ixbrl_segment_data(
        soup, "JGAAP", "3Q", _parse_context_periods(soup), datetime.date(2025, 9, 30)
    )
    assert len(rows) == 6
    current = rows[("MarketingBusiness", "current")]
    assert current["sales"] == 8_000
    assert current["sales_fact_selected_name"] == "tse-qcedjpfr-23890:revenue2"
    assert current["profit"] == 1_000


@pytest.mark.parametrize("concept", ["Revenue2", "RevenuesFromExternalCustomers2"])
def test_each_company_revenue2_variant_is_a_supported_sales_fact(concept):
    soup = _document(
        include_revenue2=concept == "Revenue2",
        include_external=concept == "RevenuesFromExternalCustomers2",
    )
    rows = _extract_ixbrl_segment_data(
        soup, "JGAAP", "3Q", _parse_context_periods(soup), datetime.date(2025, 9, 30)
    )
    assert len(rows) == 6
    assert all(data["sales"] is not None for data in rows.values())
    validation = validate_extraction_result(_records(rows), source="xbrl")
    assert validation.status is ExtractionStatus.SUCCESS
    assert validation.hard_fail_reason is HardFailReason.NONE


def test_profit_only_shape_remains_quarantined_without_revenue2_evidence():
    soup = _document(include_revenue2=False, include_external=False)
    rows = _extract_ixbrl_segment_data(
        soup, "JGAAP", "3Q", _parse_context_periods(soup), datetime.date(2025, 9, 30)
    )
    assert len(rows) == 6
    assert all(data["sales"] is None for data in rows.values())
    validation = validate_extraction_result(_records(rows), source="xbrl")
    assert validation.status is ExtractionStatus.QUARANTINE
    assert validation.hard_fail_reason is HardFailReason.TOO_FEW_SALES


def test_similarly_named_unsupported_metric_does_not_become_sales():
    soup = _document(include_revenue2=False, include_external=False)
    segment = SEGMENTS[0]
    extra = BeautifulSoup(_fact("ChangeInRevenue2", "current", segment, 10), "html.parser")
    soup.html.append(extra.find("ix:nonfraction"))
    rows = _extract_ixbrl_segment_data(
        soup, "JGAAP", "3Q", _parse_context_periods(soup), datetime.date(2025, 9, 30)
    )
    assert rows[(segment, "current")]["sales"] is None


def test_period_mismatch_rejects_revenue2_candidates():
    soup = _document()
    rows = _extract_ixbrl_segment_data(
        soup, "JGAAP", "3Q", _parse_context_periods(soup), datetime.date(2026, 9, 30)
    )
    assert rows == {}
