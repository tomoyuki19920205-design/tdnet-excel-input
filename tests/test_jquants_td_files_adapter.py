from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import requests

from lib.backfill import jquants_td_files_adapter as adapter


SIGNED_URL = (
    "https://fixture-bucket.s3.ap-northeast-1.amazonaws.com/file.zip"
    "?X-Amz-Signature=secret&X-Amz-Expires=900"
)
R2_ACCOUNT = "450d912298d9da685fb5742a5ec56b76"
R2_SIGNED_URL = (
    f"https://{R2_ACCOUNT}.r2.cloudflarestorage.com/td/xbrl.zip"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=secret"
)


class Response:
    def __init__(self, status=200, *, payload=None, body=b"", headers=None, reason=""):
        self.status_code = status
        self._payload = payload
        self.body = body
        self.headers = dict(headers or {})
        self.headers.setdefault("Content-Length", str(len(body)))
        self.reason = reason
        self.closed = False

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload

    def iter_content(self, chunk_size=65536):
        for offset in range(0, len(self.body), max(1, min(chunk_size, 3))):
            yield self.body[offset:offset + max(1, min(chunk_size, 3))]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def auth():
    return {"x-api-key": "test-secret"}, {
        "credential_present": True,
        "credential_source_type": "test",
    }


def ok_payload(disc="20230724525928", xbrl=SIGNED_URL):
    return {"discNo": disc, "files": {"pdf": None, "summaryPdf": None, "xbrl": xbrl}}


def resolve(session, disc="20230724525928"):
    counter = [0]
    result = adapter.resolve_xbrl_file(
        requested_disclosure_no=disc,
        session=session,
        timeout_seconds=1,
        network_counter=counter,
        auth_loader=auth,
    )
    return result, counter


def test_exact_disc_no_query():
    session = Session(Response(payload=ok_payload()))
    resolve(session)
    assert session.calls[0][1]["params"]["discNo"] == "20230724525928"


def test_type_is_fixed_to_x():
    session = Session(Response(payload=ok_payload()))
    resolve(session)
    assert session.calls[0][1]["params"] == {"discNo": "20230724525928", "type": "x"}


def test_only_td_files_endpoint_is_used_for_stage_a():
    session = Session(Response(payload=ok_payload()))
    resolve(session)
    assert session.calls[0][0] == "https://api.jquants.com/v2/td/files"


def test_disc_no_is_not_date_derived():
    disc = "ABCDEF12345678"
    session = Session(Response(payload=ok_payload(disc)))
    resolve(session, disc)
    assert session.calls[0][1]["params"]["discNo"] == disc


def test_auth_loader_header_is_used_but_not_evidence():
    session = Session(Response(payload=ok_payload()))
    result, _ = resolve(session)
    assert session.calls[0][1]["headers"] == {"x-api-key": "test-secret"}
    assert "test-secret" not in json.dumps(result.evidence)


def test_200_returns_one_signed_url_in_memory():
    result, counter = resolve(Session(Response(payload=ok_payload())))
    assert result.signed_url == SIGNED_URL
    assert counter == [1]
    assert result.evidence["result_code"] == "TD_FILES_OK"


def test_response_disc_no_must_match_exactly():
    with pytest.raises(adapter.TdFilesAdapterError, match="TD_FILES_RESPONSE_SCHEMA_INVALID"):
        resolve(Session(Response(payload=ok_payload("different"))))


def test_files_object_is_required():
    with pytest.raises(adapter.TdFilesAdapterError, match="TD_FILES_RESPONSE_SCHEMA_INVALID"):
        resolve(Session(Response(payload={"discNo": "20230724525928"})))


def test_xbrl_candidate_zero_is_distinct():
    with pytest.raises(adapter.TdFilesAdapterError, match="TD_FILES_XBRL_NOT_AVAILABLE"):
        resolve(Session(Response(payload=ok_payload(xbrl=None))))


def test_xbrl_candidate_multiple_is_distinct():
    with pytest.raises(adapter.TdFilesAdapterError, match="TD_FILES_MULTIPLE_XBRL_CANDIDATES"):
        resolve(Session(Response(payload=ok_payload(xbrl=[SIGNED_URL, SIGNED_URL]))))


def test_xbrl_candidate_schema_rejects_non_string():
    with pytest.raises(adapter.TdFilesAdapterError, match="TD_FILES_RESPONSE_SCHEMA_INVALID"):
        resolve(Session(Response(payload=ok_payload(xbrl={"url": SIGNED_URL}))))


@pytest.mark.parametrize(
    ("status", "classification", "retryable"),
    [
        (401, "TD_FILES_AUTH_FAILED", False),
        (403, "TD_FILES_FORBIDDEN", False),
        (404, "TD_FILES_DISCNO_NOT_FOUND", False),
        (429, "TD_FILES_RATE_LIMITED", True),
        (503, "TD_FILES_SERVER_ERROR", True),
    ],
)
def test_td_files_http_classification(status, classification, retryable):
    with pytest.raises(adapter.TdFilesAdapterError) as caught:
        resolve(Session(Response(status=status, reason="safe")))
    assert caught.value.classification == classification
    assert caught.value.retryable is retryable


