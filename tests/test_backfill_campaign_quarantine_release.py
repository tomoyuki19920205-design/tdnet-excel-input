from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from lib.backfill.campaign_state import (
    FreshDownloadReleaseCASFailed,
    apply_fresh_download_successes,
    apply_quarantine_releases,
    connect_db,
    create_campaign,
    create_campaign_filing,
    create_fresh_download,
    initialize_schema,
    plan_quarantine_releases,
    transaction,
)
from tools import backfill_campaign_quarantine_release as cli


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _db(tmp_path: Path, statuses=("QUARANTINED", "QUARANTINED")) -> Path:
    path = tmp_path / "campaign.db"
    conn = connect_db(path)
    initialize_schema(conn)
    with transaction(conn):
        create_campaign(conn, {
            "campaign_id": "c1", "campaign_name": "test", "manifest_path": "m",
            "manifest_sha256": "a" * 64, "manifest_record_count": len(statuses),
            "code_sha": "b" * 40, "worker_version": "v4", "status": "READY",
        })
        for index, status in enumerate(statuses, 1):
            row_id = f"{index:010d}"
            create_campaign_filing(conn, {
                "campaign_id": "c1", "manifest_row_id": row_id,
                "state_filing_id": f"filing-{index}",
                "requested_disclosure_no": f"20260101{index:06d}",
                "company_code": "7203", "normalized_company_code": "7203",
                "source_url": "https://example.test/x",
                "normalized_xbrl_url": "https://example.test/x.zip",
                "disclosure_date": "2026-01-01", "expected_period": "2025-12-31",
                "expected_quarter": "FY", "document_type": "earnings",
                "identity_status": "METADATA_RESOLVED", "cache_status": "MISSING",
                "overall_status": "QUARANTINED", "worker_version": "v4", "code_sha": "b" * 40,
            })
            create_fresh_download(conn, {
                "campaign_id": "c1", "manifest_row_id": row_id,
                "plan_classification": "QUARANTINE_FRESH_RECHECK",
                "fresh_status": status, "source_route": None,
                "target_zip_path": f"C:/cache/{row_id}/xbrl.zip",
                "target_provenance_path": f"C:/cache/{row_id}/provenance.json",
                "auto_ready_allowed": 0, "quarantine_release_required": 1,
                "attempt_count": index, "last_error_code": "CACHE_IDENTITY_MISMATCH",
                "last_error_stage": "identity", "last_error_message": "preserve",
                "prior_identity_status": "MISMATCH",
                "prior_cache_status": "IDENTITY_MISMATCH",
                "prior_overall_status": "QUARANTINED",
                "prior_error_code": "CACHE_IDENTITY_MISMATCH",
                "migration_run_id": "migration-1", "updated_at": f"2026-07-23T00:00:0{index}+00:00",
            })
    conn.close()
    return path


def _rows(path: Path) -> list[dict]:
    conn = connect_db(path)
    values = []
    try:
        for index in (1, 2):
            row_id = f"{index:010d}"
            fresh = dict(conn.execute(
                "SELECT * FROM campaign_fresh_downloads WHERE campaign_id='c1' AND manifest_row_id=?",
                (row_id,),
            ).fetchone())
            values.append({
                "campaign_id": "c1", "manifest_row_id": row_id,
                "filing_id": f"filing-{index}",
                "provider_native_id": f"20260101{index:06d}", "ticker": "7203",
                "retry_classification": "D.SOURCE_AVAILABLE_REDOWNLOAD_REQUIRED",
                "evidence_digest": f"{index:064x}", "expected_state": "QUARANTINED",
                "expected_attempt_count": index, "expected_updated_at": fresh["updated_at"],
                "expected_quarantine_reason": "CACHE_IDENTITY_MISMATCH",
                "retryable": True, "final_quarantine": False,
                "identity_resolved": True, "provider_metadata_unique": True,
                "protected_complete": False,
            })
    finally:
        conn.close()
    return values


