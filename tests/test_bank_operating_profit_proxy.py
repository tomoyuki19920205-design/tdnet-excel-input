"""Bank OP extraction is industry-scoped and provenance-bearing."""

from src.extractor import _parse_xbrl_content
from src.events.summary_financials import _parse_xbrl_multi_period
from src.events.earnings_production_pipeline import _canonical_profit_metric


def _ixbrl(*, bank: bool) -> bytes:
    bank_sales = (
        '<ix:nonFraction name="jppfs_cor:OrdinaryIncomeBNK" '
        'contextRef="CurrentYTDDuration" unitRef="JPY">70417000000</ix:nonFraction>'
        if bank else
        '<ix:nonFraction name="jppfs_cor:NetSales" '
        'contextRef="CurrentYTDDuration" unitRef="JPY">70417000000</ix:nonFraction>'
    )
    return f"""<html xmlns:ix="http://www.xbrl.org/2008/inlineXBRL"
      xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:jppfs_cor="urn:jppfs">
      <xbrli:context id="CurrentYTDDuration"><xbrli:entity>
      <xbrli:identifier scheme="urn:test">7350</xbrli:identifier></xbrli:entity>
      <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate>
      <xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
      {bank_sales}
      <ix:nonFraction name="jppfs_cor:OrdinaryIncome" contextRef="CurrentYTDDuration"
       unitRef="JPY">15799000000</ix:nonFraction></html>""".encode()


def test_7350_bank_ordinary_profit_remains_separate():
    result = _parse_xbrl_content(_ixbrl(bank=True), source_label="summary_xbrl")
    assert result is not None
    assert result.sales == 70_417_000_000
    assert result.ordinary_profit == 15_799_000_000
    assert result.operating_profit is None
    assert "operating_profit" not in result.field_sources


def test_general_company_never_uses_ordinary_profit_as_op():
    result = _parse_xbrl_content(_ixbrl(bank=False), source_label="summary_xbrl")
    assert result is not None
    assert result.ordinary_profit == 15_799_000_000
    assert result.operating_profit is None


def test_earnings_canonical_route_keeps_bank_ordinary_profit_out_of_op(tmp_path):
    bank = tmp_path / "bank.xbrl"
    bank.write_bytes(_ixbrl(bank=True))
    parsed = _parse_xbrl_multi_period(bank.read_bytes())["current_ytd"]
    assert parsed.sales_tag == "OrdinaryIncomeBNK"
    assert parsed.operating_profit_tag == "OrdinaryIncome"
    assert _canonical_profit_metric(15_799_000_000, str(bank)) == "ordinary_profit"

    general = tmp_path / "general.xbrl"
    general.write_bytes(_ixbrl(bank=False))
    assert _canonical_profit_metric(15_799_000_000, str(general)) == "operating_profit"
