from __future__ import annotations

import importlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from lib.backfill.campaign_state import (
    connect_db,
    create_campaign,
    create_campaign_filing,
    get_schema_version,
    initialize_schema,
    table_exists,
    transaction,
)
from tools import backfill_campaign_fresh_state_migrate as migrate_cli


def _campaign() -> dict[str, object]:
    return {
        "campaign_id": "c1", "campaign_name": "test", "manifest_path": "manifest.json",
        "manifest_sha256": "a" * 64, "manifest_record_count": 6,
        "code_sha": "b" * 40, "worker_version": "v4", "status": "CREATED",
    }


def _filing(row_id: str) -> dict[str, object]:
    return {
        "campaign_id": "c1", "manifest_row_id": row_id, "state_filing_id": f"s-{row_id}",
        "requested_disclosure_no": f"20260101{int(row_id):06d}", "company_code": "7203",
        "normalized_company_code": "7203", "source_url": "https://example.test/a.pdf",
        "normalized_xbrl_url": "https://example.test/a.zip", "disclosure_date": "2026-01-01",
        "expected_period": "2025-12-31", "expected_quarter": "FY",
        "document_type": "earnings", "internal_document_id": None,
        "worker_version": "v4", "code_sha": "b" * 40,
        "identity_status": "METADATA_RESOLVED", "cache_status": "MISSING",
        "overall_status": "IDENTITY_RESOLVED",
    }


def _make_db(path: Path) -> None:
    conn = connect_db(path)
    initialize_schema(conn, schema_version="1")
    with transaction(conn):
        create_campaign(conn, _campaign())
        for index in range(1, 7):
            create_campaign_filing(conn, _filing(f"{index:010d}"))
    conn.close()


def _artifact(row_id: str) -> dict[str, object]:
    index = int(row_id)
    return {
        "campaign_id": "c1", "manifest_row_id": row_id,
        "requested_disclosure_no": f"20260101{index:06d}",
        "plan_classification": "STANDARD_FRESH_DOWNLOAD",
        "identity_verdict": "official_linked_xbrl_match", "source_route": "JQUANTS_TD_FILES",
        "zip_sha256": f"{index:064x}", "internal_document_id": f"internal-{index}",
        "zip_internal_ticker": "7203", "zip_internal_period": "2025-12-31",
        "zip_internal_quarter": "FY", "run_id": "run-1",
        "downloaded_at_utc": "2026-07-17T00:00:00+00:00",
    }


