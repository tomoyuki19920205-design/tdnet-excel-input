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
    _extract_segment_member,
    _parse_context_periods,
)


def _context(context_id: str, member: str, *, end: str = "2025-08-31") -> str:
    return f"""
    <xbrli:context id="{context_id}">
      <xbrli:entity><xbrli:identifier>21640</xbrli:identifier></xbrli:entity>
      <xbrli:period><xbrli:startDate>2024-09-01</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period>
      <xbrli:scenario>
        <xbrldi:explicitMember dimension="jppfs_cor:ConsolidatedOrNonConsolidatedAxis">jppfs_cor:NonConsolidatedMember</xbrldi:explicitMember>
        <xbrldi:explicitMember dimension="jpcrp_cor:OperatingSegmentsAxis">{member}</xbrldi:explicitMember>
      </xbrli:scenario>
    </xbrli:context>
    """


def _fact(name: str, context_id: str, value: int) -> str:
    return f'<ix:nonFraction name="{name}" contextRef="{context_id}" unitRef="JPY">{value}</ix:nonFraction>'


def _document(*, include_real_estate: bool = True, total: int = 2_977_185) -> BeautifulSoup:
    advertising = "CurrentYearDuration_NonConsolidatedMember_tse-anedjpfr-21640AdvertisingBusiness"
    real_estate = "CurrentYearDuration_NonConsolidatedMember_tse-anedjpfr-21640RealEstateBusiness"
    total_context = "CurrentYearDuration_NonConsolidatedMember_ReportableSegmentsMember"
    other = "CurrentYearDuration_NonConsolidatedMember_OperatingSegmentsNotIncludedInReportableSegmentsAndOtherRevenueGeneratingBusinessActivitiesMember"
    parts = [
        _context(advertising, "tse-anedjpfr-21640:AdvertisingBusiness"),
        _context(total_context, "jpcrp_cor:ReportableSegmentsMember"),
        _context(other, "jpcrp_cor:OperatingSegmentsNotIncludedInReportableSegmentsAndOtherRevenueGeneratingBusinessActivitiesMember"),
        _fact("jppfs_cor:NetSales", advertising, 2_965_779),
        _fact("jppfs_cor:OperatingIncome", advertising, 481_505),
        _fact("jppfs_cor:NetSales", total_context, total),
    ]
    if include_real_estate:
        parts.extend(
            [
                _context(real_estate, "tse-anedjpfr-21640:RealEstateBusiness"),
                _fact("jppfs_cor:NetSales", real_estate, 11_405),
                _fact("jppfs_cor:OperatingIncome", real_estate, 5_723),
            ]
        )
    return BeautifulSoup("<html>" + "".join(parts) + "</html>", "html.parser")


def _records(rows):
    return [
        {
            "segment_name": name,
            "segment_sales": data.get("sales"),
            "segment_profit": data.get("profit"),
            "_segment_member_kind": data["segment_member_kind"],
        }
        for (name, _role), data in rows.items()
    ]


@pytest.mark.parametrize(
    "context_id,member,expected",
    [
        ("CurrentYearDuration_tse-anedjpfr-21640AdvertisingBusiness", "tse-anedjpfr-21640:AdvertisingBusiness", "AdvertisingBusiness"),
        ("CurrentYTDDuration_tse-qnedjpfr-21640RealEstateBusiness", "tse-qnedjpfr-21640:RealEstateBusiness", "RealEstateBusiness"),
    ],
)
def test_2164_company_members_are_resolved_from_explicit_dimension(context_id, member, expected):
    soup = BeautifulSoup(_context(context_id, member), "html.parser")
    info = _parse_context_periods(soup)[context_id]
    assert _extract_segment_member(context_id, info) == expected


def test_context_parser_preserves_dimension_and_member_qnames():
    context_id = "CurrentYearDuration_tse-anedjpfr-21640AdvertisingBusiness"
    info = _parse_context_periods(
        BeautifulSoup(_context(context_id, "tse-anedjpfr-21640:AdvertisingBusiness"), "html.parser")
    )[context_id]
    assert info["explicit_members"][-1] == {
        "dimension": "jpcrp_cor:OperatingSegmentsAxis",
        "member": "tse-anedjpfr-21640:AdvertisingBusiness",
    }


