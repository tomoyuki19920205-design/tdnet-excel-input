"""Exact PL dates must cooperate with main's completed-actual safeguards."""
import pytest

from lib.pipeline.financial_integrity import stored_quarter_is_completed_actual


@pytest.mark.parametrize("ticker,code", [("2760", "27600"), ("276A", "276A0"),
                                        ("27600", "27600"), ("276A0", "276A0")])
def test_jquants_request_preserves_numeric_and_alpha_issuers(ticker, code):
    from tools.fetch_jquants_financials import _to_local_code
    assert _to_local_code(ticker) == code


@pytest.mark.parametrize("cutoff", [15, 20])
@pytest.mark.parametrize("quarter,month", [("FY", 8), ("4Q", 8), ("2Q", 2)])
def test_exact_cutoff_actual_is_eligible_before_month_end(cutoff, quarter, month):
    period = f"2025-08-{cutoff}"
    assert stored_quarter_is_completed_actual(period, quarter, f"2025-{month:02}-{cutoff+1}")
    assert not stored_quarter_is_completed_actual(period, quarter, f"2025-{month:02}-{cutoff-1}")


@pytest.mark.parametrize("period", ["2025-08", "2025-08-31"])
def test_month_end_contract_remains_unchanged(period):
    assert not stored_quarter_is_completed_actual(period, "FY", "2025-08-30")
    assert stored_quarter_is_completed_actual(period, "FY", "2025-08-31")
    assert not stored_quarter_is_completed_actual(period, "2Q", "2025-02-27")
    assert stored_quarter_is_completed_actual(period, "2Q", "2025-02-28")


def test_future_evidence_is_not_promoted_to_actual():
    assert not stored_quarter_is_completed_actual("2099-08-20", "FY", "2099-09-01")


@pytest.mark.parametrize("period", ["2025-02", "2025-02-28", "2024-02-29"])
def test_february_month_end_retains_august_month_end(period):
    prior_year = int(period[:4]) - 1
    assert not stored_quarter_is_completed_actual(period, "2Q", f"{prior_year}-08-30")
    assert stored_quarter_is_completed_actual(period, "2Q", f"{prior_year}-08-31")


def test_classic_xbrl_uses_selected_sales_duration_and_exact_cutoff():
    from src.extractor import _parse_xbrl_content
    raw = b'''<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:t="urn:test">
      <context id="CurrentYearDuration"><entity><identifier scheme="test">9999</identifier></entity>
        <period><startDate>2025-02-21</startDate><endDate>2026-02-20</endDate></period></context>
      <t:CurrentFiscalYearStartDateDEI>2025-02-21</t:CurrentFiscalYearStartDateDEI>
      <t:CurrentFiscalYearEndDateDEI>2026-02-20</t:CurrentFiscalYearEndDateDEI>
      <t:NetSales contextRef="CurrentYearDuration">1234000000</t:NetSales>
      <t:OperatingIncome contextRef="CurrentYearDuration">25000000</t:OperatingIncome>
    </xbrl>'''
    result = _parse_xbrl_content(raw)
    assert (result.sales, result.operating_profit) == (1234000000, 25000000)
    assert (result.fiscal_year, result.quarter) == ("2026-02-20", "FY")
