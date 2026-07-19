from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import requests

import lib.backfill.campaign_fresh_downloader as downloader
from lib.backfill.jquants_td_files_adapter import TD_FILES_ENDPOINT
from lib.backfill.campaign_state import (
    connect_db, create_campaign, create_campaign_filing, create_fresh_download,
    initialize_schema, transaction,
)
from src.segment.zip_identity_verifier import ZipIdentityVerdict


CAMPAIGN_ID = "test-campaign"
REPO_ROOT = Path(__file__).resolve().parents[1]
CODE_SHA = "a" * 40


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _zip_bytes(*, ticker="7203", internal="20260717100001", period="2026-05-31", quarter="FY") -> bytes:
    q = {"1Q": "1", "2Q": "2", "3Q": "3", "FY": "4"}[quarter]
    content = (
        f'<xbrli:identifier scheme="test-sicc">{ticker}</xbrli:identifier>'
        f'<xbrli:endDate>{period}</xbrli:endDate>'
        f'<tse-ed-t:QuarterlyPeriod>{q}</tse-ed-t:QuarterlyPeriod>'
        + ("AnnualMember" if quarter == "FY" else "")
    )
    stream = io.BytesIO()
    name = f"XBRLData/Summary/tse-ed-t-{ticker}0-{internal}-summary.htm"
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(name, content)
    return stream.getvalue()


class FakeResponse:
    def __init__(self, status=200, body=b"", *, url="", headers=None, chunks=None, reason="", payload=None):
        self.status_code = status
        self.body = body
        self.url = url
        self.headers = dict(headers or {})
        self.headers.setdefault("Content-Length", str(len(body)))
        self.headers.setdefault("Content-Type", "application/zip")
        self._chunks = chunks
        self.reason = reason
        self.closed = False
        self.payload = payload

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload

    def iter_content(self, chunk_size=65536):
        if self._chunks is not None:
            yield from self._chunks
            return
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index:index + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == TD_FILES_ENDPOINT:
            disc_no = kwargs["params"]["discNo"]
            return FakeResponse(
                payload={
                    "discNo": disc_no,
                    "files": {"xbrl": "https://fixture-bucket.s3.ap-northeast-1.amazonaws.com/xbrl.zip?X-Amz-Signature=test"},
                },
                headers={"Content-Type": "application/json"},
            )
        if not self.values:
            raise AssertionError("unexpected GET")
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        if not value.url:
            value.url = url
        return value

    def close(self):
        self.closed = True


def _row(index=1, *, classification="STANDARD_FRESH_DOWNLOAD", ticker="7203", period="2026-05-31", quarter="FY"):
    requested = f"20260717{100000 + index:06d}"
    return {
        "campaign_id": CAMPAIGN_ID,
        "manifest_row_id": f"{index:010d}",
        "requested_disclosure_no": requested,
        "company_code": ticker,
        "normalized_company_code": ticker,
        "source_url": f"https://www.release.tdnet.info/inbs/1401{requested}.pdf",
        "normalized_xbrl_url": f"https://www.release.tdnet.info/inbs/0812{requested}.zip",
        "expected_period": period,
        "expected_quarter": quarter,
        "document_type": "financial_statement",
        "identity_status": "METADATA_RESOLVED" if classification.startswith("STANDARD") else "MISMATCH",
        "cache_status": "MISSING" if classification.startswith("STANDARD") else "IDENTITY_MISMATCH",
        "overall_status": "IDENTITY_RESOLVED" if classification.startswith("STANDARD") else "QUARANTINED",
        "retryable": 1 if classification.startswith("STANDARD") else 0,
        "plan_classification": classification,
        "download_allowed": True,
        "auto_ready_allowed": classification.startswith("STANDARD"),
        "quarantine_release_required": classification.startswith("QUARANTINE"),
    }


def _prepare(tmp_path: Path, rows=None):
    rows = rows or [_row()]
    root = tmp_path / "fixture"
    root.mkdir()
    db = root / "campaign.db"
    conn = connect_db(db)
    initialize_schema(conn)
    with transaction(conn):
        create_campaign(conn, {
            "campaign_id": CAMPAIGN_ID, "campaign_name": "test", "manifest_path": "manifest.json",
            "manifest_sha256": "0" * 64, "manifest_record_count": len(rows),
            "code_sha": CODE_SHA, "worker_version": "v4", "status": "READY",
        })
        for row in rows:
            create_campaign_filing(conn, row)
            create_fresh_download(conn, {
                "campaign_id": CAMPAIGN_ID, "manifest_row_id": row["manifest_row_id"],
                "plan_classification": row["plan_classification"],
                "fresh_status": "NOT_STARTED" if row["plan_classification"] == "STANDARD_FRESH_DOWNLOAD" else "QUARANTINED",
                "target_zip_path": str(tmp_path / "cache-root" / CAMPAIGN_ID / row["manifest_row_id"] / "xbrl.zip"),
                "target_provenance_path": str(tmp_path / "cache-root" / CAMPAIGN_ID / row["manifest_row_id"] / "provenance.json"),
                "auto_ready_allowed": int(row["auto_ready_allowed"]),
                "quarantine_release_required": int(row["quarantine_release_required"]),
                "prior_identity_status": row["identity_status"],
                "prior_cache_status": row["cache_status"],
                "prior_overall_status": row["overall_status"],
                "prior_error_code": row.get("error_code"),
                "prior_zip_sha256": row.get("zip_sha256"),
                "prior_internal_document_id": row.get("internal_document_id"),
                "migration_run_id": "test-migration",
            })
    conn.close()
    plan = root / "plan.jsonl"
    plan.write_bytes(b"".join(downloader._json_bytes(row) for row in rows))
    manifest = root / "manifest.json"
    manifest_rows = [{
        "manifest_row_id": row["manifest_row_id"],
        "requested_disclosure_no": row["requested_disclosure_no"],
        "plan_classification": row["plan_classification"],
    } for row in rows]
    manifest.write_bytes(downloader._json_bytes({"campaign_id": CAMPAIGN_ID, "rows": manifest_rows}))
    return {
        "root": root, "db": db, "plan": plan, "manifest": manifest,
        "cache": tmp_path / "cache-root", "output": tmp_path / "output", "rows": rows,
    }


def _run(env, session, *, apply=True, output=None, **kwargs):
    return downloader.run_downloads(
        campaign_db=env["db"], campaign_id=CAMPAIGN_ID, download_plan=env["plan"],
        manifest_list=env["manifest"], cache_root=env["cache"],
        output_dir=output or env["output"], apply=apply, repo_root=REPO_ROOT,
        code_sha=CODE_SHA, min_interval_seconds=0, timeout_seconds=1,
        max_retries=kwargs.pop("max_retries", 3),
        max_consecutive_failures=kwargs.pop("max_consecutive_failures", 10),
        session=session, sleep=kwargs.pop("sleep", lambda _: None),
        source_route="JQUANTS_TD_FILES",
        auth_loader=lambda: ({"x-api-key": "test"}, {"credential_present": True, "credential_source_type": "test"}),
        **kwargs,
    )


def _target(env, index=0):
    row = env["rows"][index]
    return downloader._target_paths(env["cache"], CAMPAIGN_ID, row["manifest_row_id"])


def test_normal_download_publishes_zip_and_provenance(tmp_path):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    session = FakeSession([FakeResponse(body=body)])
    result = _run(env, session)
    _, zip_path, provenance = _target(env)
    assert result["summary"]["status_counts"] == {"READY": 1}
    assert zip_path.read_bytes() == body and provenance.is_file()
    assert all("release.tdnet.info" not in url for url, _kwargs in session.calls)
    assert result["summary"]["static_url_requests"] == 0


def test_streaming_chunks_are_written_exactly(tmp_path):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    chunks = [body[:7], b"", body[7:31], body[31:]]
    _run(env, FakeSession([FakeResponse(body=body, chunks=chunks)]))
    assert _target(env)[1].read_bytes() == body


def test_zip_is_atomically_replaced_before_provenance(tmp_path, monkeypatch):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    calls = []
    real = os.replace
    monkeypatch.setattr(downloader.os, "replace", lambda src, dst: (calls.append(Path(dst).name), real(src, dst))[1])
    _run(env, FakeSession([FakeResponse(body=body)]))
    assert calls == ["xbrl.zip", "provenance.json"]


def test_provenance_is_ready_marker_published_last(tmp_path, monkeypatch):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    observed = []
    real = downloader._write_atomic_json
    def inspect(path, payload):
        observed.append(path.with_name("xbrl.zip").is_file())
        return real(path, payload)
    monkeypatch.setattr(downloader, "_write_atomic_json", inspect)
    _run(env, FakeSession([FakeResponse(body=body)]))
    assert observed == [True]


