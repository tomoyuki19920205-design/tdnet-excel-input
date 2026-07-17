from __future__ import annotations

import hashlib
import io
import json
import os
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


def test_content_type_rejection_has_distinct_code(tmp_path):
    env = _prepare(tmp_path)
    result = _run(env, FakeSession([FakeResponse(body=b"html", headers={"Content-Type": "text/html"})]), max_retries=1)
    failure = result["results"][0]
    assert failure["failure_code"] == "SIGNED_URL_DOWNLOAD_FAILED"
    assert failure["failure_stage"] == "SIGNED_URL"
    assert failure["http_status"] == 200


def test_cli_help_has_all_contract_arguments():
    proc = subprocess.run([sys.executable, "tools/backfill_campaign_fresh_download.py", "--help"], cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0
    for option in ("--campaign-db", "--campaign-id", "--download-plan", "--manifest-list", "--cache-root", "--output-dir", "--source-route", "--apply"):
        assert option in proc.stdout
