from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from lib.backfill.fresh_campaign_manifest_adapter import AdapterValidationError, build


def _fixture(tmp_path: Path, *, state_filing_id: str | None = "state-2286", expected_period: str = "2026-03-31", expected_quarter: str = "2Q", artifact_period: str = "2026-03-31", artifact_quarter: str = "2Q", provenance_period: str = "2026-03-31", provenance_quarter: str = "2Q") -> tuple[Path, Path, Path, Path]:
    cache = tmp_path / "cache"
    row_dir = cache / "0000002286"
    row_dir.mkdir(parents=True)
    zip_path = row_dir / "xbrl.zip"
    zip_path.write_bytes(b"fixture zip")
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    provenance = {
        "zip_sha256": digest, "zip_internal_period": provenance_period,
        "zip_internal_quarter": provenance_quarter, "zip_internal_ticker": "2130",
        "requested_disclosure_no": "20251029581610",
    }
    provenance_path = row_dir / "provenance.json"
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    db = tmp_path / "campaign.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE campaign_filings (campaign_id TEXT, manifest_row_id TEXT,
          state_filing_id TEXT, requested_disclosure_no TEXT, company_code TEXT,
          expected_period TEXT, expected_quarter TEXT, disclosure_date TEXT,
          source_url TEXT, normalized_xbrl_url TEXT, document_type TEXT,
          internal_document_id TEXT, zip_internal_period TEXT, zip_internal_quarter TEXT);
        CREATE TABLE campaign_fresh_downloads (campaign_id TEXT, manifest_row_id TEXT,
          fresh_status TEXT, target_zip_path TEXT, target_provenance_path TEXT,
          artifact_zip_sha256 TEXT, artifact_internal_document_id TEXT,
          artifact_ticker TEXT, artifact_period TEXT, artifact_quarter TEXT,
          identity_verdict TEXT, source_route TEXT);
    """)
    conn.execute("INSERT INTO campaign_filings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        "campaign", "0000002286", state_filing_id, "20251029581610", "2130",
        expected_period, expected_quarter, "2025-10-31", "https://example.test/doc",
        "https://example.test/xbrl", "financial_statement", None, None, None,
    ))
    conn.execute("INSERT INTO campaign_fresh_downloads VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
        "campaign", "0000002286", "COMPLETE", str(zip_path), str(provenance_path),
        digest, None, "2130", artifact_period, artifact_quarter,
        "official_linked_xbrl_match_without_internal_id", "JQUANTS_TD_FILES",
    ))
    conn.commit(); conn.close()
    source_manifest = tmp_path / "source.json"
    source_manifest.write_text(json.dumps({"filings": [{
        "filing_id": "jquants_20251029581610", "requested_disclosure_no": "20251029581610",
        "ticker": "2130", "title": "第2四半期決算短信", "disclosure_date": "2025-10-31",
        "doc_url": "https://example.test/doc",
    }]}), encoding="utf-8")
    state_db = tmp_path / "state.db"
    state = sqlite3.connect(state_db)
    state.execute("CREATE TABLE filing_state (filing_id TEXT PRIMARY KEY)")
    state.commit(); state.close()
    return db, cache, source_manifest, state_db


def test_2q_fiscal_year_end_differs_from_current_period_end_but_is_accepted(tmp_path: Path) -> None:
    db, cache, source, state = _fixture(tmp_path)
    row = build(db, "campaign", cache, source_manifest=source, state_db=state, expected_count=1)[0]
    assert row["expected_period"] == "2026-03-31"
    assert row["expected_quarter"] == "2Q"


def test_fy_is_accepted_when_fiscal_and_current_ends_coincide(tmp_path: Path) -> None:
    db, cache, source, state = _fixture(tmp_path, expected_quarter="FY", artifact_quarter="FY", provenance_quarter="FY")
    assert build(db, "campaign", cache, source_manifest=source, state_db=state, expected_count=1)[0]["expected_quarter"] == "FY"


@pytest.mark.parametrize("field, value, message", [
    ("artifact_period", "2025-03-31", "fiscal year end conflict"),
    ("artifact_quarter", "HY", "canonical quarter conflict"),
])
def test_true_artifact_identity_conflicts_fail_closed(tmp_path: Path, field: str, value: str, message: str) -> None:
    args = {field: value}
    db, cache, source, state = _fixture(tmp_path, **args)
    with pytest.raises(AdapterValidationError, match=message):
        build(db, "campaign", cache, source_manifest=source, state_db=state, expected_count=1)


def test_provenance_missing_period_fails_closed(tmp_path: Path) -> None:
    db, cache, source, state = _fixture(tmp_path, provenance_period="")
    with pytest.raises(AdapterValidationError, match="fiscal year end conflict"):
        build(db, "campaign", cache, source_manifest=source, state_db=state, expected_count=1)


def test_requested_id_is_not_synthesized(tmp_path: Path) -> None:
    db, cache, source, state = _fixture(tmp_path)
    provenance_path = cache / "0000002286" / "provenance.json"
    payload = json.loads(provenance_path.read_text(encoding="utf-8"))
    payload["requested_disclosure_no"] = ""
    provenance_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AdapterValidationError, match="requested disclosure mismatch"):
        build(db, "campaign", cache, source_manifest=source, state_db=state, expected_count=1)


def test_source_title_is_required_for_formal_filing_id(tmp_path: Path) -> None:
    db, cache, source, state = _fixture(tmp_path)
    source.write_text(json.dumps({"filings": []}), encoding="utf-8")
    with pytest.raises(AdapterValidationError, match="source manifest row missing"):
        build(db, "campaign", cache, source_manifest=source, state_db=state, expected_count=1)


def test_existing_state_filing_id_collision_fails_closed(tmp_path: Path) -> None:
    db, cache, source, state = _fixture(tmp_path)
    formal_id = ""  # populate through one accepted deterministic build first
    formal_id = build(db, "campaign", cache, source_manifest=source, state_db=state, expected_count=1)[0]["filing_id"]
    conn = sqlite3.connect(state); conn.execute("INSERT INTO filing_state VALUES (?)", (formal_id,)); conn.commit(); conn.close()
    with pytest.raises(AdapterValidationError, match="existing state filing id collision"):
        build(db, "campaign", cache, source_manifest=source, state_db=state, expected_count=1)