def test_zip_sha_is_recorded_and_rechecked(tmp_path):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    _run(env, FakeSession([FakeResponse(body=body)]))
    _, zip_path, provenance = _target(env)
    assert json.loads(provenance.read_text())["zip_sha256"] == _sha(zip_path)


def test_exact_identity_is_auto_ready(tmp_path):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    result = _run(env, FakeSession([FakeResponse(body=body)]))
    assert result["results"][0]["identity_status"] == "DOWNLOAD_IDENTITY_VERIFIED"
    assert result["results"][0]["identity_verdict"] == "exact_document_id_match"
    assert result["results"][0]["auto_ready"] is True


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"identity_verdict": "exact_document_id_match"}, True),
        ({"identity_verdict": "official_linked_xbrl_match"}, True),
        ({"identity_verdict": "official_linked_xbrl_match_without_internal_id"}, False),
        ({"identity_verdict": ""}, False),
        ({"identity_verdict": None}, False),
        ({"identity_verdict": "unknown"}, False),
        ({"identity_verdict": "ticker_mismatch"}, False),
        ({"identity_verdict": "period_mismatch"}, False),
        ({"identity_verdict": "quarter_mismatch"}, False),
        ({"identity_verdict": "ambiguous"}, False),
        ({"identity_verdict": "quarantined"}, False),
        ({"identity_status": "DOWNLOAD_IDENTITY_MISMATCH"}, False),
        ({"identity_status": "QUARANTINE_RECHECK_MATCH"}, False),
        ({"identity_status": None}, False),
        ({"plan_classification": "QUARANTINE_FRESH_RECHECK"}, False),
        ({"plan_classification": ""}, False),
        ({"auto_ready_allowed": False}, False),
        ({"auto_ready_allowed": 1}, False),
        ({"auto_ready_allowed": None}, False),
        ({"quarantine_release_required": True}, False),
        ({"quarantine_release_required": None}, False),
    ],
)
def test_production_ready_identity_contract_is_fail_closed(changes, expected):
    payload = {
        "identity_verdict": "official_linked_xbrl_match",
        "identity_status": "DOWNLOAD_IDENTITY_VERIFIED",
        "plan_classification": "STANDARD_FRESH_DOWNLOAD",
        "auto_ready_allowed": True,
        "quarantine_release_required": False,
    }
    payload.update(changes)
    assert downloader.is_production_ready_identity_result(payload) is expected


def test_production_ready_accepts_fully_attested_missing_internal_id():
    payload = {
        "identity_verdict": "official_linked_xbrl_match_without_internal_id",
        "identity_status": "DOWNLOAD_IDENTITY_VERIFIED",
        "plan_classification": "STANDARD_FRESH_DOWNLOAD",
        "auto_ready_allowed": True, "quarantine_release_required": False,
        "internal_document_id": None,
        "internal_document_id_status": "absent_in_artifact",
        "linkage_basis": "jquants_td_files_exact_discno",
        "source_route": "JQUANTS_TD_FILES", "td_files_type": "x",
        "td_files_http_status": 200, "td_files_result_code": "TD_FILES_OK",
        "xbrl_candidate_count": 1, "signed_url_received": True, "file_http_status": 200,
        "requested_disclosure_no": "20250521559959",
        "identity_value_sources": dict(downloader.WITHOUT_INTERNAL_ID_VALUE_SOURCES),
    }
    assert downloader.is_production_ready_identity_result(payload) is True


def test_verified_provenance_result_preserves_null_internal_id(tmp_path):
    payload = {
        "manifest_row_id": "0000000051", "requested_disclosure_no": "20250521559959",
        "plan_classification": "STANDARD_FRESH_DOWNLOAD", "zip_sha256": "a" * 64,
        "identity_status": "DOWNLOAD_IDENTITY_VERIFIED",
        "identity_verdict": "official_linked_xbrl_match_without_internal_id",
        "internal_document_id": None, "zip_internal_ticker": "1332",
        "zip_internal_period": "2025-03-31", "zip_internal_quarter": "FY",
        "document_type": "attachment_xbrl",
    }
    result = downloader._result_from_verified_provenance(
        payload, tmp_path / "xbrl.zip", tmp_path / "provenance.json", "READY"
    )
    assert result["internal_document_id"] is None


def test_standard_identity_mismatch_fails(tmp_path, monkeypatch):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    monkeypatch.setattr(downloader, "verify_zip_identity", lambda *a, **k: ZipIdentityVerdict(False, "", "ticker_mismatch", "r", "i", ""))
    result = _run(env, FakeSession([FakeResponse(body=body)]))
    assert result["summary"]["standard_failed"] == 1
    assert not _target(env)[2].exists()


def test_identity_conflict_details_are_preserved_in_failure_journal(tmp_path, monkeypatch):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    conflict = ZipIdentityVerdict(
        False,
        "ZIP_INTERNAL_IDENTITY_CONFLICT",
        "zip_internal_identity_conflict",
        env["rows"][0]["requested_disclosure_no"],
        "",
        "a" * 64,
        details={
            "conflict_fields": ["period"],
            "candidates": [
                {
                    "path": "XBRLData/Summary/summary.htm",
                    "format": "SUMMARY_IXBRL",
                    "ticker": "7203",
                    "period": "2026-03-31",
                    "quarter": "FY",
                    "document_type": "attachment_xbrl",
                    "internal_document_id": "",
                },
                {
                    "path": "XBRLData/Attachment/attachment.htm",
                    "format": "ATTACHMENT_IXBRL",
                    "ticker": "7203",
                    "period": "2025-03-31",
                    "quarter": "FY",
                    "document_type": "attachment_xbrl",
                    "internal_document_id": "",
                },
            ],
        },
    )
    monkeypatch.setattr(downloader, "verify_zip_identity", lambda *a, **k: conflict)

    result = _run(env, FakeSession([FakeResponse(body=body)]))

    failed = result["results"][0]
    attempt = failed["download_attempts"][-1]
    assert failed["failure_code"] == "ZIP_INTERNAL_IDENTITY_CONFLICT"
    assert failed["failure_stage"] == "ZIP_IDENTITY"
    assert attempt["identity_rejection_reason"] == "zip_internal_identity_conflict"
    assert attempt["identity_conflict_fields"] == ["period"]
    assert len(attempt["identity_candidates"]) == 2
    assert attempt["zip_sha256"] == "a" * 64
    assert "signed" not in json.dumps(attempt["identity_candidates"]).lower()


def test_quarantine_mismatch_is_published_but_not_ready(tmp_path, monkeypatch):
    env = _prepare(tmp_path, [_row(classification="QUARANTINE_FRESH_RECHECK")])
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    monkeypatch.setattr(downloader, "verify_zip_identity", lambda *a, **k: ZipIdentityVerdict(False, "", "quarter_mismatch", "r", "i", ""))
    result = _run(env, FakeSession([FakeResponse(body=body)]))
    payload = json.loads(_target(env)[2].read_text())
    assert result["results"][0]["status"] == "QUARANTINED"
    assert payload["auto_ready_allowed"] is False and payload["quarantine_release_required"] is True


def test_malformed_zip_fails_without_final_files(tmp_path):
    env = _prepare(tmp_path)
    result = _run(env, FakeSession([FakeResponse(body=b"not-a-zip")]))
    assert result["summary"]["failed"] == 1
    assert not _target(env)[1].exists() and not _target(env)[2].exists()


def test_html_body_is_rejected_before_write(tmp_path):
    env = _prepare(tmp_path)
    response = FakeResponse(body=b"<html>error</html>", headers={"Content-Type": "text/html"})
    result = _run(env, FakeSession([response]))
    assert result["summary"]["failed"] == 1 and not _target(env)[1].exists()


def test_404_is_not_retried(tmp_path):
    env = _prepare(tmp_path)
    session = FakeSession([FakeResponse(status=404)])
    result = _run(env, session)
    failure = result["results"][0]
    assert len(session.calls) == 2 and result["summary"]["failed"] == 1
    assert failure["http_status"] == 404 and failure["failure_code"] == "SIGNED_URL_DOWNLOAD_FAILED"
    assert failure["retryable"] is False


def test_429_is_retried_with_retry_after(tmp_path):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    sleeps = []
    session = FakeSession([FakeResponse(status=429, headers={"Retry-After": "3"}), FakeResponse(body=body)])
    result = _run(env, session, sleep=sleeps.append)
    assert len(session.calls) == 3 and sleeps == [3.0] and result["summary"]["failed"] == 0
    assert result["results"][0]["download_attempts"][1]["http_status"] == 429
    assert result["results"][0]["download_attempts"][1]["retry_after"] == "3"


def test_503_retry_after_is_respected(tmp_path):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    sleeps = []
    result = _run(env, FakeSession([FakeResponse(status=503, headers={"Retry-After": "7"}), FakeResponse(body=body)]), sleep=sleeps.append)
    assert sleeps == [7.0]
    assert result["results"][0]["download_attempts"][1]["result_code"] == "SIGNED_URL_DOWNLOAD_FAILED"