def test_td_files_network_exception_is_retryable_server_error():
    with pytest.raises(adapter.TdFilesAdapterError) as caught:
        resolve(Session(requests.Timeout("timeout")))
    assert caught.value.classification == "TD_FILES_SERVER_ERROR"
    assert caught.value.retryable is True


def test_signed_url_requires_https():
    with pytest.raises(adapter.TdFilesAdapterError, match="SIGNED_URL_HOST_REJECTED"):
        resolve(Session(Response(payload=ok_payload(xbrl="http://bucket.s3.amazonaws.com/x.zip"))))


def test_signed_url_rejects_non_s3_host():
    with pytest.raises(adapter.TdFilesAdapterError, match="SIGNED_URL_HOST_REJECTED"):
        resolve(Session(Response(payload=ok_payload(xbrl="https://example.com/x.zip?token=x"))))


@pytest.mark.parametrize(
    "url",
    [
        R2_SIGNED_URL,
        "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/x.zip?signature=x",
        "https://450D912298D9DA685FB5742A5EC56B76.R2.CLOUDFLARESTORAGE.COM/x.zip?signature=x",
    ],
)
def test_exact_cloudflare_r2_account_host_is_allowed(url):
    result, _ = resolve(Session(Response(payload=ok_payload(xbrl=url))))
    assert result.evidence["signed_url_host"] == url.split("/", 3)[2].lower()


@pytest.mark.parametrize(
    "url",
    [
        "https://1234567890abcdef1234567890abcde.r2.cloudflarestorage.com/x.zip?signature=x",
        "https://1234567890abcdef1234567890abcdef0.r2.cloudflarestorage.com/x.zip?signature=x",
        "https://g50d912298d9da685fb5742a5ec56b76.r2.cloudflarestorage.com/x.zip?signature=x",
        "https://cloudflarestorage.com/x.zip?signature=x",
        "https://r2.cloudflarestorage.com/x.zip?signature=x",
        "https://450d912298d9da685fb5742a5ec56b76.r2.cloudflarestorage.com.example.com/x.zip?signature=x",
        "https://450d912298d9da685fb5742a5ec56b76.r2.dev/x.zip?signature=x",
        "https://downloads.example.net/x.zip?signature=x",
        "http://450d912298d9da685fb5742a5ec56b76.r2.cloudflarestorage.com/x.zip?signature=x",
        "https://user@450d912298d9da685fb5742a5ec56b76.r2.cloudflarestorage.com/x.zip?signature=x",
        "https://450d912298d9da685fb5742a5ec56b76.r2.cloudflarestorage.com:8443/x.zip?signature=x",
        "https://450d912298d9da685fb5742a5ec56b76.r2.cloudflarestorage.com/x.zip?signature=x#fragment",
        "https://450d912298d9da685fb5742a5ec56b76.r2.cloudflarestorage.com/x.zip",
        "https://450d912298d9da685fb5742a5ec56b76.r2.cloudflarestorage.com?signature=x",
    ],
)
def test_cloudflare_r2_near_miss_is_rejected(url):
    with pytest.raises(adapter.TdFilesAdapterError, match="SIGNED_URL_HOST_REJECTED"):
        resolve(Session(Response(payload=ok_payload(xbrl=url))))


def test_host_rejection_preserves_stage_a_diagnostics():
    with pytest.raises(adapter.TdFilesAdapterError) as caught:
        resolve(Session(Response(payload=ok_payload(xbrl="https://example.com/x.zip?token=x"), reason="OK")))
    evidence = caught.value.evidence
    assert evidence["stage"] == "TD_FILES"
    assert evidence["http_status"] == 200
    assert evidence["reason_phrase"] == "OK"
    assert evidence["xbrl_candidate_count"] == 1
    assert evidence["signed_url_received"] is True


def test_download_rejects_url_not_attested_by_td_files_response(tmp_path):
    resolution = adapter.TdFilesResolution(signed_url=R2_SIGNED_URL, evidence={})
    session = Session(Response(body=b"unused"))
    counter = [0]
    with pytest.raises(adapter.TdFilesAdapterError, match="SIGNED_URL_HOST_REJECTED"):
        adapter.download_signed_zip(
            resolution=resolution, destination=tmp_path / "x.zip",
            session=session, timeout_seconds=1, network_counter=counter,
        )
    assert session.calls == [] and counter == [0]


def test_signed_url_expired_is_distinct(tmp_path):
    resolution, _ = resolve(Session(Response(payload=ok_payload())))
    with pytest.raises(adapter.TdFilesAdapterError, match="SIGNED_URL_EXPIRED"):
        adapter.download_signed_zip(
            resolution=resolution, destination=tmp_path / "x.zip",
            session=Session(Response(status=403)), timeout_seconds=1, network_counter=[0],
        )


