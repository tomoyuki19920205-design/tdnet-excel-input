"""Bank OP extraction is industry-scoped and provenance-bearing."""

from src.extractor import _parse_xbrl_content


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


def test_7350_bank_ordinary_profit_is_operating_profit_proxy():
    result = _parse_xbrl_content(_ixbrl(bank=True), source_label="summary_xbrl")
    assert result is not None
    assert result.operating_profit == 15_799_000_000
    assert result.field_sources["operating_profit"] == (
        "summary_xbrl|bank_ordinary_profit_proxy|OrdinaryIncome"
    )


def test_general_company_never_uses_ordinary_profit_as_op():
    result = _parse_xbrl_content(_ixbrl(bank=False), source_label="summary_xbrl")
    assert result is not None
    assert result.ordinary_profit == 15_799_000_000
    assert result.operating_profit is None
