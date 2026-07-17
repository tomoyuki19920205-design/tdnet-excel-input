from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import lib.backfill.campaign_cache_publish as publisher
from lib.backfill.campaign_state import (
    connect_db, create_campaign, create_campaign_filing, initialize_schema, transaction,
)


CAMPAIGN_ID = "v4-jquants-3y-20260710"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_zip(path: Path, *, ticker: str, internal: str, period: str, quarter: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    q = {"1Q": "1", "2Q": "2", "3Q": "3", "FY": "4"}[quarter]
    content = (
        f'<xbrli:identifier scheme="test-sicc">{ticker}</xbrli:identifier>'
        f'<xbrli:endDate>{period}</xbrli:endDate>'
        f'<tse-ed-t:QuarterlyPeriod>{q}</tse-ed-t:QuarterlyPeriod>'
        + ("AnnualMember" if quarter == "FY" else "")
    )
    name = f"XBRLData/Summary/tse-ed-t-{ticker}0-{internal}-summary.htm"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, content)


def _row(index: int, classification: str, *, ticker: str | None = None) -> dict[str, object]:
    ticker = ticker or f"{1300 + index}"
    requested = f"20260717{100000 + index:06d}"
    internal = f"20260718{130000 + index:06d}"
    period = "2026-05-31"
    quarter = "FY"
    target = f"{requested}/xbrl.zip"
    legacy = f"legacy-{index}/xbrl.zip" if classification == "LEGACY_CACHE_COPY_CANDIDATE" else ""
    return {
        "campaign_id": CAMPAIGN_ID,
        "manifest_row_id": f"{index:010d}",
        "requested_disclosure_no": requested,
        "classification": classification,
        "target_relative_path": target,
        "legacy_relative_path": legacy,
        "expected_identity": {
            "normalized_company_code": ticker,
            "expected_period": period,
            "expected_quarter": quarter,
        },
        "actual_identity": {
            "ticker": ticker, "period": period, "quarter": quarter,
            "document_type": "attachment_xbrl", "internal_document_id": internal,
            "zip_sha256": "",
        },
    }


def _prepare(tmp_path: Path, classes: list[str]) -> dict[str, object]:
    root = tmp_path / "run"
    cache = root / "cache"
    db = root / "db" / "campaign.db"
    output = root / "results"
    manifest = root / "input" / "manifest.json"
    cache.mkdir(parents=True)
    db.parent.mkdir(parents=True)
    manifest.parent.mkdir(parents=True)
    rows = [_row(i + 1, classification) for i, classification in enumerate(classes)]
    for row in rows:
        actual = row["actual_identity"]
        assert isinstance(actual, dict)
        rel = row["legacy_relative_path"] if row["classification"] == "LEGACY_CACHE_COPY_CANDIDATE" else row["target_relative_path"]
        zip_path = cache / str(rel)
        _write_zip(
            zip_path, ticker=str(actual["ticker"]), internal=str(actual["internal_document_id"]),
            period=str(actual["period"]), quarter=str(actual["quarter"]),
        )
        actual["zip_sha256"] = _sha(zip_path)
    conn = connect_db(db)
    initialize_schema(conn)
    with transaction(conn):
        create_campaign(conn, {
            "campaign_id": CAMPAIGN_ID, "campaign_name": "test", "manifest_path": str(manifest),
            "manifest_sha256": "0" * 64, "manifest_record_count": len(rows),
            "code_sha": "a" * 40, "worker_version": "v4", "status": "READY",
        })
        for row in rows:
            actual = row["actual_identity"]
            expected = row["expected_identity"]
            assert isinstance(actual, dict) and isinstance(expected, dict)
            create_campaign_filing(conn, {
                "campaign_id": CAMPAIGN_ID, "manifest_row_id": row["manifest_row_id"],
                "requested_disclosure_no": row["requested_disclosure_no"],
                "company_code": actual["ticker"], "normalized_company_code": actual["ticker"],
                "expected_period": expected["expected_period"], "expected_quarter": expected["expected_quarter"],
                "internal_document_id": actual["internal_document_id"], "zip_sha256": actual["zip_sha256"],
                "zip_internal_ticker": actual["ticker"], "zip_internal_period": actual["period"],
                "zip_internal_quarter": actual["quarter"], "identity_status": "VERIFIED",
                "cache_status": publisher.CLASS_TO_STATUS[str(row["classification"])],
                "overall_status": "IDENTITY_VERIFIED", "retryable": 1,
            })
    conn.close()
    # READY includes an already valid sidecar.
    for row in rows:
        if row["classification"] == "READY_IDENTITY_VERIFIED":
            target = cache / str(row["target_relative_path"])
            _, prov = publisher._metadata(target, row)
            publisher._publish_sidecar(target, prov)
    manifest.write_bytes(publisher._json_bytes({"campaign_id": CAMPAIGN_ID, "rows": rows}))
    return {"root": root, "cache": cache, "db": db, "output": output, "manifest": manifest, "rows": rows}


