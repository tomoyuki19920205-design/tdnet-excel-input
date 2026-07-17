from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import lib.backfill.campaign_fresh_download_plan as plan
from lib.backfill.campaign_state import (
    connect_db, create_campaign, create_campaign_filing, initialize_schema, transaction,
)


CAMPAIGN_ID = "test-campaign"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _url(requested: str) -> str:
    return f"https://www.release.tdnet.info/inbs/0812{requested}.zip"


def _row(index: int, *, retryable: object = 1, url: str | None = None, row_id: str | None = None, ticker: str = "7203") -> dict[str, object]:
    requested = f"20260717{100000 + index:06d}"
    return {
        "campaign_id": CAMPAIGN_ID,
        "manifest_row_id": row_id or f"{index:010d}",
        "requested_disclosure_no": requested,
        "company_code": ticker,
        "normalized_company_code": ticker,
        "source_url": f"https://www.release.tdnet.info/inbs/1401{requested}.pdf",
        "normalized_xbrl_url": _url(requested) if url is None else url,
        "identity_status": "VERIFIED",
        "cache_status": "MISSING",
        "overall_status": "IDENTITY_VERIFIED",
        "error_code": None if retryable else "CACHE_IDENTITY_MISMATCH",
        "retryable": retryable,
    }


def _database(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    db = tmp_path / "campaign.db"
    conn = connect_db(db)
    initialize_schema(conn)
    with transaction(conn):
        create_campaign(conn, {
            "campaign_id": CAMPAIGN_ID, "campaign_name": "test",
            "manifest_path": "manifest.json", "manifest_sha256": "a" * 64,
            "manifest_record_count": len(rows), "code_sha": "b" * 40,
            "worker_version": "v4", "status": "READY",
        })
        for row in rows:
            create_campaign_filing(conn, row)
    conn.close()
    return db


def _build(rows: list[dict[str, object]], tmp_path: Path, *, root: Path | None = None):
    return plan.build_download_plan(
        rows, campaign_id=CAMPAIGN_ID,
        target_cache_root=root or (tmp_path / "new-cache"),
    )


def test_all_rows_standard(tmp_path):
    rows = [_row(1), _row(2)]
    result, _, _ = _build(rows, tmp_path)
    assert [row["plan_classification"] for row in result] == ["STANDARD_FRESH_DOWNLOAD"] * 2


def test_quarantine_classification_disables_auto_ready(tmp_path):
    result, _, _ = _build([_row(1, retryable=0)], tmp_path)
    assert result[0]["plan_classification"] == "QUARANTINE_FRESH_RECHECK"
    assert result[0]["download_allowed"] is True
    assert result[0]["auto_ready_allowed"] is False
    assert result[0]["quarantine_release_required"] is True


@pytest.mark.parametrize("url,reason", [
    ("", "MISSING_NORMALIZED_XBRL_URL"),
    ("http://www.release.tdnet.info/inbs/081220260717100001.zip", "NON_HTTPS_URL"),
    ("https://example.com/inbs/081220260717100001.zip", "NON_OFFICIAL_TDNET_HOST"),
    ("https://www.release.tdnet.info/inbs/not-a-zip.pdf", "INVALID_TDNET_XBRL_ZIP_PATH"),
])
def test_invalid_url_classification(tmp_path, url, reason):
    result, _, _ = _build([_row(1, url=url)], tmp_path)
    assert result[0]["plan_classification"] == "INVALID_OR_MISSING_DOWNLOAD_URL"
    assert result[0]["reason_code"] == reason
    assert result[0]["download_allowed"] is False


def test_requested_id_url_mismatch(tmp_path):
    result, _, _ = _build([_row(1, url=_url("20260717999999"))], tmp_path)
    assert result[0]["reason_code"] == "REQUESTED_ID_URL_MISMATCH"


def test_query_fragment_and_percent_encoding_rejected(tmp_path):
    requested = _row(1)["requested_disclosure_no"]
    for suffix, reason in (("?x=1", "URL_QUERY_OR_FRAGMENT_PRESENT"), ("%2Ezip", "NON_CANONICAL_PERCENT_ENCODING")):
        value = _url(str(requested)) + suffix if suffix.startswith("?") else _url(str(requested))[:-4] + suffix
        result, _, _ = _build([_row(1, url=value)], tmp_path)
        assert result[0]["reason_code"] == reason


def test_duplicate_url_rows_are_not_merged(tmp_path):
    first = _row(1)
    second = _row(2, url=str(first["normalized_xbrl_url"]))
    second["requested_disclosure_no"] = first["requested_disclosure_no"]
    result, _, groups = _build([first, second], tmp_path)
    assert len(result) == 2 and len(groups) == 1
    assert all(row["plan_classification"] == "DUPLICATE_DOWNLOAD_URL" for row in result)
    assert result[0]["target_directory"] != result[1]["target_directory"]


def test_manifest_row_id_is_path_key_not_requested_id(tmp_path):
    row = _row(1, row_id="0000004321")
    result, _, _ = _build([row], tmp_path)
    assert Path(result[0]["target_directory"]).name == "0000004321"
    assert str(row["requested_disclosure_no"]) not in result[0]["target_directory"]


def test_alpha_ticker_is_preserved(tmp_path):
    result, _, _ = _build([_row(1, ticker="581A")], tmp_path)
    assert result[0]["company_code"] == result[0]["normalized_company_code"] == "581A"


def test_windows_casefold_collision_detected(tmp_path):
    with pytest.raises(plan.FreshDownloadPlanStop, match=plan.STOP_PATH_CONFLICT):
        _build([_row(1, row_id="abc"), _row(2, row_id="ABC")], tmp_path)


@pytest.mark.parametrize("reserved", ["CON", "aux", "LPT1"])
def test_windows_reserved_name_detected(tmp_path, reserved):
    with pytest.raises(plan.FreshDownloadPlanStop, match=plan.STOP_PATH_CONFLICT):
        _build([_row(1, row_id=reserved)], tmp_path)


def test_windows_path_length_detected(tmp_path):
    root = tmp_path / ("x" * 240)
    with pytest.raises(plan.FreshDownloadPlanStop, match=plan.STOP_PATH_CONFLICT):
        _build([_row(1)], tmp_path, root=root)


def test_repository_output_is_rejected(tmp_path):
    rows, audit, duplicates = _build([_row(1)], tmp_path)
    with pytest.raises(plan.FreshDownloadPlanStop, match=plan.STOP_OUTPUT):
        plan.write_download_plan(
            output_dir=REPO_ROOT / "forbidden-output", repo_root=REPO_ROOT,
            rows=rows, path_audit=audit, duplicate_groups=duplicates, execution={},
        )


def test_campaign_db_is_read_only_and_hash_unchanged(tmp_path):
    db = _database(tmp_path, [_row(1)])
    before = _sha(db)
    rows = plan.load_campaign_rows(
        db, campaign_id=CAMPAIGN_ID, expected_count=1,
        campaign_db_sha256=before,
    )
    assert len(rows) == 1 and _sha(db) == before
    conn = plan.connect_read_only(db)
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE campaign_filings SET cache_status='READY'")
    conn.close()


def test_campaign_hash_and_count_are_enforced(tmp_path):
    db = _database(tmp_path, [_row(1)])
    with pytest.raises(plan.FreshDownloadPlanStop, match=plan.STOP_CAMPAIGN_CHANGED):
        plan.load_campaign_rows(db, campaign_id=CAMPAIGN_ID, expected_count=2, campaign_db_sha256=_sha(db))
    with pytest.raises(plan.FreshDownloadPlanStop, match=plan.STOP_CAMPAIGN_CHANGED):
        plan.load_campaign_rows(db, campaign_id=CAMPAIGN_ID, expected_count=1, campaign_db_sha256="0" * 64)


def test_target_cache_root_is_not_created(tmp_path):
    cache_root = tmp_path / "not-created"
    rows, audit, duplicates = _build([_row(1)], tmp_path, root=cache_root)
    output = tmp_path / "output"
    plan.write_download_plan(
        output_dir=output, repo_root=REPO_ROOT, rows=rows,
        path_audit=audit, duplicate_groups=duplicates, execution={},
    )
    assert output.is_dir() and not cache_root.exists()


def test_classification_total_matches_input(tmp_path):
    rows = [_row(1), _row(2, retryable=0), _row(3, url="")]
    result, _, _ = _build(rows, tmp_path)
    assert len(result) == 3
    assert sum(1 for row in result if row["plan_classification"] in plan.CLASSIFICATIONS) == 3


def test_unknown_plan_classification_rejected(tmp_path):
    rows, audit, duplicates = _build([_row(1)], tmp_path)
    rows[0]["plan_classification"] = "UNKNOWN"
    with pytest.raises(plan.FreshDownloadPlanStop, match=plan.STOP_COUNT):
        plan.write_download_plan(
            output_dir=tmp_path / "output", repo_root=REPO_ROOT,
            rows=rows, path_audit=audit, duplicate_groups=duplicates, execution={},
        )


def test_digest_is_deterministic(tmp_path):
    rows, audit, duplicates = _build([_row(1), _row(2, retryable=0)], tmp_path)
    first = plan.write_download_plan(
        output_dir=tmp_path / "one", repo_root=REPO_ROOT, rows=rows,
        path_audit=audit, duplicate_groups=duplicates, execution={"git_head": "a" * 40},
    )
    second = plan.write_download_plan(
        output_dir=tmp_path / "two", repo_root=REPO_ROOT, rows=rows,
        path_audit=audit, duplicate_groups=duplicates, execution={"git_head": "a" * 40},
    )
    assert first["digests"] == second["digests"]


def test_rate_and_resume_contracts_are_fixed():
    assert plan.RATE_LIMIT_CONTRACT["workers"] == 1
    assert plan.RATE_LIMIT_CONTRACT["minimum_interval_seconds"] == 1
    assert plan.RATE_LIMIT_CONTRACT["maximum_attempts"] == 3
    assert plan.RATE_LIMIT_CONTRACT["maximum_chunk_size"] == 100
    assert plan.RESUME_CONTRACT["zip_without_provenance"] == "incomplete"
    assert plan.RESUME_CONTRACT["quarantine_release"] == "separate_review_required"
    assert plan.RESUME_CONTRACT["provenance_publish_order"] == "zip_first_provenance_last"
    assert len(plan.RESUME_CONTRACT["provenance_required_fields"]) == 23


def test_no_network_download_or_zip_access_implementation():
    source = (REPO_ROOT / "lib" / "backfill" / "campaign_fresh_download_plan.py").read_text(encoding="utf-8")
    assert "import requests" not in source
    assert "urlopen" not in source and "get_file_url" not in source
    assert "zipfile" not in source
    assert "supabase" not in source.lower()


def test_import_has_no_filesystem_side_effect(tmp_path):
    before = set(tmp_path.iterdir())
    command = [sys.executable, "-c", "import lib.backfill.campaign_fresh_download_plan; print('OK')"]
    completed = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    assert completed.returncode == 0 and completed.stdout.strip() == "OK"
    assert set(tmp_path.iterdir()) == before


def test_cli_help_has_required_args_and_no_apply():
    command = [sys.executable, str(REPO_ROOT / "tools" / "backfill_campaign_fresh_download_plan.py"), "--help"]
    completed = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    assert completed.returncode == 0
    for flag in ("--campaign-db", "--campaign-id", "--target-cache-root", "--output-dir", "--expected-count", "--campaign-db-sha256"):
        assert flag in completed.stdout
    assert "--apply" not in completed.stdout