@pytest.fixture
def migration_fixture(tmp_path: Path, monkeypatch):
    current, backup = tmp_path / "current.db", tmp_path / "backup.db"
    _make_db(current)
    shutil.copy2(current, backup)
    conn = connect_db(current)
    for index in (1, 2):
        row_id = f"{index:010d}"
        artifact = _artifact(row_id)
        conn.execute(
            "UPDATE campaign_filings SET internal_document_id=?,zip_sha256=?,zip_internal_ticker=?,"
            "zip_internal_period=?,zip_internal_quarter=?,identity_status='VERIFIED',cache_status='READY',"
            "overall_status='IDENTITY_VERIFIED' WHERE campaign_id='c1' AND manifest_row_id=?",
            (artifact["internal_document_id"], artifact["zip_sha256"], "7203", "2025-12-31", "FY", row_id),
        )
    conn.commit(); conn.close()
    plan = tmp_path / "plan.jsonl"
    plan_rows = []
    for index in range(1, 7):
        row_id = f"{index:010d}"
        classification = "QUARANTINE_FRESH_RECHECK" if index == 6 else "STANDARD_FRESH_DOWNLOAD"
        plan_rows.append({
            "campaign_id": "c1", "manifest_row_id": row_id,
            "requested_disclosure_no": f"20260101{index:06d}",
            "plan_classification": classification, "download_allowed": True,
            "target_zip_path": f"C:/target/{row_id}/xbrl.zip",
            "target_provenance_path": f"C:/target/{row_id}/provenance.json",
        })
    plan.write_text("".join(json.dumps(row) + "\n" for row in plan_rows), encoding="utf-8")
    cache = tmp_path / "cache"; cache.mkdir()
    for index in (1, 2):
        directory = cache / f"{index:010d}"; directory.mkdir()
        (directory / "xbrl.zip").write_bytes(f"zip-{index}".encode())
        (directory / "provenance.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(migrate_cli, "load_provenance", lambda zip_path, _provenance: _artifact(zip_path.parent.name))

    def kwargs(output: str = "output", apply: bool = False):
        return {
            "campaign_db": current, "campaign_id": "c1", "legacy_backup_db": backup,
            "download_plan": plan, "cache_root": cache, "migration_run_id": "migration-1",
            "output_dir": tmp_path / output, "campaign_db_sha256": migrate_cli.sha256_file(current),
            "legacy_backup_sha256": migrate_cli.sha256_file(backup),
            "download_plan_sha256": migrate_cli.sha256_file(plan),
            "cache_tree_digest_value": migrate_cli.cache_tree_digest(cache),
            "expected_total": 6, "expected_complete": 2, "expected_not_started": 3,
            "expected_quarantined": 1, "apply": apply,
            "repo_root": Path(__file__).resolve().parents[1],
        }
    return current, backup, plan, cache, kwargs


def test_parser_requires_all_arguments():
    with pytest.raises(SystemExit) as exc:
        migrate_cli.build_parser().parse_args([])
    assert exc.value.code == 2


def test_import_has_no_side_effect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.iterdir())
    sys.modules.pop("tools.backfill_campaign_fresh_state_migrate", None)
    importlib.import_module("tools.backfill_campaign_fresh_state_migrate")
    assert set(tmp_path.iterdir()) == before


def test_dry_run_does_not_change_database(migration_fixture):
    current, _backup, _plan, _cache, kwargs = migration_fixture
    before = migrate_cli.sha256_file(current)
    result = migrate_cli.run_migration(**kwargs())
    assert result["status"] == "READY_TO_MIGRATE"
    assert migrate_cli.sha256_file(current) == before
    assert sorted(path.name for path in kwargs()["output_dir"].iterdir()) == [
        "digests.json", "execution.json", "migration-plan.json", "preflight.json",
    ]


def test_apply_migrates_and_verifies(migration_fixture):
    current, _backup, _plan, _cache, kwargs = migration_fixture
    result = migrate_cli.run_migration(**kwargs(apply=True))
    assert result["status"] == "MIGRATED"
    verification = result["verification"]
    assert verification["schema_version"] == "2"
    assert verification["fresh_status"] == {"COMPLETE": 2, "NOT_STARTED": 3, "QUARANTINED": 1}
    assert verification["complete_artifact_matches"] == 2
    assert verification["legacy_restore"]["changed"] == 0
    assert verification["integrity_check"] == "ok"
    assert verification["foreign_key_check"] == 0
    assert get_schema_version(connect_db(current)) == "2"


def test_migrated_dry_run_reports_already_migrated(migration_fixture):
    current, _backup, _plan, _cache, kwargs = migration_fixture
    migrate_cli.run_migration(**kwargs(apply=True))
    second = kwargs(output="second")
    second["campaign_db_sha256"] = migrate_cli.sha256_file(current)
    result = migrate_cli.run_migration(**second)
    assert result["status"] == "ALREADY_MIGRATED"
    assert result["verification"]["fresh_count"] == 6


@pytest.mark.parametrize("field", [
    "campaign_db_sha256", "legacy_backup_sha256", "download_plan_sha256", "cache_tree_digest_value",
])
def test_digest_mismatch_is_rejected_without_db_change(migration_fixture, field):
    current, _backup, _plan, _cache, kwargs = migration_fixture
    values = kwargs(); before = migrate_cli.sha256_file(current); values[field] = "0" * 64
    with pytest.raises(migrate_cli.FreshStateMigrationCLIStop, match="INPUT_CHANGED"):
        migrate_cli.run_migration(**values)
    assert migrate_cli.sha256_file(current) == before