def test_timeout_is_retried_with_exponential_backoff(tmp_path):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    sleeps = []
    result = _run(env, FakeSession([requests.Timeout("slow"), FakeResponse(body=body)]), sleep=sleeps.append)
    assert result["summary"]["failed"] == 0 and sleeps == [1.0]
    first = result["results"][0]["download_attempts"][1]
    assert first["result_code"] == "SIGNED_URL_DOWNLOAD_FAILED" and first["exception_type"] == "Timeout"


def test_maximum_attempts_is_three(tmp_path):
    env = _prepare(tmp_path)
    session = FakeSession([FakeResponse(status=500), FakeResponse(status=502), FakeResponse(status=504)])
    result = _run(env, session, max_retries=3)
    assert len(session.calls) == 4 and result["summary"]["failed"] == 1


def test_consecutive_failure_limit_stops_remaining_rows(tmp_path):
    env = _prepare(tmp_path, [_row(1), _row(2)])
    session = FakeSession([FakeResponse(status=404)])
    result = _run(env, session, max_consecutive_failures=1)
    assert result["summary"]["consecutive_failure_stop"] is True
    assert result["summary"]["processed"] == 1


def test_off_domain_redirect_is_fail_closed(tmp_path):
    env = _prepare(tmp_path)
    response = FakeResponse(status=302, headers={"Location": "https://example.com/file.zip"})
    result = _run(env, FakeSession([response]))
    failure = result["results"][0]
    assert failure["failure_code"] == "SIGNED_URL_DOWNLOAD_FAILED"
    assert len(failure["download_attempts"]) == 2
    assert failure["download_attempts"][1]["http_status"] == 302


def test_signed_url_redirect_is_not_followed(tmp_path):
    env = _prepare(tmp_path)
    row = env["rows"][0]
    url = row["normalized_xbrl_url"]
    body = _zip_bytes(internal=row["requested_disclosure_no"])
    session = FakeSession([FakeResponse(status=302, headers={"Location": url}), FakeResponse(body=body)])
    result = _run(env, session, max_retries=1)
    assert result["summary"]["failed"] == 1
    assert result["summary"]["network_calls"] == 2
    assert all(call[1]["allow_redirects"] is False for call in session.calls)


def test_partial_stream_failure_retries_and_leaves_no_provenance(tmp_path):
    env = _prepare(tmp_path)
    class BrokenResponse(FakeResponse):
        def iter_content(self, chunk_size=65536):
            yield b"partial"
            raise requests.ConnectionError("broken body")
    session = FakeSession([BrokenResponse(body=b""), BrokenResponse(body=b""), BrokenResponse(body=b"")])
    result = _run(env, session)
    assert result["summary"]["failed"] == 1
    assert len(session.calls) == 4
    assert not _target(env)[2].exists() and not _target(env)[1].exists()


def test_failed_download_cleans_temporary_files(tmp_path):
    env = _prepare(tmp_path)
    _run(env, FakeSession([FakeResponse(body=b"bad")]))
    directory = _target(env)[0]
    assert not list(directory.glob("*.tmp")) and not list(directory.glob(".*.tmp"))


def test_completed_entry_is_idempotent_without_network(tmp_path):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    _run(env, FakeSession([FakeResponse(body=body)]))
    second = FakeSession([])
    result = _run(env, second, output=tmp_path / "output-two")
    assert result["results"][0]["status"] == "ALREADY_COMPLETE" and second.calls == []


def test_manifest_row_id_is_cache_path_key(tmp_path):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    _run(env, FakeSession([FakeResponse(body=body)]))
    directory = _target(env)[0]
    assert directory.name == env["rows"][0]["manifest_row_id"]
    assert env["rows"][0]["requested_disclosure_no"] not in str(directory)


def test_repository_cache_root_is_rejected(tmp_path):
    env = _prepare(tmp_path)
    env["cache"] = REPO_ROOT / "forbidden-cache"
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_UNSAFE_PATH):
        _run(env, FakeSession([]), apply=False)


def test_repository_output_is_rejected(tmp_path):
    env = _prepare(tmp_path)
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_UNSAFE_PATH):
        _run(env, FakeSession([]), apply=False, output=REPO_ROOT / "forbidden-output")


def test_without_apply_has_zero_network_and_zero_writes(tmp_path):
    env = _prepare(tmp_path)
    session = FakeSession([])
    result = _run(env, session, apply=False)
    assert result == {"apply": False, "selected": 1, "network_calls": 0, "cache_writes": 0, "output_writes": 0}
    assert session.calls == [] and not env["cache"].exists() and not env["output"].exists()


def test_missing_source_route_fails_without_static_url_fallback(tmp_path):
    env = _prepare(tmp_path)
    session = FakeSession([FakeResponse(body=b"unused")])
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_URL):
        downloader.run_downloads(
            campaign_db=env["db"], campaign_id=CAMPAIGN_ID,
            download_plan=env["plan"], manifest_list=env["manifest"],
            cache_root=env["cache"], output_dir=env["output"], apply=True,
            repo_root=REPO_ROOT, code_sha=CODE_SHA, session=session,
        )
    assert session.calls == []
    assert not env["cache"].exists() and not env["output"].exists()


def test_campaign_database_is_read_only_and_unchanged(tmp_path):
    env = _prepare(tmp_path)
    before = _sha(env["db"])
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    _run(env, FakeSession([FakeResponse(body=body)]))
    assert _sha(env["db"]) == before


def test_unrelated_cache_sentinel_is_unchanged(tmp_path):
    env = _prepare(tmp_path)
    sentinel = tmp_path / "production-cache-sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    _run(env, FakeSession([FakeResponse(body=body)]))
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def test_module_import_has_no_filesystem_side_effect(tmp_path):
    code = "import lib.backfill.campaign_fresh_downloader; print('OK')"
    proc = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(REPO_ROOT)}, capture_output=True, text=True)
    assert proc.returncode == 0 and proc.stdout.strip() == "OK" and not list(tmp_path.iterdir())


def test_json_encoding_digest_is_deterministic():
    value = {"z": "日本語", "a": [2, 1]}
    first = downloader._json_bytes(value)
    assert first == downloader._json_bytes(value)
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(downloader._json_bytes(value)).hexdigest()


def test_content_length_mismatch_is_rejected(tmp_path):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    response = FakeResponse(body=body, headers={"Content-Length": str(len(body) + 1)})
    result = _run(env, FakeSession([response]))
    assert result["summary"]["failed"] == 1 and not _target(env)[2].exists()


def test_duplicate_manifest_row_is_rejected(tmp_path):
    env = _prepare(tmp_path)
    payload = json.loads(env["manifest"].read_text())
    payload["rows"].append(dict(payload["rows"][0]))
    env["manifest"].write_bytes(downloader._json_bytes(payload))
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_INPUT):
        _run(env, FakeSession([]), apply=False)


def test_manifest_plan_classification_mismatch_is_rejected(tmp_path):
    env = _prepare(tmp_path)
    payload = json.loads(env["manifest"].read_text())
    payload["rows"][0]["plan_classification"] = "QUARANTINE_FRESH_RECHECK"
    env["manifest"].write_bytes(downloader._json_bytes(payload))
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_INPUT):
        _run(env, FakeSession([]), apply=False)


def test_provenance_loader_rejects_tampered_hash(tmp_path):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    _run(env, FakeSession([FakeResponse(body=body)]))
    _, zip_path, provenance = _target(env)
    zip_path.write_bytes(b"tampered")
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_IDENTITY):
        downloader.load_provenance(zip_path, provenance)


def test_provenance_contains_required_schema_fields(tmp_path):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    _run(env, FakeSession([FakeResponse(body=body)]))
    payload = json.loads(_target(env)[2].read_text())
    assert payload["schema_version"] == "1"
    assert payload["code_sha"] == CODE_SHA and payload["download_tool_version"] == downloader.TOOL_VERSION
    assert payload["download_attempts"][0]["attempt_number"] == 1