def _run(env: dict[str, object], *, apply: bool = True) -> dict[str, object]:
    return publisher.publish_campaign_cache(
        campaign_db=env["db"], campaign_id=CAMPAIGN_ID, cache_root=env["cache"],
        manifest_list=env["manifest"], output_dir=env["output"], apply=apply,
        repo_root=REPO_ROOT,
    )


def _db_row(db: Path, row_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM campaign_filings WHERE manifest_row_id=?", (row_id,)).fetchone()
    conn.close()
    assert row is not None
    return row


def test_dry_run_changes_nothing(tmp_path):
    env = _prepare(tmp_path, ["TARGET_ZIP_NEEDS_SIDECAR"])
    before = _sha(env["db"])
    result = _run(env, apply=False)
    assert result["changed"] is False
    assert _sha(env["db"]) == before
    assert not env["output"].exists()
    assert not Path(str(env["cache"] / env["rows"][0]["target_relative_path"]) + ".provenance.json").exists()


def test_sidecar_required_publishes_sidecar_then_db(tmp_path):
    env = _prepare(tmp_path, ["TARGET_ZIP_NEEDS_SIDECAR"])
    result = _run(env)
    row = env["rows"][0]
    sidecar = Path(str(env["cache"] / row["target_relative_path"]) + ".provenance.json")
    assert sidecar.is_file()
    assert result["published"] == 1 and result["sidecars_published"] == 1
    assert _db_row(env["db"], row["manifest_row_id"])["cache_status"] == "READY"


def test_legacy_copy_publishes_zip_and_sidecar(tmp_path):
    env = _prepare(tmp_path, ["LEGACY_CACHE_COPY_CANDIDATE"])
    result = _run(env)
    row = env["rows"][0]
    target = env["cache"] / row["target_relative_path"]
    source = env["cache"] / row["legacy_relative_path"]
    assert target.is_file() and _sha(target) == _sha(source)
    assert Path(str(target) + ".provenance.json").is_file()
    assert result["zip_published"] == 1


def test_ready_is_unchanged(tmp_path):
    env = _prepare(tmp_path, ["READY_IDENTITY_VERIFIED"])
    row = env["rows"][0]
    target = env["cache"] / row["target_relative_path"]
    sidecar = Path(str(target) + ".provenance.json")
    before = (_sha(env["db"]), _sha(target), _sha(sidecar))
    result = _run(env)
    assert result["already_ready"] == 1 and result["published"] == 0
    assert (_sha(env["db"]), _sha(target), _sha(sidecar)) == before


def test_mixed_canary_counts(tmp_path):
    env = _prepare(tmp_path, [
        "TARGET_ZIP_NEEDS_SIDECAR", "TARGET_ZIP_NEEDS_SIDECAR", "TARGET_ZIP_NEEDS_SIDECAR",
        "LEGACY_CACHE_COPY_CANDIDATE", "LEGACY_CACHE_COPY_CANDIDATE", "LEGACY_CACHE_COPY_CANDIDATE",
        "READY_IDENTITY_VERIFIED",
    ])
    result = _run(env)
    assert (result["requested"], result["published"], result["already_ready"], result["failed"]) == (7, 6, 1, 0)
    assert result["cache_status"] == {"READY": 7}
    assert result["network_calls"] == result["download_calls"] == 0


def test_sidecar_is_last_file_replace(tmp_path, monkeypatch):
    env = _prepare(tmp_path, ["LEGACY_CACHE_COPY_CANDIDATE"])
    calls = []
    original = os.replace
    monkeypatch.setattr(publisher.os, "replace", lambda source, target: (calls.append((Path(source), Path(target))), original(source, target))[1])
    _run(env)
    assert calls[-1][1].name == "xbrl.zip.provenance.json"
    assert calls[0][1].name == "xbrl.zip"


def test_os_replace_operands_are_files(tmp_path, monkeypatch):
    env = _prepare(tmp_path, ["LEGACY_CACHE_COPY_CANDIDATE"])
    original = os.replace
    def checked(source, target):
        assert Path(source).is_file()
        assert not Path(source).is_dir() and not Path(target).is_dir()
        return original(source, target)
    monkeypatch.setattr(publisher.os, "replace", checked)
    _run(env)


def test_zip_publish_failure_leaves_no_sidecar(tmp_path, monkeypatch):
    env = _prepare(tmp_path, ["LEGACY_CACHE_COPY_CANDIDATE"])
    row = env["rows"][0]
    target = env["cache"] / row["target_relative_path"]
    original = os.replace
    def fail_zip(source, destination):
        if Path(destination).name == "xbrl.zip":
            raise OSError("injected zip publish failure")
        return original(source, destination)
    monkeypatch.setattr(publisher.os, "replace", fail_zip)
    with pytest.raises(OSError, match="injected"):
        _run(env)
    assert not target.exists()
    assert not Path(str(target) + ".provenance.json").exists()
    assert _db_row(env["db"], row["manifest_row_id"])["cache_status"] == "LEGACY_COPY_REQUIRED"


def test_sidecar_publish_failure_does_not_update_db(tmp_path, monkeypatch):
    env = _prepare(tmp_path, ["TARGET_ZIP_NEEDS_SIDECAR"])
    row = env["rows"][0]
    original = os.replace
    def fail_sidecar(source, destination):
        if Path(destination).name.endswith(".provenance.json"):
            raise OSError("injected sidecar publish failure")
        return original(source, destination)
    monkeypatch.setattr(publisher.os, "replace", fail_sidecar)
    with pytest.raises(OSError, match="injected"):
        _run(env)
    assert _db_row(env["db"], row["manifest_row_id"])["cache_status"] == "SIDECAR_REQUIRED"


def test_legacy_source_zip_is_unchanged(tmp_path):
    env = _prepare(tmp_path, ["LEGACY_CACHE_COPY_CANDIDATE"])
    row = env["rows"][0]
    source = env["cache"] / row["legacy_relative_path"]
    before = (source.stat().st_size, source.stat().st_mtime_ns, _sha(source))
    _run(env)
    assert (source.stat().st_size, source.stat().st_mtime_ns, _sha(source)) == before


def test_existing_wrong_target_is_not_overwritten(tmp_path):
    env = _prepare(tmp_path, ["LEGACY_CACHE_COPY_CANDIDATE"])
    row = env["rows"][0]
    target = env["cache"] / row["target_relative_path"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"not-the-source")
    before = target.read_bytes()
    with pytest.raises(RuntimeError, match=publisher.STOP_CONFLICT):
        _run(env)
    assert target.read_bytes() == before


def test_existing_invalid_sidecar_is_not_overwritten(tmp_path):
    env = _prepare(tmp_path, ["TARGET_ZIP_NEEDS_SIDECAR"])
    row = env["rows"][0]
    sidecar = Path(str(env["cache"] / row["target_relative_path"]) + ".provenance.json")
    sidecar.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match=publisher.STOP_CONFLICT):
        _run(env)
    assert sidecar.read_text(encoding="utf-8") == "{}"


