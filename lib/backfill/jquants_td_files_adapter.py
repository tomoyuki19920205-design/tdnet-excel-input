"""Fail-closed J-Quants TD files adapter for campaign XBRL downloads.

The signed URL is deliberately kept in memory only.  Public evidence returned
by this module contains a redacted digest and the validated host, never the URL
query, API credential, or Authorization header.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

import requests

TD_FILES_ENDPOINT = "https://api.jquants.com/v2/td/files"
TD_FILES_TYPE = "x"
SOURCE_ROUTE = "JQUANTS_TD_FILES"
RETRYABLE_CLASSIFICATIONS = frozenset({"TD_FILES_RATE_LIMITED", "TD_FILES_SERVER_ERROR"})


class TdFilesAdapterError(RuntimeError):
    """A secret-free, stage-specific TD files failure."""

    def __init__(
        self,
        classification: str,
        *,
        stage: str,
        evidence: Mapping[str, object],
        retryable: bool = False,
    ) -> None:
        super().__init__(classification)
        self.classification = classification
        self.stage = stage
        self.evidence = dict(evidence)
        self.retryable = retryable


@dataclass(frozen=True)
class TdFilesResolution:
    """Validated Stage A result; ``signed_url`` must never be serialized."""

    signed_url: str
    evidence: dict[str, object]


@dataclass(frozen=True)
class SignedZipDownload:
    """Validated Stage B stream result."""

    sha256: str
    size: int
    evidence: dict[str, object]


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _elapsed(started: float) -> float:
    return round(max(0.0, time.monotonic() - started), 6)


def _safe_text(value: object, *, limit: int = 200) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    return text[:limit]


def _validate_disc_no(value: object) -> str:
    disc_no = str(value or "")
    if not re.fullmatch(r"[0-9A-Za-z]{10,32}", disc_no):
        raise ValueError("invalid requested disclosure number")
    return disc_no


def _credential_headers() -> tuple[dict[str, str], dict[str, object]]:
    # Reuse the established, secret-silent loader.  Do not use tools.jquants_auth,
    # whose legacy informational log includes a credential prefix.
    from src.jquants.adapter import _build_headers

    headers = _build_headers()
    key = str(headers.get("x-api-key") or "")
    if not key:
        raise TdFilesAdapterError(
            "TD_FILES_AUTH_FAILED",
            stage="TD_FILES",
            evidence={"credential_present": False, "credential_source_type": "project_env"},
        )
    return {"x-api-key": key}, {
        "credential_present": True,
        "credential_source_type": "project_env",
    }


def _stage_a_classification(status: int) -> tuple[str, bool]:
    if status == 401:
        return "TD_FILES_AUTH_FAILED", False
    if status == 403:
        return "TD_FILES_FORBIDDEN", False
    if status == 404:
        return "TD_FILES_DISCNO_NOT_FOUND", False
    if status == 429:
        return "TD_FILES_RATE_LIMITED", True
    if status in {500, 502, 503, 504}:
        return "TD_FILES_SERVER_ERROR", True
    return "TD_FILES_RESPONSE_SCHEMA_INVALID", False


def _redacted_url_digest(url: str) -> str:
    """Digest the complete ephemeral URL without returning it."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _signed_url_identity(url: object) -> tuple[str, str, str]:
    value = str(url or "")
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https" or not host or parts.username or parts.password:
        raise TdFilesAdapterError(
            "SIGNED_URL_HOST_REJECTED",
            stage="SIGNED_URL",
            evidence={"signed_url_received": bool(value), "signed_url_scheme": parts.scheme.lower(), "signed_url_host": host},
        )
    # J-Quants file URLs are AWS S3 presigned URLs.  Accept only an S3 endpoint,
    # not an arbitrary HTTPS hostname supplied by a malformed response.
    is_s3 = (
        host == "s3.amazonaws.com"
        or host.endswith(".s3.amazonaws.com")
        or bool(re.fullmatch(r"[a-z0-9.-]+\.s3[.-][a-z0-9-]+\.amazonaws\.com", host))
    )
    if not is_s3:
        raise TdFilesAdapterError(
            "SIGNED_URL_HOST_REJECTED",
            stage="SIGNED_URL",
            evidence={"signed_url_received": True, "signed_url_scheme": "https", "signed_url_host": host},
        )
    redacted = urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, "", ""))
    return host, _redacted_url_digest(value), redacted