def test_row51_attachment_formal_loader_is_production_ready(tmp_path):
    source = Path(
        r"C:\tmp\v4-fresh-row51-identity-audit-20260718-100133"
        r"\diagnostic-download\row51-xbrl.zip"
    )
    assert source.is_file()
    assert _sha(source) == "cb555a7cad60fa76e029b94aa4f2d2904e8f4a9691e465c45e4145a0e0f0a5f5"
    zip_path = tmp_path / "xbrl.zip"
    shutil.copyfile(source, zip_path)
    payload = {
        "schema_version": "1", "campaign_id": CAMPAIGN_ID,
        "manifest_row_id": "0000000051", "requested_disclosure_no": "20250521559959",
        "company_code": "13320", "normalized_company_code": "1332",
        "source_url": "jquants://td-files/20250521559959/x",
        "normalized_xbrl_url": "jquants://td-files/20250521559959/x",
        "source_route": "JQUANTS_TD_FILES", "td_files_type": "x",
        "td_files_http_status": 200, "td_files_result_code": "TD_FILES_OK",
        "xbrl_candidate_count": 1, "signed_url_received": True, "file_http_status": 200,
        "final_url": None,
        "downloaded_at": "2026-07-18T01:00:00+00:00",
        "downloaded_at_utc": "2026-07-18T01:00:00+00:00",
        "downloaded_at_jst": "2026-07-18T10:00:00+09:00", "http_status": 200,
        "content_type": "application/zip", "content_length": str(zip_path.stat().st_size),
        "download_attempts": [], "zip_sha256": _sha(zip_path), "zip_size": zip_path.stat().st_size,
        "internal_document_id": None, "internal_document_id_status": "absent_in_artifact",
        "linkage_basis": "jquants_td_files_exact_discno",
        "identity_value_sources": dict(downloader.WITHOUT_INTERNAL_ID_VALUE_SOURCES),
        "zip_internal_ticker": "1332",
        "zip_internal_period": "2025-03-31", "zip_internal_quarter": "FY",
        "document_type": "attachment_xbrl", "identity_status": "DOWNLOAD_IDENTITY_VERIFIED",
        "identity_verdict": "official_linked_xbrl_match_without_internal_id",
        "plan_classification": "STANDARD_FRESH_DOWNLOAD", "auto_ready_allowed": True,
        "quarantine_release_required": False, "code_sha": CODE_SHA, "run_id": "row51",
        "download_tool_version": "1", "error_code": None, "error_message": None,
    }
    provenance_path = tmp_path / "provenance.json"
    provenance_path.write_bytes(downloader._json_bytes(payload))

    loaded = downloader.load_provenance(zip_path, provenance_path)

    assert loaded["internal_document_id"] is None
    assert downloader.is_production_ready_identity_result(loaded) is True


def test_formal_loader_rejects_unknown_verified_verdict(tmp_path):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    _run(env, FakeSession([FakeResponse(body=body)]))
    _, zip_path, provenance_path = _target(env)
    payload = json.loads(provenance_path.read_text())
    payload["identity_verdict"] = "unknown"
    provenance_path.write_bytes(downloader._json_bytes(payload))

    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_IDENTITY):
        downloader.load_provenance(zip_path, provenance_path)


@pytest.mark.parametrize("field,value", [
    ("internal_document_id_status", None),
    ("linkage_basis", None),
    ("linkage_basis", "wrong"),
    ("source_route", "STATIC_URL"),
    ("td_files_type", "pdf"),
    ("td_files_result_code", None),
    ("td_files_result_code", "TD_FILES_EMPTY"),
    ("xbrl_candidate_count", 2),
    ("signed_url_received", False),
    ("identity_value_sources", None),
    ("http_status", 206),
    ("requested_disclosure_no", "not-a-disc-no"),
])
def test_row51_missing_internal_id_formal_loader_requires_official_linkage(
    tmp_path, field, value
):
    source = Path(
        r"C:\tmp\v4-fresh-row51-identity-audit-20260718-100133"
        r"\diagnostic-download\row51-xbrl.zip"
    )
    zip_path = tmp_path / "xbrl.zip"
    shutil.copyfile(source, zip_path)
    payload = {
        "schema_version": "1", "campaign_id": CAMPAIGN_ID,
        "manifest_row_id": "0000000051", "requested_disclosure_no": "20250521559959",
        "company_code": "13320", "normalized_company_code": "1332",
        "source_url": "jquants://td-files/20250521559959/x",
        "normalized_xbrl_url": "jquants://td-files/20250521559959/x",
        "source_route": "JQUANTS_TD_FILES", "td_files_type": "x",
        "td_files_http_status": 200, "td_files_result_code": "TD_FILES_OK",
        "xbrl_candidate_count": 1, "signed_url_received": True, "file_http_status": 200,
        "final_url": None,
        "downloaded_at": "2026-07-18T01:00:00+00:00",
        "downloaded_at_utc": "2026-07-18T01:00:00+00:00",
        "downloaded_at_jst": "2026-07-18T10:00:00+09:00", "http_status": 200,
        "content_type": "application/zip", "content_length": str(zip_path.stat().st_size),
        "download_attempts": [], "zip_sha256": _sha(zip_path), "zip_size": zip_path.stat().st_size,
        "internal_document_id": None, "internal_document_id_status": "absent_in_artifact",
        "linkage_basis": "jquants_td_files_exact_discno",
        "identity_value_sources": dict(downloader.WITHOUT_INTERNAL_ID_VALUE_SOURCES),
        "zip_internal_ticker": "1332",
        "zip_internal_period": "2025-03-31", "zip_internal_quarter": "FY",
        "document_type": "attachment_xbrl", "identity_status": "DOWNLOAD_IDENTITY_VERIFIED",
        "identity_verdict": "official_linked_xbrl_match_without_internal_id",
        "plan_classification": "STANDARD_FRESH_DOWNLOAD", "auto_ready_allowed": True,
        "quarantine_release_required": False, "code_sha": CODE_SHA, "run_id": "row51",
        "download_tool_version": "1", "error_code": None, "error_message": None,
    }
    payload[field] = value
    provenance_path = tmp_path / "provenance.json"
    provenance_path.write_bytes(downloader._json_bytes(payload))

    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_IDENTITY):
        downloader.load_provenance(zip_path, provenance_path)


def test_403_preserves_status_reason_and_safe_headers(tmp_path):
    env = _prepare(tmp_path)
    response = FakeResponse(
        status=403, reason="Forbidden",
        headers={"Content-Type": "text/html", "Server": "edge", "Set-Cookie": "secret", "Authorization": "secret"},
    )
    result = _run(env, FakeSession([response]), max_retries=1)
    failure = result["results"][0]
    assert failure["http_status"] == 403 and failure["reason_phrase"] == "Forbidden"
    assert failure["failure_code"] == "SIGNED_URL_EXPIRED" and failure["failure_stage"] == "SIGNED_URL"
    assert failure["response_headers"] == {
        "content-length": "0", "content-type": "text/html", "server": "edge",
    }
    assert failure["td_files_http_status"] == 200
    assert failure["td_files_result_code"] == "TD_FILES_OK"
    assert failure["file_http_status"] == 403
    assert "secret" not in json.dumps(failure)


def test_relative_signed_url_redirect_is_rejected_without_follow(tmp_path):
    env = _prepare(tmp_path)
    row = env["rows"][0]
    filename = Path(str(row["normalized_xbrl_url"])).name
    body = _zip_bytes(internal=row["requested_disclosure_no"])
    first = FakeResponse(status=302, reason="Found", headers={"Location": filename})
    session = FakeSession([first, FakeResponse(body=body)])
    result = _run(env, session, max_retries=1)
    assert result["summary"]["failed"] == 1
    assert len(session.calls) == 2


def test_tls_exception_is_preserved(tmp_path):
    env = _prepare(tmp_path)
    result = _run(env, FakeSession([requests.exceptions.SSLError("certificate verify failed")]), max_retries=1)
    failure = result["results"][0]
    assert failure["http_status"] is None
    assert failure["failure_code"] == "SIGNED_URL_DOWNLOAD_FAILED" and failure["failure_stage"] == "SIGNED_URL"
    assert failure["exception_type"] == "SSLError"
    assert "certificate verify failed" in failure["exception_message"]


def test_dns_exception_is_preserved(tmp_path):
    env = _prepare(tmp_path)
    error = requests.ConnectionError("NameResolutionError: getaddrinfo failed")
    result = _run(env, FakeSession([error]), max_retries=1)
    failure = result["results"][0]
    assert failure["failure_code"] == "SIGNED_URL_DOWNLOAD_FAILED" and failure["failure_stage"] == "SIGNED_URL"
    assert failure["exception_type"] == "ConnectionError" and failure["retryable"] is True


def test_exception_before_response_has_stage_and_null_status(tmp_path):
    env = _prepare(tmp_path)
    result = _run(env, FakeSession([requests.ConnectionError("connection refused")]), max_retries=1)
    failure = result["results"][0]
    assert failure["http_status"] is None and failure["failure_stage"] == "SIGNED_URL"
    assert failure["failure_code"] == "SIGNED_URL_DOWNLOAD_FAILED"
    assert failure["attempt_count"] == 2


def test_failure_details_survive_json_serialization(tmp_path):
    env = _prepare(tmp_path)
    _run(env, FakeSession([FakeResponse(status=403, reason="Forbidden")]), max_retries=1)
    serialized = json.loads((env["output"] / "download-results.json").read_text(encoding="utf-8"))[0]
    assert serialized["http_status"] == 403
    assert serialized["reason_phrase"] == "Forbidden"
    assert serialized["failure_code"] == "SIGNED_URL_EXPIRED"
    assert serialized["download_attempts"][0]["request_finished_at"]


