from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed


CURRENT_CONTEXT = (
    "CurrentYearDuration_"
    "tse-acedjpfr-19300EquipmentInstallationReportableSegmentMember"
)
PREVIOUS_CONTEXT = (
    "Prior1YearDuration_"
    "tse-acedjpfr-19300EquipmentInstallationReportableSegmentMember"
)


def _write_zip(path: Path, facts: list[dict[str, str]]) -> None:
    contexts = f"""
      <xbrli:context id="{CURRENT_CONTEXT}"><xbrli:period>
        <xbrli:startDate>2024-04-01</xbrli:startDate><xbrli:endDate>2025-03-31</xbrli:endDate>
      </xbrli:period></xbrli:context>
      <xbrli:context id="{PREVIOUS_CONTEXT}"><xbrli:period>
        <xbrli:startDate>2023-04-01</xbrli:startDate><xbrli:endDate>2024-03-31</xbrli:endDate>
      </xbrli:period></xbrli:context>
    """
    rendered = []
    for fact in facts:
        nil = ' xsi:nil="true"' if fact.get("nil") == "true" else ""
        rendered.append(
            f'<ix:nonfraction name="{fact["name"]}" contextref="{fact["context"]}" '
            f'unitref="JPY" decimals="0"{nil}>{fact.get("value", "")}</ix:nonfraction>'
        )
    main = f"""
    <html><body>
      <ix:nonNumeric name="jpcrp_cor:DocumentTitle">2025年3月期 決算短信</ix:nonNumeric>
      {contexts}
    </body></html>
    """
    segment = f"<html><body>{''.join(rendered)}</body></html>"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("XBRLData/Summary/main-ixbrl.htm", main)
        archive.writestr("XBRLData/Attachment/0400000-acsg01-ixbrl.htm", segment)


def _extract(path: Path):
    return extract_segments_from_xbrl_zip_detailed(
        str(path), period="2025-03-31", quarter="FY",
        allow_expected_quarter_without_title=True,
    )


def test_unsupported_current_and_previous_metrics_do_not_create_empty_rows(tmp_path: Path) -> None:
    path = tmp_path / "unsupported-only.zip"
    _write_zip(path, [
        {"name": "jppfs_cor:AmortizationOfGoodwillSGA", "context": CURRENT_CONTEXT, "value": "254"},
        {"name": "jppfs_cor:AmortizationOfGoodwillSGA", "context": PREVIOUS_CONTEXT, "value": "149"},
    ])

    result = _extract(path)

    assert result.status == "success_empty"
    assert result.segments == []
    assert result.date_guard_status == "PASS"


def test_member_definition_without_numeric_fact_is_verified_empty(tmp_path: Path) -> None:
    path = tmp_path / "member-only.zip"
    _write_zip(path, [])

    result = _extract(path)

    assert result.status == "success_empty"
    assert result.segments == []


@pytest.mark.parametrize(
    ("name", "field"),
    [
        ("jppfs_cor:NetSales", "sales"),
        ("jppfs_cor:OperatingIncome", "profit"),
    ],
)
def test_supported_non_nil_metric_is_never_normalized_to_empty(
    tmp_path: Path, name: str, field: str
) -> None:
    path = tmp_path / f"supported-{field}.zip"
    _write_zip(path, [{"name": name, "context": CURRENT_CONTEXT, "value": "100"}])

    result = _extract(path)

    assert result.status == "success_with_rows"
    assert len(result.segments) == 1
    assert getattr(result.segments[0], field) == 100


@pytest.mark.parametrize(
    "name",
    ["jppfs_cor:NetSales", "jppfs_cor:OperatingIncome"],
)
def test_supported_explicit_nil_metric_remains_a_supported_fact(
    tmp_path: Path, name: str
) -> None:
    path = tmp_path / "supported-nil.zip"
    _write_zip(path, [{"name": name, "context": CURRENT_CONTEXT, "nil": "true"}])

    result = _extract(path)

    assert result.status == "success_with_rows"
    assert len(result.segments) == 1


def test_previous_supported_metric_is_not_normalized_to_empty(tmp_path: Path) -> None:
    path = tmp_path / "previous-supported.zip"
    _write_zip(path, [
        {"name": "jppfs_cor:NetSales", "context": PREVIOUS_CONTEXT, "value": "100"}
    ])

    result = _extract(path)

    assert result.status == "date_guard_skip"
    assert result.segments == []


def test_invalid_supported_numeric_is_not_treated_as_verified_empty(tmp_path: Path) -> None:
    path = tmp_path / "invalid-supported.zip"
    _write_zip(path, [
        {"name": "jppfs_cor:NetSales", "context": CURRENT_CONTEXT, "value": "not-a-number"}
    ])

    result = _extract(path)

    assert result.status == "success_with_rows"
    assert len(result.segments) == 1
    assert result.segments[0].sales is None
    assert result.segments[0].raw_json["_sales_fact_explicit_nil"] is False


def test_source_missing_and_parser_error_remain_fail_closed(tmp_path: Path) -> None:
    missing = _extract(tmp_path / "missing.zip")
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"not a zip")
    broken = _extract(corrupt)

    assert missing.status == "zip_not_found"
    assert broken.status == "parse_error"


def test_current_period_mismatch_remains_date_guard_skip(tmp_path: Path) -> None:
    path = tmp_path / "period-mismatch.zip"
    _write_zip(path, [
        {"name": "jppfs_cor:AmortizationOfGoodwillSGA", "context": CURRENT_CONTEXT, "value": "254"}
    ])

    result = extract_segments_from_xbrl_zip_detailed(
        str(path), period="2026-03-31", quarter="FY",
        allow_expected_quarter_without_title=True,
    )

    assert result.status == "date_guard_skip"
    assert result.segments == []