def resolve_xbrl_file(
    *,
    requested_disclosure_no: str,
    session: requests.Session,
    timeout_seconds: float,
    network_counter: list[int],
    auth_loader: Callable[[], tuple[dict[str, str], dict[str, object]]] = _credential_headers,
) -> TdFilesResolution:
    """Resolve exactly one XBRL signed URL using an exact DiscNo query."""
    disc_no = _validate_disc_no(requested_disclosure_no)
    started_at = _now_utc()
    started = time.monotonic()
    try:
        headers, credential = auth_loader()
    except TdFilesAdapterError:
        raise
    except Exception as exc:
        raise TdFilesAdapterError(
            "TD_FILES_AUTH_FAILED",
            stage="TD_FILES",
            evidence={"credential_present": False, "credential_source_type": "project_env", "exception_type": type(exc).__name__},
        ) from exc
    evidence: dict[str, object] = {
        "stage": "TD_FILES",
        "requested_disc_no": disc_no,
        "td_files_type": TD_FILES_TYPE,
        "endpoint": TD_FILES_ENDPOINT,
        "request_started_at": started_at,
        "request_finished_at": None,
        "elapsed_seconds": None,
        "http_status": None,
        "reason_phrase": None,
        "response_headers": {},
        "retry_after": None,
        "result_code": None,
        "xbrl_candidate_count": 0,
        **credential,
    }
    response: requests.Response | None = None
    try:
        network_counter[0] += 1
        response = session.get(
            TD_FILES_ENDPOINT,
            headers=headers,
            params={"discNo": disc_no, "type": TD_FILES_TYPE},
            timeout=timeout_seconds,
            allow_redirects=False,
        )
        status = int(response.status_code)
        evidence["http_status"] = status
        if status != 200:
            classification, retryable = _stage_a_classification(status)
            evidence.update({
                "result_code": classification,
                "reason_phrase": _safe_text(getattr(response, "reason", "")),
                "retry_after": _safe_text(response.headers.get("Retry-After")) or None,
            })
            raise TdFilesAdapterError(classification, stage="TD_FILES", evidence=evidence, retryable=retryable)
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            evidence["result_code"] = "TD_FILES_RESPONSE_SCHEMA_INVALID"
            raise TdFilesAdapterError("TD_FILES_RESPONSE_SCHEMA_INVALID", stage="TD_FILES", evidence=evidence) from exc
        if not isinstance(payload, dict) or str(payload.get("discNo") or "") != disc_no:
            evidence["result_code"] = "TD_FILES_RESPONSE_SCHEMA_INVALID"
            raise TdFilesAdapterError("TD_FILES_RESPONSE_SCHEMA_INVALID", stage="TD_FILES", evidence=evidence)
        files = payload.get("files")
        if not isinstance(files, dict):
            evidence["result_code"] = "TD_FILES_RESPONSE_SCHEMA_INVALID"
            raise TdFilesAdapterError("TD_FILES_RESPONSE_SCHEMA_INVALID", stage="TD_FILES", evidence=evidence)
        raw = files.get("xbrl")
        if raw is None or raw == "" or raw == []:
            evidence.update({"result_code": "TD_FILES_XBRL_NOT_AVAILABLE", "xbrl_candidate_count": 0})
            raise TdFilesAdapterError("TD_FILES_XBRL_NOT_AVAILABLE", stage="TD_FILES", evidence=evidence)
        candidates = [raw] if isinstance(raw, str) else list(raw) if isinstance(raw, list) else []
        if not candidates or any(not isinstance(item, str) or not item for item in candidates):
            evidence["result_code"] = "TD_FILES_RESPONSE_SCHEMA_INVALID"
            raise TdFilesAdapterError("TD_FILES_RESPONSE_SCHEMA_INVALID", stage="TD_FILES", evidence=evidence)
        evidence["xbrl_candidate_count"] = len(candidates)
        if len(candidates) != 1:
            evidence["result_code"] = "TD_FILES_MULTIPLE_XBRL_CANDIDATES"
            raise TdFilesAdapterError("TD_FILES_MULTIPLE_XBRL_CANDIDATES", stage="TD_FILES", evidence=evidence)
        host, digest, _redacted = _signed_url_identity(candidates[0])
        received_at = _now_utc()
        evidence.update({
            "result_code": "TD_FILES_OK",
            "signed_url_received": True,
            "signed_url_host": host,
            "signed_url_scheme": "https",
            "signed_url_received_at": received_at,
            "signed_url_redacted_digest": digest,
        })
        return TdFilesResolution(signed_url=candidates[0], evidence=evidence)
    except requests.RequestException as exc:
        evidence.update({"result_code": "TD_FILES_SERVER_ERROR", "exception_type": type(exc).__name__})
        raise TdFilesAdapterError("TD_FILES_SERVER_ERROR", stage="TD_FILES", evidence=evidence, retryable=True) from exc
    finally:
        evidence["request_finished_at"] = _now_utc()
        evidence["elapsed_seconds"] = _elapsed(started)
        if response is not None:
            response.close()