def test_success_attempt_has_complete_diagnostic_schema(tmp_path):
    env = _prepare(tmp_path)
    row = env["rows"][0]
    body = _zip_bytes(internal=row["requested_disclosure_no"])
    result = _run(env, FakeSession([FakeResponse(body=body, reason="OK")]))
    attempt = result["results"][0]["download_attempts"][1]
    required = {
        "attempt_number", "download_started_at", "download_finished_at",
        "elapsed_seconds", "http_status", "reason_phrase", "response_headers",
        "content_type", "content_length", "retry_after", "bytes_received",
        "signed_url_host", "signed_url_scheme", "signed_url_redacted_digest",
        "zip_sha256", "result_code",
    }
    assert required.issubset(attempt)
    assert attempt["http_status"] == 200 and attempt["reason_phrase"] == "OK"
    assert attempt["bytes_received"] == len(body) and attempt["result_code"] == "SIGNED_URL_DOWNLOAD_OK"


def test_zip_invalid_has_distinct_failure_code(tmp_path):
    env = _prepare(tmp_path)
    result = _run(env, FakeSession([FakeResponse(body=b"not-a-zip")]), max_retries=1)
    failure = result["results"][0]
    assert failure["failure_code"] == "ZIP_INVALID"
    assert failure["failure_stage"] == "ZIP_VALIDATION"
    assert failure["http_status"] == 200
    assert failure["td_files_http_status"] == 200
    assert failure["td_files_result_code"] == "TD_FILES_OK"
    assert failure["file_http_status"] == 200


def test_content_type_rejection_has_distinct_code(tmp_path):
    env = _prepare(tmp_path)
    result = _run(env, FakeSession([FakeResponse(body=b"html", headers={"Content-Type": "text/html"})]), max_retries=1)
    failure = result["results"][0]
    assert failure["failure_code"] == "SIGNED_URL_DOWNLOAD_FAILED"
    assert failure["failure_stage"] == "SIGNED_URL"
    assert failure["http_status"] == 200
    assert failure["td_files_http_status"] == 200
    assert failure["td_files_result_code"] == "TD_FILES_OK"
    assert failure["xbrl_candidate_count"] == 1
    assert failure["signed_url_received"] is True
    assert failure["file_http_status"] == 200


def test_stage_a_host_rejection_keeps_stage_a_status(tmp_path):
    env = _prepare(tmp_path)

    class RejectingHostSession(FakeSession):
        def get(self, url, **kwargs):
            if url == TD_FILES_ENDPOINT:
                self.calls.append((url, kwargs))
                return FakeResponse(
                    payload={
                        "discNo": kwargs["params"]["discNo"],
                        "files": {"xbrl": "https://example.com/xbrl.zip?signature=x"},
                    },
                    headers={"Content-Type": "application/json"},
                    reason="OK",
                )
            return super().get(url, **kwargs)

    result = _run(env, RejectingHostSession([]), max_retries=1)
    failure = result["results"][0]
    assert failure["failure_code"] == "SIGNED_URL_HOST_REJECTED"
    assert failure["td_files_http_status"] == 200
    assert failure["td_files_reason"] == "OK"
    assert failure["td_files_result_code"] == "SIGNED_URL_HOST_REJECTED"
    assert failure["xbrl_candidate_count"] == 1
    assert failure["signed_url_received"] is True
    assert failure["file_http_status"] is None
    assert "example.com/xbrl.zip" not in json.dumps(failure)


def test_cli_help_has_all_contract_arguments():
    proc = subprocess.run([sys.executable, "tools/backfill_campaign_fresh_download.py", "--help"], cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0
    for option in ("--campaign-db", "--campaign-id", "--download-plan", "--manifest-list", "--cache-root", "--output-dir", "--source-route", "--apply"):
        assert option in proc.stdout


def _production_fixture(tmp_path: Path, count: int = 5):
    rows = [_row(index) for index in range(1, count + 1)]
    env = _prepare(tmp_path, rows)
    env["cache"] = tmp_path / "repo" / "data" / "v4_campaign_cache" / CAMPAIGN_ID
    conn = connect_db(env["db"])
    for row in rows:
        row_id = row["manifest_row_id"]
        conn.execute(
            "UPDATE campaign_fresh_downloads SET target_zip_path=?,target_provenance_path=? "
            "WHERE campaign_id=? AND manifest_row_id=?",
            (
                str(env["cache"] / row_id / "xbrl.zip"),
                str(env["cache"] / row_id / "provenance.json"),
                CAMPAIGN_ID, row_id,
            ),
        )
    conn.commit(); conn.close()
    env["output"] = tmp_path / "prod-output"
    environment = downloader.ProductionEnvironment(
        campaign_db=env["db"], cache_root=env["cache"], output_parent=tmp_path,
        output_pattern=re.compile(r"^prod-output$"),
    )
    known = tmp_path / "known"
    known.mkdir()
    sources = {}
    for row in rows:
        row_id = row["manifest_row_id"]
        directory = known / row_id
        directory.mkdir()
        zip_path = directory / "xbrl.zip"
        zip_path.write_bytes(_zip_bytes(
            ticker=row["normalized_company_code"], internal=row["requested_disclosure_no"],
            period=row["expected_period"], quarter=row["expected_quarter"],
        ))
        payload = {
            "schema_version": "1", "campaign_id": CAMPAIGN_ID,
            "manifest_row_id": row_id, "requested_disclosure_no": row["requested_disclosure_no"],
            "company_code": row["company_code"], "normalized_company_code": row["normalized_company_code"],
            "source_url": row["source_url"], "normalized_xbrl_url": row["normalized_xbrl_url"],
            "source_route": "JQUANTS_TD_FILES", "final_url": None,
            "downloaded_at": "2026-07-17T00:00:00+00:00",
            "downloaded_at_utc": "2026-07-17T00:00:00+00:00",
            "downloaded_at_jst": "2026-07-17T09:00:00+09:00", "http_status": 200,
            "content_type": "application/zip", "content_length": str(zip_path.stat().st_size),
            "download_attempts": [], "zip_sha256": _sha(zip_path), "zip_size": zip_path.stat().st_size,
            "internal_document_id": row["requested_disclosure_no"],
            "zip_internal_ticker": row["normalized_company_code"],
            "zip_internal_period": row["expected_period"], "zip_internal_quarter": row["expected_quarter"],
            "document_type": "attachment_xbrl", "identity_status": "DOWNLOAD_IDENTITY_VERIFIED",
            "identity_verdict": "official_linked_xbrl_match",
            "plan_classification": "STANDARD_FRESH_DOWNLOAD", "auto_ready_allowed": True,
            "quarantine_release_required": False, "code_sha": CODE_SHA, "run_id": "known",
            "download_tool_version": "1", "error_code": None, "error_message": None,
        }
        provenance = directory / "provenance.json"
        provenance.write_bytes(downloader._json_bytes(payload))
        sources[row_id] = (zip_path, provenance)

    def provider(row, cache_root, campaign_id):
        source_zip, source_provenance = sources[row["manifest_row_id"]]
        return downloader.publish_injected_verified_artifact(
            row=row, cache_root=cache_root, campaign_id=campaign_id,
            source_zip=source_zip, source_provenance=source_provenance,
        )
    runtime_checks = []
    def runtime(_repo):
        evidence = {"checked_at": len(runtime_checks), "active_processes": [], "locks": []}
        runtime_checks.append(evidence)
        return evidence
    return env, environment, provider, runtime, runtime_checks


def _production_kwargs(env, environment, provider, runtime):
    selected, _ = downloader.load_manifest_list(env["manifest"], CAMPAIGN_ID)
    count = len(env["rows"])
    return {
        "campaign_db": env["db"], "campaign_id": CAMPAIGN_ID,
        "campaign_db_sha256": _sha(env["db"]), "download_plan": env["plan"],
        "download_plan_sha256": _sha(env["plan"]), "manifest_list": env["manifest"],
        "manifest_byte_sha256": _sha(env["manifest"]),
        "manifest_semantic_sha256_value": downloader.manifest_semantic_sha256(selected),
        "cache_root": env["cache"], "output_dir": env["output"],
        "expected_count": count, "max_items": count,
        "confirm_production_item_count": count,
        "confirm_production_cache_root": str(env["cache"]),
        "confirm_campaign_id": CAMPAIGN_ID, "apply": True, "production_apply": True,
        "source_route": "JQUANTS_TD_FILES", "repo_root": REPO_ROOT, "code_sha": CODE_SHA,
        "min_interval_seconds": 0, "environment": environment,
        "runtime_checker": runtime, "artifact_provider": provider,
    }


def test_production_simulation_publishes_five_then_updates_database(tmp_path):
    env, environment, provider, runtime, checks = _production_fixture(tmp_path)
    result = downloader.run_production_downloads(**_production_kwargs(env, environment, provider, runtime))
    assert result["summary"]["db_updated"] == 5
    assert result["summary"]["network_calls"] == 10
    assert result["summary"]["non_target_changed"] == 0
    assert result["journal"]["current_phase"] == "COMPLETE"
    assert len(checks) == 4
    assert sum(1 for path in env["cache"].rglob("xbrl.zip")) == 5
    assert sum(1 for path in env["cache"].rglob("provenance.json")) == 5
    assert not list(env["cache"].rglob("*.tmp"))
    conn = connect_db(env["db"])
    rows = conn.execute("SELECT * FROM campaign_fresh_downloads ORDER BY manifest_row_id").fetchall()
    assert len(rows) == 5
    assert all(row["fresh_status"] == "COMPLETE" and row["attempt_count"] == 1 for row in rows)
    filings = conn.execute("SELECT * FROM campaign_filings ORDER BY manifest_row_id").fetchall()
    assert all((row["identity_status"], row["cache_status"], row["overall_status"]) == (
        "METADATA_RESOLVED", "MISSING", "IDENTITY_RESOLVED",
    ) for row in filings)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_production_simulation_accepts_manifest_bound_hundred(tmp_path):
    env, environment, provider, runtime, checks = _production_fixture(tmp_path, count=100)
    result = downloader.run_production_downloads(
        **_production_kwargs(env, environment, provider, runtime)
    )
    assert result["summary"]["db_updated"] == 100
    assert result["summary"]["network_calls"] == 200
    assert len(checks) == 4
    assert all(
        row["stage_a_state"] == "SUCCEEDED"
        and row["stage_b_state"] == "SUCCEEDED"
        and row["zip_state"] == "VERIFIED"
        and row["provenance_state"] == "VERIFIED"
        and row["loader_state"] == "ACCEPTED"
        and row["fresh_db_start_state"] == "NOT_STARTED"
        and row["fresh_db_end_state"] == "COMPLETE"
        and row["failure_code"] is None
        for row in result["journal"]["rows"].values()
    )
    conn = connect_db(env["db"])
    assert conn.execute(
        "SELECT COUNT(*) FROM campaign_fresh_downloads WHERE fresh_status='COMPLETE'"
    ).fetchone()[0] == 100
    assert conn.execute(
        "SELECT COUNT(*) FROM campaign_filings WHERE cache_status='MISSING'"
    ).fetchone()[0] == 100
    conn.close()


@pytest.mark.parametrize("count", [1, 37])
def test_production_count_contract_accepts_arbitrary_in_range_counts(tmp_path, count):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path, count=count)
    result = downloader.run_production_downloads(
        **_production_kwargs(env, environment, provider, runtime)
    )
    assert result["summary"]["selected"] == count
    assert result["summary"]["db_updated"] == count
    assert result["summary"]["network_calls"] == count * 2


