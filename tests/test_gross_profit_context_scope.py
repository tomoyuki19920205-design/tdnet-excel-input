"""Company gross profit must never be selected from segment contexts."""
from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from src.events.summary_financials import extract_earnings_data
from src.extractor import _parse_xbrl_content
from tools.repair_missing_gross_profit_4tickers import _action_for


def _fixture() -> str:
    return """<html
      xmlns:ix="http://www.xbrl.org/2008/inlineXBRL"
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
      xmlns:jppfs_cor="urn:jppfs"
      xmlns:company="urn:company">
      <xbrli:context id="CurrentYTDDuration_company:ArchitectureMember">
        <xbrli:entity><xbrli:identifier scheme="urn:test">1892</xbrli:identifier>
          <xbrli:segment><xbrldi:explicitMember dimension="company:SegmentAxis">company:ArchitectureMember</xbrldi:explicitMember></xbrli:segment>
        </xbrli:entity>
        <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2025-06-30</xbrli:endDate></xbrli:period>
      </xbrli:context>
      <xbrli:context id="CurrentYTDDuration">
        <xbrli:entity><xbrli:identifier scheme="urn:test">1892</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2025-06-30</xbrli:endDate></xbrli:period>
      </xbrli:context>
      <ix:nonFraction name="jppfs_cor:GrossProfit" contextRef="CurrentYTDDuration_company:ArchitectureMember" unitRef="JPY" scale="6">791</ix:nonFraction>
      <ix:nonFraction name="jppfs_cor:NetSales" contextRef="CurrentYTDDuration" unitRef="JPY" scale="6">13,976</ix:nonFraction>
      <ix:nonFraction name="jppfs_cor:GrossProfit" contextRef="CurrentYTDDuration" unitRef="JPY" scale="6">1,417</ix:nonFraction>
      <ix:nonFraction name="jppfs_cor:OperatingIncome" contextRef="CurrentYTDDuration" unitRef="JPY" scale="6">314</ix:nonFraction>
    </html>"""


def test_summary_extractor_rejects_segment_gp_before_company_total() -> None:
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
        path = Path(handle.name)
        with zipfile.ZipFile(handle, "w") as archive:
            archive.writestr("XBRLData/Attachment/0102010-qcpl11-ixbrl.htm", _fixture())
    try:
        result = extract_earnings_data(
            xbrl_path=str(path),
            title="2026年3月期 第1四半期決算短信〔日本基準〕（連結）",
        )
    finally:
        path.unlink(missing_ok=True)

    assert result is not None
    assert result.gross_profit_current == 1_417_000_000


def test_common_extractor_rejects_segment_gp_before_company_total() -> None:
    result = _parse_xbrl_content(_fixture().encode("utf-8"))

    assert result is not None
    assert result.gross_profit == 1_417_000_000


def test_repair_action_never_derives_missing_gp() -> None:
    assert _action_for(
        official=None,
        details_gp=None,
        existing=[],
        disclosure_id="official-id",
    ) == "NO_ACTION_NO_VALID_GP"


def test_repair_action_is_idempotent_for_exact_official_row() -> None:
    official = {"normalized_value_millions_jpy": 1417}
    existing = [{
        "source": "tdnet_xbrl",
        "filing_id": "official-id",
        "value": 1417,
    }]
    assert _action_for(
        official=official,
        details_gp=999_999_999,
        existing=existing,
        disclosure_id="official-id",
    ) == "NO_ACTION_OFFICIAL_XBRL_EXISTS"
