"""Tests for the manifest-scoped XBRL cache hydration gate.

All filesystem effects are confined to ``tmp_path``.  No test opens the real
state DB, cache root, network, worker, or canonical writer.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import backfill_segments_tdnet as cli
from src.segment.zip_identity_verifier import TrustedProvenance


def _state_db(path: Path, *rows: tuple[str, str, str]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE filing_state (filing_id TEXT PRIMARY KEY, ticker TEXT, "
            "period TEXT, quarter TEXT, xbrl_url TEXT)"
        )
        conn.executemany("INSERT INTO filing_state VALUES (?, ?, NULL, NULL, ?)", rows)


def _manifest(path: Path, *, filing_id: str = "state-1", requested: str = "20260713591788") -> Path:
    path.write_text(json.dumps([{
        "filing_id": filing_id,
        "requested_disclosure_no": requested,
        "company_code": "4057",
        "expected_period": "2026-05-31",
        "expected_quarter": "FY",
    }]), encoding="utf-8")
    return path


def _provenance(path: Path, record: dict[str, object], *, state_filing_id: str):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return TrustedProvenance(
        source="jquants", requested_disclosure_no=str(record["requested_disclosure_no"]),
        requested_file_type="x", resolved_by_function=f"manifest_cache_hydration/{state_filing_id}/official_linked_xbrl_match",
        official_request_succeeded=True, response_status=200, downloaded_size=path.stat().st_size,
        downloaded_sha256=digest, internal_document_id="20260713340570", ticker="4057",
        period="2026-05-31", quarter="FY", document_type="financial_statement", resolved_at="2026-07-16T00:00:00Z",
    )


@pytest.fixture
def hydration_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_db = tmp_path / "state.db"
    _state_db(state_db, ("state-1", "4057", "https://www.release.tdnet.info/inbs/081220260713591788.zip"))
    manifest = _manifest(tmp_path / "manifest.json")
    cache = tmp_path / "cache"
    monkeypatch.setattr(cli, "_canonical_metadata_for_hydration", lambda records: {"ok": object()})
    monkeypatch.setattr(
        cli,
        "_verify_hydration_zip",
        lambda path, record, *, state_filing_id: (_provenance(path, record, state_filing_id=state_filing_id), SimpleNamespace(passed=True, verdict="official_linked_xbrl_match")),
    )
    return state_db, manifest, cache


def test_plan_copy_is_read_only(hydration_env):
    state_db, manifest, cache = hydration_env
    source = cache / "state-1" / "xbrl.zip"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fixture-zip")
    before = source.read_bytes()

    result = cli._hydrate_manifest_cache(mode="plan", filing_list_path=str(manifest), state_db_path=str(state_db), cache_root=str(cache))

    assert result["network_calls"] == 0
    assert result["writes"] == 0
    assert result["plans"][0]["action"] == "COPY_FROM_STATE_FILING_CACHE"
    assert source.read_bytes() == before
    assert not (cache / "20260713591788").exists()


def test_apply_copies_zip_and_existing_sidecar_schema(hydration_env):
    state_db, manifest, cache = hydration_env
    source = cache / "state-1" / "xbrl.zip"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fixture-zip")

    result = cli._hydrate_manifest_cache(mode="apply", filing_list_path=str(manifest), state_db_path=str(state_db), cache_root=str(cache))

    target = cache / "20260713591788" / "xbrl.zip"
    sidecar = Path(str(target) + ".provenance.json")
    assert result["writes"] == 2
    assert result["network_calls"] == 0
    assert target.read_bytes() == b"fixture-zip"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["requested_disclosure_no"] == "20260713591788"
    assert data["source"] == "jquants"
    assert data["resolved_by_function"].endswith("official_linked_xbrl_match")
    assert not (cache / ".manifest_cache_hydration.lock").exists()


def test_plan_does_not_create_cache_root(hydration_env):
    state_db, manifest, cache = hydration_env
    result = cli._hydrate_manifest_cache(mode="plan", filing_list_path=str(manifest), state_db_path=str(state_db), cache_root=str(cache))
    assert result["writes"] == 0
    assert not cache.exists()


def test_requested_id_collision_is_rejected_globally(hydration_env):
    state_db, manifest, cache = hydration_env
    with sqlite3.connect(state_db) as conn:
        conn.execute("INSERT INTO filing_state VALUES (?, ?, NULL, NULL, ?)", ("other", "9999", "https://www.release.tdnet.info/inbs/081220260713591788.zip"))
    with pytest.raises(cli.CacheHydrationStop) as exc_info:
        cli._hydrate_manifest_cache(mode="plan", filing_list_path=str(manifest), state_db_path=str(state_db), cache_root=str(cache))
    assert exc_info.value.code == "STOP_BACKFILL_CACHE_HYDRATION_REQUESTED_ID_COLLISION"


@pytest.mark.parametrize("mutator", [
    lambda row: row.pop("expected_period"),
    lambda row: row.update(requested_disclosure_no="bad"),
    lambda row: row.update(company_code=""),
])
def test_manifest_identity_fields_are_required(tmp_path: Path, mutator):
    record = {"filing_id": "one", "requested_disclosure_no": "20260713591788", "company_code": "4057", "expected_period": "2026-05-31", "expected_quarter": "FY"}
    mutator(record)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps([record]), encoding="utf-8")
    with pytest.raises(cli.CacheHydrationStop) as exc_info:
        cli._load_hydration_manifest(str(path))
    assert exc_info.value.code == "STOP_BACKFILL_CACHE_HYDRATION_MANIFEST_INVALID"


@pytest.mark.parametrize("flag", ["--apply", "--dry-run", "--resume", "--isolated-worker-dry-run"])
def test_cli_rejects_worker_modes_before_hydration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, flag: str):
    manifest = _manifest(tmp_path / "manifest.json")
    monkeypatch.setattr(sys, "argv", ["backfill_segments_tdnet.py", "--hydrate-manifest-cache", "plan", "--filing-list", str(manifest), "--workers", "1", flag])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1
    assert "STOP_BACKFILL_CACHE_HYDRATION_INVALID_MODE" in capsys.readouterr().err


def test_cli_requires_one_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    manifest = _manifest(tmp_path / "manifest.json")
    monkeypatch.setattr(sys, "argv", ["backfill_segments_tdnet.py", "--hydrate-manifest-cache", "plan", "--filing-list", str(manifest), "--workers", "2"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "STOP_BACKFILL_CACHE_HYDRATION_INVALID_MODE" in capsys.readouterr().err


def test_target_conflict_never_overwrites_target(hydration_env):
    state_db, manifest, cache = hydration_env
    target = cache / "20260713591788" / "xbrl.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")
    original = target.read_bytes()

    def reject(*args, **kwargs):
        raise cli.CacheHydrationStop(cli._CACHE_HYDRATION_IDENTITY_REJECTED)

    # The fixture's verifier is replaced only for this target-conflict case.
    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli, "_verify_hydration_zip", reject)
        with _pytest.raises(cli.CacheHydrationStop) as exc_info:
            cli._hydrate_manifest_cache(mode="apply", filing_list_path=str(manifest), state_db_path=str(state_db), cache_root=str(cache))
    assert exc_info.value.code == "STOP_BACKFILL_CACHE_HYDRATION_TARGET_CONFLICT"
    assert target.read_bytes() == original


def test_already_ready_is_an_apply_noop(hydration_env):
    state_db, manifest, cache = hydration_env
    target = cache / "20260713591788" / "xbrl.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"ready")
    Path(str(target) + ".provenance.json").write_text("{}", encoding="utf-8")
    from src.segment import segment_zip_resolver
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(segment_zip_resolver, "_load_sidecar_provenance", lambda *args: _provenance(target, {"requested_disclosure_no": "20260713591788"}, state_filing_id="state-1"))
        result = cli._hydrate_manifest_cache(mode="apply", filing_list_path=str(manifest), state_db_path=str(state_db), cache_root=str(cache))
    assert result["plans"][0]["action"] == "ALREADY_READY"
    assert result["writes"] == 0
    assert target.read_bytes() == b"ready"


def test_download_path_is_official_and_atomic(hydration_env, monkeypatch: pytest.MonkeyPatch):
    state_db, manifest, cache = hydration_env
    calls: list[str] = []

    def download(url: str, destination: Path):
        calls.append(url)
        destination.write_bytes(b"downloaded")

    monkeypatch.setattr(cli, "_download_official_xbrl_zip", download)
    result = cli._hydrate_manifest_cache(mode="apply", filing_list_path=str(manifest), state_db_path=str(state_db), cache_root=str(cache))
    assert result["plans"][0]["action"] == "DOWNLOAD_REQUIRED"
    assert result["network_calls"] == 1
    assert calls == ["https://www.release.tdnet.info/inbs/081220260713591788.zip"]
    assert (cache / "20260713591788" / "xbrl.zip").read_bytes() == b"downloaded"
    assert not list(cache.glob(".*.hydrate-*"))


@pytest.mark.parametrize("url", [
    "https://example.com/file.zip",
    "https://www.release.tdnet.info/file.pdf",
    "http://www.release.tdnet.info/file.zip",
])
def test_nonofficial_or_pdf_download_url_is_rejected(url: str):
    with pytest.raises(cli.CacheHydrationStop) as exc_info:
        cli._official_xbrl_url(url)
    assert exc_info.value.code == "STOP_BACKFILL_CACHE_HYDRATION_DOWNLOAD_URL_INVALID"


def test_missing_state_filing_is_rejected(hydration_env):
    state_db, manifest, cache = hydration_env
    with sqlite3.connect(state_db) as conn:
        conn.execute("DELETE FROM filing_state")
    with pytest.raises(cli.CacheHydrationStop) as exc_info:
        cli._hydrate_manifest_cache(mode="plan", filing_list_path=str(manifest), state_db_path=str(state_db), cache_root=str(cache))
    assert exc_info.value.code == "STOP_BACKFILL_CACHE_HYDRATION_MANIFEST_INVALID"


@pytest.mark.parametrize("field,value", [
    ("ticker", "9999"),
    ("period", "2027-05-31"),
    ("quarter", "1Q"),
])
def test_state_identity_mismatch_is_rejected(hydration_env, field: str, value: str):
    state_db, manifest, cache = hydration_env
    with sqlite3.connect(state_db) as conn:
        conn.execute(f"UPDATE filing_state SET {field} = ? WHERE filing_id = 'state-1'", (value,))
    with pytest.raises(cli.CacheHydrationStop) as exc_info:
        cli._hydrate_manifest_cache(mode="plan", filing_list_path=str(manifest), state_db_path=str(state_db), cache_root=str(cache))
    assert exc_info.value.code == "STOP_BACKFILL_CACHE_HYDRATION_MANIFEST_INVALID"


def test_metadata_unresolved_is_rejected(hydration_env, monkeypatch: pytest.MonkeyPatch):
    state_db, manifest, cache = hydration_env
    monkeypatch.setattr(cli, "_canonical_metadata_for_hydration", lambda records: (_ for _ in ()).throw(cli.CacheHydrationStop(cli._CACHE_HYDRATION_METADATA_UNRESOLVED)))
    with pytest.raises(cli.CacheHydrationStop) as exc_info:
        cli._hydrate_manifest_cache(mode="plan", filing_list_path=str(manifest), state_db_path=str(state_db), cache_root=str(cache))
    assert exc_info.value.code == "STOP_BACKFILL_CACHE_HYDRATION_METADATA_UNRESOLVED"


def test_apply_releases_lock_when_copy_fails(hydration_env, monkeypatch: pytest.MonkeyPatch):
    state_db, manifest, cache = hydration_env
    source = cache / "state-1" / "xbrl.zip"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    monkeypatch.setattr(cli, "_verify_hydration_zip", lambda *a, **k: (_ for _ in ()).throw(cli.CacheHydrationStop(cli._CACHE_HYDRATION_IDENTITY_REJECTED)))
    with pytest.raises(cli.CacheHydrationStop):
        cli._hydrate_manifest_cache(mode="apply", filing_list_path=str(manifest), state_db_path=str(state_db), cache_root=str(cache))
    assert not (cache / ".manifest_cache_hydration.lock").exists()
    assert not list(cache.glob(".*.hydrate-*"))