@pytest.mark.parametrize(
    "member",
    [
        "jpcrp_cor:ReportableSegmentsMember",
        "jpcrp_cor:ReconcilingItemsMember",
        "jpcrp_cor:OperatingSegmentsNotIncludedInReportableSegmentsAndOtherRevenueGeneratingBusinessActivitiesMember",
    ],
)
def test_structural_operating_axis_members_are_not_reportable_rows(member):
    context_id = "CurrentYearDuration_CustomAggregate"
    info = _parse_context_periods(BeautifulSoup(_context(context_id, member), "html.parser"))[context_id]
    assert _extract_segment_member(context_id, info) is None


def test_2164_two_reportable_members_are_extracted():
    soup = _document()
    contexts = _parse_context_periods(soup)
    rows = _extract_ixbrl_segment_data(
        soup, "JP", "FY", contexts, datetime.date(2025, 8, 31)
    )
    assert {(name, role) for name, role in rows if rows[(name, role)]["segment_member_kind"] == "reportable"} == {
        ("AdvertisingBusiness", "current"),
        ("RealEstateBusiness", "current"),
    }


def test_recovered_reportable_sales_reconcile_to_formal_total():
    soup = _document()
    rows = _extract_ixbrl_segment_data(soup, "JP", "FY", _parse_context_periods(soup), datetime.date(2025, 8, 31))
    reportable = [data for data in rows.values() if data["segment_member_kind"] == "reportable"]
    assert sum(row["sales"] for row in reportable) == 2_977_184
    assert {row["selected_reportable_sales_total"] for row in reportable} == {2_977_185}


def test_recovered_rows_clear_too_few_valid_segments():
    soup = _document()
    rows = _extract_ixbrl_segment_data(soup, "JP", "FY", _parse_context_periods(soup), datetime.date(2025, 8, 31))
    result = validate_extraction_result(_records(rows), source="xbrl")
    assert result.status is ExtractionStatus.SUCCESS
    assert result.hard_fail_reason is HardFailReason.NONE


def test_missing_second_member_remains_quarantined():
    soup = _document(include_real_estate=False)
    rows = _extract_ixbrl_segment_data(soup, "JP", "FY", _parse_context_periods(soup), datetime.date(2025, 8, 31))
    result = validate_extraction_result(_records(rows), source="xbrl")
    assert result.status is ExtractionStatus.QUARANTINE
    assert result.hard_fail_reason is HardFailReason.TOO_FEW_VALID_SEGMENTS


def test_reconciliation_mismatch_does_not_invent_a_member():
    soup = _document(include_real_estate=False, total=9_999_999)
    rows = _extract_ixbrl_segment_data(soup, "JP", "FY", _parse_context_periods(soup), datetime.date(2025, 8, 31))
    assert len([data for data in rows.values() if data["segment_member_kind"] == "reportable"]) == 1


def test_non_operating_axis_member_is_not_a_segment():
    context_id = "CurrentYearDuration_Custom"
    soup = BeautifulSoup(
        _context(context_id, "tse-anedjpfr-21640:AdvertisingBusiness").replace(
            "jpcrp_cor:OperatingSegmentsAxis", "jpcrp_cor:GeographicalAreasAxis"
        ),
        "html.parser",
    )
    info = _parse_context_periods(soup)[context_id]
    assert _extract_segment_member(context_id, info) is None


def test_period_mismatch_drops_candidates():
    soup = _document()
    rows = _extract_ixbrl_segment_data(soup, "JP", "FY", _parse_context_periods(soup), datetime.date(2026, 8, 31))
    assert rows == {}


def test_plain_context_without_formal_member_remains_unresolved():
    assert _extract_segment_member("CurrentYearDuration_AdvertisingBusiness", {}) is None
