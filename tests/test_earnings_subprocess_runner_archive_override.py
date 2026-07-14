import zipfile

import pytest

from src.events import earnings_subprocess_runner
from src.events.earnings_subprocess_runner import run_earnings_subprocess_dry_run


def test_dry_run_uses_explicit_archive_root_for_single_doc(tmp_path):
    isolated_archive = tmp_path / "isolated_archive"
    isolated_archive.mkdir()
    zip_path = isolated_archive / "581A_20260714_081220260714592943.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "XBRLData/Summary/tse-acedjpsm-581A0-202607143581A0.xsd", ""
        )

    target_doc = {
        "ticker": "581A",
        "company_name": "Ｇ－ＧＯ",
        "title": "2026年5月期 決算短信〔日本基準〕（連結）",
        "disclosed_at": "2026-07-14 15:30:00+09:00",
        "source_doc_id": "140120260714592943",
        "xbrl_doc_id": "081220260714592943",
        "archive_date": "20260714",
        "source_url": "https://www.release.tdnet.info/inbs/140120260714592943.pdf",
        "pdf_url": "https://www.release.tdnet.info/inbs/140120260714592943.pdf",
    }

    result = run_earnings_subprocess_dry_run(
        docs=[target_doc],
        worker_count=1,
        archive_root=str(isolated_archive),
    )

    assert result["total_count"] == 1
    assert result["success_count"] == 1


def _target_doc():
    return {
        "ticker": "581A",
        "company_name": "Ｇ－ＧＯ",
        "title": "2026年5月期 決算短信〔日本基準〕（連結）",
        "disclosed_at": "2026-07-14 15:30:00+09:00",
        "source_doc_id": "140120260714592943",
        "xbrl_doc_id": "081220260714592943",
        "archive_date": "20260714",
        "source_url": "https://www.release.tdnet.info/inbs/140120260714592943.pdf",
        "pdf_url": "https://www.release.tdnet.info/inbs/140120260714592943.pdf",
    }


def _write_archive_zip(archive_root):
    archive_root.mkdir(parents=True, exist_ok=True)
    zip_path = archive_root / "581A_20260714_081220260714592943.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("XBRLData/Summary/test.xsd", "")
    return zip_path


def test_explicit_archive_root_does_not_fallback_to_default(tmp_path, monkeypatch):
    fake_project_root = tmp_path / "project"
    _write_archive_zip(fake_project_root / "data" / "xbrl_archive")
    isolated_archive = tmp_path / "isolated_archive"
    isolated_archive.mkdir()
    monkeypatch.setattr(earnings_subprocess_runner, "_PROJECT_ROOT", fake_project_root)

    with pytest.raises(ValueError, match="file_not_found: No matching ZIP"):
        earnings_subprocess_runner.find_zip_for_doc(
            _target_doc(),
            "140120260714592943",
            "081220260714592943",
            "20260714",
            archive_root=isolated_archive,
        )


def test_default_archive_root_is_used_when_not_overridden(tmp_path, monkeypatch):
    fake_project_root = tmp_path / "project"
    expected_zip = _write_archive_zip(fake_project_root / "data" / "xbrl_archive")
    monkeypatch.setattr(earnings_subprocess_runner, "_PROJECT_ROOT", fake_project_root)

    actual_zip, reason = earnings_subprocess_runner.find_zip_for_doc(
        _target_doc(),
        "140120260714592943",
        "081220260714592943",
        "20260714",
    )

    assert actual_zip == str(expected_zip)
    assert reason == "found 20260714592943"
