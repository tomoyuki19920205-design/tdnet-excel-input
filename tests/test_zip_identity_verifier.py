import hashlib
import zipfile
from pathlib import Path
import src.segment.zip_identity_verifier as verifier

from src.segment.zip_identity_verifier import (
    TrustedProvenance,
    extract_actual_metadata_from_zip,
    verify_zip_identity,
)

ROW51_ZIP = Path(
    r"C:\tmp\v4-fresh-row51-identity-audit-20260718-100133"
    r"\diagnostic-download\row51-xbrl.zip"
)
ROW51_SHA256 = "cb555a7cad60fa76e029b94aa4f2d2904e8f4a9691e465c45e4145a0e0f0a5f5"


def _write_zip(path, dates, *, include_summary=True, quarter="4", markers=""):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("XBRLData/Summary/tse-qcedjpsm-12340-20240101555555.xsd", "")
        if include_summary:
            contexts = "".join(
                f"<xbrli:context id='c{i}'><xbrli:period>"
                f"<xbrli:endDate>{date}</xbrli:endDate>"
                f"</xbrli:period></xbrli:context>"
                for i, date in enumerate(dates)
            )
            zf.writestr(
                "XBRLData/Summary/tse-qcedjpsm-12340-20240101555555-ixbrl.htm",
                "<html xmlns:xbrli='http://www.xbrl.org/2003/instance'>"
                f"{contexts}{markers}<ix:nonFraction name='tse-ed-t:QuarterlyPeriod'>{quarter}</ix:nonFraction>"
                "</html>",
            )


def test_fy_selects_expected_actual_not_forecast(tmp_path):
    path = tmp_path / "xbrl.zip"
    _write_zip(path, ["2024-03-31", "2025-03-31"])

    meta = extract_actual_metadata_from_zip(
        str(path), expected_period="2024-03-31", expected_quarter="FY"
    )
    verdict = verify_zip_identity(
        str(path), "20240101555555", "1234", "2024-03-31", "FY"
    )

    assert meta["period"] == "2024-03-31"
    assert meta["period"] != "2025-03-31"
    assert verdict.passed is True
    assert verdict.verdict == "exact_document_id_match"


def test_fy_missing_expected_period_is_not_injected(tmp_path):
    path = tmp_path / "xbrl.zip"
    _write_zip(path, ["2025-03-31"])

    meta = extract_actual_metadata_from_zip(
        str(path), expected_period="2024-03-31", expected_quarter="FY"
    )
    verdict = verify_zip_identity(
        str(path), "20240101555555", "1234", "2024-03-31", "FY"
    )

    assert meta["period"] == "2025-03-31"
    assert verdict.passed is False
    assert verdict.rejection_reason == "period_mismatch"


def test_fy_exact_match_is_order_independent(tmp_path):
    paths = [tmp_path / "a.zip", tmp_path / "b.zip"]
    _write_zip(paths[0], ["2023-03-31", "2024-03-31", "2025-03-31"])
    _write_zip(paths[1], ["2025-03-31", "2024-03-31", "2023-03-31"])

    selected = [
        extract_actual_metadata_from_zip(
            str(path), expected_period="2024-03-31", expected_quarter="FY"
        )["period"]
        for path in paths
    ]

    assert selected == ["2024-03-31", "2024-03-31"]


def test_non_fy_keeps_existing_max_date_selection(tmp_path):
    path = tmp_path / "xbrl.zip"
    _write_zip(path, ["2024-03-31", "2025-03-31"])

    meta = extract_actual_metadata_from_zip(
        str(path), expected_period="2024-03-31", expected_quarter="1Q"
    )

    assert meta["period"] == "2025-03-31"


def test_missing_summary_remains_metadata_unresolved(tmp_path):
    path = tmp_path / "xbrl.zip"
    _write_zip(path, [], include_summary=False)

    verdict = verify_zip_identity(
        str(path), "20240101555555", "1234", "2024-03-31", "FY"
    )

    assert verdict.passed is False
    assert verdict.rejection_reason == "metadata_unresolved"


