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
    connect_db, create_campaign, create_campaign_filing, initialize_schema, transaction,
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


def test_standard_identity_mismatch_fails(tmp_path, monkeypatch):
    env = _prepare(tmp_path)
    body = _zip_bytes(internal=env["rows"][0]["requested_disclosure_no"])
    monkeypatch.setattr(downloader, "verify_zip_identity", lambda *a, **k: ZipIdentityVerdict(False, "", "ticker_mismatch", "r", "i", ""))
    result = _run(env, FakeSession([FakeResponse(body=body)]))
    assert result["summary"]["standard_failed"] == 1
    assert not _target(env)[2].exists()


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


def _production_fixture(tmp_path: Path):
    rows = [_row(index) for index in range(1, 6)]
    env = _prepare(tmp_path, rows)
    env["cache"] = tmp_path / "repo" / "data" / "v4_campaign_cache" / CAMPAIGN_ID
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
    return {
        "campaign_db": env["db"], "campaign_id": CAMPAIGN_ID,
        "campaign_db_sha256": _sha(env["db"]), "download_plan": env["plan"],
        "download_plan_sha256": _sha(env["plan"]), "manifest_list": env["manifest"],
        "manifest_byte_sha256": _sha(env["manifest"]),
        "manifest_semantic_sha256_value": downloader.manifest_semantic_sha256(selected),
        "cache_root": env["cache"], "output_dir": env["output"],
        "expected_count": 5, "max_items": 5,
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
    assert result["summary"]["network_calls"] == 0
    assert result["summary"]["non_target_changed"] == 0
    assert result["journal"]["current_phase"] == "COMPLETE"
    assert len(checks) == 4
    assert sum(1 for path in env["cache"].rglob("xbrl.zip")) == 5
    assert sum(1 for path in env["cache"].rglob("provenance.json")) == 5
    assert not list(env["cache"].rglob("*.tmp"))
    conn = connect_db(env["db"])
    rows = conn.execute("SELECT * FROM campaign_filings ORDER BY manifest_row_id").fetchall()
    assert len(rows) == 5
    assert all((row["identity_status"], row["cache_status"], row["overall_status"]) == (
        "VERIFIED", "READY", "IDENTITY_VERIFIED",
    ) for row in rows)
    assert all(row["error_code"] is None and row["error_stage"] is None and row["error_message"] is None for row in rows)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


@pytest.mark.parametrize("change", [
    {"production_apply": False}, {"apply": False}, {"expected_count": 4},
    {"max_items": 6}, {"confirm_campaign_id": "wrong"},
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
    assert conn.execute("SELECT COUNT(*) FROM campaign_filings WHERE cache_status='READY'").fetchone()[0] == 0
    conn.close()


def test_production_zip_only_and_invalid_provenance_fail_closed(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    directory, zip_path, _provenance = downloader._production_target_paths(env["cache"], CAMPAIGN_ID, "0000000001")
    directory.mkdir(parents=True)
    zip_path.write_bytes(b"partial")
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_ARTIFACT):
        downloader.run_production_downloads(**_production_kwargs(env, environment, provider, runtime))


def test_production_database_ready_without_artifacts_fails_closed(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    conn = connect_db(env["db"])
    conn.execute("UPDATE campaign_filings SET identity_status='VERIFIED', cache_status='READY'")
    conn.commit(); conn.close()
    kwargs = _production_kwargs(env, environment, provider, runtime)
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_DIVERGENCE):
        downloader.run_production_downloads(**kwargs)


def test_production_valid_artifacts_enable_database_only_repair(tmp_path):
    env, environment, provider, runtime, _checks = _production_fixture(tmp_path)
    for row in env["rows"]:
        provider(row, env["cache"], CAMPAIGN_ID)
    def forbidden(*_args):
        raise AssertionError("provider must not be called")
    kwargs = _production_kwargs(env, environment, forbidden, runtime)
    result = downloader.run_production_downloads(**kwargs)
    assert {row["status"] for row in result["results"]} == {"DB_ONLY_REPAIR"}


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
        conn.execute("UPDATE campaign_filings SET error_message='external' WHERE manifest_row_id='0000000003'")
        conn.commit()
        return original(conn, **kwargs)
    monkeypatch.setattr(downloader, "apply_fresh_download_successes", stale)
    with pytest.raises(downloader.FreshDownloaderStop, match=downloader.STOP_PRODUCTION_DB_CAS):
        downloader.run_production_downloads(**_production_kwargs(env, environment, provider, runtime))
    conn = connect_db(env["db"])
    assert conn.execute("SELECT COUNT(*) FROM campaign_filings WHERE cache_status='READY'").fetchone()[0] == 0
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


def test_production_already_complete_is_database_no_change(tmp_path):
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
    result = downloader.run_production_downloads(**kwargs)
    assert result["summary"]["db_updated"] == 0
    assert {row["status"] for row in result["results"]} == {"ALREADY_COMPLETE"}
    assert _sha(env["db"]) == before_sha


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
    assert conn.execute("SELECT COUNT(*) FROM campaign_filings WHERE cache_status='READY'").fetchone()[0] == 0
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
    ):
        assert option in proc.stdout