def test_signed_url_other_http_failure_is_distinct(tmp_path):
    resolution, _ = resolve(Session(Response(payload=ok_payload())))
    with pytest.raises(adapter.TdFilesAdapterError, match="SIGNED_URL_DOWNLOAD_FAILED"):
        adapter.download_signed_zip(
            resolution=resolution, destination=tmp_path / "x.zip",
            session=Session(Response(status=500)), timeout_seconds=1, network_counter=[0],
        )


def test_signed_zip_is_streamed_and_hashed(tmp_path):
    body = b"abcdefghij"
    resolution, _ = resolve(Session(Response(payload=ok_payload())))
    result = adapter.download_signed_zip(
        resolution=resolution, destination=tmp_path / "x.zip",
        session=Session(Response(body=body, headers={"Content-Type": "application/zip"})),
        timeout_seconds=1, network_counter=[0],
    )
    assert (tmp_path / "x.zip").read_bytes() == body
    assert result.sha256 == hashlib.sha256(body).hexdigest()


def test_content_length_mismatch_is_rejected(tmp_path):
    resolution, _ = resolve(Session(Response(payload=ok_payload())))
    with pytest.raises(adapter.TdFilesAdapterError, match="SIGNED_URL_DOWNLOAD_FAILED"):
        adapter.download_signed_zip(
            resolution=resolution, destination=tmp_path / "x.zip",
            session=Session(Response(body=b"zip", headers={"Content-Length": "99"})),
            timeout_seconds=1, network_counter=[0],
        )


def test_html_download_is_rejected(tmp_path):
    resolution, _ = resolve(Session(Response(payload=ok_payload())))
    with pytest.raises(adapter.TdFilesAdapterError, match="SIGNED_URL_DOWNLOAD_FAILED"):
        adapter.download_signed_zip(
            resolution=resolution, destination=tmp_path / "x.zip",
            session=Session(Response(body=b"<html>", headers={"Content-Type": "text/html"})),
            timeout_seconds=1, network_counter=[0],
        )


def test_signed_url_is_not_present_in_evidence():
    result, _ = resolve(Session(Response(payload=ok_payload())))
    serialized = json.dumps(result.evidence, sort_keys=True)
    assert SIGNED_URL not in serialized
    assert "X-Amz-Signature" not in serialized


def test_redacted_digest_is_deterministic():
    one, _ = resolve(Session(Response(payload=ok_payload())))
    two, _ = resolve(Session(Response(payload=ok_payload())))
    assert one.evidence["signed_url_redacted_digest"] == two.evidence["signed_url_redacted_digest"]


def test_secret_material_audit_detects_signature_and_headers():
    assert adapter.contains_secret_material({"x": "https://x/?X-Amz-Signature=secret"})
    assert adapter.contains_secret_material({"Authorization": "Bearer secret"})
    assert adapter.contains_secret_material({"x-api-key": "secret"})


def test_secret_material_audit_accepts_public_evidence():
    result, _ = resolve(Session(Response(payload=ok_payload())))
    assert adapter.contains_secret_material(result.evidence) is False


def test_stage_a_and_b_each_increment_once(tmp_path):
    session = Session(
        Response(payload=ok_payload()),
        Response(body=b"zip", headers={"Content-Type": "application/zip"}),
    )
    counter = [0]
    resolution = adapter.resolve_xbrl_file(
        requested_disclosure_no="20230724525928", session=session,
        timeout_seconds=1, network_counter=counter, auth_loader=auth,
    )
    adapter.download_signed_zip(
        resolution=resolution, destination=tmp_path / "x.zip",
        session=session, timeout_seconds=1, network_counter=counter,
    )
    assert counter == [2]


def test_import_has_no_filesystem_side_effect(tmp_path):
    before = list(tmp_path.iterdir())
    importlib.import_module("lib.backfill.jquants_td_files_adapter")
    assert list(tmp_path.iterdir()) == before


def test_module_import_smoke_in_subprocess():
    completed = subprocess.run(
        [sys.executable, "-c", "import lib.backfill.jquants_td_files_adapter; print('ok')"],
        check=True, capture_output=True, text=True,
    )
    assert completed.stdout.strip() == "ok"
    assert completed.stderr == ""


def test_invalid_disc_no_stops_before_network():
    session = Session()
    with pytest.raises(ValueError):
        resolve(session, "../bad")
    assert session.calls == []


def test_auth_failure_stops_before_network():
    session = Session()

    def missing():
        raise RuntimeError("missing")

    with pytest.raises(adapter.TdFilesAdapterError) as caught:
        adapter.resolve_xbrl_file(
            requested_disclosure_no="20230724525928", session=session,
            timeout_seconds=1, network_counter=[0], auth_loader=missing,
        )
    assert caught.value.classification == "TD_FILES_AUTH_FAILED"
    assert session.calls == []
