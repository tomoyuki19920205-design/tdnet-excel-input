"""Non-consolidated earnings notices use their own actual cumulative facts."""
from __future__ import annotations

from zipfile import ZipFile

import pytest

from src.events.summary_financials import EarningsSummaryData, extract_earnings_data
from src.xbrl_context_scope import is_actual_reporting_duration_context


def _summary() -> bytes:
    contexts = "".join(
        f"""
        <xbrli:context id="{ref}">
          <xbrli:entity><xbrli:identifier scheme="test">75120</xbrli:identifier>
            <xbrli:segment>
              <xbrldi:explicitMember dimension="tse:ConsolidatedNonconsolidatedAxis">tse:NonConsolidatedMember</xbrldi:explicitMember>
              <xbrldi:explicitMember dimension="tse:ResultForecastAxis">tse:{kind}Member</xbrldi:explicitMember>
            </xbrli:segment>
          </xbrli:entity>
          <xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period>
        </xbrli:context>"""
        for ref, start, end, kind in (
            ("CurrentAccumulatedQ2Duration_NonConsolidatedMember_ResultMember", "2026-03-01", "2026-08-31", "Result"),
            ("PriorAccumulatedQ2Duration_NonConsolidatedMember_ResultMember", "2025-03-01", "2025-08-31", "Result"),
            ("CurrentYearDuration_NonConsolidatedMember_ForecastMember", "2026-03-01", "2027-02-28", "Forecast"),
        )
    )
    facts = "".join(
        f'<ix:nonFraction name="tse:{concept}" contextRef="{ref}" unitRef="JPY" scale="6">{value}</ix:nonFraction>'
        for ref, concept, value in (
            ("CurrentAccumulatedQ2Duration_NonConsolidatedMember_ResultMember", "NetSales", "188,263"),
            ("PriorAccumulatedQ2Duration_NonConsolidatedMember_ResultMember", "NetSales", "185,947"),
            ("CurrentAccumulatedQ2Duration_NonConsolidatedMember_ResultMember", "OperatingIncome", "1,517"),
            ("PriorAccumulatedQ2Duration_NonConsolidatedMember_ResultMember", "OperatingIncome", "2,463"),
            ("CurrentYearDuration_NonConsolidatedMember_ForecastMember", "OperatingIncome", "8,700"),
        )
    )
    return f"""<html xmlns:ix="http://www.xbrl.org/2008/inlineXBRL"
        xmlns:xbrli="http://www.xbrl.org/2003/instance"
        xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
        xmlns:tse="http://www.tse.or.jp/taxonomy/ed/t/2025-11-01/tse-ed-t">
        {contexts}{facts}</html>""".encode()


def test_7512_cumulative_actuals_recover_operating_yoy(tmp_path):
    xbrl_zip = tmp_path / "xbrl.zip"
    with ZipFile(xbrl_zip, "w") as archive:
        archive.writestr("XBRLData/Summary/75120-ixbrl.htm", _summary())
    title = "2027年2月期 第2四半期（中間期）決算短信〔日本基準〕（非連結）"
    actual = extract_earnings_data(xbrl_path=str(xbrl_zip), title=title, ticker="7512")
    assert actual is not None
    assert (actual.sales_current, actual.sales_prior) == (188_263_000_000, 185_947_000_000)
    assert (actual.op_current, actual.op_prior) == (1_517_000_000, 2_463_000_000)
    assert actual.sales_yoy == pytest.approx(0.012455161954750604)
    assert actual.op_yoy == pytest.approx(-0.3840844498578969)
    assert actual.format_summary_line().count("営業利益") == 1

    # A consolidated filing must never borrow this standalone operating fact.
    consolidated = extract_earnings_data(
        xbrl_path=str(xbrl_zip),
        title="2027年2月期 第2四半期決算短信〔日本基準〕（連結）",
        ticker="7512",
    )
    assert consolidated is not None and consolidated.op_current is None
    assert consolidated.op_yoy is None


@pytest.mark.parametrize(
    ("current", "prior", "expected"),
    [(100, 100, 0.0), (0, 100, -1.0), (-20, 100, -1.2), (100, 0, None), (100, None, None)],
)
def test_cumulative_yoy_keeps_zero_loss_and_missing_prior(current, prior, expected):
    row = EarningsSummaryData(
        op_current=current, op_prior=prior,
        op_q_current=200, op_q_prior=100,
    )
    assert row.op_yoy == expected


def test_quarter_only_values_are_used_only_without_cumulative_facts():
    row = EarningsSummaryData(op_q_current=110, op_q_prior=100)
    assert row.op_yoy == pytest.approx(0.1)
    row = EarningsSummaryData(sales_current=188_263, sales_prior=185_947,
                              sales_q_current=100, sales_q_prior=50)
    assert row.sales_yoy == pytest.approx(188_263 / 185_947 - 1)


def test_nonconsolidated_scope_excludes_forecast_segment_and_opposite_scope():
    contexts = {
        "valid": {"start": "2026-03-01", "end": "2026-08-31", "instant": "", "members": ["NonConsolidatedMember", "ResultMember"]},
        "forecast": {"start": "2026-03-01", "end": "2027-02-28", "instant": "", "members": ["NonConsolidatedMember", "ForecastMember"]},
        "segment": {"start": "2026-03-01", "end": "2026-08-31", "instant": "", "members": ["NonConsolidatedMember", "RetailSegmentMember"]},
        "consolidated": {"start": "2026-03-01", "end": "2026-08-31", "instant": "", "members": ["ConsolidatedMember", "ResultMember"]},
    }
    for ref in ("valid", "forecast", "segment", "consolidated"):
        assert is_actual_reporting_duration_context(ref, contexts, reporting_scope="nonconsolidated") is (ref == "valid")
