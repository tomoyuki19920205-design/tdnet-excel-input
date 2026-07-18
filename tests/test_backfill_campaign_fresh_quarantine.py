from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lib.backfill.campaign_state import (
    connect_db,
    create_campaign,
    create_campaign_filing,
    create_fresh_download,
    initialize_schema,
    transaction,
)
from tools import backfill_campaign_fresh_quarantine as cli
from lib.backfill import campaign_fresh_download_loop as loop


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "campaign.db"
    conn = connect_db(path)
    initialize_schema(conn)
    with transaction(conn):
        create_campaign(conn, {
            "campaign_id": "c1", "campaign_name": "test", "manifest_path": "manifest.json",
            "manifest_sha256": "a" * 64, "manifest_record_count": 2,
            "code_sha": "b" * 40, "worker_version": "v4", "status": "READY",
        })
        for index in (1, 2):
            row_id = f"{index:010d}"
            requested = f"20260101{index:06d}"
            create_campaign_filing(conn, {
                "campaign_id": "c1", "manifest_row_id": row_id,
                "state_filing_id": f"state-{index}", "requested_disclosure_no": requested,
                "company_code": "7203", "normalized_company_code": "7203",
                "source_url": "https://example.test/x", "normalized_xbrl_url": "https://example.test/x.zip",
                "disclosure_date": "2026-01-01", "expected_period": "2025-12-31",
                "expected_quarter": "FY", "document_type": "earnings",
                "internal_document_id": None, "worker_version": "v4", "code_sha": "b" * 40,
                "identity_status": "METADATA_RESOLVED", "cache_status": "MISSING",
                "overall_status": "IDENTITY_RESOLVED",
            })
            create_fresh_download(conn, {
                "campaign_id": "c1", "manifest_row_id": row_id,
                "plan_classification": "STANDARD_FRESH_DOWNLOAD", "fresh_status": "NOT_STARTED",
                "target_zip_path": f"C:/cache/{row_id}/xbrl.zip",
                "target_provenance_path": f"C:/cache/{row_id}/provenance.json",
                "auto_ready_allowed": 1, "quarantine_release_required": 0,
                "attempt_count": 0, "prior_identity_status": "METADATA_RESOLVED",
                "prior_cache_status": "MISSING", "prior_overall_status": "IDENTITY_RESOLVED",
                "migration_run_id": "migration-1",
            })
    conn.close()
    return path