@pytest.mark.parametrize("field,bad", [
    ("ticker", "9999"), ("period", "2025-05-31"),
    ("quarter", "3Q"), ("internal_document_id", "20260718999999"),
])
def test_identity_mismatch_is_fail_closed(tmp_path, field, bad):
    env = _prepare(tmp_path, ["TARGET_ZIP_NEEDS_SIDECAR"])
    row = env["rows"][0]
    actual = dict(row["actual_identity"])
    actual[field] = bad
    _write_zip(
        env["cache"] / row["target_relative_path"], ticker=str(actual["ticker"]),
        internal=str(actual["internal_document_id"]), period=str(actual["period"]),
        quarter=str(actual["quarter"]),
    )
    with pytest.raises(RuntimeError, match=publisher.STOP_IDENTITY):
        _run(env)


def test_hash_mismatch_is_fail_closed(tmp_path):
    env = _prepare(tmp_path, ["TARGET_ZIP_NEEDS_SIDECAR"])
    env["rows"][0]["actual_identity"]["zip_sha256"] = "0" * 64
    env["manifest"].write_bytes(publisher._json_bytes({"campaign_id": CAMPAIGN_ID, "rows": env["rows"]}))
    with pytest.raises(RuntimeError, match=publisher.STOP_PRECONDITION):
        _run(env)