def download_signed_zip(
    *,
    resolution: TdFilesResolution,
    destination: Path,
    session: requests.Session,
    timeout_seconds: float,
    network_counter: list[int],
) -> SignedZipDownload:
    """Stream one validated signed URL to a new file and fsync it."""
    host, digest, _redacted = _signed_url_identity(resolution.signed_url)
    started_at = _now_utc()
    started = time.monotonic()
    evidence: dict[str, object] = {
        "stage": "SIGNED_URL",
        "signed_url_host": host,
        "signed_url_scheme": "https",
        "signed_url_redacted_digest": digest,
        "download_started_at": started_at,
        "download_finished_at": None,
        "elapsed_seconds": None,
        "http_status": None,
        "content_type": None,
        "content_length": None,
        "bytes_received": 0,
        "zip_sha256": None,
        "result_code": None,
    }
    response: requests.Response | None = None
    try:
        network_counter[0] += 1
        response = session.get(
            resolution.signed_url,
            timeout=timeout_seconds,
            stream=True,
            allow_redirects=False,
        )
        status = int(response.status_code)
        content_type = str(response.headers.get("Content-Type") or "")
        content_length = str(response.headers.get("Content-Length") or "") or None
        evidence.update({"http_status": status, "content_type": content_type or None, "content_length": content_length})
        evidence.update({
            "reason_phrase": _safe_text(getattr(response, "reason", "")) or None,
            "response_headers": {
                str(key).lower(): str(value)
                for key, value in response.headers.items()
                if str(key).lower() in {"content-type", "content-length", "retry-after", "date", "server"}
            },
            "retry_after": _safe_text(response.headers.get("Retry-After")) or None,
        })
        if status != 200:
            classification = "SIGNED_URL_EXPIRED" if status in {401, 403, 410} else "SIGNED_URL_DOWNLOAD_FAILED"
            retryable = status == 429 or status in {500, 502, 503, 504}
            evidence["result_code"] = classification
            raise TdFilesAdapterError(classification, stage="SIGNED_URL", evidence=evidence, retryable=retryable)
        if "html" in content_type.lower():
            evidence["result_code"] = "SIGNED_URL_DOWNLOAD_FAILED"
            raise TdFilesAdapterError("SIGNED_URL_DOWNLOAD_FAILED", stage="SIGNED_URL", evidence=evidence)
        digest_state = hashlib.sha256()
        total = 0
        with destination.open("xb") as stream:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                stream.write(chunk)
                digest_state.update(chunk)
                total += len(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if content_length is not None:
            try:
                if int(content_length) != total:
                    raise ValueError("content length mismatch")
            except ValueError as exc:
                evidence.update({"bytes_received": total, "result_code": "SIGNED_URL_DOWNLOAD_FAILED"})
                raise TdFilesAdapterError("SIGNED_URL_DOWNLOAD_FAILED", stage="SIGNED_URL", evidence=evidence) from exc
        zip_sha = digest_state.hexdigest()
        evidence.update({"bytes_received": total, "zip_sha256": zip_sha, "result_code": "SIGNED_URL_DOWNLOAD_OK"})
        return SignedZipDownload(sha256=zip_sha, size=total, evidence=evidence)
    except TdFilesAdapterError:
        raise
    except requests.RequestException as exc:
        evidence.update({
            "result_code": "SIGNED_URL_DOWNLOAD_FAILED",
            "exception_type": type(exc).__name__,
            "exception_message": _safe_text(exc),
        })
        raise TdFilesAdapterError("SIGNED_URL_DOWNLOAD_FAILED", stage="SIGNED_URL", evidence=evidence, retryable=True) from exc
    finally:
        evidence["download_finished_at"] = _now_utc()
        evidence["elapsed_seconds"] = _elapsed(started)
        if response is not None:
            response.close()


def contains_secret_material(value: object) -> bool:
    """Conservative recursive audit helper for serialized evidence."""
    text = value if isinstance(value, str) else ""
    if text and ("x-amz-signature=" in text.lower() or "x-api-key" in text.lower() or "authorization" in text.lower()):
        return True
    if isinstance(value, Mapping):
        return any(contains_secret_material(key) or contains_secret_material(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_secret_material(item) for item in value)
    return False