@pytest.mark.parametrize("count", [0, 101])
def test_production_count_contract_rejects_out_of_range_before_output(tmp_path, count):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    kwargs = _production_kwargs(env, environment, provider, runtime)
    kwargs.update(expected_count=count, max_items=count, confirm_production_item_count=count)
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_COUNT):
        downloader.run_production_downloads(**kwargs)
    assert not env["output"].exists()
    assert not env["cache"].exists()


def test_production_count_contract_rejects_missing_confirm_before_output(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    kwargs = _production_kwargs(env, environment, provider, runtime)
    kwargs["confirm_production_item_count"] = None
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_COUNT):
        downloader.run_production_downloads(**kwargs)
    assert not env["output"].exists()
    assert not env["cache"].exists()


def test_production_count_contract_rejects_duplicate_manifest_before_output(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    kwargs = _production_kwargs(env, environment, provider, runtime)
    payload = json.loads(env["manifest"].read_text())
    payload["rows"][1] = dict(payload["rows"][0])
    env["manifest"].write_bytes(downloader._json_bytes(payload))
    kwargs["manifest_byte_sha256"] = _sha(env["manifest"])
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_COUNT):
        downloader.run_production_downloads(**kwargs)
    assert not env["output"].exists()
    assert not env["cache"].exists()


def test_production_schema_rejects_duplicate_target_path_before_run(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    conn = connect_db(env["db"])
    duplicate = str(env["cache"] / "0000000001" / "xbrl.zip")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        conn.execute(
            "UPDATE campaign_fresh_downloads SET target_zip_path=? WHERE manifest_row_id='0000000002'",
            (duplicate,),
        )
    conn.rollback(); conn.close()
    assert not env["output"].exists()
    assert not env["cache"].exists()


def test_production_hundred_partial_stage_a_failure_keeps_database_unchanged(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path, count=100)
    before_sha = _sha(env["db"])
    def fail_at_37(row, cache_root, campaign_id):
        if row["manifest_row_id"] == "0000000037":
            raise downloader.FreshDownloaderStop(downloader.STOP_HTTP)
        return provider(row, cache_root, campaign_id)
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_HTTP):
        downloader.run_production_downloads(
            **_production_kwargs(env, environment, fail_at_37, runtime)
        )
    assert _sha(env["db"]) == before_sha
    assert sum(1 for _ in env["cache"].rglob("xbrl.zip")) == 36
    journal = json.loads((env["output"] / "journal.json").read_text())
    assert journal["current_phase"] == "FAILED"
    assert journal["rows"]["0000000037"]["stage_a_state"] == "FAILED"
    assert journal["rows"]["0000000037"]["failure_code"] == downloader.STOP_HTTP


@pytest.mark.parametrize("failure_code,raw_failure_stage,expected_stage,http_status", [
    ("TD_FILES_DISCNO_NOT_FOUND", "TD_FILES", "STAGE_A", 404),
    ("ZIP_INTERNAL_IDENTITY_CONFLICT", "ZIP_IDENTITY", "ZIP_IDENTITY", 200),
])
def test_production_known_failure_writes_canonical_secret_free_evidence(
    tmp_path, failure_code, raw_failure_stage, expected_stage, http_status,
):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path, count=5)
    before_sha = _sha(env["db"])
    def fail_at_three(row, cache_root, campaign_id):
        if row["manifest_row_id"] == "0000000003":
            raise downloader.DownloadFailure(
                downloader.STOP_IDENTITY if raw_failure_stage == "ZIP_IDENTITY" else downloader.STOP_HTTP,
                failure_code=failure_code, failure_stage=raw_failure_stage, retryable=False,
                attempts=[{"stage":"TD_FILES", "http_status":http_status,
                    "reason_phrase":"known", "result_code":failure_code,
                    "endpoint":"https://api.jquants.com/v2/td/files/redacted",
                    "elapsed_seconds":0.157, "signed_url_received":False,
                    "requested_url":"https://example.test/?token=must-not-leak"}],
            )
        return provider(row, cache_root, campaign_id)
    with pytest.raises(downloader.DownloadFailure):
        downloader.run_production_downloads(
            **_production_kwargs(env, environment, fail_at_three, runtime)
        )
    assert _sha(env["db"]) == before_sha
    journal = json.loads((env["output"] / "journal.json").read_text(encoding="utf-8"))
    failed = journal["rows"]["0000000003"]
    evidence = failed["failure_evidence"]
    assert failed["failure_code"] == failure_code
    assert failed["failure_stage"] == expected_stage
    assert failed["raw_failure_stage"] == raw_failure_stage
    assert failed["canonical_failure_stage"] == expected_stage
    assert failed["http_status"] == http_status
    assert failed["failure_telemetry"]["raw_failure_stage"] == raw_failure_stage
    assert failed["failure_telemetry"]["canonical_failure_stage"] == expected_stage
    assert failed["failure_telemetry"]["endpoint_host"] == "api.jquants.com"
    assert failed["failure_telemetry"]["elapsed_milliseconds"] == 157.0
    assert failed["failure_telemetry"]["signed_url_received"] is False
    assert failed["failure_telemetry"]["stage_b_started"] is False
    raw = Path(evidence["file"]).read_text(encoding="utf-8")
    assert "token" not in raw.lower() and "example.test" not in raw
    payload = json.loads(raw)
    assert payload["failure"]["failure_code"] == failure_code
    assert payload["failure"]["raw_failure_stage"] == raw_failure_stage
    assert payload["failure"]["canonical_failure_stage"] == expected_stage