def test_missing_legacy_source_stops(tmp_path):
    env = _prepare(tmp_path, ["LEGACY_CACHE_COPY_CANDIDATE"])
    row = env["rows"][0]
    (env["cache"] / row["legacy_relative_path"]).unlink()
    with pytest.raises(RuntimeError, match=publisher.STOP_PRECONDITION):
        _run(env)


@pytest.mark.parametrize("bad_path", ["../xbrl.zip", "C:/tmp/xbrl.zip", "wrong/xbrl.zip"])
def test_target_path_escape_or_mismatch_rejected(tmp_path, bad_path):
    env = _prepare(tmp_path, ["TARGET_ZIP_NEEDS_SIDECAR"])
    env["rows"][0]["target_relative_path"] = bad_path
    env["manifest"].write_bytes(publisher._json_bytes({"campaign_id": CAMPAIGN_ID, "rows": env["rows"]}))
    with pytest.raises(RuntimeError, match=publisher.STOP_INPUT):
        _run(env, apply=False)


def test_duplicate_manifest_row_id_rejected(tmp_path):
    env = _prepare(tmp_path, ["TARGET_ZIP_NEEDS_SIDECAR", "TARGET_ZIP_NEEDS_SIDECAR"])
    env["rows"][1]["manifest_row_id"] = env["rows"][0]["manifest_row_id"]
    env["manifest"].write_bytes(publisher._json_bytes({"campaign_id": CAMPAIGN_ID, "rows": env["rows"]}))
    with pytest.raises(RuntimeError, match=publisher.STOP_INPUT):
        _run(env, apply=False)


def test_unsupported_class_rejected(tmp_path):
    env = _prepare(tmp_path, ["TARGET_ZIP_NEEDS_SIDECAR"])
    env["rows"][0]["classification"] = "METADATA_RESOLVED_CACHE_MISSING"
    env["manifest"].write_bytes(publisher._json_bytes({"campaign_id": CAMPAIGN_ID, "rows": env["rows"]}))
    with pytest.raises(RuntimeError, match=publisher.STOP_INPUT):
        _run(env, apply=False)


def test_db_status_mismatch_rejected(tmp_path):
    env = _prepare(tmp_path, ["TARGET_ZIP_NEEDS_SIDECAR"])
    conn = sqlite3.connect(env["db"])
    conn.execute("UPDATE campaign_filings SET cache_status='MISSING'")
    conn.commit(); conn.close()
    with pytest.raises(RuntimeError, match=publisher.STOP_PRECONDITION):
        _run(env)


def test_cache_error_is_cleared_but_protected_fields_hold(tmp_path):
    env = _prepare(tmp_path, ["TARGET_ZIP_NEEDS_SIDECAR"])
    row = env["rows"][0]
    conn = sqlite3.connect(env["db"])
    conn.execute("UPDATE campaign_filings SET error_code='SIDECAR_MISSING',error_stage='cache',error_message='missing'")
    before = conn.execute("SELECT requested_disclosure_no,internal_document_id,expected_period FROM campaign_filings").fetchone()
    conn.commit(); conn.close()
    _run(env)
    after = _db_row(env["db"], row["manifest_row_id"])
    assert after["error_code"] is after["error_stage"] is after["error_message"] is None
    assert tuple(after[key] for key in ("requested_disclosure_no", "internal_document_id", "expected_period")) == before