def test_fy_ignores_accumulated_q2_in_forecast_context_when_safe(tmp_path):
    path = tmp_path / "xbrl.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("XBRLData/Summary/tse-qcedjpsm-12340-20240101555555.xsd", "")
        zf.writestr(
            "XBRLData/Summary/tse-qcedjpsm-12340-20240101555555-ixbrl.htm",
            "<html xmlns:xbrli='http://www.xbrl.org/2003/instance'>"
            "<xbrli:endDate>2024-03-31</xbrli:endDate>"
            "<xbrli:endDate>2025-03-31</xbrli:endDate>"
            "AnnualMember YearEndMember AccumulatedQ2"
            "</html>",
        )

    meta = extract_actual_metadata_from_zip(
        str(path), expected_period="2024-03-31", expected_quarter="FY"
    )

    assert meta["period"] == "2024-03-31"
    assert meta["quarter"] == "FY"
    with zipfile.ZipFile(path) as zf:
        summary = zf.read(
            "XBRLData/Summary/tse-qcedjpsm-12340-20240101555555-ixbrl.htm"
        ).decode("utf-8")
    assert "QuarterlyPeriod" not in summary


def test_fy_without_annual_marker_is_rejected(tmp_path):
    path = tmp_path / "xbrl.zip"
    _write_zip(path, ["2024-03-31", "2025-03-31"], quarter="2")
    meta = extract_actual_metadata_from_zip(str(path), expected_period="2024-03-31", expected_quarter="FY")
    verdict = verify_zip_identity(str(path), "20240101555555", "1234", "2024-03-31", "FY")
    assert meta["period"] == "2024-03-31"
    assert meta["quarter"] == "2Q"
    assert verdict.passed is False
    assert verdict.rejection_reason == "quarter_mismatch"


def test_document_type_gate_rejects_only_mismatch(tmp_path, monkeypatch):
    path = tmp_path / "xbrl.zip"
    _write_zip(path, ["2024-03-31"], markers="AnnualMember")
    base = {"ticker":"1234","period":"2024-03-31","quarter":"FY","document_type":"not_allowed","internal_document_id":"20240101555555"}
    monkeypatch.setattr(verifier, "extract_actual_metadata_from_zip", lambda *a, **k: base)
    verdict = verifier.verify_zip_identity(str(path), "20240101555555", "1234", "2024-03-31", "FY")
    assert verdict.passed is False
    assert verdict.rejection_reason == "document_type_mismatch"
    assert verdict.verdict != "exact_document_id_match"


def test_document_type_gate_accepts_allowed_control(tmp_path, monkeypatch):
    path = tmp_path / "xbrl.zip"
    _write_zip(path, ["2024-03-31"], markers="AnnualMember")
    base = {"ticker":"1234","period":"2024-03-31","quarter":"FY","document_type":"attachment_xbrl","internal_document_id":"20240101555555"}
    monkeypatch.setattr(verifier, "extract_actual_metadata_from_zip", lambda *a, **k: base)
    verdict = verifier.verify_zip_identity(str(path), "20240101555555", "1234", "2024-03-31", "FY")
    assert verdict.passed is True
    assert verdict.verdict == "exact_document_id_match"


def test_non_fy_quarter_regressions(tmp_path):
    for quarter, expected in [("1", "1Q"), ("2", "2Q"), ("3", "3Q")]:
        path = tmp_path / f"{quarter}.zip"
        _write_zip(path, ["2024-03-31"], quarter=quarter, markers="AnnualMember")
        meta = extract_actual_metadata_from_zip(str(path), expected_period="2024-03-31", expected_quarter=expected)
        verdict = verify_zip_identity(str(path), "20240101555555", "1234", "2024-03-31", expected)
        assert meta["period"] == "2024-03-31"
        assert meta["quarter"] == expected
        assert verdict.passed is True


