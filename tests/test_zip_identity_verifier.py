import zipfile
import src.segment.zip_identity_verifier as verifier

from src.segment.zip_identity_verifier import (
    extract_actual_metadata_from_zip,
    verify_zip_identity,
)


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