def test_non_cache_error_is_preserved(tmp_path):
    env = _prepare(tmp_path, ["TARGET_ZIP_NEEDS_SIDECAR"])
    row = env["rows"][0]
    conn = sqlite3.connect(env["db"])
    conn.execute("UPDATE campaign_filings SET error_code='BUSINESS_ERROR',error_stage='extract',error_message='keep'")
    conn.commit(); conn.close()
    _run(env)
    after = _db_row(env["db"], row["manifest_row_id"])
    assert (after["error_code"], after["error_stage"], after["error_message"]) == ("BUSINESS_ERROR", "extract", "keep")


def test_transaction_failure_rolls_back_db(tmp_path, monkeypatch):
    env = _prepare(tmp_path, ["TARGET_ZIP_NEEDS_SIDECAR"])
    row = env["rows"][0]
    original = publisher._read_db_rows
    calls = 0
    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError(publisher.STOP_DB)
        return original(*args, **kwargs)
    monkeypatch.setattr(publisher, "_read_db_rows", fail_second)
    with pytest.raises(RuntimeError, match=publisher.STOP_DB):
        _run(env)
    assert _db_row(env["db"], row["manifest_row_id"])["cache_status"] == "SIDECAR_REQUIRED"


def test_recovery_after_db_rollback_and_second_run_idempotent(tmp_path, monkeypatch):
    env = _prepare(tmp_path, ["TARGET_ZIP_NEEDS_SIDECAR"])
    original = publisher._read_db_rows
    calls = 0
    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError(publisher.STOP_DB)
        return original(*args, **kwargs)
    monkeypatch.setattr(publisher, "_read_db_rows", fail_second)
    with pytest.raises(RuntimeError):
        _run(env)
    monkeypatch.setattr(publisher, "_read_db_rows", original)
    result = _run(env)
    assert result["published"] == 1
    second_output = env["root"] / "results-second"
    env["output"] = second_output
    before = _sha(env["db"])
    second = _run(env)
    assert second["published"] == 0 and second["already_ready"] == 1
    assert _sha(env["db"]) == before


def test_existing_lock_rejected(tmp_path):
    env = _prepare(tmp_path, ["TARGET_ZIP_NEEDS_SIDECAR"])
    lock = env["cache"] / ".v4_campaign_cache_publish.lock"
    lock.write_text("held", encoding="utf-8")
    with pytest.raises(RuntimeError, match=publisher.STOP_LOCKED):
        _run(env)


def test_audit_digests_match(tmp_path):
    env = _prepare(tmp_path, ["TARGET_ZIP_NEEDS_SIDECAR"])
    result = _run(env)
    disk = json.loads((env["output"] / "digests.json").read_text(encoding="utf-8"))
    assert disk == result["digests"]
    assert all(_sha(env["output"] / name) == digest for name, digest in disk.items())


def test_semantic_digest_is_deterministic():
    actions = [{
        "manifest_row_id": "0001", "requested_disclosure_no": "20260717123456",
        "classification": "TARGET_ZIP_NEEDS_SIDECAR", "action": "PUBLISHED_SIDECAR",
        "target_relative_path": "20260717123456/xbrl.zip", "zip_sha256": "a" * 64,
        "sidecar_sha256": "first-run-only", "internal_document_id": "20260718123450",
    }]
    first = publisher._semantic_digest(actions)
    actions[0]["sidecar_sha256"] = "different-timestamp-sidecar"
    assert publisher._semantic_digest(actions) == first


def test_cli_help_has_required_arguments():
    command = [sys.executable, str(REPO_ROOT / "tools" / "backfill_campaign_cache_publish.py"), "--help"]
    completed = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    assert completed.returncode == 0
    for flag in ("--campaign-db", "--campaign-id", "--cache-root", "--manifest-list", "--output-dir", "--apply"):
        assert flag in completed.stdout


def test_module_has_no_network_or_supabase_imports():
    text = (REPO_ROOT / "lib" / "backfill" / "campaign_cache_publish.py").read_text(encoding="utf-8")
    assert "import requests" not in text
    assert "import supabase" not in text.lower()
    assert "from supabase" not in text.lower()
    assert "get_file_url" not in text