def test_production_unknown_http_failure_keeps_safe_telemetry_without_evidence(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path, count=5)
    def fail_at_three(row, cache_root, campaign_id):
        if row["manifest_row_id"] == "0000000003":
            raise downloader.DownloadFailure(
                downloader.STOP_HTTP, failure_code="HTTP_FAILED",
                failure_stage="TD_FILES", retryable=True,
                attempts=[{"stage":"TD_FILES", "http_status":500,
                    "reason_phrase":"Server Error", "result_code":"HTTP_FAILED",
                    "endpoint":"https://api.jquants.com/v2/td/files/redacted",
                    "elapsed_seconds":0.25, "signed_url_received":False}],
            )
        return provider(row, cache_root, campaign_id)
    with pytest.raises(downloader.DownloadFailure):
        downloader.run_production_downloads(
            **_production_kwargs(env, environment, fail_at_three, runtime)
        )
    journal = json.loads((env["output"] / "journal.json").read_text(encoding="utf-8"))
    failed = journal["rows"]["0000000003"]
    assert failed["failure_code"] == "HTTP_FAILED"
    assert failed["raw_failure_stage"] == "TD_FILES"
    assert failed["canonical_failure_stage"] == "TD_FILES"
    assert failed["failure_telemetry"]["http_status"] == 500
    assert failed["failure_telemetry"]["endpoint_host"] == "api.jquants.com"
    assert "failure_evidence" not in failed


@pytest.mark.parametrize("change", [
    {"production_apply": False}, {"apply": False}, {"expected_count": 4},
    {"max_items": 6}, {"confirm_production_item_count": 4},
    {"confirm_production_item_count": 101}, {"confirm_campaign_id": "wrong"},
    {"campaign_db_sha256": "0" * 64}, {"manifest_byte_sha256": "0" * 64},
    {"manifest_semantic_sha256_value": "0" * 64},
])
def test_production_guard_failures_are_before_output_and_artifacts(tmp_path, change):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    kwargs = _production_kwargs(env, environment, provider, runtime)
    kwargs.update(change)
    with pytest.raises(downloader.FreshDownloaderStop):
        downloader.run_production_downloads(**kwargs)
    assert not env["output"].exists()
    assert not env["cache"].exists()


def test_production_allowlist_and_reparse_are_fail_closed(tmp_path, monkeypatch):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    kwargs = _production_kwargs(env, environment, provider, runtime)
    kwargs["cache_root"] = tmp_path / "wrong" / CAMPAIGN_ID
    kwargs["confirm_production_cache_root"] = str(kwargs["cache_root"])
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_PATH):
        downloader.run_production_downloads(**kwargs)
    kwargs = _production_kwargs(env, environment, provider, runtime)
    monkeypatch.setattr(downloader, "_has_reparse_component", lambda _path: True)
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_PATH):
        downloader.run_production_downloads(**kwargs)


def test_production_backup_failure_prevents_artifact_publish(tmp_path, monkeypatch):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    monkeypatch.setattr(downloader, "create_verified_backup", lambda *_a, **_k: (_ for _ in ()).throw(
        downloader.FreshDownloaderStop(downloader.STOP_PRODUCTION_BACKUP)
    ))
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_BACKUP):
        downloader.run_production_downloads(**_production_kwargs(env, environment, provider, runtime))
    assert not env["cache"].exists()


def test_production_runtime_active_prevents_output(tmp_path):
    env, environment, provider, _runtime, _checks = _production_fixture(tmp_path)
    def active(_repo):
        raise downloader.FreshDownloaderStop(downloader.STOP_PRODUCTION_RUNTIME)
    kwargs = _production_kwargs(env, environment, provider, active)
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_RUNTIME):
        downloader.run_production_downloads(**kwargs)
    assert not env["output"].exists()


def test_production_detects_database_change_after_artifacts(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    def changing_provider(row, cache_root, campaign_id):
        result = provider(row, cache_root, campaign_id)
        if row["manifest_row_id"] == "0000000005":
            conn = connect_db(env["db"])
            conn.execute("UPDATE campaigns SET updated_at='external' WHERE campaign_id=?", (CAMPAIGN_ID,))
            conn.commit(); conn.close()
        return result
    kwargs = _production_kwargs(env, environment, changing_provider, runtime)
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_DB_CHANGED):
        downloader.run_production_downloads(**kwargs)
    assert json.loads((env["output"] / "journal.json").read_text())["current_phase"] == "DB_PENDING"


def test_production_transaction_failure_rolls_back_all_five(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    def fail(index, _conn):
        if index == 2:
            raise sqlite3.OperationalError("injected")
    kwargs = _production_kwargs(env, environment, provider, runtime)
    kwargs["state_after_update"] = fail
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_DB_CAS):
        downloader.run_production_downloads(**kwargs)
    conn = connect_db(env["db"])
    assert conn.execute("SELECT COUNT(*) FROM campaign_fresh_downloads WHERE fresh_status='COMPLETE'").fetchone()[0] == 0
    conn.close()


def test_production_zip_only_and_invalid_provenance_fail_closed(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    directory, zip_path, _provenance = downloader._production_target_paths(env["cache"], CAMPAIGN_ID, "0000000001")
    directory.mkdir(parents=True)
    zip_path.write_bytes(b"partial")
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_ARTIFACT):
        downloader.run_production_downloads(**_production_kwargs(env, environment, provider, runtime))


@pytest.mark.parametrize("cache_status", ["READY", "SIDECAR_REQUIRED", "LEGACY_COPY_REQUIRED"])
def test_production_legacy_cache_state_with_fresh_not_started_downloads(tmp_path, cache_status):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    conn = connect_db(env["db"])
    conn.execute(
        "UPDATE campaign_filings SET identity_status='VERIFIED',cache_status=?,overall_status='IDENTITY_VERIFIED'",
        (cache_status,),
    )
    conn.commit(); conn.close()
    kwargs = _production_kwargs(env, environment, provider, runtime)
    result = downloader.run_production_downloads(**kwargs)
    assert result["summary"]["db_updated"] == 5


def test_production_fresh_complete_without_artifacts_fails_closed(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    conn = connect_db(env["db"])
    conn.execute("UPDATE campaign_fresh_downloads SET fresh_status='COMPLETE'")
    conn.commit(); conn.close()
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_COUNT):
        downloader.run_production_downloads(**_production_kwargs(env, environment, provider, runtime))


def test_production_quarantined_rejected_before_output_and_network(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    conn = connect_db(env["db"])
    conn.execute("UPDATE campaign_fresh_downloads SET fresh_status='QUARANTINED'")
    conn.commit(); conn.close()
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_COUNT):
        downloader.run_production_downloads(**_production_kwargs(env, environment, provider, runtime))
    assert not env["output"].exists()
    assert not env["cache"].exists()