def _write_alpha_zip(path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in entries:
            zf.writestr(name, content)


def test_extracts_internal_document_id_for_alpha_ticker(tmp_path):
    path = tmp_path / "alpha.zip"
    prefix = "XBRLData/Summary/tse-acedjpsm-581A0-202607143581A0"
    _write_alpha_zip(
        path,
        [
            (f"{prefix}-def.xml", ""),
            (f"{prefix}.xsd", ""),
            (
                f"{prefix}-ixbrl.htm",
                "<html xmlns:xbrli='http://www.xbrl.org/2003/instance'>"
                "<xbrli:endDate>2026-05-31</xbrli:endDate>"
                "AnnualMember</html>",
            ),
        ],
    )

    meta = extract_actual_metadata_from_zip(
        str(path), expected_period="2026-05-31", expected_quarter="FY"
    )

    assert meta["ticker"] == "581A"
    assert meta["period"] == "2026-05-31"
    assert meta["quarter"] == "FY"
    assert meta["document_type"] == "attachment_xbrl"
    assert meta["internal_document_id"] == "202607143581A0"


def test_rejects_internal_document_id_for_mismatched_alpha_ticker(tmp_path):
    path = tmp_path / "mismatched-alpha.zip"
    _write_alpha_zip(
        path,
        [
            ("XBRLData/Summary/tse-acedjpsm-581A0-202607143472A0.xsd", ""),
            (
                "XBRLData/Summary/tse-acedjpsm-581A0-no-id-ixbrl.htm",
                "<html xmlns:xbrli='http://www.xbrl.org/2003/instance'>"
                "<xbrli:endDate>2026-05-31</xbrli:endDate>AnnualMember</html>",
            ),
        ],
    )

    meta = extract_actual_metadata_from_zip(str(path))

    assert meta["ticker"] == "581A"
    assert meta["internal_document_id"] == ""


def test_rejects_ambiguous_internal_document_ids_for_alpha_ticker(tmp_path):
    path = tmp_path / "ambiguous-alpha.zip"
    _write_alpha_zip(
        path,
        [
            ("XBRLData/Summary/tse-acedjpsm-581A0-202607143581A0.xsd", ""),
            ("XBRLData/Summary/tse-acedjpsm-581A0-202607144581A0-def.xml", ""),
        ],
    )

    meta = extract_actual_metadata_from_zip(str(path))

    assert meta["internal_document_id"] == ""


def test_does_not_use_html_title_timestamp_as_internal_document_id(tmp_path):
    path = tmp_path / "html-title.zip"
    _write_alpha_zip(
        path,
        [
            ("XBRLData/Summary/tse-acedjpsm-581A0-no-id.xsd", ""),
            (
                "XBRLData/Summary/tse-acedjpsm-581A0-no-id-ixbrl.htm",
                "<html><title>決算短信_20260714124604</title></html>",
            ),
        ],
    )

    meta = extract_actual_metadata_from_zip(str(path))

    assert meta["internal_document_id"] == ""


DEI_NS = "http://disclosure.edinet-fsa.go.jp/taxonomy/jpdei/2013-08-31/jpdei_cor"


def _attachment_ixbrl(
    *, ticker="13320", period="2025-03-31", quarter="FY",
    document_type="FYFinancialStatements_Consolidated_JP", dei_ns=DEI_NS,
    dei_prefix="jpdei_cor", include_ix=True, include_context=True,
    include_dei=True, include_identifier=True, include_schema_ref=True,
):
    fact_tag = "ix:nonNumeric" if include_ix else "span"
    return (
        "<html xmlns='http://www.w3.org/1999/xhtml' "
        "xmlns:ix='http://www.xbrl.org/2008/inlineXBRL' "
        "xmlns:xbrli='http://www.xbrl.org/2003/instance' "
        "xmlns:link='http://www.xbrl.org/2003/linkbase' "
        f"xmlns:{dei_prefix}='{dei_ns}'>"
        + ("<link:schemaRef href='schema.xsd'/>" if include_schema_ref else "")
        + ("<xbrli:context id='c'><xbrli:entity>" if include_context else "")
        + (f"<xbrli:identifier scheme='http://www.tse.or.jp/sicc'>{ticker}</xbrli:identifier>" if include_identifier else "")
        + ("</xbrli:entity><xbrli:period><xbrli:instant>2025-03-31</xbrli:instant></xbrli:period></xbrli:context>" if include_context else "")
        + (f"<{fact_tag} name='{dei_prefix}:CurrentPeriodEndDateDEI'>{period}</{fact_tag}>" if include_dei else "")
        + (f"<{fact_tag} name='{dei_prefix}:CurrentFiscalYearEndDateDEI'>{period}</{fact_tag}>" if include_dei else "")
        + (f"<{fact_tag} name='{dei_prefix}:TypeOfCurrentPeriodDEI'>{quarter}</{fact_tag}>" if include_dei else "")
        + (f"<{fact_tag} name='{dei_prefix}:DocumentTypeDEI'>{document_type}</{fact_tag}>" if include_dei else "")
        +
        "</html>"
    )


def _write_attachment_zip(path, candidates, extension="htm"):
    with zipfile.ZipFile(path, "w") as zf:
        for index, content in enumerate(candidates):
            zf.writestr(
                f"XBRLData/Attachment/030000{index}-acbs01-tse-acedjpfr-13320-2025-03-31-01-2025-05-14-ixbrl.{extension}",
                content,
            )


def test_attachment_ixbrl_metadata_without_internal_id(tmp_path):
    path = tmp_path / "attachment.zip"
    _write_attachment_zip(path, [_attachment_ixbrl(), _attachment_ixbrl()])

    meta = extract_actual_metadata_from_zip(str(path), "2025-03-31", "FY")

    assert meta == {
        "ticker": "1332", "period": "2025-03-31", "quarter": "FY",
        "document_type": "attachment_xbrl", "internal_document_id": "",
    }


def test_attachment_inline_extensions_and_prefix_independence(tmp_path):
    for extension in ("htm", "html", "xhtml"):
        path = tmp_path / f"attachment-{extension}.zip"
        _write_attachment_zip(
            path, [_attachment_ixbrl(dei_prefix="different_prefix")], extension
        )
        assert extract_actual_metadata_from_zip(str(path))["period"] == "2025-03-31"


def test_attachment_plain_or_structurally_incomplete_html_is_rejected(tmp_path):
    variants = [
        "<html><body>plain explanation</body></html>",
        _attachment_ixbrl(include_ix=False),
        _attachment_ixbrl(include_context=False),
        _attachment_ixbrl(include_dei=False),
        _attachment_ixbrl(include_identifier=False),
        _attachment_ixbrl(include_schema_ref=False),
    ]
    for index, content in enumerate(variants):
        path = tmp_path / f"invalid-{index}.zip"
        _write_attachment_zip(path, [content])
        meta = extract_actual_metadata_from_zip(str(path))
        assert not meta["period"] and not meta["quarter"]


def test_attachment_unknown_dei_namespace_is_rejected(tmp_path):
    path = tmp_path / "unknown-dei.zip"
    _write_attachment_zip(path, [_attachment_ixbrl(dei_ns="https://example.test/dei")])

    meta = extract_actual_metadata_from_zip(str(path))

    assert meta["period"] == ""


def test_attachment_ixbrl_conflicting_period_fails_closed(tmp_path):
    path = tmp_path / "conflict.zip"
    _write_attachment_zip(path, [
        _attachment_ixbrl(period="2025-03-31"),
        _attachment_ixbrl(period="2024-03-31"),
    ])

    meta = extract_actual_metadata_from_zip(str(path), "2025-03-31", "FY")
    verdict = verify_zip_identity(
        str(path), "20250521559959", "1332", "2025-03-31", "FY"
    )

    assert meta["period"] == ""
    assert verdict.passed is False
    assert verdict.rejection_reason == "zip_internal_identity_conflict"
    assert verdict.verdict == "ZIP_INTERNAL_IDENTITY_CONFLICT"
    assert verdict.details["conflict_fields"] == ["period"]
    assert verdict.internal_id == ""


def test_attachment_ixbrl_conflicting_quarter_fails_closed(tmp_path):
    path = tmp_path / "conflict-quarter.zip"
    _write_attachment_zip(path, [_attachment_ixbrl(), _attachment_ixbrl(quarter="3Q")])

    meta = extract_actual_metadata_from_zip(str(path), "2025-03-31", "FY")
    verdict = verify_zip_identity(
        str(path), "20250521559959", "1332", "2025-03-31", "FY"
    )

    assert meta["quarter"] == ""
    assert verdict.rejection_reason == "zip_internal_identity_conflict"
    assert verdict.details["conflict_fields"] == ["quarter"]


def test_attachment_ixbrl_conflicting_ticker_fails_closed(tmp_path):
    path = tmp_path / "conflict-ticker.zip"
    _write_attachment_zip(path, [_attachment_ixbrl(), _attachment_ixbrl(ticker="99990")])

    meta = extract_actual_metadata_from_zip(str(path), "2025-03-31", "FY")
    verdict = verify_zip_identity(
        str(path), "20250521559959", "1332", "2025-03-31", "FY"
    )

    assert meta["ticker"] == ""
    assert verdict.rejection_reason == "zip_internal_identity_conflict"
    assert verdict.details["conflict_fields"] == ["ticker"]


def test_attachment_ixbrl_conflicting_document_type_fails_closed(tmp_path):
    path = tmp_path / "conflict-document.zip"
    _write_attachment_zip(path, [
        _attachment_ixbrl(),
        _attachment_ixbrl(document_type="QuarterlyFinancialStatements_Consolidated_JP"),
    ])
    assert extract_actual_metadata_from_zip(str(path))["document_type"] == "attachment_xbrl"
    verdict = verify_zip_identity(
        str(path), "20250521559959", "1332", "2025-03-31", "FY"
    )
    assert verdict.rejection_reason == "provenance_missing"


def test_attachment_non_financial_document_type_is_not_compatible(tmp_path):
    path = tmp_path / "incompatible-document.zip"
    _write_attachment_zip(path, [
        _attachment_ixbrl(document_type="AnnualSecuritiesReport"),
    ])

    meta = extract_actual_metadata_from_zip(str(path))
    verdict = verify_zip_identity(
        str(path), "20250521559959", "1332", "2025-03-31", "FY"
    )

    assert meta["document_type"] == ""
    assert verdict.rejection_reason == "metadata_unresolved"


def test_attachment_internal_id_conflict_is_rejected(tmp_path):
    path = tmp_path / "conflict-internal.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for internal in ("20250521559959", "20250521559960"):
            zf.writestr(
                f"XBRLData/Attachment/tse-acedjpfr-13320-{internal}-ixbrl.htm",
                _attachment_ixbrl(),
            )
    verdict = verify_zip_identity(
        str(path), "20250521559959", "1332", "2025-03-31", "FY"
    )
    assert verdict.rejection_reason == "zip_internal_identity_conflict"
    assert verdict.details["conflict_fields"] == ["internal_document_id"]


def _summary_ixbrl(*, ticker="13320", period="2025-03-31", quarter="4"):
    return (
        "<html xmlns='http://www.w3.org/1999/xhtml' "
        "xmlns:ix='http://www.xbrl.org/2008/inlineXBRL' "
        "xmlns:xbrli='http://www.xbrl.org/2003/instance' "
        "xmlns:link='http://www.xbrl.org/2003/linkbase' "
        "xmlns:tse-ed-t='http://www.tse.or.jp/taxonomy/tse-ed-t'>"
        "<link:schemaRef href='summary.xsd'/>"
        "<xbrli:context id='c'><xbrli:entity>"
        f"<xbrli:identifier scheme='http://www.tse.or.jp/sicc'>{ticker}</xbrli:identifier>"
        "</xbrli:entity><xbrli:period>"
        f"<xbrli:instant>{period}</xbrli:instant>"
        "</xbrli:period></xbrli:context>"
        f"<ix:nonNumeric name='tse-ed-t:QuarterlyPeriod'>{quarter}</ix:nonNumeric>"
        "</html>"
    )


def _write_summary_attachment_zip(path, *, summary_period, attachment_period, incomplete=False):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "XBRLData/Summary/tse-qnedjpsm-13320-20250521133200-ixbrl.htm",
            _summary_ixbrl(period=summary_period),
        )
        zf.writestr(
            "XBRLData/Attachment/0500000-qnbs02-tse-qnedjpfr-13320-ixbrl.htm",
            _attachment_ixbrl(
                period=attachment_period,
                include_schema_ref=not incomplete,
            ),
        )


