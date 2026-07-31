"""IFRS PL concepts used by TDNET realtime filings."""
from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from src.events.summary_financials import extract_earnings_data


def _ixbrl(concept: str, value: int, context: str) -> str:
    return (
        f'<ix:nonFraction name="jpigp_cor:{concept}" '
        f'contextRef="{context}" unitRef="JPY" scale="6">{value}</ix:nonFraction>'
    )


def test_ifrs_net_sales_and_operating_profit_loss_are_extracted():
    """6268/6503のIFRS PL conceptをcurrent/prior期間で抽出する。"""
    html = "<html xmlns:ix=\"http://www.xbrl.org/2008/inlineXBRL\">" + "".join([
        _ixbrl("NetSalesIFRS", 1200, "CurrentYearDuration"),
        _ixbrl("OperatingProfitLossIFRS", 120, "CurrentYearDuration"),
        _ixbrl("NetSalesIFRS", 1000, "Prior1YearDuration"),
        _ixbrl("OperatingProfitLossIFRS", 100, "Prior1YearDuration"),
    ]) + "</html>"

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        path = Path(f.name)
        with zipfile.ZipFile(f, "w") as zf:
            zf.writestr("XBRLData/Attachment/0101010-qcpl13-ixbrl.htm", html)
    try:
        result = extract_earnings_data(xbrl_path=str(path), title="2027年3月期 第1四半期決算短信〔IFRS〕（連結）")
    finally:
        path.unlink(missing_ok=True)

    assert result is not None
    assert result.sales_current == 1_200_000_000
    assert result.sales_prior == 1_000_000_000
    assert result.op_current == 120_000_000
    assert result.op_prior == 100_000_000
