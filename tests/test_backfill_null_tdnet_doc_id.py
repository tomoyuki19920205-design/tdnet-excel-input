from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tools.backfill_null_tdnet_doc_id import RepairContractError, file_sha256, run_repair


def _db(path: Path, *, duplicate: bool = False, non_null: str | None = None) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE segment_financials (id INTEGER PRIMARY KEY, company_code TEXT, fiscal_year_end TEXT, quarter TEXT, segment_name TEXT, segment_sales REAL, segment_profit REAL, tdnet_doc_id TEXT, untouched TEXT)")
    con.execute("INSERT INTO segment_financials VALUES (1,'1332','2025-03-31','FY','Marine Products',380824,8418,?,'keep')", (non_null,))
    con.execute("INSERT INTO segment_financials VALUES (2,'9999','2025-03-31','FY','Other',1,2,NULL,'outside')")
    if duplicate:
        con.execute("INSERT INTO segment_financials VALUES (3,'1332','2025-03-31','FY','Marine Products',380824,8418,NULL,'duplicate')")
    con.commit(); con.close(); return path


def _manifest(path: Path, **updates) -> Path:
    row = {"row_id": 1, "company_code": "1332", "fiscal_year_end": "2025-03-31", "quarter": "FY", "segment_name": "Marine Products", "segment_sales": 380824.0, "segment_profit": 8418.0, "expected_tdnet_doc_id": "20250514313320"}
    row.update(updates)
    path.write_text(json.dumps({"rows": [row]}), encoding="utf-8")
    return path


def test_plan_and_apply_are_limited_and_idempotent(tmp_path):
    db = _db(tmp_path / "decision.db"); manifest = _manifest(tmp_path / "repair.json")
    plan = run_repair(db, manifest, 1, apply=False, expected_db_sha256=file_sha256(db))
    assert (plan["pending_updates"], plan["updated_rows"]) == (1, 0)
    before = sqlite3.connect(db).execute("SELECT * FROM segment_financials ORDER BY id").fetchall()
    applied = run_repair(db, manifest, 1, apply=True, confirm_count=1)
    after = sqlite3.connect(db).execute("SELECT * FROM segment_financials ORDER BY id").fetchall()
    assert applied["updated_rows"] == 1 and applied["inserted_rows"] == applied["deleted_rows"] == 0
    assert applied["database_sha256_after"] == file_sha256(db)
    assert applied["database_sha256_after"] != applied["database_sha256_before"]
    assert after[0][:-2] == before[0][:-2] and after[0][-1] == before[0][-1]
    assert after[0][-2] == "20250514313320" and after[1] == before[1]
    second = run_repair(db, manifest, 1, apply=False)
    assert second["pending_updates"] == 0 and second["already_completed"] == 1


@pytest.mark.parametrize("field,value", [("segment_sales", 1.0), ("segment_profit", 1.0), ("segment_name", "Mismatch")])
def test_value_or_natural_key_mismatch_is_rejected(tmp_path, field, value):
    db = _db(tmp_path / "decision.db"); manifest = _manifest(tmp_path / "repair.json", **{field: value})
    with pytest.raises(RepairContractError, match="row_contract_mismatch"):
        run_repair(db, manifest, 1, apply=False)


def test_duplicate_natural_key_is_rejected(tmp_path):
    db = _db(tmp_path / "decision.db", duplicate=True); manifest = _manifest(tmp_path / "repair.json")
    with pytest.raises(RepairContractError, match="natural_key_duplicate"):
        run_repair(db, manifest, 1, apply=False)


def test_non_null_different_document_id_is_rejected(tmp_path):
    db = _db(tmp_path / "decision.db", non_null="different"); manifest = _manifest(tmp_path / "repair.json")
    with pytest.raises(RepairContractError, match="non_null_document_id_conflict"):
        run_repair(db, manifest, 1, apply=False)


def test_expected_count_and_confirmation_are_fail_closed(tmp_path):
    db = _db(tmp_path / "decision.db"); manifest = _manifest(tmp_path / "repair.json")
    with pytest.raises(RepairContractError, match="expected_row_count_mismatch"):
        run_repair(db, manifest, 2, apply=False)
    with pytest.raises(RepairContractError, match="apply_confirmation_mismatch"):
        run_repair(db, manifest, 1, apply=True, confirm_count=2)


def test_database_digest_mismatch_stops_before_update(tmp_path):
    db = _db(tmp_path / "decision.db"); manifest = _manifest(tmp_path / "repair.json")
    before = db.read_bytes()
    with pytest.raises(RepairContractError, match="database_sha256_mismatch"):
        run_repair(db, manifest, 1, apply=True, confirm_count=1, expected_db_sha256="0" * 64)
    assert db.read_bytes() == before


def test_apply_to_repository_production_path_is_forbidden(tmp_path, monkeypatch):
    db = _db(tmp_path / "decision.db"); manifest = _manifest(tmp_path / "repair.json")
    monkeypatch.setattr("tools.backfill_null_tdnet_doc_id._production_db_path", lambda _: True)
    with pytest.raises(RepairContractError, match="production_path_apply_forbidden"):
        run_repair(db, manifest, 1, apply=True, confirm_count=1)
