from __future__ import annotations

from bs4 import BeautifulSoup
import pytest

from src.segment.extraction_result_validator import (
    ExtractionStatus,
    HardFailReason,
    validate_extraction_result,
)
from src.segment.xbrl_segment_extractor import _extract_ixbrl_segment_data


def _records(*, reconciled: bool = True, second_sales=None, role: str = "current"):
    common = {
        "period": "2025-12-31",
        "quarter": "FY",
        "_segment_period_role": role,
        "_segment_member_kind": "reportable",
        "_reportable_sales_total_raw": 2_132_680,
        "_consolidated_sales_raw": 2_132_680,
        "_sales_reconciliation_verified": reconciled,
    }
    return [
        {
            **common,
            "segment_name": "DX Solution",
            "segment_sales": 2_132,
            "segment_profit": 178,
            "_sales_fact_explicit_nil": False,
            "_sales_fact_names": ["jpcrp_cor:revenuesfromexternalcustomers"],
        },
        {
            **common,
            "segment_name": "Investment Business",
            "segment_sales": second_sales,
            "segment_profit": -10,
            "_sales_fact_explicit_nil": second_sales is None,
            "_sales_fact_names": ["jpcrp_cor:revenuesfromexternalcustomers"],
        },
    ]


@pytest.mark.parametrize("quarter", ["2Q", "3Q", "FY"])
def test_145a_explicit_nil_sales_is_verified_partial(quarter):
    records = _records()
    for record in records:
        record["quarter"] = quarter
    result = validate_extraction_result(records, source="xbrl")
    assert result.status is ExtractionStatus.PARTIAL
    assert result.hard_fail_reason is HardFailReason.NONE
    assert result.sales_non_null_count == 1
    assert "explicitly undisclosed" in result.reason


@pytest.mark.parametrize(
    "mutation",
    [
        {"_sales_fact_explicit_nil": False},
        {"_sales_fact_names": []},
        {"_sales_reconciliation_verified": False},
        {"_segment_member_kind": "adjustment"},
        {"_segment_period_role": "previous"},
        {"quarter": "3Q"},
    ],
)
def test_undisclosed_sales_contract_fails_closed(mutation):
    records = _records()
    records[1].update(mutation)
    result = validate_extraction_result(records, source="xbrl")
    assert result.status is ExtractionStatus.QUARANTINE
    assert result.hard_fail_reason is HardFailReason.TOO_FEW_SALES


def test_true_second_sales_remains_normal_success():
    records = _records(second_sales=50)
    records[1]["_sales_fact_explicit_nil"] = False
    result = validate_extraction_result(records, source="xbrl")
    assert result.status is ExtractionStatus.SUCCESS


def test_plain_missing_sales_without_source_evidence_stays_quarantined():
    records = [
        {"segment_name": "Alpha Business", "segment_sales": 100, "segment_profit": 10},
        {"segment_name": "Beta Business", "segment_sales": None, "segment_profit": 5},
    ]
    result = validate_extraction_result(records, source="xbrl")
    assert result.status is ExtractionStatus.QUARANTINE
    assert result.hard_fail_reason is HardFailReason.TOO_FEW_SALES


def test_parser_retains_nil_and_reconciliation_evidence_without_total_rows():
    html = """
    <html><body>
      <ix:nonfraction name="jpcrp_cor:revenuesfromexternalcustomers"
        contextref="CurrentYearDuration_tse-acedjpfr-145A0DXSolutionReportableSegmentMember">2132680</ix:nonfraction>
      <ix:nonfraction name="jppfs_cor:operatingincome"
        contextref="CurrentYearDuration_tse-acedjpfr-145A0DXSolutionReportableSegmentMember">178000</ix:nonfraction>
      <ix:nonfraction name="jpcrp_cor:revenuesfromexternalcustomers" xsi:nil="true"
        contextref="CurrentYearDuration_tse-acedjpfr-145A0InvestmentBusinessReportableSegmentMember"></ix:nonfraction>
      <ix:nonfraction name="jppfs_cor:operatingincome" sign="-"
        contextref="CurrentYearDuration_tse-acedjpfr-145A0InvestmentBusinessReportableSegmentMember">10000</ix:nonfraction>
      <ix:nonfraction name="jpcrp_cor:revenuesfromexternalcustomers"
        contextref="CurrentYearDuration_ReportableSegmentsMember">2132680</ix:nonfraction>
      <ix:nonfraction name="jpcrp_cor:revenuesfromexternalcustomers"
        contextref="CurrentYearDuration">2132680</ix:nonfraction>
      <ix:nonfraction name="jpcrp_cor:revenuesfromexternalcustomers" xsi:nil="true"
        contextref="CurrentYearDuration_ReconcilingItemsMember"></ix:nonfraction>
    </body></html>
    """
    contexts = {
        key: {"type": "duration", "start": "2025-01-01", "end": "2025-12-31", "duration_days": 364}
        for key in (
            "CurrentYearDuration_tse-acedjpfr-145A0DXSolutionReportableSegmentMember",
            "CurrentYearDuration_tse-acedjpfr-145A0InvestmentBusinessReportableSegmentMember",
            "CurrentYearDuration_ReportableSegmentsMember",
            "CurrentYearDuration",
            "CurrentYearDuration_ReconcilingItemsMember",
        )
    }
    rows = _extract_ixbrl_segment_data(
        BeautifulSoup(html, "html.parser"), "JGAAP", "FY", contexts
    )
    assert set(rows) == {
        ("A0DXSolution", "current"),
        ("A0InvestmentBusiness", "current"),
    }
    missing = rows[("A0InvestmentBusiness", "current")]
    assert missing["sales"] is None
    assert missing["sales_fact_explicit_nil"] is True
    assert missing["segment_member_kind"] == "reportable"
    assert missing["sales_reconciliation_verified"] is True


def test_parser_reconciliation_mismatch_is_not_verified():
    html = """
    <html><body>
      <ix:nonfraction name="jppfs_cor:netsales" contextref="CurrentYearDuration_tse-x-145A0AlphaReportableSegmentMember">100</ix:nonfraction>
      <ix:nonfraction name="jppfs_cor:netsales" xsi:nil="true" contextref="CurrentYearDuration_tse-x-145A0BetaReportableSegmentMember"></ix:nonfraction>
      <ix:nonfraction name="jppfs_cor:netsales" contextref="CurrentYearDuration_ReportableSegmentsMember">101</ix:nonfraction>
      <ix:nonfraction name="jppfs_cor:netsales" contextref="CurrentYearDuration">101</ix:nonfraction>
    </body></html>
    """
    contexts = {
        key: {"type": "duration", "start": "2025-01-01", "end": "2025-12-31", "duration_days": 364}
        for key in (
            "CurrentYearDuration_tse-x-145A0AlphaReportableSegmentMember",
            "CurrentYearDuration_tse-x-145A0BetaReportableSegmentMember",
            "CurrentYearDuration_ReportableSegmentsMember",
            "CurrentYearDuration",
        )
    }
    rows = _extract_ixbrl_segment_data(
        BeautifulSoup(html, "html.parser"), "JGAAP", "FY", contexts
    )
    assert rows
    assert all(not row["sales_reconciliation_verified"] for row in rows.values())