def test_production_valid_artifacts_enable_database_only_repair(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    for row in env["rows"]:
        provider(row, env["cache"], CAMPAIGN_ID)
    def forbidden(*_args):
        raise AssertionError("provider must not be called")
    kwargs = _production_kwargs(env, environment, forbidden, runtime)
    result = downloader.run_production_downloads(**kwargs)
    assert {row["status"] for row in result["results"]} == {"DB_ONLY_REPAIR"}
    assert result["summary"]["network_calls"] == 0
    assert all(row["attempt_count"] == 0 for row in result["readback"])
    assert all(row["artifact_reused"] is True for row in result["journal"]["rows"].values())
    assert all(row["network_attempted"] is False for row in result["journal"]["rows"].values())


def test_production_reuses_ninety_six_and_publishes_only_four(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path, count=100)
    for row in env["rows"][:96]:
        provider(row, env["cache"], CAMPAIGN_ID)
    reused_before = {
        path.relative_to(env["cache"]).as_posix(): (_sha(path), path.stat().st_size, path.stat().st_mtime_ns)
        for path in env["cache"].rglob("*") if path.is_file()
    }
    calls = []
    def missing_only(row, cache_root, campaign_id):
        calls.append(row["manifest_row_id"])
        return provider(row, cache_root, campaign_id)
    conn = connect_db(env["db"])
    filings_before = downloader._campaign_rows_digest(conn, CAMPAIGN_ID, set())
    conn.close()
    result = downloader.run_production_downloads(
        **_production_kwargs(env, environment, missing_only, runtime)
    )
    assert calls == [f"{index:010d}" for index in range(97, 101)]
    assert result["summary"]["db_updated"] == 100
    assert result["summary"]["network_calls"] == 8
    assert sum(row["artifact_reused"] is True for row in result["journal"]["rows"].values()) == 96
    assert sum(row["network_attempts_started"] == 1 for row in result["journal"]["rows"].values()) == 4
    for relative, before in reused_before.items():
        path = env["cache"] / relative
        assert (_sha(path), path.stat().st_size, path.stat().st_mtime_ns) == before
    conn = connect_db(env["db"])
    counts = dict(conn.execute("SELECT fresh_status,COUNT(*) FROM campaign_fresh_downloads GROUP BY fresh_status"))
    attempts = dict(conn.execute("SELECT attempt_count,COUNT(*) FROM campaign_fresh_downloads GROUP BY attempt_count"))
    conn.close()
    assert counts == {"COMPLETE": 100}
    assert attempts == {0: 96, 1: 4}
    conn = connect_db(env["db"])
    assert downloader._campaign_rows_digest(conn, CAMPAIGN_ID, set()) == filings_before
    conn.close()


def test_production_exact_identity_artifacts_enable_database_only_repair(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    for row in env["rows"]:
        provider(row, env["cache"], CAMPAIGN_ID)
        _directory, _zip_path, provenance = downloader._production_target_paths(
            env["cache"], CAMPAIGN_ID, row["manifest_row_id"]
        )
        payload = json.loads(provenance.read_text(encoding="utf-8"))
        payload["identity_verdict"] = "exact_document_id_match"
        provenance.write_bytes(downloader._json_bytes(payload))

    def forbidden(*_args):
        raise AssertionError("provider must not be called")

    result = downloader.run_production_downloads(
        **_production_kwargs(env, environment, forbidden, runtime)
    )
    assert {row["status"] for row in result["results"]} == {"DB_ONLY_REPAIR"}
    assert {row["identity_verdict"] for row in result["results"]} == {"exact_document_id_match"}
    assert result["summary"]["network_calls"] == 0


@pytest.mark.parametrize("verdict", ["", "ambiguous", "ticker_mismatch"])
def test_production_non_ready_verdict_artifact_is_fail_closed(tmp_path, verdict):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    provider(env["rows"][0], env["cache"], CAMPAIGN_ID)
    _directory, _zip_path, provenance = downloader._production_target_paths(
        env["cache"], CAMPAIGN_ID, "0000000001"
    )
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    payload["identity_verdict"] = verdict
    provenance.write_bytes(downloader._json_bytes(payload))
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_ARTIFACT):
        downloader.run_production_downloads(
            **_production_kwargs(env, environment, provider, runtime)
        )


def test_exact_verdict_does_not_bypass_formal_loader(tmp_path):
    env, _environment, provider, _runtime, _checks = _production_fixture(tmp_path)
    provider(env["rows"][0], env["cache"], CAMPAIGN_ID)
    _directory, zip_path, provenance = downloader._production_target_paths(
        env["cache"], CAMPAIGN_ID, "0000000001"
    )
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    payload["identity_verdict"] = "exact_document_id_match"
    provenance.write_bytes(downloader._json_bytes(payload))
    zip_path.write_bytes(b"not-a-zip")
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_ARTIFACT):
        downloader._load_production_provenance(zip_path, provenance)


def test_production_protected_field_violation_rolls_back(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    def alter_protected(index, conn):
        if index == 1:
            conn.execute("UPDATE campaign_filings SET requested_disclosure_no='wrong' WHERE manifest_row_id='0000000002'")
    kwargs = _production_kwargs(env, environment, provider, runtime)
    kwargs["state_after_update"] = alter_protected
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_DB_CAS):
        downloader.run_production_downloads(**kwargs)
    conn = connect_db(env["db"])
    assert conn.execute("SELECT COUNT(*) FROM campaign_filings WHERE requested_disclosure_no='wrong'").fetchone()[0] == 0
    conn.close()


def test_production_single_row_cas_mismatch_rolls_back(tmp_path, monkeypatch):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    original = downloader.apply_fresh_download_successes
    def stale(conn, **kwargs):
        conn.execute("UPDATE campaign_fresh_downloads SET updated_at='external' WHERE manifest_row_id='0000000003'")
        conn.commit()
        return original(conn, **kwargs)
    monkeypatch.setattr(downloader, "apply_fresh_download_successes", stale)
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_DB_CAS):
        downloader.run_production_downloads(**_production_kwargs(env, environment, provider, runtime))
    conn = connect_db(env["db"])
    assert conn.execute("SELECT COUNT(*) FROM campaign_fresh_downloads WHERE fresh_status='COMPLETE'").fetchone()[0] == 0
    conn.close()


def test_production_invalid_provenance_is_artifact_conflict(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    provider(env["rows"][0], env["cache"], CAMPAIGN_ID)
    _directory, _zip_path, provenance = downloader._production_target_paths(
        env["cache"], CAMPAIGN_ID, "0000000001"
    )
    payload = json.loads(provenance.read_text())
    payload["zip_sha256"] = "0" * 64
    provenance.write_bytes(downloader._json_bytes(payload))
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_ARTIFACT):
        downloader.run_production_downloads(**_production_kwargs(env, environment, provider, runtime))


def test_production_already_complete_is_rejected_before_new_output(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    downloader.run_production_downloads(**_production_kwargs(env, environment, provider, runtime))
    before_sha = _sha(env["db"])
    second_output = tmp_path / "prod-output-2"
    second_environment = downloader.ProductionEnvironment(
        campaign_db=env["db"], cache_root=env["cache"], output_parent=tmp_path,
        output_pattern=re.compile(r"^prod-output-2$"),
    )
    def forbidden(*_args):
        raise AssertionError("provider must not run")
    kwargs = _production_kwargs(env, second_environment, forbidden, runtime)
    kwargs["output_dir"] = second_output
    kwargs["campaign_db_sha256"] = before_sha
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_COUNT):
        downloader.run_production_downloads(**kwargs)
    assert not second_output.exists()
    assert _sha(env["db"]) == before_sha


def test_production_complete_with_invalid_artifact_is_divergence(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    downloader.run_production_downloads(**_production_kwargs(env, environment, provider, runtime))
    _directory, _zip, provenance = downloader._production_target_paths(
        env["cache"], CAMPAIGN_ID, "0000000001"
    )
    payload = json.loads(provenance.read_text())
    payload["zip_sha256"] = "0" * 64
    provenance.write_bytes(downloader._json_bytes(payload))
    next_output = tmp_path / "prod-output-invalid-complete"
    next_environment = downloader.ProductionEnvironment(
        campaign_db=env["db"], cache_root=env["cache"], output_parent=tmp_path,
        output_pattern=re.compile(r"^prod-output-invalid-complete$"),
    )
    kwargs = _production_kwargs(env, next_environment, provider, runtime)
    kwargs["output_dir"] = next_output
    kwargs["campaign_db_sha256"] = _sha(env["db"])
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_COUNT):
        downloader.run_production_downloads(**kwargs)


def test_production_non_target_change_is_rolled_back_inside_transaction(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    conn = connect_db(env["db"])
    outside = _row(99)
    outside.pop("plan_classification"); outside.pop("download_allowed")
    outside.pop("auto_ready_allowed"); outside.pop("quarantine_release_required")
    with transaction(conn):
        create_campaign_filing(conn, outside)
    conn.close()
    kwargs = _production_kwargs(env, environment, provider, runtime)
    def alter_outside(index, conn):
        if index == 1:
            conn.execute("UPDATE campaign_filings SET error_message='changed' WHERE manifest_row_id='0000000099'")
    kwargs["state_after_update"] = alter_outside
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_DB_CAS):
        downloader.run_production_downloads(**kwargs)
    conn = connect_db(env["db"])
    assert conn.execute("SELECT error_message FROM campaign_filings WHERE manifest_row_id='0000000099'").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM campaign_fresh_downloads WHERE fresh_status='COMPLETE'").fetchone()[0] == 0
    conn.close()


def test_production_resumes_db_pending_journal_without_redownload(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    def changing_provider(row, cache_root, campaign_id):
        result = provider(row, cache_root, campaign_id)
        if row["manifest_row_id"] == "0000000005":
            conn = connect_db(env["db"])
            conn.execute("UPDATE campaigns SET updated_at='external' WHERE campaign_id=?", (CAMPAIGN_ID,))
            conn.commit(); conn.close()
        return result
    kwargs = _production_kwargs(env, environment, changing_provider, runtime)
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_DB_CHANGED):
        downloader.run_production_downloads(**kwargs)
    backup = env["output"] / "backup" / "backfill_campaign_v4.before.db"
    shutil.copyfile(backup, env["db"])
    def forbidden(*_args):
        raise AssertionError("resume must not redownload")
    kwargs = _production_kwargs(env, environment, forbidden, runtime)
    result = downloader.run_production_downloads(**kwargs)
    assert result["journal"]["current_phase"] == "COMPLETE"
    assert {row["status"] for row in result["results"]} == {"DB_ONLY_REPAIR"}


def test_cli_help_includes_production_contract_arguments():
    proc = subprocess.run([sys.executable, "tools/backfill_campaign_fresh_download.py", "--help"], cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0
    for option in (
        "--production-apply", "--campaign-db-sha256", "--download-plan-sha256",
        "--manifest-byte-sha256", "--manifest-semantic-sha256", "--expected-count",
        "--max-items", "--confirm-production-cache-root", "--confirm-campaign-id",
        "--confirm-production-item-count",
    ):
        assert option in proc.stdout
