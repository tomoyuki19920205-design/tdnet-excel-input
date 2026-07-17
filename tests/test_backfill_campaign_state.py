from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

from lib.backfill.campaign_state import (
    FreshDownloadCASFailed,
    SCHEMA_VERSION,
    apply_fresh_download_successes,
    connect_db,
    create_campaign,
    create_campaign_filing,
    create_campaign_filings,
    get_schema_version,
    initialize_schema,
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
    before = [dict(row) for row in conn.execute(
        "SELECT * FROM campaign_filings WHERE campaign_id='c1' ORDER BY manifest_row_id"
    )]
    results = [{
        "manifest_row_id": row["manifest_row_id"],
        "internal_document_id": f"2026010172030{index}",
        "zip_sha256": f"{index:064x}", "ticker": "7203",
        "period": "2025-12-31", "quarter": "FY",
    } for index, row in enumerate(before, 1)]
    return before, results


def test_fresh_download_success_updates_exact_five_and_protects_other_columns(tmp_path):
    conn = _db(tmp_path)
    before, results = _fresh_download_fixture(conn)
    readback = apply_fresh_download_successes(
        conn, campaign_id="c1", before_rows=before, verified_results=results,
        updated_at="2026-07-17T00:00:00+00:00",
    )
    assert len(readback) == 5
    for old, new, result in zip(before, readback, results):
        assert new["internal_document_id"] == result["internal_document_id"]
        assert new["zip_sha256"] == result["zip_sha256"]
        assert (new["identity_status"], new["cache_status"], new["overall_status"]) == (
            "VERIFIED", "READY", "IDENTITY_VERIFIED",
        )
        assert (new["error_code"], new["error_stage"], new["error_message"]) == (None, None, None)
        for field in (
            "campaign_id", "manifest_row_id", "requested_disclosure_no", "company_code",
            "source_url", "document_type", "registration_status", "created_at",
            "extraction_status", "sqlite_save_status", "canonical_save_status",
            "supabase_save_status", "started_at", "completed_at",
        ):
            assert new[field] == old[field]
    conn.close()


def test_fresh_download_cas_mismatch_rolls_back_all_rows(tmp_path):
    conn = _db(tmp_path)
    before, results = _fresh_download_fixture(conn)
    conn.execute(
        "UPDATE campaign_filings SET error_message='external' WHERE manifest_row_id='0000000003'"
    )
    conn.commit()
    with pytest.raises(FreshDownloadCASFailed, match="CAS failed"):
        apply_fresh_download_successes(
            conn, campaign_id="c1", before_rows=before, verified_results=results,
        )
    assert conn.execute("SELECT COUNT(*) FROM campaign_filings WHERE cache_status='READY'").fetchone()[0] == 0
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
            after_update=fail,
        )
    assert conn.execute("SELECT COUNT(*) FROM campaign_filings WHERE cache_status='READY'").fetchone()[0] == 0
    conn.close()
