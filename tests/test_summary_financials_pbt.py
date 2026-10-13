"""Official TDnet IFRS profit-before-tax extraction contract."""
from __future__ import annotations

from src.events.summary_financials import _parse_xbrl_multi_period


def _document(
    concept: str,
    value: int,
    context_id: str,
    *,
    prefix: str = "jpigp_cor",
    members: tuple[str, ...] = (),
) -> bytes:
    dimensions = "".join(
        '<xbrldi:explicitMember dimension="tse-ed-t:ConsolidatedOrNonConsolidatedAxis">'
        f"tse-ed-t:{member}</xbrldi:explicitMember>"
        for member in members
    )
    return f"""
    <html xmlns:ix="http://www.xbrl.org/2008/inlineXBRL"
          xmlns:xbrli="http://www.xbrl.org/2003/instance"
          xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
          xmlns:jpigp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpigp/2025-11-01/jpigp_cor"
          xmlns:tse-ed-t="http://www.tse.or.jp/taxonomy/ed/t/2025-11-01/tse-ed-t">
      <xbrli:context id="{context_id}">
        <xbrli:entity><xbrli:identifier scheme="test">57130</xbrli:identifier>
          <xbrli:segment>{dimensions}</xbrli:segment>
        </xbrli:entity>
        <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
      </xbrli:context>
      <ix:nonFraction name="{prefix}:{concept}" contextRef="{context_id}"
          unitRef="JPY" scale="6">{value}</ix:nonFraction>
    </html>
    """.encode()


def _pbt(raw: bytes):
    return _parse_xbrl_multi_period(raw, include_evidence=True)["current_ytd"]


def test_detailed_ifrs_profit_loss_before_tax_maps_to_canonical_pbt():
    row = _pbt(_document("ProfitLossBeforeTaxIFRS", 255_680, "CurrentYTDDuration"))
    assert row.profit_before_tax == 255_680_000_000
    evidence = next(e for e in row.evidences if e.metric == "profit_before_tax")
    assert evidence.qname == "jpigp_cor:ProfitLossBeforeTaxIFRS"
    assert evidence.namespace.endswith("/jpigp_cor")


def test_summary_profit_before_tax_maps_to_canonical_pbt():
    row = _pbt(_document(
        "ProfitBeforeTaxIFRS",
        118_040,
        "CurrentAccumulatedQ1Duration_ConsolidatedMember_ResultMember",
        prefix="tse-ed-t",
        members=("ConsolidatedMember", "ResultMember"),
    ))
    assert row.profit_before_tax == 118_040_000_000


def test_nonconsolidated_pbt_context_is_rejected():
    row = _pbt(_document(
        "ProfitBeforeTaxIFRS",
        999,
        "CurrentYearDuration_NonConsolidatedMember_ResultMember",
        prefix="tse-ed-t",
        members=("NonConsolidatedMember", "ResultMember"),
    ))
    assert row.profit_before_tax is None


def test_forecast_pbt_context_is_rejected_from_actual():
    row = _pbt(_document(
        "ProfitBeforeTaxIFRS",
        999,
        "CurrentYearDuration_ConsolidatedMember_ForecastMember",
        prefix="tse-ed-t",
        members=("ConsolidatedMember", "ForecastMember"),
    ))
    assert row.profit_before_tax is None


def test_5713_fy2026_fixture_is_255680_million_yen():
    row = _pbt(_document("ProfitLossBeforeTaxIFRS", 255_680, "CurrentYTDDuration"))
    assert row.profit_before_tax // 1_000_000 == 255_680


def test_5713_fy2027_q1_fixture_is_118040_million_yen():
    row = _pbt(_document(
        "ProfitBeforeTaxIFRS",
        118_040,
        "CurrentAccumulatedQ1Duration_ConsolidatedMember_ResultMember",
        prefix="tse-ed-t",
        members=("ConsolidatedMember", "ResultMember"),
    ))
    assert row.profit_before_tax // 1_000_000 == 118_040
