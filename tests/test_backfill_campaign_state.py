from __future__ import annotations

import importlib
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from lib.backfill.campaign_state import (
    FreshDownloadCASFailed,
    FreshStateMigrationConflict,
    SCHEMA_VERSION,
    apply_fresh_download_successes,
    connect_db,
    create_campaign,
    create_campaign_filing,
    create_campaign_filings,
    create_fresh_download,
    get_schema_version,
    initialize_schema,
    load_fresh_download_rows,
    migrate_fresh_download_state,
    select_next_fresh_downloads,
    table_exists,
    transaction,
)


def _campaign(campaign_id: str = "c1") -> dict:
    return {
        "campaign_id": campaign_id, "campaign_name": "test", "manifest_path": "manifest.json",
        "manifest_sha256": "a" * 64, "manifest_record_count": 2, "code_sha": "b" * 40,
        "worker_version": "v4", "status": "CREATED",
    }


def _filing(row_id: str, requested: str = "20260101000000", ticker: str = "1000") -> dict:
    return {
        "campaign_id": "c1", "manifest_row_id": row_id, "state_filing_id": f"s-{row_id}",
        "requested_disclosure_no": requested, "company_code": ticker,
        "normalized_company_code": ticker, "source_url": "https://example.test/x",
        "normalized_xbrl_url": "https://example.test/x.zip", "disclosure_date": "2026-01-01",
        "expected_period": "2025-12-31", "expected_quarter": "FY", "document_type": "earnings",
        "internal_document_id": f"i-{row_id}", "worker_version": "v4", "code_sha": "b" * 40,
    }


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect_db(tmp_path / "campaign.db")
    initialize_schema(conn)
    return conn


def test_schema_creation_and_tables(tmp_path):
    conn = _db(tmp_path)
    assert table_exists(conn, "campaigns")
    assert table_exists(conn, "campaign_filings")
    assert table_exists(conn, "campaign_fresh_downloads")
    assert get_schema_version(conn) == SCHEMA_VERSION
    conn.close()


def test_schema_initialization_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    initialize_schema(conn)
    initialize_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION
    conn.close()


def test_campaign_creation(tmp_path):
    conn = _db(tmp_path)
    create_campaign(conn, _campaign())
    assert conn.execute("SELECT campaign_id FROM campaigns").fetchone()[0] == "c1"
    conn.close()


def test_composite_primary_key_rejects_duplicate(tmp_path):
    conn = _db(tmp_path)
    create_campaign(conn, _campaign())
    create_campaign_filing(conn, _filing("r1"))
    with pytest.raises(sqlite3.IntegrityError):
        create_campaign_filing(conn, _filing("r1"))
    conn.close()


def test_campaigns_are_isolated_by_campaign_id(tmp_path):
    conn = _db(tmp_path)
    create_campaign(conn, _campaign("c1"))
    create_campaign(conn, _campaign("c2"))
    create_campaign_filing(conn, _filing("r1"))
    other = _filing("r1")
    other["campaign_id"] = "c2"
    create_campaign_filing(conn, other)
    assert conn.execute("SELECT COUNT(*) FROM campaign_filings").fetchone()[0] == 2
    conn.close()


def test_requested_id_duplicates_are_allowed(tmp_path):
    conn = _db(tmp_path)
    create_campaign(conn, _campaign())
    create_campaign_filing(conn, _filing("r1", ticker="1000"))
    create_campaign_filing(conn, _filing("r2", ticker="2000"))
    assert conn.execute("SELECT COUNT(*) FROM campaign_filings WHERE requested_disclosure_no = ?", ("20260101000000",)).fetchone()[0] == 2
    conn.close()


def test_foreign_key_rejects_unknown_campaign(tmp_path):
    conn = _db(tmp_path)
    bad = _filing("r1")
    bad["campaign_id"] = "missing"
    with pytest.raises(sqlite3.IntegrityError):
        create_campaign_filing(conn, bad)
    conn.close()


def test_initial_status_defaults(tmp_path):
    conn = _db(tmp_path)
    create_campaign(conn, _campaign())
    create_campaign_filing(conn, _filing("r1"))
    row = conn.execute("SELECT registration_status, identity_status, cache_status, extraction_status, overall_status, retryable FROM campaign_filings").fetchone()
    assert tuple(row) == ("REGISTERED", "UNVERIFIED", "UNKNOWN", "NOT_STARTED", "REGISTERED", 1)
    conn.close()


def test_schema_version_mismatch_is_explicit(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(RuntimeError, match="version mismatch"):
        initialize_schema(conn, schema_version="999")
    conn.close()


def test_import_has_no_database_side_effect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.iterdir())
    sys.modules.pop("lib.backfill.campaign_state", None)
    importlib.import_module("lib.backfill.campaign_state")
    assert set(tmp_path.iterdir()) == before


