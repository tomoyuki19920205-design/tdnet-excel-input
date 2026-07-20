from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed


def _write_short_year_zip(
    path: Path,
    *,
    title: str,
    fiscal_year_end: str,
    current_period_ends: tuple[str, ...],
) -> None:
    dei = "".join(
        f'<ix:nonNumeric name="jpdei_cor:CurrentPeriodEndDateDEI">{value}</ix:nonNumeric>'
        for value in current_period_ends
    )
    main = f"""
    <html><body>
      <ix:nonNumeric name="jpcrp_cor:DocumentTitle">{title}</ix:nonNumeric>
      <ix:nonNumeric name="jpdei_cor:CurrentFiscalYearEndDateDEI">{fiscal_year_end}</ix:nonNumeric>
      {dei}
      <xbrli:context id="CurrentYearYTD_Duration">
        <xbrli:period>
          <xbrli:startDate>2024-04-01</xbrli:startDate>
          <xbrli:endDate>{current_period_ends[0]}</xbrli:endDate>
        </xbrli:period>
      </xbrli:context>
    </body></html>
    """
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("XBRLData/Summary/main-ixbrl.htm", main)
        archive.writestr("XBRLData/Attachment/0103010-qnsg02-ixbrl.htm", "<html><body></body></html>")


@pytest.mark.parametrize(
    ("title", "quarter", "current_period_end"),
    [
        ("2024年12月期 第1四半期決算短信", "1Q", "2024-06-30"),
        ("2024年12月期 第2四半期決算短信", "2Q", "2024-09-30"),
    ],
)
def test_short_first_fiscal_year_uses_consensus_dei_period_end(
    tmp_path: Path,
    title: str,
    quarter: str,
    current_period_end: str,
) -> None:
    archive = tmp_path / f"175a-{quarter}.zip"
    _write_short_year_zip(
        archive,
        title=title,
        fiscal_year_end="2024-12-31",
        current_period_ends=(current_period_end,),
    )

    result = extract_segments_from_xbrl_zip_detailed(
        str(archive), period="2024-12-31", quarter=quarter
    )

    assert result.status == "success_empty"
    assert result.reason is None
    assert result.date_guard_status == "PASS"
    assert result.candidate_file_count == 1
    assert result.parsed_file_count == 1
    assert result.segments == []


def test_conflicting_dei_current_period_ends_fail_closed(tmp_path: Path) -> None:
    archive = tmp_path / "175a-conflict.zip"
    _write_short_year_zip(
        archive,
        title="2024年12月期 第1四半期決算短信",
        fiscal_year_end="2024-12-31",
        current_period_ends=("2024-06-30", "2024-07-31"),
    )

    result = extract_segments_from_xbrl_zip_detailed(
        str(archive), period="2024-12-31", quarter="1Q"
    )

    assert result.status == "context_unresolved"
    assert result.reason == "dei_period_date_conflict"
    assert result.segments == []


def test_dei_fiscal_year_mismatch_does_not_override_date_guard(tmp_path: Path) -> None:
    archive = tmp_path / "175a-fiscal-mismatch.zip"
    _write_short_year_zip(
        archive,
        title="2024年12月期 第1四半期決算短信",
        fiscal_year_end="2025-12-31",
        current_period_ends=("2024-06-30",),
    )

    result = extract_segments_from_xbrl_zip_detailed(
        str(archive), period="2024-12-31", quarter="1Q"
    )

    assert result.status == "date_guard_skip"
    assert result.reason == "all_candidate_files_skipped_by_date_guard"
    assert result.segments == []
