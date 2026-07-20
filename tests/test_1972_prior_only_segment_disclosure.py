from __future__ import annotations

import zipfile
from pathlib import Path

from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed


CURRENT_CONTEXT = "CurrentYTDDuration_NonConsolidatedMember"
PRIOR_CONTEXT = (
    "Prior1YTDDuration_NonConsolidatedMember_"
    "tse-qnedjpfr-99990RoofReportableSegmentsMember"
)


def _write_zip(
    path: Path,
    *,
    include_current_narrative: bool = True,
    current_numeric_context: str | None = None,
    current_numeric_name: str = "jppfs_cor:NetSales",
) -> None:
    contexts = f"""
    <xbrli:context id="{CURRENT_CONTEXT}"><xbrli:period>
      <xbrli:startDate>2024-04-01</xbrli:startDate><xbrli:endDate>2024-06-30</xbrli:endDate>
    </xbrli:period></xbrli:context>
    <xbrli:context id="{PRIOR_CONTEXT}"><xbrli:period>
      <xbrli:startDate>2023-04-01</xbrli:startDate><xbrli:endDate>2023-06-30</xbrli:endDate>
    </xbrli:period></xbrli:context>
    """
    summary = f"""<html><body>
      <ix:nonNumeric name="jpcrp_cor:DocumentTitle">2025年3月期 第1四半期決算短信</ix:nonNumeric>
      {contexts}
    </body></html>"""
    narrative = ""
    if include_current_narrative:
        narrative = f"""
        <ix:nonNumeric name="jpcrp_cor:NotesSegmentInformationEtcQuarterlyFinancialStatementsTextBlock"
          contextRef="{CURRENT_CONTEXT}">当第1四半期の定量セグメント情報は省略する。</ix:nonNumeric>
        """
    current_numeric = ""
    if current_numeric_context:
        current_numeric = f"""
        <ix:nonfraction name="{current_numeric_name}" contextRef="{current_numeric_context}">100</ix:nonfraction>
        """
    segment = f"""<html><body>
      {narrative}
      <ix:nonfraction name="jppfs_cor:NetSales" contextRef="{PRIOR_CONTEXT}">80</ix:nonfraction>
      <ix:nonfraction name="jppfs_cor:OperatingIncome" contextRef="{PRIOR_CONTEXT}">8</ix:nonfraction>
      {current_numeric}
    </body></html>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("XBRLData/Summary/main-ixbrl.htm", summary)
        archive.writestr("XBRLData/Attachment/1400000-qnsg02-ixbrl.htm", segment)


def _extract(path: Path):
    return extract_segments_from_xbrl_zip_detailed(
        str(path), period="2025-03-31", quarter="1Q"
    )


def test_cross_document_prior_only_segment_table_is_verified_empty(tmp_path: Path) -> None:
    archive = tmp_path / "prior-only.zip"
    _write_zip(archive)

    result = _extract(archive)

    assert result.status == "success_empty"
    assert result.reason is None
    assert result.date_guard_status == "PASS"
    assert result.segments == []


def test_prior_only_without_current_narrative_remains_fail_closed(tmp_path: Path) -> None:
    archive = tmp_path / "no-current-narrative.zip"
    _write_zip(archive, include_current_narrative=False)

    result = _extract(archive)

    assert result.status == "date_guard_skip"
    assert result.reason == "all_candidate_files_skipped_by_date_guard"


def test_current_supported_metric_prevents_empty_normalization(tmp_path: Path) -> None:
    archive = tmp_path / "current-supported.zip"
    current_context = (
        "CurrentYTDDuration_NonConsolidatedMember_"
        "tse-qnedjpfr-99990RoofReportableSegmentsMember"
    )
    _write_zip(archive, current_numeric_context=current_context)

    assert _extract(archive).status != "success_empty"


def test_current_unknown_segment_metric_prevents_empty_normalization(tmp_path: Path) -> None:
    archive = tmp_path / "current-unknown.zip"
    current_context = (
        "CurrentYTDDuration_NonConsolidatedMember_"
        "tse-qnedjpfr-99990RoofReportableSegmentsMember"
    )
    _write_zip(
        archive,
        current_numeric_context=current_context,
        current_numeric_name="example:UnmappedSegmentMeasure",
    )

    assert _extract(archive).status != "success_empty"


def test_previous_only_rows_are_never_emitted_as_current(tmp_path: Path) -> None:
    archive = tmp_path / "prior-only.zip"
    _write_zip(archive)

    assert _extract(archive).segments == []