def test_repository_database_path_is_rejected(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir(); db = repo / "campaign.db"; db.write_bytes(b"x")
    with pytest.raises(migrate_cli.FreshStateMigrationCLIStop, match="UNSAFE_DB_PATH"):
        migrate_cli._safe_write_path(db, repo_root=repo, must_exist=True)


def test_non_temp_database_path_is_rejected():
    path = Path("C:/Windows/not-a-temp-campaign.db")
    with pytest.raises(migrate_cli.FreshStateMigrationCLIStop, match="UNSAFE_DB_PATH"):
        migrate_cli._safe_write_path(path, repo_root=Path("C:/repo"), must_exist=False)


def test_artifact_mismatch_is_rejected(migration_fixture, monkeypatch):
    current, _backup, _plan, _cache, kwargs = migration_fixture
    monkeypatch.setattr(migrate_cli, "load_provenance", lambda *_: {**_artifact("0000000001"), "campaign_id": "wrong"})
    before = migrate_cli.sha256_file(current)
    with pytest.raises(migrate_cli.FreshStateMigrationCLIStop, match="ARTIFACT_MISMATCH"):
        migrate_cli.run_migration(**kwargs())
    assert migrate_cli.sha256_file(current) == before


def test_existing_conflict_rejects_second_apply(migration_fixture):
    current, _backup, _plan, _cache, kwargs = migration_fixture
    migrate_cli.run_migration(**kwargs(apply=True))
    values = kwargs(output="again", apply=True)
    values["campaign_db_sha256"] = migrate_cli.sha256_file(current)
    with pytest.raises(migrate_cli.FreshStateMigrationCLIStop, match="EXISTING_CONFLICT"):
        migrate_cli.run_migration(**values)


def test_output_json_and_digests_are_valid(migration_fixture):
    _current, _backup, _plan, _cache, kwargs = migration_fixture
    output = kwargs()["output_dir"]
    migrate_cli.run_migration(**kwargs())
    digests = json.loads((output / "digests.json").read_text(encoding="utf-8"))
    assert digests
    for name, digest in digests.items():
        assert json.loads((output / name).read_text(encoding="utf-8")) is not None
        assert migrate_cli.sha256_file(output / name) == digest


def test_unknown_schema_is_rejected(migration_fixture):
    current, _backup, _plan, _cache, kwargs = migration_fixture
    conn = sqlite3.connect(current)
    conn.execute("UPDATE campaign_schema_metadata SET schema_version='999'"); conn.commit(); conn.close()
    values = kwargs(); values["campaign_db_sha256"] = migrate_cli.sha256_file(current)
    with pytest.raises(migrate_cli.FreshStateMigrationCLIStop, match="INPUT_CHANGED"):
        migrate_cli.run_migration(**values)


def test_sidecar_file_is_rejected(migration_fixture):
    current, _backup, _plan, _cache, kwargs = migration_fixture
    Path(str(current) + "-wal").write_bytes(b"")
    with pytest.raises(migrate_cli.FreshStateMigrationCLIStop, match="INPUT_CHANGED"):
        migrate_cli.run_migration(**kwargs())


def test_apply_outputs_full_verification_set(migration_fixture):
    _current, _backup, _plan, _cache, kwargs = migration_fixture
    output = kwargs()["output_dir"]
    migrate_cli.run_migration(**kwargs(apply=True))
    assert {path.name for path in output.iterdir()} == {
        "preflight.json", "migration-plan.json", "migration-summary.json",
        "fresh-status-summary.json", "complete-five-verification.json",
        "legacy-restore-verification.json", "next-100-selection.json",
        "database-verification.json", "execution.json", "digests.json",
    }