def test_summary_attachment_period_conflict_precedes_manifest_comparison(tmp_path):
    path = tmp_path / "summary-attachment-conflict.zip"
    _write_summary_attachment_zip(
        path, summary_period="2025-03-31", attachment_period="2024-03-31"
    )

    verdict = verify_zip_identity(
        str(path), "20250521559959", "1332", "2024-03-31", "FY"
    )

    assert verdict.rejection_reason == "zip_internal_identity_conflict"
    assert verdict.details["conflict_fields"] == ["period"]
    assert verdict.details["candidate_count"] == 2
    assert {candidate["format"] for candidate in verdict.details["candidates"]} == {
        "SUMMARY_IXBRL", "ATTACHMENT_IXBRL",
    }


def test_summary_attachment_consensus_and_incomplete_attachment_compatibility(tmp_path):
    matching = tmp_path / "matching.zip"
    _write_summary_attachment_zip(
        matching, summary_period="2025-03-31", attachment_period="2025-03-31"
    )
    incomplete = tmp_path / "incomplete.zip"
    _write_summary_attachment_zip(
        incomplete, summary_period="2025-03-31", attachment_period="2024-03-31",
        incomplete=True,
    )

    matching_verdict = verify_zip_identity(
        str(matching), "20250521133200", "1332", "2025-03-31", "FY"
    )
    incomplete_verdict = verify_zip_identity(
        str(incomplete), "20250521133200", "1332", "2025-03-31", "FY"
    )

    assert matching_verdict.passed is True
    assert matching_verdict.verdict == "exact_document_id_match"
    assert incomplete_verdict.passed is True
    assert incomplete_verdict.verdict == "exact_document_id_match"