def _evidence(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "evidence"
    root.mkdir()
    result = root / "http-diagnostic-result.json"
    result.write_text(json.dumps({
        "judgment": "STOP_V4_ROW719_HTTP_DIAGNOSTIC_UNRESOLVED",
        "failure": {"http_status": 404, "failure_code": "TD_FILES_DISCNO_NOT_FOUND"},
    }, sort_keys=True), encoding="utf-8")
    entries = [{"path": result.name, "size": result.stat().st_size, "sha256": _sha(result)}]
    digest = hashlib.sha256(cli._json_bytes(entries)).hexdigest()
    (root / "digests.json").write_bytes(cli._json_bytes({
        "files": entries, "tree_digest_excluding_digests_json": digest,
    }))
    return root, digest


def _kwargs(tmp_path: Path, **overrides):
    database = overrides.pop("campaign_db", _database(tmp_path))
    evidence, evidence_sha = _evidence(tmp_path)
    values = {
        "campaign_db": database, "campaign_db_sha256": _sha(database),
        "campaign_id": "c1", "manifest_row_id": "0000000001",
        "requested_document_id": "20260101000001", "expected_status": "NOT_STARTED",
        "expected_attempt_count": 0, "reason_code": "TD_FILES_DISCNO_NOT_FOUND",
        "failure_stage": "STAGE_A", "source_route": "JQUANTS_TD_FILES",
        "http_status": 404, "evidence_path": evidence, "evidence_sha256": evidence_sha,
        "output_dir": tmp_path / "v4-fresh-quarantine-20260718-010101",
        "confirm_campaign_id": "c1", "confirm_manifest_row_id": "0000000001",
        "apply": True, "production_apply": True, "repo_root": tmp_path / "repo",
        "command_sanitized": "python -m tools.backfill_campaign_fresh_quarantine --redacted",
    }
    values.update(overrides)
    return values


def test_formal_cli_quarantines_one_row_and_writes_complete_audit(tmp_path):
    values = _kwargs(tmp_path)
    result = cli.run_quarantine(**values)
    assert result["status"] == "QUARANTINED"
    after = result["result"]["after"]
    assert after["fresh_status"] == "QUARANTINED"
    assert after["attempt_count"] == 0
    assert after["last_error_code"] == "TD_FILES_DISCNO_NOT_FOUND"
    assert all(after[column] is None for column in (
        "artifact_zip_sha256", "artifact_internal_document_id", "artifact_ticker",
        "artifact_period", "artifact_quarter", "identity_verdict", "completed_at",
    ))
    output = values["output_dir"]
    assert json.loads((output / "journal.json").read_text())["current_phase"] == "COMPLETE"
    assert {path.name for path in output.iterdir()} == {
        "journal.json", "before-row.json", "after-row.json", "invariants.json",
        "command-sanitized.txt", "digests.json",
    }


def test_parent_dynamic_selection_excludes_runtime_quarantine(tmp_path):
    values = _kwargs(tmp_path)
    cli.run_quarantine(**values)
    selected = loop.select_next_rows(values["campaign_db"], "c1", 100)
    assert [row["manifest_row_id"] for row in selected] == ["0000000002"]
    assert selected[0]["requested_disclosure_no"] == "20260101000002"


@pytest.mark.parametrize(("field", "value"), [
    ("apply", False), ("production_apply", False),
    ("confirm_campaign_id", "other"), ("confirm_manifest_row_id", "other"),
    ("expected_status", "FAILED_RETRYABLE"), ("expected_attempt_count", 1),
    ("reason_code", "OTHER"), ("failure_stage", "STAGE_B"),
    ("source_route", "OTHER"), ("http_status", 500),
])
def test_cli_contract_guards_change_nothing(tmp_path, field, value):
    values = _kwargs(tmp_path)
    database = values["campaign_db"]
    before = _sha(database)
    values[field] = value
    with pytest.raises(cli.FreshQuarantineCLIStop, match=cli.STOP_GUARD):
        cli.run_quarantine(**values)
    assert _sha(database) == before
    assert not values["output_dir"].exists()


def test_cli_database_and_evidence_digest_guards(tmp_path):
    values = _kwargs(tmp_path)
    database = values["campaign_db"]
    before = _sha(database)
    with pytest.raises(cli.FreshQuarantineCLIStop, match=cli.STOP_DATABASE):
        cli.run_quarantine(**{**values, "campaign_db_sha256": "0" * 64})
    with pytest.raises(cli.FreshQuarantineCLIStop, match=cli.STOP_EVIDENCE):
        cli.run_quarantine(**{**values, "evidence_sha256": "0" * 64})
    assert _sha(database) == before


def test_cli_requested_id_mismatch_fails_after_evidence_without_db_change(tmp_path):
    values = _kwargs(tmp_path)
    database = values["campaign_db"]
    before = _sha(database)
    with pytest.raises(Exception, match="precondition"):
        cli.run_quarantine(**{**values, "requested_document_id": "20260101999999"})
    assert _sha(database) == before
    journal = json.loads((values["output_dir"] / "journal.json").read_text())
    assert journal["current_phase"] == "FAILED"


def test_cli_rejects_non_allowlisted_repository_database(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    database = _database(repo)
    values = _kwargs(tmp_path)
    values.update({
        "campaign_db": database, "campaign_db_sha256": _sha(database),
        "repo_root": repo,
    })
    with pytest.raises(cli.FreshQuarantineCLIStop, match=cli.STOP_PATH):
        cli.run_quarantine(**values)


def test_cli_rejects_artifact_state_attempt_and_completed_rows(tmp_path):
    for index, (column, value) in enumerate((
        ("artifact_zip_sha256", "a" * 64),
        ("attempt_count", 1),
        ("completed_at", "2026-07-18T00:00:00+00:00"),
        ("fresh_status", "COMPLETE"),
    ), 1):
        case = tmp_path / f"case-{index}"
        case.mkdir()
        values = _kwargs(case)
        database = values["campaign_db"]
        conn = connect_db(database)
        conn.execute(
            f"UPDATE campaign_fresh_downloads SET {column}=? WHERE manifest_row_id='0000000001'",
            (value,),
        )
        conn.commit(); conn.close()
        values["campaign_db_sha256"] = _sha(database)
        with pytest.raises(Exception, match="precondition"):
            cli.run_quarantine(**values)


def test_cli_import_and_help_have_no_filesystem_side_effect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.iterdir())
    sys.modules.pop("tools.backfill_campaign_fresh_quarantine", None)
    importlib.import_module("tools.backfill_campaign_fresh_quarantine")
    assert set(tmp_path.iterdir()) == before
    completed = subprocess.run(
        [sys.executable, "-m", "tools.backfill_campaign_fresh_quarantine", "--help"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0
    assert "--evidence-sha256" in completed.stdout
