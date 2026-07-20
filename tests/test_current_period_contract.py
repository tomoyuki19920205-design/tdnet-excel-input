from __future__ import annotations

import json
import sqlite3
import zipfile

import pytest

from lib.backfill.batch_upsert import normalize_and_validate_rec
from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed


def _write_segment_zip(path, *, title: str, current_end: str, previous_end: str) -> None:
    main = f"""
    <html><body>
      <ix:nonNumeric name="jpcrp_cor:DocumentTitle">{title}</ix:nonNumeric>
      <xbrli:context id="CurrentYTDDuration_tse-qcedjpfr-19670AlphaReportableSegmentMember">
        <xbrli:period><xbrli:startDate>2025-03-21</xbrli:startDate><xbrli:endDate>{current_end}</xbrli:endDate></xbrli:period>
      </xbrli:context>
      <xbrli:context id="Prior1YTDDuration_tse-qcedjpfr-19670AlphaReportableSegmentMember">
        <xbrli:period><xbrli:startDate>2024-03-21</xbrli:startDate><xbrli:endDate>{previous_end}</xbrli:endDate></xbrli:period>
      </xbrli:context>
    </body></html>
    """
    segment = """
    <html><body>
      <ix:nonfraction name="jpcrp_cor:revenuesfromexternalcustomers" contextref="CurrentYTDDuration_tse-qcedjpfr-19670AlphaReportableSegmentMember" unitref="JPY" decimals="0">200000000</ix:nonfraction>
      <ix:nonfraction name="jpcrp_cor:revenuesfromexternalcustomers" contextref="Prior1YTDDuration_tse-qcedjpfr-19670AlphaReportableSegmentMember" unitref="JPY" decimals="0">100000000</ix:nonfraction>
    </body></html>
    """
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("XBRLData/Summary/main-ixbrl.htm", main)
        archive.writestr("XBRLData/Attachment/0103010-qnsg02-ixbrl.htm", segment)


@pytest.mark.parametrize(
    ("quarter", "title", "current_end", "previous_end"),
    [
        ("1Q", "2026年3月期 第1四半期決算短信", "2025-06-20", "2024-06-20"),
        ("2Q", "2026年3月期 第2四半期決算短信", "2025-09-20", "2024-09-20"),
        ("3Q", "2026年3月期 第3四半期決算短信", "2025-12-20", "2024-12-20"),
        ("FY", "2026年3月期 決算短信", "2026-03-20", "2025-03-20"),
    ],
)
def test_caller_bound_non_month_end_period_is_lossless(
    tmp_path, quarter, title, current_end, previous_end
):
    zip_path = tmp_path / f"{quarter}.zip"
    _write_segment_zip(
        zip_path, title=title, current_end=current_end, previous_end=previous_end
    )

    result = extract_segments_from_xbrl_zip_detailed(
        str(zip_path),
        period="2026-03-20",
        quarter=quarter,
        title=title,
        include_context_evidence=True,
        allow_expected_quarter_without_title=True,
    )

    assert result.status == "success_with_rows"
    by_role = {row.raw_json["_segment_period_role"]: row for row in result.segments}
    assert by_role["current"].period == "2026-03-20"
    assert by_role["previous"].period == "2025-03-20"
    assert by_role["current"].quarter == quarter
    assert by_role["previous"].quarter == quarter
    evidence = by_role["current"].raw_json["_context_evidence"]
    assert evidence["context_end"] == current_end
    assert evidence["current_or_previous"] == "current"
    assert json.loads(json.dumps(evidence, sort_keys=True)) == evidence


def _verified_record(*, period="2026-03-20", quarter="2Q", role="current"):
    return {
        "ticker": "1967",
        "period": period,
        "quarter": quarter,
        "tdnet_doc_id": "20251104319670",
        "disclosure_date": "2025-11-04",
        "source": "backfill_xbrl",
        "extractor_route": "xbrl",
        "_segment_period_role": role,
        "_identity_verified": True,
        "_identity_verdict": "exact_document_id_match",
        "_requested_disclosure_no": "20251104586069",
        "_internal_document_id": "20251104319670",
        "_canonical_expected_period": "2026-03-20",
        "_canonical_expected_quarter": quarter,
        "_resolved_zip_sha256": "a" * 64,
        "_verified_xbrl_same_zip": True,
        "_worker_version": "v4",
    }


@pytest.mark.parametrize("quarter", ["1Q", "2Q", "3Q", "FY"])
def test_verified_current_contract_accepts_exact_canonical_identity(quarter):
    ok, reason, source = normalize_and_validate_rec(
        sqlite3.connect(":memory:"), _verified_record(quarter=quarter)
    )
    assert (ok, reason, source) == (True, "ok", "verified_current_xbrl")


def test_verified_current_contract_rejects_month_end_coercion():
    ok, reason, source = normalize_and_validate_rec(
        sqlite3.connect(":memory:"), _verified_record(period="2026-03-31")
    )
    assert (ok, reason, source) == (
        False,
        "verified_current_period_contract_mismatch",
        "none",
    )


def test_verified_previous_contract_rejects_current_period_value():
    ok, reason, source = normalize_and_validate_rec(
        sqlite3.connect(":memory:"), _verified_record(role="previous")
    )
    assert (ok, reason, source) == (
        False,
        "verified_previous_period_contract_mismatch",
        "none",
    )
