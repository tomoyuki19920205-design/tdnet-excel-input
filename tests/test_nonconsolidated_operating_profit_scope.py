"""Regression contract for consolidated operating-profit XBRL scope."""
from __future__ import annotations

import pytest

from src.events.summary_financials import _parse_xbrl_multi_period
from src.extractor import _parse_xbrl_content


def _document(
    op_concept: str,
    op_value: int,
    context_id: str,
    member: str,
    *,
    op_prefix: str = "tse-ed-t",
) -> bytes:
    return f"""
    <html xmlns:ix="http://www.xbrl.org/2008/inlineXBRL"
          xmlns:xbrli="http://www.xbrl.org/2003/instance"
          xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
          xmlns:tse-ed-t="http://www.tse.or.jp/taxonomy/ed/t/2025-11-01/tse-ed-t"
          xmlns:jppfs_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jppfs/2025-11-01/jppfs_cor"
          xmlns:jpigp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpigp/2025-11-01/jpigp_cor">
      <xbrli:context id="{context_id}">
        <xbrli:entity><xbrli:identifier scheme="test">TEST</xbrli:identifier>
          <xbrli:segment>
            <xbrldi:explicitMember dimension="tse-ed-t:ConsolidatedOrNonConsolidatedAxis">tse-ed-t:{member}</xbrldi:explicitMember>
            <xbrldi:explicitMember dimension="tse-ed-t:ResultOrForecastAxis">tse-ed-t:ResultMember</xbrldi:explicitMember>
          </xbrli:segment>
        </xbrli:entity>
        <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
      </xbrli:context>
      <ix:nonFraction name="tse-ed-t:NetSales" contextRef="{context_id}" unitRef="JPY" scale="6">100</ix:nonFraction>
      <ix:nonFraction name="{op_prefix}:{op_concept}" contextRef="{context_id}" unitRef="JPY" scale="6">{abs(op_value)}</ix:nonFraction>
    </html>
    """.encode()


@pytest.mark.parametrize(
    ("ticker", "concept", "value", "prefix", "context_id"),
    [
        ("8053", "OperatingIncome", -36_812, "tse-ed-t", "CurrentYearDuration_NonConsolidatedMember_ResultMember"),
        ("8253", "OperatingIncome", 55_536, "jppfs_cor", "CurrentYearDuration_NonConsolidatedMember"),
        ("9984", "OperatingIncome", 1_952_956, "tse-ed-t", "CurrentYearDuration_NonConsolidatedMember_ResultMember"),
    ],
)
def test_nonconsolidated_operating_income_never_becomes_consolidated_op(
    ticker: str,
    concept: str,
    value: int,
    prefix: str,
    context_id: str,
):
    raw = _document(
        concept,
        value,
        context_id,
        "NonConsolidatedMember",
        op_prefix=prefix,
    )
    assert _parse_xbrl_multi_period(raw)["current_ytd"].operating_profit is None
    assert _parse_xbrl_content(raw).operating_profit is None


def test_dimension_member_wins_over_misleading_consolidated_context_id():
    raw = _document(
        "OperatingIncome",
        999,
        "CurrentYearDuration_ConsolidatedMember_ResultMember",
        "NonConsolidatedMember",
    )
    assert _parse_xbrl_multi_period(raw)["current_ytd"].operating_profit is None
    assert _parse_xbrl_content(raw).operating_profit is None


def test_7741_valid_consolidated_operating_profit_is_preserved():
    raw = _document(
        "OperatingProfitLossIFRS",
        82_626,
        "CurrentAccumulatedQ1Duration_ConsolidatedMember_ResultMember",
        "ConsolidatedMember",
        op_prefix="jpigp_cor",
    )
    assert _parse_xbrl_multi_period(raw)["current_ytd"].operating_profit == 82_626_000_000
    assert _parse_xbrl_content(raw).operating_profit == 82_626_000_000