def test_summary_fiscal_year_end_fact_precedes_forecast_context_date(tmp_path):
    path = tmp_path / "summary-fiscal-year-end.zip"
    summary = _summary_ixbrl(period="2025-03-31").replace(
        "</html>",
        "<ix:nonNumeric name='tse-ed-t:FiscalYearEnd'>2025-03-31</ix:nonNumeric>"
        "<xbrli:context id='forecast'><xbrli:entity>"
        "<xbrli:identifier scheme='http://www.tse.or.jp/sicc'>13320</xbrli:identifier>"
        "</xbrli:entity><xbrli:period><xbrli:instant>2026-03-31</xbrli:instant>"
        "</xbrli:period></xbrli:context></html>",
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "XBRLData/Summary/tse-qnedjpsm-13320-20250521133200-ixbrl.htm", summary
        )
        zf.writestr(
            "XBRLData/Attachment/0500000-qnbs02-tse-qnedjpfr-13320-ixbrl.htm",
            _attachment_ixbrl(period="2025-03-31"),
        )

    verdict = verify_zip_identity(
        str(path), "20250521133200", "1332", "2025-03-31", "FY"
    )

    assert verdict.passed is True
    assert verdict.verdict == "exact_document_id_match"


def test_attachment_xml_and_xbrl_are_not_reclassified_as_inline(tmp_path):
    for extension in ("xml", "xbrl"):
        path = tmp_path / f"instance-{extension}.zip"
        _write_zip(path, ["2025-03-31"])
        with zipfile.ZipFile(path, "a") as zf:
            zf.writestr(f"XBRLData/Attachment/instance.{extension}", _attachment_ixbrl())
        meta = extract_actual_metadata_from_zip(
            str(path), expected_period="2025-03-31", expected_quarter="FY"
        )
        assert meta["internal_document_id"] == "20240101555555"
        assert meta["ticker"] == "1234"


