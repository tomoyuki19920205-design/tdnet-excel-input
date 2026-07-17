from __future__ import annotations

import json
from pathlib import Path

import pytest

import tools.backfill_campaign_register as register_mod


def _campaign() -> dict:
    return {"campaign_id": "v4-test", "campaign_name": "test", "manifest_path": "manifest.json", "manifest_sha256": "a" * 64, "manifest_record_count": 2, "code_sha": "b" * 40, "worker_version": "v4"}


def _candidate(row_id: str, requested: str = "20260101000000", ticker: str = "143A") -> dict:
    return {"campaign_id": "v4-test", "manifest_row_id": row_id, "state_filing_id": None, "requested_disclosure_no": requested, "company_code": ticker, "normalized_company_code": ticker, "source_url": "https://www.release.tdnet.info/inbs/140120260101000000.pdf", "normalized_xbrl_url": "https://www.release.tdnet.info/inbs/081220260101000000.zip", "disclosure_date": "2026-01-01", "expected_period": "2026-12-31", "expected_quarter": None, "document_type": "financial_statement", "internal_document_id": None, "zip_sha256": None, "zip_internal_ticker": None, "zip_internal_period": None, "zip_internal_quarter": None, "run_id": None, "worker_version": "v4", "extractor_version": None, "extractor_route": None, "code_sha": "b" * 40, "registration_status": "REGISTERED", "identity_status": "UNVERIFIED", "cache_status": "UNKNOWN", "extraction_status": "NOT_STARTED", "sqlite_save_status": "NOT_STARTED", "canonical_save_status": "NOT_STARTED", "supabase_save_status": "NOT_STARTED", "overall_status": "REGISTERED", "error_code": "MISSING_EXPECTED_QUARTER", "error_stage": "registration", "error_message": "MISSING_EXPECTED_QUARTER", "retryable": True, "classification": "METADATA_INCOMPLETE"}


def _patch_input(monkeypatch, candidates: list[dict]):
    monkeypatch.setattr(register_mod, "load_candidates", lambda _path: (_campaign(), candidates, {"input_count": len(candidates)}))
    monkeypatch.setattr(register_mod, "_git_provenance", lambda _root: {"git_head": "c" * 40, "git_branch": "feature", "working_tree_code_present": False, "tracked_diff_present": False, "staged_diff_present": False, "registration_tool_code_sha": "c" * 40})


def test_registers_candidates_and_preserves_null_and_status(monkeypatch, tmp_path):
    candidates = [_candidate("0000000001"), _candidate("0000000002", ticker="2000")]
    _patch_input(monkeypatch, candidates)
    result = register_mod.register(campaign_dir=tmp_path, db_path=tmp_path / "v4_campaign.db", output_dir=tmp_path / "audit", apply=True)
    assert result["campaign_count"] == 1
    assert result["filing_count"] == 2
    assert result["expected_quarter_null"] == 2
    assert result["semantic_changed_rows"] == 0
    assert result["integrity_check"] == "ok"


def test_requested_id_duplicates_are_allowed(monkeypatch, tmp_path):
    candidates = [_candidate("0000000001"), _candidate("0000000002", ticker="2000")]
    _patch_input(monkeypatch, candidates)
    result = register_mod.register(campaign_dir=tmp_path, db_path=tmp_path / "v4_campaign.db", output_dir=tmp_path / "audit", apply=True)
    assert result["filing_count"] == 2
    assert result["requested_id_distinct"] == 1


def test_double_registration_is_rejected(monkeypatch, tmp_path):
    _patch_input(monkeypatch, [_candidate("0000000001")])
    db = tmp_path / "v4_campaign.db"
    register_mod.register(campaign_dir=tmp_path, db_path=db, output_dir=tmp_path / "a", apply=True)
    with pytest.raises(RuntimeError, match="ALREADY_REGISTERED"):
        register_mod.register(campaign_dir=tmp_path, db_path=db, output_dir=tmp_path / "b", apply=True)


def test_without_apply_does_not_create_db(monkeypatch, tmp_path):
    _patch_input(monkeypatch, [_candidate("0000000001")])
    db = tmp_path / "v4_campaign.db"
    result = register_mod.register(campaign_dir=tmp_path, db_path=db, output_dir=tmp_path / "audit", apply=False)
    assert result["db_created"] is False
    assert not db.exists()


def test_unsafe_db_path_is_rejected(monkeypatch, tmp_path):
    _patch_input(monkeypatch, [_candidate("0000000001")])
    with pytest.raises(RuntimeError, match="UNSAFE_DB_PATH"):
        register_mod.register(campaign_dir=tmp_path, db_path=Path(__file__).resolve(), output_dir=tmp_path / "audit", apply=True)


def test_dirty_worktree_is_rejected_before_database(monkeypatch, tmp_path):
    _patch_input(monkeypatch, [_candidate("0000000001")])
    monkeypatch.setattr(register_mod, "_git_provenance", lambda _root: (_ for _ in ()).throw(RuntimeError(register_mod.STOP_DIRTY)))
    db = tmp_path / "v4_campaign.db"
    with pytest.raises(RuntimeError, match="DIRTY_WORKTREE"):
        register_mod.register(campaign_dir=tmp_path, db_path=db, output_dir=tmp_path / "audit", apply=True)
    assert not db.exists()


def test_import_has_no_side_effect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.iterdir())
    assert set(tmp_path.iterdir()) == before