def _digest(rows: list[dict]) -> str:
    payload = b"".join(
        (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
        for row in rows
    )
    return hashlib.sha256(payload).hexdigest()


def test_release_success_preserves_history_and_second_plan_is_zero(tmp_path):
    path = _db(tmp_path)
    rows = _rows(path)
    conn = connect_db(path)
    result = apply_quarantine_releases(
        conn, campaign_id="c1", release_rows=rows, manifest_digest=_digest(rows),
        release_run_id="release-1", released_at="2026-07-23T01:00:00+00:00",
    )
    assert result["released_count"] == 2
    assert result["second_plan"]["pending_count"] == 0
    assert result["second_plan"]["already_released_count"] == 2
    after = [dict(row) for row in conn.execute(
        "SELECT * FROM campaign_fresh_downloads ORDER BY manifest_row_id"
    )]
    assert [row["fresh_status"] for row in after] == ["FAILED_RETRYABLE"] * 2
    assert [row["attempt_count"] for row in after] == [1, 2]
    assert {row["last_error_message"] for row in after} == {"preserve"}
    history = [dict(row) for row in conn.execute(
        "SELECT * FROM backfill_quarantine_releases ORDER BY manifest_row_id"
    )]
    assert len(history) == 2
    assert {row["original_quarantine_reason"] for row in history} == {"CACHE_IDENTITY_MISMATCH"}
    conn.close()


@pytest.mark.parametrize("status", ["NOT_STARTED", "COMPLETE"])
def test_non_quarantined_states_are_rejected(tmp_path, status):
    path = _db(tmp_path, statuses=(status, "QUARANTINED"))
    rows = _rows(path)
    conn = connect_db(path)
    plan = plan_quarantine_releases(
        conn, campaign_id="c1", release_rows=rows, manifest_digest=_digest(rows)
    )
    assert plan["conflict_count"] == 1
    conn.close()


@pytest.mark.parametrize(("field", "value"), [
    ("final_quarantine", True),
    ("retryable", False),
    ("identity_resolved", False),
    ("provider_metadata_unique", False),
    ("protected_complete", True),
    ("retry_classification", "G.TRUE_CONTENT_QUARANTINE"),
])
def test_broad_or_final_release_is_rejected(tmp_path, field, value):
    path = _db(tmp_path)
    rows = _rows(path)
    rows[0][field] = value
    conn = connect_db(path)
    with pytest.raises(FreshDownloadReleaseCASFailed):
        plan_quarantine_releases(
            conn, campaign_id="c1", release_rows=rows, manifest_digest=_digest(rows)
        )
    conn.close()


@pytest.mark.parametrize(("field", "value"), [
    ("expected_attempt_count", 999),
    ("expected_updated_at", "drift"),
    ("expected_quarantine_reason", "drift"),
    ("provider_native_id", "requested-id-substitution"),
    ("ticker", "9999"),
])
def test_cas_identity_or_version_drift_is_reported(tmp_path, field, value):
    path = _db(tmp_path)
    rows = _rows(path)
    rows[0][field] = value
    conn = connect_db(path)
    plan = plan_quarantine_releases(
        conn, campaign_id="c1", release_rows=rows, manifest_digest=_digest(rows)
    )
    assert plan["conflict_count"] == 1
    conn.close()


def test_duplicate_filing_is_rejected(tmp_path):
    path = _db(tmp_path)
    rows = _rows(path)
    rows[1]["filing_id"] = rows[0]["filing_id"]
    conn = connect_db(path)
    with pytest.raises(FreshDownloadReleaseCASFailed):
        plan_quarantine_releases(
            conn, campaign_id="c1", release_rows=rows, manifest_digest=_digest(rows)
        )
    conn.close()


def test_one_cas_failure_rolls_back_all_rows_and_history(tmp_path):
    path = _db(tmp_path)
    rows = _rows(path)
    conn = connect_db(path)
    with pytest.raises(RuntimeError, match="boom"):
        apply_quarantine_releases(
            conn, campaign_id="c1", release_rows=rows, manifest_digest=_digest(rows),
            release_run_id="release-1",
            after_update=lambda index, _conn: (_ for _ in ()).throw(RuntimeError("boom"))
            if index == 1 else None,
        )
    assert [row[0] for row in conn.execute(
        "SELECT fresh_status FROM campaign_fresh_downloads ORDER BY manifest_row_id"
    )] == ["QUARANTINED", "QUARANTINED"]
    assert conn.execute("SELECT COUNT(*) FROM backfill_quarantine_releases").fetchone()[0] == 0
    conn.close()


def test_released_row_is_accepted_by_worker_success_cas(tmp_path):
    path = _db(tmp_path)
    rows = _rows(path)[:1]
    conn = connect_db(path)
    apply_quarantine_releases(
        conn, campaign_id="c1", release_rows=rows, manifest_digest=_digest(rows),
        release_run_id="release-1",
    )
    fresh = dict(conn.execute(
        "SELECT * FROM campaign_fresh_downloads WHERE manifest_row_id='0000000001'"
    ).fetchone())
    filing = dict(conn.execute(
        "SELECT * FROM campaign_filings WHERE manifest_row_id='0000000001'"
    ).fetchone())
    before = {**filing, **fresh}
    result = {
        "manifest_row_id": "0000000001", "internal_document_id": "doc-1",
        "zip_sha256": "f" * 64, "ticker": "7203", "period": "2025-12-31",
        "quarter": "FY", "identity_verdict": "official_linked_xbrl_match",
    }
    assert apply_fresh_download_successes(
        conn, campaign_id="c1", before_rows=[before], verified_results=[result],
        expected_count=1, run_id="worker", journal_path="journal",
    )[0]["fresh_status"] == "COMPLETE"
    conn.close()


def test_cli_defaults_to_dry_run_and_does_not_modify_database(tmp_path):
    path = _db(tmp_path)
    rows = _rows(path)
    manifest = tmp_path / "release.jsonl"
    manifest.write_text("".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    ), encoding="utf-8")
    before = _sha(path)
    output = tmp_path / "output"
    result = cli.run_release(
        campaign_db=path.resolve(), expected_db_sha256=before, campaign_id="c1",
        manifest=manifest.resolve(), evidence_digest=_sha(manifest), run_id="dry-run",
        output_dir=output.resolve(), apply=False, confirm_count=2,
    )
    assert result["released_count"] == 0
    assert _sha(path) == before


def test_cli_manifest_digest_and_count_are_fail_closed(tmp_path):
    path = _db(tmp_path)
    manifest = tmp_path / "release.jsonl"
    manifest.write_text("{}\n", encoding="utf-8")
    with pytest.raises(cli.QuarantineReleaseCLIStop):
        cli.run_release(
            campaign_db=path.resolve(), expected_db_sha256=_sha(path), campaign_id="c1",
            manifest=manifest.resolve(), evidence_digest="0" * 64, run_id="run",
            output_dir=(tmp_path / "output").resolve(), apply=False, confirm_count=2,
        )