def test_row51_diagnostic_zip_accepts_official_link_without_internal_id():
    assert ROW51_ZIP.is_file()
    assert hashlib.sha256(ROW51_ZIP.read_bytes()).hexdigest() == ROW51_SHA256
    meta = extract_actual_metadata_from_zip(str(ROW51_ZIP), "2025-03-31", "FY")
    provenance = TrustedProvenance(
        source="jquants", requested_disclosure_no="20250521559959",
        requested_file_type="x", resolved_by_function="get_td_file",
        official_request_succeeded=True, response_status=200,
        downloaded_size=ROW51_ZIP.stat().st_size, downloaded_sha256=ROW51_SHA256,
        internal_document_id="", ticker="1332", period="2025-03-31", quarter="FY",
        document_type="attachment_xbrl", resolved_at="2026-07-18T01:00:00+00:00",
    )

    verdict = verify_zip_identity(
        str(ROW51_ZIP), "20250521559959", "1332", "2025-03-31", "FY", provenance
    )

    assert meta == {
        "ticker": "1332", "period": "2025-03-31", "quarter": "FY",
        "document_type": "attachment_xbrl", "internal_document_id": "",
    }
    assert verdict.passed is True
    assert verdict.verdict == "official_linked_xbrl_match_without_internal_id"
    assert verdict.internal_id == ""


def test_attachment_without_internal_id_requires_official_provenance(tmp_path):
    path = tmp_path / "attachment.zip"
    _write_attachment_zip(path, [_attachment_ixbrl()])

    verdict = verify_zip_identity(
        str(path), "20250521559959", "1332", "2025-03-31", "FY"
    )

    assert verdict.passed is False
    assert verdict.rejection_reason == "provenance_missing"
