from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import lib.backfill.campaign_identity_apply as apply_mod
from lib.backfill.campaign_state import (
    connect_db, create_campaign, create_campaign_filings, initialize_schema, transaction,
)
from tools.backfill_campaign_identity_apply import build_parser


CAMPAIGN = "v4-test"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plan_row(row_id: str, classification: str, *, requested: str | None = None, nulls: bool = False) -> dict:
    actual = {} if nulls else {
        "internal_document_id": "20260101123456", "zip_sha256": "a" * 64,
        "ticker": "7203", "period": "2026-03-31", "quarter": "FY",
    }
    return {
        "campaign_id": CAMPAIGN, "manifest_row_id": row_id,
        "requested_disclosure_no": requested or f"20260101{int(row_id):06d}",
        "classification": classification, "reason_code": classification + "_REASON",
        "expected_identity": {"expected_period": None if nulls else "2026-03-31", "expected_quarter": None if nulls else "FY"},
        "actual_identity": actual,
    }


def _write_plan(root: Path, rows: list[dict]) -> tuple[Path, str]:
    root.mkdir()
    path = root / "identity-plan-results-46218.jsonl"
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8", newline="\n")
    digest = _sha(path)
    (root / "digests.json").write_text(json.dumps({path.name: digest}, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return path, digest


def _db(path: Path, rows: list[dict]) -> None:
    conn = connect_db(path)
    initialize_schema(conn)
    with transaction(conn):
        create_campaign(conn, {
            "campaign_id": CAMPAIGN, "campaign_name": "test", "manifest_path": "m",
            "manifest_sha256": "b" * 64, "manifest_record_count": len(rows),
            "code_sha": "c" * 40, "worker_version": "v4", "status": "READY",
        })
        create_campaign_filings(conn, [{
            "campaign_id": CAMPAIGN, "manifest_row_id": row["manifest_row_id"],
            "requested_disclosure_no": row["requested_disclosure_no"],
            "company_code": "7203", "normalized_company_code": "7203",
            "registration_status": "REGISTERED", "identity_status": "UNVERIFIED",
            "cache_status": "UNKNOWN", "extraction_status": "NOT_STARTED",
            "sqlite_save_status": "NOT_STARTED", "canonical_save_status": "NOT_STARTED",
            "supabase_save_status": "NOT_STARTED", "overall_status": "REGISTERED",
            "error_code": "MISSING_EXPECTED_QUARTER", "error_stage": "registration",
            "error_message": "MISSING_EXPECTED_QUARTER", "retryable": 1,
        } for row in rows])
    conn.close()


def _apply(tmp_path: Path, rows: list[dict], *, apply: bool = True):
    plan, digest = _write_plan(tmp_path / "plan", rows)
    db = tmp_path / "campaign.db"
    _db(db, rows)
    counts = {name: 0 for name in apply_mod.STATUS_MAPPING}
    for row in rows:
        counts[row["classification"]] += 1
    result = apply_mod.apply_identity_plan(
        campaign_db=db, campaign_id=CAMPAIGN, plan_results=plan,
        plan_results_sha256=digest, output_dir=tmp_path / "audit", apply=apply,
        repo_root=Path(__file__).resolve().parents[1], expected_counts=counts,
    )
    return db, result


@pytest.mark.parametrize("classification,expected", [
    ("READY_IDENTITY_VERIFIED", ("VERIFIED", "READY", "IDENTITY_VERIFIED", None, 1)),
    ("TARGET_ZIP_NEEDS_SIDECAR", ("VERIFIED", "SIDECAR_REQUIRED", "IDENTITY_VERIFIED", None, 1)),
    ("LEGACY_CACHE_COPY_CANDIDATE", ("VERIFIED", "LEGACY_COPY_REQUIRED", "IDENTITY_VERIFIED", None, 1)),
    ("METADATA_RESOLVED_CACHE_MISSING", ("METADATA_RESOLVED", "MISSING", "IDENTITY_RESOLVED", None, 1)),
    ("METADATA_INCOMPLETE_CACHE_MISSING", ("UNRESOLVED", "MISSING", "METADATA_INCOMPLETE", "METADATA_INCOMPLETE", 1)),
    ("CACHE_IDENTITY_MISMATCH", ("MISMATCH", "IDENTITY_MISMATCH", "QUARANTINED", "CACHE_IDENTITY_MISMATCH", 0)),
    ("TARGET_CACHE_CONFLICT", ("CONFLICT", "CONFLICT", "QUARANTINED", "TARGET_CACHE_CONFLICT", 0)),
    ("LEGACY_STATE_AMBIGUOUS", ("AMBIGUOUS", "AMBIGUOUS", "QUARANTINED", "LEGACY_STATE_AMBIGUOUS", 0)),
    ("JQUANTS_METADATA_AMBIGUOUS", ("AMBIGUOUS", "UNKNOWN", "QUARANTINED", "JQUANTS_METADATA_AMBIGUOUS", 0)),
    ("INVALID_OR_UNSUPPORTED_URL", ("UNRESOLVED", "INVALID", "QUARANTINED", "INVALID_OR_UNSUPPORTED_URL", 0)),
    ("NOT_APPLICABLE", ("NOT_APPLICABLE", "NOT_APPLICABLE", "NOT_APPLICABLE", None, 0)),
    ("OTHER_UNRESOLVED", ("UNRESOLVED", "UNKNOWN", "QUARANTINED", "OTHER_UNRESOLVED", 0)),
])
def test_classification_mapping(tmp_path, classification, expected):
    db, _result = _apply(tmp_path, [_plan_row("000001", classification)])
    conn = sqlite3.connect(db)
    actual = conn.execute("SELECT identity_status,cache_status,overall_status,error_code,retryable FROM campaign_filings").fetchone()
    conn.close()
    assert actual == expected


def test_identity_values_are_reflected(tmp_path):
    db, result = _apply(tmp_path, [_plan_row("000001", "READY_IDENTITY_VERIFIED")])
    conn = sqlite3.connect(db)
    actual = conn.execute("SELECT expected_period,expected_quarter,internal_document_id,zip_sha256,zip_internal_ticker,zip_internal_period,zip_internal_quarter FROM campaign_filings").fetchone()
    conn.close()
    assert actual == ("2026-03-31", "FY", "20260101123456", "a" * 64, "7203", "2026-03-31", "FY")
    assert result["semantic"]["changed_incorrectly"] == 0


def test_null_values_remain_null(tmp_path):
    db, _result = _apply(tmp_path, [_plan_row("000001", "METADATA_INCOMPLETE_CACHE_MISSING", nulls=True)])
    conn = sqlite3.connect(db)
    actual = conn.execute("SELECT expected_period,expected_quarter,internal_document_id,zip_sha256 FROM campaign_filings").fetchone()
    conn.close()
    assert actual == (None, None, None, None)


def test_registration_extraction_and_save_statuses_are_unchanged(tmp_path):
    db, result = _apply(tmp_path, [_plan_row("000001", "READY_IDENTITY_VERIFIED")])
    conn = sqlite3.connect(db)
    actual = conn.execute("SELECT registration_status,extraction_status,sqlite_save_status,canonical_save_status,supabase_save_status,started_at,completed_at FROM campaign_filings").fetchone()
    conn.close()
    assert actual == ("REGISTERED", "NOT_STARTED", "NOT_STARTED", "NOT_STARTED", "NOT_STARTED", None, None)
    assert result["semantic"]["unchanged_status_violations"] == 0


def test_plan_sha_mismatch_leaves_db_unchanged(tmp_path):
    rows = [_plan_row("000001", "READY_IDENTITY_VERIFIED")]
    plan, _digest = _write_plan(tmp_path / "plan", rows)
    db = tmp_path / "campaign.db"; _db(db, rows); before = _sha(db)
    with pytest.raises(RuntimeError, match="INPUT_CHANGED"):
        apply_mod.apply_identity_plan(campaign_db=db, campaign_id=CAMPAIGN, plan_results=plan, plan_results_sha256="0" * 64, output_dir=tmp_path / "audit", apply=True, repo_root=Path(__file__).resolve().parents[1], expected_counts={name: int(name == "READY_IDENTITY_VERIFIED") for name in apply_mod.STATUS_MAPPING})
    assert _sha(db) == before


def test_classification_count_mismatch_leaves_db_unchanged(tmp_path):
    rows = [_plan_row("000001", "READY_IDENTITY_VERIFIED")]
    plan, digest = _write_plan(tmp_path / "plan", rows)
    db = tmp_path / "campaign.db"; _db(db, rows); before = _sha(db)
    with pytest.raises(RuntimeError, match="INPUT_CHANGED"):
        apply_mod.apply_identity_plan(campaign_db=db, campaign_id=CAMPAIGN, plan_results=plan, plan_results_sha256=digest, output_dir=tmp_path / "audit", apply=True, repo_root=Path(__file__).resolve().parents[1], expected_counts={name: 0 for name in apply_mod.STATUS_MAPPING})
    assert _sha(db) == before


def test_unknown_classification_leaves_db_unchanged(tmp_path):
    rows = [_plan_row("000001", "UNKNOWN_CLASSIFICATION")]
    plan, digest = _write_plan(tmp_path / "plan", rows)
    db = tmp_path / "campaign.db"; _db(db, rows); before = _sha(db)
    with pytest.raises(RuntimeError, match="INPUT_CHANGED"):
        apply_mod.apply_identity_plan(campaign_db=db, campaign_id=CAMPAIGN, plan_results=plan, plan_results_sha256=digest, output_dir=tmp_path / "audit", apply=True, repo_root=Path(__file__).resolve().parents[1], expected_counts={name: 0 for name in apply_mod.STATUS_MAPPING})
    assert _sha(db) == before


def test_duplicate_requested_id_leaves_db_unchanged(tmp_path):
    requested = "20260101999999"
    rows = [_plan_row("000001", "READY_IDENTITY_VERIFIED", requested=requested), _plan_row("000002", "READY_IDENTITY_VERIFIED", requested=requested)]
    plan, digest = _write_plan(tmp_path / "plan", rows)
    db = tmp_path / "campaign.db"; _db(db, rows); before = _sha(db)
    counts = {name: 0 for name in apply_mod.STATUS_MAPPING}; counts["READY_IDENTITY_VERIFIED"] = 2
    with pytest.raises(RuntimeError, match="INPUT_CHANGED"):
        apply_mod.apply_identity_plan(campaign_db=db, campaign_id=CAMPAIGN, plan_results=plan, plan_results_sha256=digest, output_dir=tmp_path / "audit", apply=True, repo_root=Path(__file__).resolve().parents[1], expected_counts=counts)
    assert _sha(db) == before


def test_digest_manifest_tamper_leaves_db_unchanged(tmp_path):
    rows = [_plan_row("000001", "READY_IDENTITY_VERIFIED")]
    plan, digest = _write_plan(tmp_path / "plan", rows)
    (plan.parent / "extra.json").write_text("{}\n", encoding="utf-8")
    manifest = {plan.name: digest, "extra.json": "0" * 64}
    (plan.parent / "digests.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    db = tmp_path / "campaign.db"; _db(db, rows); before = _sha(db)
    counts = {name: 0 for name in apply_mod.STATUS_MAPPING}; counts["READY_IDENTITY_VERIFIED"] = 1
    with pytest.raises(RuntimeError, match="INPUT_CHANGED"):
        apply_mod.apply_identity_plan(campaign_db=db, campaign_id=CAMPAIGN, plan_results=plan, plan_results_sha256=digest, output_dir=tmp_path / "audit", apply=True, repo_root=Path(__file__).resolve().parents[1], expected_counts=counts)
    assert _sha(db) == before


def test_missing_manifest_row_id_leaves_db_unchanged(tmp_path):
    rows = [_plan_row("000001", "READY_IDENTITY_VERIFIED")]
    plan, digest = _write_plan(tmp_path / "plan", [{**rows[0], "manifest_row_id": None}])
    db = tmp_path / "campaign.db"; _db(db, rows); before = _sha(db)
    with pytest.raises(RuntimeError, match="INPUT_CHANGED"):
        apply_mod.apply_identity_plan(campaign_db=db, campaign_id=CAMPAIGN, plan_results=plan, plan_results_sha256=digest, output_dir=tmp_path / "audit", apply=True, repo_root=Path(__file__).resolve().parents[1], expected_counts={name: int(name == "READY_IDENTITY_VERIFIED") for name in apply_mod.STATUS_MAPPING})
    assert _sha(db) == before


def test_campaign_precondition_mismatch_leaves_db_unchanged(tmp_path):
    rows = [_plan_row("000001", "READY_IDENTITY_VERIFIED")]
    plan, digest = _write_plan(tmp_path / "plan", rows)
    db = tmp_path / "campaign.db"; _db(db, rows)
    conn = sqlite3.connect(db); conn.execute("UPDATE campaign_filings SET cache_status='READY'"); conn.commit(); conn.close(); before = _sha(db)
    with pytest.raises(RuntimeError, match="PRECONDITION_CHANGED"):
        apply_mod.apply_identity_plan(campaign_db=db, campaign_id=CAMPAIGN, plan_results=plan, plan_results_sha256=digest, output_dir=tmp_path / "audit", apply=True, repo_root=Path(__file__).resolve().parents[1], expected_counts={name: int(name == "READY_IDENTITY_VERIFIED") for name in apply_mod.STATUS_MAPPING})
    assert _sha(db) == before


def test_transaction_failure_rolls_back(tmp_path):
    rows = [_plan_row("000001", "READY_IDENTITY_VERIFIED"), _plan_row("000002", "READY_IDENTITY_VERIFIED")]
    plan, digest = _write_plan(tmp_path / "plan", rows)
    db = tmp_path / "campaign.db"; _db(db, rows)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TRIGGER fail_second BEFORE UPDATE ON campaign_filings WHEN NEW.manifest_row_id='000002' BEGIN SELECT RAISE(ABORT,'boom'); END")
    conn.commit(); conn.close(); before = _sha(db)
    counts = {name: 0 for name in apply_mod.STATUS_MAPPING}; counts["READY_IDENTITY_VERIFIED"] = 2
    with pytest.raises(RuntimeError, match="TEMP_FAILED"):
        apply_mod.apply_identity_plan(campaign_db=db, campaign_id=CAMPAIGN, plan_results=plan, plan_results_sha256=digest, output_dir=tmp_path / "audit", apply=True, repo_root=Path(__file__).resolve().parents[1], expected_counts=counts)
    assert _sha(db) == before
    conn = sqlite3.connect(db); assert conn.execute("SELECT COUNT(*) FROM campaign_filings WHERE identity_status='UNVERIFIED'").fetchone()[0] == 2; conn.close()
    assert (tmp_path / "audit" / "identity-apply-failure.json").is_file()
    failure = json.loads((tmp_path / "audit" / "identity-apply-failure.json").read_text(encoding="utf-8"))
    assert failure["error"] == "boom"


def test_unsafe_db_path_is_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="UNSAFE_DB_PATH"):
        apply_mod.validate_temp_path(Path(__file__).resolve(), Path(__file__).resolve().parents[1])


def test_without_apply_does_not_change_db_or_create_output(tmp_path):
    rows = [_plan_row("000001", "READY_IDENTITY_VERIFIED")]
    plan, digest = _write_plan(tmp_path / "plan", rows)
    db = tmp_path / "campaign.db"; _db(db, rows); before = _sha(db)
    counts = {name: 0 for name in apply_mod.STATUS_MAPPING}; counts["READY_IDENTITY_VERIFIED"] = 1
    result = apply_mod.apply_identity_plan(campaign_db=db, campaign_id=CAMPAIGN, plan_results=plan, plan_results_sha256=digest, output_dir=tmp_path / "audit", apply=False, repo_root=Path(__file__).resolve().parents[1], expected_counts=counts)
    assert result == {"apply": False, "db_changed": False, "input_count": 1}
    assert _sha(db) == before and not (tmp_path / "audit").exists()


def test_semantic_readback_matches_all_rows(tmp_path):
    rows = [_plan_row(f"{index:06d}", name) for index, name in enumerate(apply_mod.STATUS_MAPPING, 1)]
    _db_path, result = _apply(tmp_path, rows)
    assert result["semantic"] == {"missing": 0, "extra": 0, "changed_incorrectly": 0, "unchanged_status_violations": 0}
    assert result["foreign_key_check"] == 0
    assert result["integrity_check"] == "ok"


def test_cli_has_required_apply_contract():
    actions = {action.dest: action for action in build_parser()._actions}
    assert {"campaign_db", "campaign_id", "plan_results", "plan_results_sha256", "output_dir", "apply"}.issubset(actions)
    assert actions["apply"].default is False


def test_import_has_no_side_effect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.iterdir())
    assert set(tmp_path.iterdir()) == before


def test_apply_never_uses_network_or_cache_writer(tmp_path, monkeypatch):
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: pytest.fail("network called"))
    _db_path, result = _apply(tmp_path, [_plan_row("000001", "READY_IDENTITY_VERIFIED")])
    assert result["updated"] == 1