def test_transaction_rolls_back_on_error(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(RuntimeError):
        with transaction(conn):
            create_campaign(conn, _campaign())
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 0
    conn.close()


def test_bulk_insert_rolls_back_on_duplicate_manifest_row(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(conn):
            create_campaign(conn, _campaign())
            create_campaign_filings(conn, [_filing("r1"), _filing("r1")])
    assert conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM campaign_filings").fetchone()[0] == 0
    conn.close()


def _fresh_download_fixture(conn: sqlite3.Connection):
    with transaction(conn):
        create_campaign(conn, _campaign())
        for index in range(1, 6):
            filing = _filing(f"{index:010d}", requested=f"20260101{index:06d}", ticker="7203")
            filing.update({
                "internal_document_id": None, "identity_status": "METADATA_RESOLVED",
                "cache_status": "MISSING", "overall_status": "IDENTITY_RESOLVED",
                "error_code": "old", "error_stage": "identity", "error_message": "old",
            })
            create_campaign_filing(conn, filing)
            create_fresh_download(conn, {
                "campaign_id": "c1", "manifest_row_id": f"{index:010d}",
                "plan_classification": "STANDARD_FRESH_DOWNLOAD",
                "fresh_status": "NOT_STARTED", "source_route": None,
                "target_zip_path": f"C:/cache/{index:010d}/xbrl.zip",
                "target_provenance_path": f"C:/cache/{index:010d}/provenance.json",
                "auto_ready_allowed": 1, "quarantine_release_required": 0,
                "attempt_count": 0, "prior_identity_status": "METADATA_RESOLVED",
                "prior_cache_status": "MISSING", "prior_overall_status": "IDENTITY_RESOLVED",
                "prior_error_code": "old", "migration_run_id": "migration-1",
            })
    filing_rows = {row["manifest_row_id"]: dict(row) for row in conn.execute(
        "SELECT * FROM campaign_filings WHERE campaign_id='c1' ORDER BY manifest_row_id"
    )}
    before = [{**filing_rows[row["manifest_row_id"]], **dict(row)} for row in conn.execute(
        "SELECT * FROM campaign_fresh_downloads WHERE campaign_id='c1' ORDER BY manifest_row_id"
    )]
    results = [{
        "manifest_row_id": row["manifest_row_id"],
        "internal_document_id": f"2026010172030{index}",
        "zip_sha256": f"{index:064x}", "ticker": "7203",
        "period": "2025-12-31", "quarter": "FY",
        "identity_verdict": "official_linked_xbrl_match",
    } for index, row in enumerate(before, 1)]
    return before, results


def test_fresh_download_success_updates_dedicated_rows_and_preserves_filings(tmp_path):
    conn = _db(tmp_path)
    before, results = _fresh_download_fixture(conn)
    filing_before = [dict(row) for row in conn.execute(
        "SELECT * FROM campaign_filings ORDER BY manifest_row_id"
    )]
    readback = apply_fresh_download_successes(
        conn, campaign_id="c1", before_rows=before, verified_results=results,
        expected_count=5, run_id="run-1", journal_path="C:/audit/journal.json",
        updated_at="2026-07-17T00:00:00+00:00",
    )
    assert len(readback) == 5
    for old, new, result in zip(before, readback, results):
        assert new["fresh_status"] == "COMPLETE"
        assert new["artifact_internal_document_id"] == result["internal_document_id"]
        assert new["artifact_zip_sha256"] == result["zip_sha256"]
        assert new["attempt_count"] == old["attempt_count"] + 1
        assert new["last_run_id"] == "run-1"
    assert [dict(row) for row in conn.execute(
        "SELECT * FROM campaign_filings ORDER BY manifest_row_id"
    )] == filing_before
    conn.close()


def test_fresh_download_success_accepts_explicit_verified_missing_internal_id(tmp_path):
    conn = _db(tmp_path)
    before, results = _fresh_download_fixture(conn)
    results[0]["internal_document_id"] = None
    results[0]["identity_verdict"] = "official_linked_xbrl_match_without_internal_id"

    readback = apply_fresh_download_successes(
        conn, campaign_id="c1", before_rows=before, verified_results=results,
        expected_count=5, run_id="run-1", journal_path="journal.json",
    )

    assert readback[0]["artifact_internal_document_id"] is None
    assert readback[0]["identity_verdict"] == "official_linked_xbrl_match_without_internal_id"
    conn.close()


@pytest.mark.parametrize(
    ("internal_id", "verdict"),
    [
        (None, "official_linked_xbrl_match"),
        ("20260101720301", "official_linked_xbrl_match_without_internal_id"),
    ],
)
def test_fresh_download_success_rejects_inconsistent_internal_identity(
    tmp_path, internal_id, verdict
):
    conn = _db(tmp_path)
    before, results = _fresh_download_fixture(conn)
    results[0]["internal_document_id"] = internal_id
    results[0]["identity_verdict"] = verdict

    with pytest.raises(FreshDownloadCASFailed, match="internal identity"):
        apply_fresh_download_successes(
            conn, campaign_id="c1", before_rows=before, verified_results=results,
            expected_count=5, run_id="run-1", journal_path="journal.json",
        )

    assert conn.execute(
        "SELECT COUNT(*) FROM campaign_fresh_downloads WHERE fresh_status='COMPLETE'"
    ).fetchone()[0] == 0
    conn.close()


def test_fresh_download_cas_mismatch_rolls_back_all_rows(tmp_path):
    conn = _db(tmp_path)
    before, results = _fresh_download_fixture(conn)
    conn.execute(
        "UPDATE campaign_fresh_downloads SET updated_at='external' WHERE manifest_row_id='0000000003'"
    )
    conn.commit()
    with pytest.raises(FreshDownloadCASFailed):
        apply_fresh_download_successes(
            conn, campaign_id="c1", before_rows=before, verified_results=results,
            expected_count=5, run_id="run-1", journal_path="journal.json",
        )
    assert conn.execute("SELECT COUNT(*) FROM campaign_fresh_downloads WHERE fresh_status='COMPLETE'").fetchone()[0] == 0
    conn.close()


def test_fresh_download_mid_transaction_failure_rolls_back_all_rows(tmp_path):
    conn = _db(tmp_path)
    before, results = _fresh_download_fixture(conn)
    def fail(index, _conn):
        if index == 2:
            raise sqlite3.OperationalError("injected")
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        apply_fresh_download_successes(
            conn, campaign_id="c1", before_rows=before, verified_results=results,
            expected_count=5, run_id="run-1", journal_path="journal.json",
            after_update=fail,
        )
    assert conn.execute("SELECT COUNT(*) FROM campaign_fresh_downloads WHERE fresh_status='COMPLETE'").fetchone()[0] == 0
    conn.close()


def test_fresh_status_and_path_constraints(tmp_path):
    conn = _db(tmp_path)
    with transaction(conn):
        create_campaign(conn, _campaign())
        create_campaign_filing(conn, _filing("r1"))
        create_campaign_filing(conn, _filing("r2"))
    payload = {
        "campaign_id": "c1", "manifest_row_id": "r1",
        "plan_classification": "STANDARD_FRESH_DOWNLOAD", "fresh_status": "NOT_STARTED",
        "target_zip_path": "C:/cache/r1/xbrl.zip",
        "target_provenance_path": "C:/cache/r1/provenance.json",
        "auto_ready_allowed": 1, "quarantine_release_required": 0,
        "prior_identity_status": "UNVERIFIED", "prior_cache_status": "UNKNOWN",
        "prior_overall_status": "REGISTERED", "migration_run_id": "m1",
    }
    create_fresh_download(conn, payload)
    bad = {**payload, "manifest_row_id": "r2", "fresh_status": "UNKNOWN"}
    with pytest.raises(ValueError, match="invalid fresh download status"):
        create_fresh_download(conn, bad)
    duplicate_path = {**payload, "manifest_row_id": "r2"}
    with pytest.raises(sqlite3.IntegrityError):
        create_fresh_download(conn, duplicate_path)
    conn.close()


def _legacy_migration_fixture(tmp_path: Path):
    current_path, backup_path = tmp_path / "current.db", tmp_path / "backup.db"
    current = connect_db(current_path)
    initialize_schema(current, schema_version="1")
    with transaction(current):
        create_campaign(current, {**_campaign(), "manifest_record_count": 6})
        for index in range(1, 7):
            filing = _filing(f"{index:010d}", requested=f"20260101{index:06d}", ticker="7203")
            filing.update({
                "internal_document_id": None, "identity_status": "METADATA_RESOLVED",
                "cache_status": "MISSING", "overall_status": "IDENTITY_RESOLVED",
            })
            create_campaign_filing(current, filing)
    current.close()
    shutil.copy2(current_path, backup_path)
    current = connect_db(current_path)
    backup = connect_db(backup_path)
    complete = {}
    plan = []
    for index in range(1, 7):
        row_id = f"{index:010d}"
        classification = "QUARANTINE_FRESH_RECHECK" if index == 6 else "STANDARD_FRESH_DOWNLOAD"
        plan.append({
            "campaign_id": "c1", "manifest_row_id": row_id,
            "plan_classification": classification,
            "target_zip_path": f"C:/cache/{row_id}/xbrl.zip",
            "target_provenance_path": f"C:/cache/{row_id}/provenance.json",
        })
        if index <= 2:
            complete[row_id] = {
                "zip_sha256": f"{index:064x}", "internal_document_id": f"internal-{index}",
                "zip_internal_ticker": "7203", "zip_internal_period": "2025-12-31",
                "zip_internal_quarter": "FY", "identity_verdict": "official_linked_xbrl_match",
                "run_id": "run-1", "downloaded_at_utc": "2026-07-17T00:00:00+00:00",
            }
            current.execute(
                "UPDATE campaign_filings SET internal_document_id=?,zip_sha256=?,zip_internal_ticker='7203',"
                "zip_internal_period='2025-12-31',zip_internal_quarter='FY',identity_status='VERIFIED',"
                "cache_status='READY',overall_status='IDENTITY_VERIFIED' WHERE campaign_id='c1' AND manifest_row_id=?",
                (f"internal-{index}", f"{index:064x}", row_id),
            )
    current.commit()
    return current, backup, plan, complete


def test_explicit_v1_to_v2_migration_restores_legacy_and_is_idempotent(tmp_path):
    current, backup, plan, complete = _legacy_migration_fixture(tmp_path)
    backup_snapshot = [dict(row) for row in backup.execute(
        "SELECT * FROM campaign_filings ORDER BY manifest_row_id"
    )]
    result = migrate_fresh_download_state(
        current, backup_conn=backup, campaign_id="c1", plan_rows=plan,
        complete_artifacts=complete, migration_run_id="migration-1",
        migrated_at="2026-07-17T01:00:00+00:00", journal_path="journal.json",
    )
    assert result == {"status": "MIGRATED", "rows": 6, "restored_rows": 2}
    assert get_schema_version(current) == "2"
    assert [dict(row) for row in current.execute(
        "SELECT * FROM campaign_filings ORDER BY manifest_row_id"
    )] == backup_snapshot
    assert dict(current.execute(
        "SELECT fresh_status,COUNT(*) n FROM campaign_fresh_downloads GROUP BY fresh_status"
    ).fetchall()) == {"COMPLETE": 2, "NOT_STARTED": 3, "QUARANTINED": 1}
    again = migrate_fresh_download_state(
        current, backup_conn=backup, campaign_id="c1", plan_rows=plan,
        complete_artifacts=complete, migration_run_id="migration-1",
        migrated_at="2026-07-17T02:00:00+00:00", journal_path="journal.json",
    )
    assert again == {"status": "ALREADY_MIGRATED", "rows": 6, "restored_rows": 0}
    current.execute("UPDATE campaign_fresh_downloads SET fresh_status='CONFLICT' WHERE manifest_row_id='0000000003'")
    current.commit()
    with pytest.raises(FreshStateMigrationConflict, match="EXISTING_CONFLICT"):
        migrate_fresh_download_state(
            current, backup_conn=backup, campaign_id="c1", plan_rows=plan,
            complete_artifacts=complete, migration_run_id="migration-1",
        )
    current.close(); backup.close()


def test_next_hundred_selection_ignores_legacy_cache_status(tmp_path):
    conn = _db(tmp_path)
    with transaction(conn):
        create_campaign(conn, {**_campaign(), "manifest_record_count": 105})
        for index in range(1, 106):
            row_id = f"{index:010d}"
            filing = _filing(row_id, requested=f"20260101{index:06d}")
            filing["cache_status"] = "SIDECAR_REQUIRED" if index in {2, 10} else "MISSING"
            create_campaign_filing(conn, filing)
            create_fresh_download(conn, {
                "campaign_id": "c1", "manifest_row_id": row_id,
                "plan_classification": "STANDARD_FRESH_DOWNLOAD",
                "fresh_status": "COMPLETE" if index <= 5 else "NOT_STARTED",
                "target_zip_path": f"C:/cache/{row_id}/xbrl.zip",
                "target_provenance_path": f"C:/cache/{row_id}/provenance.json",
                "auto_ready_allowed": 1, "quarantine_release_required": 0,
                "prior_identity_status": "VERIFIED", "prior_cache_status": filing["cache_status"],
                "prior_overall_status": "IDENTITY_VERIFIED", "migration_run_id": "m1",
            })
    selected = select_next_fresh_downloads(conn, "c1", limit=100)
    assert [row["manifest_row_id"] for row in selected] == [f"{index:010d}" for index in range(6, 106)]
    assert len(load_fresh_download_rows(conn, "c1", [row["manifest_row_id"] for row in selected])) == 100
    conn.close()
