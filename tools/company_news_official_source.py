"""Deterministic official-source resolution and stable company identity reuse.

The module has no import-time I/O. Network access is available only through an
explicit transport supplied to :class:`OfficialSourceResolver`.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter


IDENTITY_STATUSES = {"VERIFIED", "UNVERIFIED", "STALE"}
WINDOW_STATES = {"COMPLETE", "PARTIAL", "BLOCKED", "NOT_APPLICABLE"}
FAILURE_TYPES = {
    "HTTP_4XX", "HTTP_5XX", "TIMEOUT", "DNS", "TLS", "TLS_CA_CHAIN", "TLS_HOSTNAME",
    "TLS_EXPIRED", "TLS_NOT_YET_VALID", "TLS_PROTOCOL", "TLS_HANDSHAKE", "TLS_UNKNOWN",
    "TLS_TRUST_INIT", "REDIRECT_BLOCKED",
    "MIME_REJECTED", "SIZE_LIMIT", "ACCESS_DENIED", "OUTCOME_LOST",
    "BODY_READ", "BODY_ENCODING", "NORMALIZATION", "EMPTY_BODY", "UNKNOWN",
}
TLS_BACKEND_CURRENT_CA = "current-ca-v1"
TLS_BACKEND_WINDOWS_SYSTEM_TRUST = "windows-system-trust-v1"
TLS_BACKEND_WINDOWS_ROOT_BUNDLE_SEMANTIC = "windows-root-bundle-v1"
SCHANNEL_TRANSPORT_ID = "windows-schannel-v1"
SCHANNEL_IMPLEMENTATION_FINGERPRINT = "dotnet10-sslstream-schannel-cryptoapi-v4"
HTTP_JOURNAL_PHASES = {
    "PLANNED", "REQUEST_STARTED", "RESPONSE_RECEIVED", "NORMALIZED", "FAILED", "OUTCOME_LOST",
}
VERIFICATION_METHODS = {"ACTUAL_OFFICIAL_BODY", "REPOSITORY_VERIFIED_ARTIFACT"}
ALLOWED_MIME_TYPES = {
    "text/html", "application/xhtml+xml", "application/pdf", "text/plain",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DATE = re.compile(r"\b(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])\b")


class OfficialSourceError(ValueError):
    """Raised when identity, coverage, or fetch policy violates the contract."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalized_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = re.sub(r"[\s\u3000]", "", normalized)
    for designator in ("株式会社", "㈱", "(株)", "（株）"):
        normalized = normalized.replace(designator, "")
    return normalized.casefold()


def _domain(value: str) -> str:
    return value.strip().lower().rstrip(".")


def _canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def _is_official_host(hostname: str, official_domain: str) -> bool:
    host, domain = _domain(hostname), _domain(official_domain)
    return bool(domain) and (host == domain or host.endswith("." + domain))


@dataclass(frozen=True)
class CompanyIdentityRecord:
    security_code: str
    company_name: str
    normalized_name: str
    official_domain: str
    verified_official_url: str | None
    verification_source: str | None
    verification_method: str | None
    verified_at: str | None
    evidence_hash: str | None
    status: str
    evidence_excerpt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def company_identity_registry_schema() -> dict[str, Any]:
    nullable = lambda kind: {"type": [kind, "null"]}
    record = {
        "type": "object", "additionalProperties": False,
        "required": ["security_code", "company_name", "normalized_name", "official_domain",
                     "verified_official_url", "verification_source", "verification_method",
                     "verified_at", "evidence_hash", "status", "evidence_excerpt"],
        "properties": {
            "security_code": {"type": "string"}, "company_name": {"type": "string"},
            "normalized_name": {"type": "string"}, "official_domain": {"type": "string"},
            "verified_official_url": nullable("string"), "verification_source": nullable("string"),
            "verification_method": nullable("string"), "verified_at": nullable("string"),
            "evidence_hash": nullable("string"), "status": {"type": "string", "enum": sorted(IDENTITY_STATUSES)},
            "evidence_excerpt": nullable("string"),
        },
    }
    return {"type": "object", "additionalProperties": False, "required": ["schema_version", "records"],
            "properties": {"schema_version": {"type": "string", "const": "company_identity_registry_v1"},
                           "records": {"type": "array", "items": record}}}


def validate_identity_record(record: CompanyIdentityRecord) -> CompanyIdentityRecord:
    if record.status not in IDENTITY_STATUSES:
        raise OfficialSourceError("invalid identity status")
    if not record.security_code.strip() or not record.company_name.strip() or not record.official_domain.strip():
        raise OfficialSourceError("identity key fields are required")
    if record.normalized_name != _normalized_name(record.company_name):
        raise OfficialSourceError("normalized company name mismatch")
    if record.status != "VERIFIED":
        return record
    if record.verification_method not in VERIFICATION_METHODS:
        raise OfficialSourceError("VERIFIED identity requires an accepted body verification method")
    if not all((record.verified_official_url, record.verification_source, record.verified_at,
                record.evidence_hash, record.evidence_excerpt)):
        raise OfficialSourceError("VERIFIED identity evidence is incomplete")
    parsed = urlsplit(record.verified_official_url or "")
    if parsed.scheme != "https" or not parsed.hostname or not _is_official_host(parsed.hostname, record.official_domain):
        raise OfficialSourceError("verified identity URL is outside the official domain")
    if not _SHA256.fullmatch(record.evidence_hash or ""):
        raise OfficialSourceError("invalid identity evidence hash")
    try:
        datetime.fromisoformat((record.verified_at or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise OfficialSourceError("invalid identity verified_at") from exc
    return record


class CompanyIdentityRegistry:
    def __init__(self, records: list[CompanyIdentityRecord] | None = None) -> None:
        self._records: dict[str, CompanyIdentityRecord] = {}
        for record in records or []:
            self.put(record)

    def put(self, record: CompanyIdentityRecord) -> None:
        validate_identity_record(record)
        self._records[record.security_code] = record

    def get(self, security_code: str) -> CompanyIdentityRecord | None:
        return self._records.get(str(security_code))

    def usable(self, security_code: str, company_name: str | None = None) -> CompanyIdentityRecord | None:
        record = self.get(security_code)
        if record is None or record.status != "VERIFIED":
            return None
        validate_identity_record(record)
        if company_name and record.normalized_name != _normalized_name(company_name):
            return None
        return record

    def invalidate(self, security_code: str, reason: str) -> CompanyIdentityRecord:
        record = self.get(security_code)
        if record is None or not reason.strip():
            raise OfficialSourceError("identity and invalidation reason are required")
        stale = CompanyIdentityRecord(**{**record.to_dict(), "status": "STALE"})
        self._records[security_code] = stale
        return stale

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": "company_identity_registry_v1",
                "records": [self._records[key].to_dict() for key in sorted(self._records)]}

    @classmethod
    def load(cls, path: Path) -> "CompanyIdentityRegistry":
        if not path.exists():
            return cls()
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") != "company_identity_registry_v1" or not isinstance(value.get("records"), list):
            raise OfficialSourceError("invalid identity registry document")
        return cls([CompanyIdentityRecord(**row) for row in value["records"]])

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)


def identity_invalidation_reasons(record: CompanyIdentityRecord, *, security_code: str,
                                  company_name: str, official_domain: str,
                                  merger_or_reorganization: bool = False,
                                  explicit_reverification: bool = False) -> list[str]:
    reasons = []
    if record.security_code != security_code:
        reasons.append("SECURITY_CODE_CHANGED")
    if record.normalized_name != _normalized_name(company_name):
        reasons.append("COMPANY_NAME_CHANGED")
    if _domain(record.official_domain) != _domain(official_domain):
        reasons.append("OFFICIAL_DOMAIN_CHANGED")
    if merger_or_reorganization:
        reasons.append("MERGER_OR_REORGANIZATION")
    if explicit_reverification:
        reasons.append("EXPLICIT_REVERIFICATION")
    return reasons


@dataclass(frozen=True)
class OfficialWindowCoverage:
    security_code: str
    search_from: str
    search_to: str
    state: str
    checked_families: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    evidence_hashes: tuple[str, ...] = ()


def validate_window_coverage(value: OfficialWindowCoverage) -> OfficialWindowCoverage:
    if value.state not in WINDOW_STATES:
        raise OfficialSourceError("invalid official window state")
    if value.state == "COMPLETE":
        required = {"news", "ir", "announcements"}
        if not required <= set(value.checked_families) or value.gaps:
            raise OfficialSourceError("COMPLETE official window lacks required checks")
    if value.state in {"PARTIAL", "BLOCKED"} and not value.gaps:
        raise OfficialSourceError("incomplete official window needs a gap reason")
    return value


def window_allows_complete_coverage(value: OfficialWindowCoverage | Mapping[str, Any] | None) -> bool:
    if value is None:
        return True
    if isinstance(value, Mapping):
        value = OfficialWindowCoverage(**value)
    return validate_window_coverage(value).state in {"COMPLETE", "NOT_APPLICABLE"}


@dataclass(frozen=True)
class FetchPolicy:
    timeout_seconds: float = 15.0
    max_redirects: int = 2
    max_response_bytes: int = 2_000_000
    allowed_mime_types: frozenset[str] = frozenset(ALLOWED_MIME_TYPES)


@dataclass(frozen=True)
class DelegatedStaticResourcePolicy:
    """Bound request metadata to an exact resource embedded by a verified official page."""

    parent_identity: CompanyIdentityRecord
    parent_url: str
    parent_body_sha256: str
    embedded_child_urls: tuple[str, ...]
    expected_mime_types: tuple[str, ...]
    accept: str
    referer: str | None = None

    def headers_for(self, child_url: str) -> dict[str, str]:
        identity = validate_identity_record(self.parent_identity)
        parent = urlsplit(self.parent_url)
        if (identity.status != "VERIFIED" or parent.scheme.lower() != "https"
                or not _is_official_host(parent.hostname or "", identity.official_domain)):
            raise OfficialSourceError("ACCESS_DENIED: delegated parent must be a verified official HTTPS page")
        if not _SHA256.fullmatch(self.parent_body_sha256):
            raise OfficialSourceError("ACCESS_DENIED: delegated parent evidence hash is required")
        child = _canonical_url(child_url)
        allowed = {_canonical_url(value) for value in self.embedded_child_urls}
        parsed_child = urlsplit(child)
        if (child not in allowed or parsed_child.scheme.lower() != "https"
                or not parsed_child.hostname or parsed_child.username or parsed_child.password
                or parsed_child.port not in {None, 443}):
            raise OfficialSourceError("ACCESS_DENIED: child is not an exact embedded HTTPS resource")
        if not self.expected_mime_types or not any(value in self.accept for value in self.expected_mime_types):
            raise OfficialSourceError("ACCESS_DENIED: Accept does not cover the delegated resource MIME")
        headers = {"accept": _safe_request_header_value(self.accept, "Accept")}
        if self.referer is not None:
            if _canonical_url(self.referer) != _canonical_url(self.parent_url):
                raise OfficialSourceError("ACCESS_DENIED: delegated Referer must equal the verified parent URL")
            headers["referer"] = _safe_request_header_value(self.referer, "Referer")
        return headers


def _safe_request_header_value(value: str, name: str) -> str:
    if (not value or len(value) > 1000 or any(ord(char) < 32 or ord(char) > 126 for char in value)):
        raise OfficialSourceError(f"ACCESS_DENIED: invalid delegated {name} header")
    return value


def parse_http_rejection_artifact(body: bytes, *, max_text_chars: int = 2000) -> dict[str, Any]:
    """Extract bounded offline evidence from an already retained HTTP rejection body."""
    soup = BeautifulSoup(body, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    text = " ".join(soup.get_text(" ", strip=True).split())[:max_text_chars]
    lower = text.lower()
    providers = [name for name in ("akamai", "cloudflare", "cloudfront", "imperva", "fastly")
                 if name in lower]
    markers = {
        "cookie": "cookie" in lower,
        "authentication": any(value in lower for value in ("authenticate", "authentication", "login")),
        "captcha": "captcha" in lower,
        "waf_or_anti_automation": any(value in lower for value in (
            "access denied", "request rejected", "security policy", "bot", "automated")),
    }
    references = sorted(set(re.findall(
        r"(?i)\b(?:reference|request|error|incident)[- _]?(?:id|number)?\s*[:#]?\s*([A-Za-z0-9._-]{6,80})",
        text)))[:10]
    return {
        "title": title,
        "body_text": text,
        "provider_markers": providers,
        "requirement_markers": markers,
        "reference_ids": references,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "body_bytes": len(body),
    }


def rejection_bodies_are_identical(bodies: list[bytes] | tuple[bytes, ...]) -> bool:
    return bool(bodies) and len({hashlib.sha256(value).digest() for value in bodies}) == 1


@dataclass(frozen=True)
class TransportPayload:
    requested_url: str
    final_url: str
    status_code: int
    mime_type: str | None
    body: bytes | None
    fetch_method: str
    failure_reason: str | None = None
    safe_headers: Mapping[str, str] | None = None
    transport_metadata: Mapping[str, Any] | None = None


class OfficialFetchTransport(Protocol):
    name: str

    def fetch(self, requested_url: str, official_domain: str, policy: FetchPolicy) -> TransportPayload: ...


def validate_fetch_url(url: str, official_domain: str,
                       dns_resolver: Callable[..., Any] = socket.getaddrinfo) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise OfficialSourceError("ACCESS_DENIED: invalid public HTTP URL")
    if parsed.port not in {None, 80, 443}:
        raise OfficialSourceError("ACCESS_DENIED: nonstandard port")
    if not _is_official_host(parsed.hostname, official_domain):
        raise OfficialSourceError("ACCESS_DENIED: host is outside official domain")
    try:
        addresses = {row[4][0] for row in dns_resolver(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise OfficialSourceError("DNS: official host resolution failed") from exc
    if not addresses:
        raise OfficialSourceError("DNS: official host has no address")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or
                ip.is_multicast or ip.is_unspecified):
            raise OfficialSourceError("ACCESS_DENIED: non-public address")
    return _canonical_url(url)


class RepositoryExistingTransport:
    """Offline adapter for already saved, verified bytes; it never accesses a URL."""
    name = "repository_existing"

    def __init__(self, artifacts: Mapping[str, tuple[bytes, str, int]]) -> None:
        self.artifacts = {_canonical_url(key): value for key, value in artifacts.items()}

    def fetch(self, requested_url: str, official_domain: str, policy: FetchPolicy) -> TransportPayload:
        parsed = urlsplit(requested_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not _is_official_host(parsed.hostname, official_domain):
            raise OfficialSourceError("ACCESS_DENIED: repository artifact host is not official")
        key = _canonical_url(requested_url)
        if key not in self.artifacts:
            return TransportPayload(key, key, 404, None, None, self.name, "HTTP_4XX")
        body, mime_type, status = self.artifacts[key]
        if status >= 500:
            return TransportPayload(key, key, status, mime_type, None, self.name, "HTTP_5XX")
        if status >= 400:
            return TransportPayload(key, key, status, mime_type, None, self.name, "HTTP_4XX")
        if mime_type not in policy.allowed_mime_types:
            return TransportPayload(key, key, status, mime_type, None, self.name, "MIME_REJECTED")
        if len(body) > policy.max_response_bytes:
            return TransportPayload(key, key, status, mime_type, None, self.name, "SIZE_LIMIT")
        return TransportPayload(key, key, status, mime_type, body, self.name)


class TlsTrustInitializationError(OfficialSourceError):
    """Raised before any request when a verified trust backend cannot initialize."""


def create_verified_tls_context(*, cadata: str | None = None,
                                cafile: str | None = None) -> ssl.SSLContext:
    """Create a client context that always enforces CA and hostname verification."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    if cadata is not None or cafile is not None:
        context.load_verify_locations(cafile=cafile, cadata=cadata)
    return context


def windows_system_trust_context() -> ssl.SSLContext:
    """Load the current Windows ROOT store into an OpenSSL verified client context."""
    if not hasattr(ssl, "enum_certificates"):
        raise TlsTrustInitializationError("TLS_TRUST_INIT: Windows certificate store API unavailable")
    try:
        certificates = [ssl.DER_cert_to_PEM_cert(body)
                        for body, encoding, _trust in ssl.enum_certificates("ROOT")
                        if encoding == "x509_asn"]
        if not certificates:
            raise TlsTrustInitializationError("TLS_TRUST_INIT: Windows ROOT store has no X.509 certificates")
        context = create_verified_tls_context(cadata="\n".join(certificates))
    except TlsTrustInitializationError:
        raise
    except Exception as exc:
        raise TlsTrustInitializationError(
            f"TLS_TRUST_INIT: Windows ROOT initialization failed: {type(exc).__name__}") from exc
    return context


def validate_verified_tls_context(context: ssl.SSLContext) -> ssl.SSLContext:
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise TlsTrustInitializationError("TLS_TRUST_INIT: certificate and hostname verification are required")
    return context


def classify_tls_failure(exc: BaseException) -> str:
    message = str(exc).casefold()
    if any(value in message for value in ("unable to get local issuer", "self-signed certificate",
                                           "unknown ca", "certificate verify failed")):
        return "TLS_CA_CHAIN"
    if "hostname" in message or "doesn't match" in message or "does not match" in message:
        return "TLS_HOSTNAME"
    if "expired" in message:
        return "TLS_EXPIRED"
    if "not yet valid" in message:
        return "TLS_NOT_YET_VALID"
    if any(value in message for value in ("wrong version", "unsupported protocol", "protocol version")):
        return "TLS_PROTOCOL"
    if "handshake" in message:
        return "TLS_HANDSHAKE"
    return "TLS_UNKNOWN"


def classify_schannel_failure(response: Mapping[str, Any]) -> str:
    """Classify a structured Schannel failure without discarding its TLS stage."""
    reported = str(response.get("failure_class") or "UNKNOWN")
    certificate = response.get("certificate_summary") or {}
    if certificate.get("hostname_validation") == "FAIL":
        return "TLS_HOSTNAME"
    if certificate.get("chain_status"):
        return "TLS_CA_CHAIN"
    if reported != "TLS_UNKNOWN":
        return reported
    stage = str(response.get("failure_stage") or "").upper()
    messages = " ".join(str(value or "") for value in (
        response.get("error_detail"), *(response.get("exception_chain") or [])))
    classified = classify_tls_failure(RuntimeError(messages))
    if classified != "TLS_UNKNOWN":
        return classified
    if stage == "TLS" and any(token in messages.casefold() for token in (
            "authenticateasclient", "authentication failed", "handshake")):
        return "TLS_HANDSHAKE"
    return reported


def derive_exact_document_url(saved_body: bytes, listing_url: str, title: str,
                              official_domain: str) -> str | None:
    """Return one literal document URL from saved official HTML; never guess a route."""
    soup = BeautifulSoup(saved_body, "html.parser")
    wanted = re.sub(r"\s+", " ", title).strip().casefold()
    found: set[str] = set()
    candidates = list(soup.find_all("a")) + list(soup.find_all(attrs={"data-url": True}))
    for candidate in candidates:
        candidate_text = re.sub(r"\s+", " ", candidate.get_text(" ", strip=True)).casefold()
        container_text = re.sub(
            r"\s+", " ", candidate.parent.get_text(" ", strip=True)).casefold()
        if wanted not in candidate_text and wanted not in container_text:
            continue
        raw = candidate.get("href") or candidate.get("data-url")
        if not raw:
            continue
        absolute = _canonical_url(urljoin(listing_url, str(raw)))
        parsed = urlsplit(absolute)
        if parsed.scheme.lower() == "https" and _is_official_host(
                parsed.hostname or "", official_domain):
            found.add(absolute)
    if len(found) > 1:
        raise OfficialSourceError(
            "AMBIGUOUS_DOCUMENT_URL: saved evidence has multiple exact links")
    return next(iter(found), None)


class VerifiedSslContextAdapter(HTTPAdapter):
    """Requests adapter whose pool uses a prevalidated SSLContext."""

    def __init__(self, context: ssl.SSLContext, **kwargs: Any) -> None:
        self._verified_context = validate_verified_tls_context(context)
        super().__init__(**kwargs)

    def init_poolmanager(self, connections: int, maxsize: int, block: bool = False,
                         **pool_kwargs: Any) -> None:
        pool_kwargs["ssl_context"] = self._verified_context
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> Any:
        proxy_kwargs["ssl_context"] = self._verified_context
        return super().proxy_manager_for(proxy, **proxy_kwargs)


class DirectHttpTransport:
    """GET-only requests adapter with DNS, redirect, MIME, and size enforcement."""

    def __init__(self, *, session_factory: Callable[[], Any] = requests.Session,
                 dns_resolver: Callable[..., Any] = socket.getaddrinfo,
                 journal: "HttpFetchJournal | None" = None,
                 tls_backend: str = TLS_BACKEND_CURRENT_CA,
                 system_context_factory: Callable[[], ssl.SSLContext] = windows_system_trust_context) -> None:
        if tls_backend not in {TLS_BACKEND_CURRENT_CA, TLS_BACKEND_WINDOWS_SYSTEM_TRUST}:
            raise TlsTrustInitializationError("TLS_TRUST_INIT: unsupported TLS backend")
        self.session_factory, self.dns_resolver = session_factory, dns_resolver
        self.journal = journal
        self.tls_backend = tls_backend
        self._system_context = None
        if tls_backend == TLS_BACKEND_WINDOWS_SYSTEM_TRUST:
            self._system_context = validate_verified_tls_context(system_context_factory())
        self._attempts = 0
        self._request_for_target: dict[str, str] = {}

    @property
    def name(self) -> str:
        if self.tls_backend == TLS_BACKEND_CURRENT_CA:
            return "direct_http"
        return "direct_http/windows-system-trust-v1"

    def _session(self) -> Any:
        session = self.session_factory()
        if getattr(session, "verify", True) is False:
            if hasattr(session, "close"):
                session.close()
            raise TlsTrustInitializationError("TLS_TRUST_INIT: verify=False session rejected")
        if self._system_context is not None:
            if not hasattr(session, "mount"):
                if hasattr(session, "close"):
                    session.close()
                raise TlsTrustInitializationError("TLS_TRUST_INIT: session cannot mount verified context")
            session.mount("https://", VerifiedSslContextAdapter(self._system_context))
        return session

    def _start_request(self, target_url: str, wire_url: str) -> tuple[str | None, float]:
        started = time.monotonic()
        if self.journal is None:
            return None, started
        self._attempts += 1
        request_id = f"request-{self._attempts:04d}-{uuid.uuid4().hex[:12]}"
        self.journal.start(request_id=request_id, transport=self.name,
                           requested_url=wire_url, attempt_number=self._attempts)
        self._request_for_target[_canonical_url(target_url)] = request_id
        return request_id, started

    def _finish_request(self, request_id: str | None, payload: TransportPayload,
                        started: float, *, access_status: str | None = None) -> TransportPayload:
        if self.journal is not None and request_id is not None:
            self.journal.response_received(request_id, payload,
                                           elapsed=time.monotonic() - started,
                                           access_status=access_status)
            if payload.failure_reason:
                self.journal.failed(request_id, category=payload.failure_reason)
        return payload

    def _fail_request(self, request_id: str | None, exc: Exception, category: str) -> None:
        if self.journal is not None and request_id is not None:
            self.journal.failed(request_id, category=category, detail=str(exc))

    def record_normalized(self, requested_url: str, *, normalized_chars: int, body_hash: str) -> None:
        if self.journal is not None:
            request_id = self._request_for_target[_canonical_url(requested_url)]
            self.journal.normalized(request_id, normalized_chars=normalized_chars, body_hash=body_hash)

    def record_normalization_failure(self, requested_url: str, exc: Exception) -> None:
        if self.journal is not None:
            request_id = self._request_for_target[_canonical_url(requested_url)]
            self.journal.failed(request_id, category="NORMALIZATION_FAILED", detail=str(exc))

    def fetch(self, requested_url: str, official_domain: str, policy: FetchPolicy) -> TransportPayload:
        current = validate_fetch_url(requested_url, official_domain, self.dns_resolver)
        session = self._session()
        if hasattr(session, "trust_env"):
            session.trust_env = False
        if hasattr(session, "cookies"):
            session.cookies.clear()
        try:
            for redirects in range(policy.max_redirects + 1):
                request_id, request_started = self._start_request(requested_url, current)
                try:
                    response = session.get(current, headers={"User-Agent": "CompanyNewsOfficialResolver/1.0",
                                                             "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain"},
                                           timeout=policy.timeout_seconds, stream=True, allow_redirects=False)
                except requests.Timeout as exc:
                    self._fail_request(request_id, exc, "TIMEOUT")
                    return TransportPayload(requested_url, current, 0, None, None, self.name, "TIMEOUT")
                except requests.exceptions.SSLError as exc:
                    failure = classify_tls_failure(exc)
                    self._fail_request(request_id, exc, failure)
                    return TransportPayload(requested_url, current, 0, None, None, self.name, failure)
                except requests.exceptions.ConnectionError as exc:
                    self._fail_request(request_id, exc, "DNS")
                    return TransportPayload(requested_url, current, 0, None, None, self.name, "DNS")
                safe_headers = {
                    key.lower(): str(value)[:1000]
                    for key, value in response.headers.items()
                    if key.lower() in {"content-type", "content-length", "location", "etag", "last-modified"}
                }
                if 300 <= response.status_code < 400:
                    location = response.headers.get("Location")
                    if not location or redirects == policy.max_redirects:
                        payload = TransportPayload(requested_url, current, response.status_code, None, None,
                                                   self.name, "REDIRECT_BLOCKED", safe_headers)
                        return self._finish_request(request_id, payload, request_started)
                    destination = urljoin(current, location)
                    host = urlsplit(destination).hostname or ""
                    if not _is_official_host(host, official_domain):
                        payload = TransportPayload(requested_url, destination, response.status_code, None, None,
                                                   self.name, "REDIRECT_BLOCKED", safe_headers)
                        return self._finish_request(request_id, payload, request_started)
                    redirect_payload = TransportPayload(current, current, response.status_code, None, None,
                                                        self.name, safe_headers=safe_headers)
                    self._finish_request(request_id, redirect_payload, request_started,
                                         access_status="REDIRECT")
                    current = validate_fetch_url(destination, official_domain, self.dns_resolver)
                    continue
                mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() or None
                if response.status_code >= 500:
                    payload = TransportPayload(requested_url, current, response.status_code, mime, None, self.name,
                                               "HTTP_5XX", safe_headers)
                    return self._finish_request(request_id, payload, request_started)
                if response.status_code >= 400:
                    payload = TransportPayload(requested_url, current, response.status_code, mime, None, self.name,
                                               "HTTP_4XX", safe_headers)
                    return self._finish_request(request_id, payload, request_started)
                if mime not in policy.allowed_mime_types:
                    payload = TransportPayload(requested_url, current, response.status_code, mime, None, self.name,
                                               "MIME_REJECTED", safe_headers)
                    return self._finish_request(request_id, payload, request_started)
                chunks, size = [], 0
                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > policy.max_response_bytes:
                        payload = TransportPayload(requested_url, current, response.status_code, mime, None,
                                                   self.name, "SIZE_LIMIT", safe_headers)
                        return self._finish_request(request_id, payload, request_started)
                    chunks.append(chunk)
                payload = TransportPayload(requested_url, current, response.status_code, mime,
                                           b"".join(chunks), self.name, safe_headers=safe_headers)
                return self._finish_request(request_id, payload, request_started)
        finally:
            if hasattr(session, "close"):
                session.close()
        return TransportPayload(requested_url, current, 0, None, None, self.name, "UNKNOWN")


class WindowsSchannelTransport:
    """GET-only native Windows Schannel transport behind a strict JSON helper boundary."""

    name = SCHANNEL_TRANSPORT_ID
    implementation_fingerprint = SCHANNEL_IMPLEMENTATION_FINGERPRINT

    def __init__(self, *, journal: "HttpFetchJournal | None" = None,
                 dns_resolver: Callable[..., Any] = socket.getaddrinfo,
                 helper_path: Path | None = None,
                 helper_runner: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
                 retain_raw_html: bool = False,
                 delegated_policy: DelegatedStaticResourcePolicy | None = None) -> None:
        self.journal, self.dns_resolver = journal, dns_resolver
        self.helper_path = Path(helper_path or Path(__file__).with_name("company_news_windows_schannel_fetch.ps1"))
        self.helper_runner = helper_runner or self._run_helper
        self.retain_raw_html = retain_raw_html
        self.delegated_policy = delegated_policy
        self._attempts = 0
        self._request_for_target: dict[str, str] = {}

    def _run_helper(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        executable = shutil.which("pwsh")
        if not executable and os.name == "nt":
            candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PowerShell" / "7" / "pwsh.exe"
            executable = str(candidate) if candidate.exists() else None
        if not executable or not self.helper_path.is_file():
            raise OfficialSourceError("SCHANNEL_HELPER: Windows Schannel helper unavailable")
        completed = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-File", str(self.helper_path)],
            input=json.dumps(dict(request), ensure_ascii=False), capture_output=True, text=True,
            encoding="utf-8", timeout=float(request["timeout_seconds"]) + 5, check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or "Schannel helper failed")[:2000]
            raise OfficialSourceError(f"SCHANNEL_HELPER: {detail}")
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise OfficialSourceError("SCHANNEL_HELPER: invalid structured response") from exc
        if value.get("schema_version") != "company_news_schannel_helper_v1":
            raise OfficialSourceError("SCHANNEL_HELPER: invalid response schema")
        if (value.get("transport_id") != self.name
                or value.get("implementation_fingerprint") != self.implementation_fingerprint):
            raise OfficialSourceError("SCHANNEL_HELPER: transport provenance mismatch")
        return value

    def _start_request(self, target_url: str, *, parent_request_id: str | None = None,
                       redirect_hop: int = 0, redirect_source_url: str | None = None,
                       redirect_location: str | None = None) -> tuple[str | None, float]:
        started = time.monotonic()
        if self.journal is None:
            return None, started
        self._attempts += 1
        request_id = f"request-{self._attempts:04d}-{uuid.uuid4().hex[:12]}"
        self.journal.start(request_id=request_id, transport=self.name,
                           requested_url=target_url, attempt_number=self._attempts,
                           parent_request_id=parent_request_id, redirect_hop=redirect_hop,
                           redirect_source_url=redirect_source_url,
                           redirect_location=redirect_location,
                           redirect_target_url=target_url if parent_request_id else None)
        self._request_for_target[_canonical_url(target_url)] = request_id
        return request_id, started

    def _finish_request(self, request_id: str | None, payload: TransportPayload,
                        started: float, *, raw_body: bytes | None = None) -> TransportPayload:
        if self.journal is not None and request_id is not None:
            retained = payload.body if raw_body is None else raw_body
            if (self.retain_raw_html and retained is not None
                    and payload.mime_type in {"text/html", "application/xhtml+xml"}):
                relative = Path("raw-http") / f"{request_id}.html"
                self.journal.store.write_bytes(relative, retained)
                metadata = dict(payload.transport_metadata or {})
                metadata.update({
                    "raw_body_artifact": relative.as_posix(),
                    "raw_body_sha256": hashlib.sha256(retained).hexdigest(),
                    "raw_body_bytes": len(retained),
                })
                payload = TransportPayload(
                    payload.requested_url, payload.final_url, payload.status_code,
                    payload.mime_type, payload.body, payload.fetch_method,
                    payload.failure_reason, payload.safe_headers, metadata,
                )
            elapsed = float((payload.transport_metadata or {}).get(
                "elapsed_seconds", time.monotonic() - started))
            access_status = ("REDIRECT" if payload.status_code in range(300, 400)
                             and not payload.failure_reason else None)
            self.journal.response_received(request_id, payload, elapsed=elapsed,
                                           access_status=access_status)
            if payload.failure_reason:
                self.journal.failed(request_id, category=payload.failure_reason)
        return payload

    def record_normalized(self, requested_url: str, *, normalized_chars: int, body_hash: str) -> None:
        if self.journal is not None:
            self.journal.normalized(self._request_for_target[_canonical_url(requested_url)],
                                    normalized_chars=normalized_chars, body_hash=body_hash)

    def record_normalization_failure(self, requested_url: str, exc: Exception) -> None:
        if self.journal is not None:
            self.journal.failed(self._request_for_target[_canonical_url(requested_url)],
                                category="NORMALIZATION", detail=str(exc))

    def fetch(self, requested_url: str, official_domain: str, policy: FetchPolicy) -> TransportPayload:
        if urlsplit(requested_url).scheme.lower() != "https":
            raise OfficialSourceError("ACCESS_DENIED: Schannel transport requires HTTPS")
        current = validate_fetch_url(requested_url, official_domain, self.dns_resolver)
        request_headers: dict[str, str] = {}
        if self.delegated_policy is not None:
            if policy.max_redirects != 0:
                raise OfficialSourceError("ACCESS_DENIED: delegated static resources cannot follow redirects")
            request_headers = self.delegated_policy.headers_for(current)
        visited = {_canonical_url(current)}
        parent_request_id: str | None = None
        redirect_source_url: str | None = None
        redirect_location: str | None = None
        for redirects in range(policy.max_redirects + 1):
            request_id, started = self._start_request(
                current, parent_request_id=parent_request_id, redirect_hop=redirects,
                redirect_source_url=redirect_source_url, redirect_location=redirect_location)
            try:
                response = dict(self.helper_runner({
                    "url": current,
                    "allowed_host": _domain(official_domain),
                    "timeout_seconds": policy.timeout_seconds,
                    "max_response_bytes": policy.max_response_bytes,
                    "request_headers": request_headers,
                }))
            except subprocess.TimeoutExpired as exc:
                if self.journal is not None and request_id is not None:
                    self.journal.failed(request_id, category="TIMEOUT", detail=str(exc))
                return TransportPayload(requested_url, current, 0, None, None, self.name, "TIMEOUT")
            except Exception as exc:
                if self.journal is not None and request_id is not None:
                    self.journal.failed(request_id, category="SCHANNEL_HELPER", detail=str(exc))
                return TransportPayload(requested_url, current, 0, None, None, self.name, "UNKNOWN")
            metadata = {
                "tls_backend": "windows-schannel-cryptoapi",
                "implementation_fingerprint": response.get("implementation_fingerprint"),
                "certificate_summary": response.get("certificate_summary"),
                "elapsed_seconds": response.get("elapsed_seconds"),
                "error_detail": str(response.get("error_detail") or "")[:2000] or None,
                "response_bytes": response.get("response_bytes"),
                "body_sha256": response.get("body_sha256"),
                "request_header_profile": response.get("request_header_profile"),
                "failure_stage": response.get("failure_stage"),
                "exception_type": response.get("exception_type"),
                "exception_chain": response.get("exception_chain") or [],
            }
            if response.get("result") != "RESPONSE":
                failure = classify_schannel_failure(response)
                payload = TransportPayload(
                    requested_url, current, int(response.get("status_code") or 0),
                    response.get("mime_type"), None, self.name, failure,
                    response.get("safe_headers") or {}, metadata)
                return self._finish_request(request_id, payload, started)
            decode_failure: str | None = None
            try:
                body = base64.b64decode(str(response.get("body_base64") or ""), validate=True)
            except Exception:
                body, decode_failure = b"", "BODY_ENCODING"
            if int(response.get("response_bytes") or 0) != len(body):
                decode_failure = "BODY_ENCODING"
            if _canonical_url(str(response.get("final_url") or "")) != _canonical_url(current):
                decode_failure = "REDIRECT_BLOCKED"
            status = int(response.get("status_code") or 0)
            mime = response.get("mime_type")
            safe_headers = response.get("safe_headers") or {}
            if status in range(300, 400):
                location = safe_headers.get("location")
                if not location or redirects == policy.max_redirects:
                    return self._finish_request(request_id, TransportPayload(
                        requested_url, current, status, mime, None, self.name,
                        "REDIRECT_BLOCKED", safe_headers, metadata), started, raw_body=body)
                destination = urljoin(current, location)
                if (urlsplit(destination).scheme.lower() != "https"
                        or not _is_official_host(urlsplit(destination).hostname or "", official_domain)):
                    return self._finish_request(request_id, TransportPayload(
                        requested_url, destination, status, mime, None, self.name,
                        "REDIRECT_BLOCKED", safe_headers, metadata), started, raw_body=body)
                if _canonical_url(destination) in visited:
                    return self._finish_request(request_id, TransportPayload(
                        requested_url, destination, status, mime, None, self.name,
                        "REDIRECT_BLOCKED", safe_headers, metadata), started, raw_body=body)
                self._finish_request(request_id, TransportPayload(
                    current, current, status, mime, None, self.name, None, safe_headers, metadata),
                    started, raw_body=body)
                parent_request_id = request_id
                redirect_source_url = current
                redirect_location = str(location)
                current = validate_fetch_url(destination, official_domain, self.dns_resolver)
                visited.add(_canonical_url(current))
                continue
            failure = decode_failure or response.get("failure_class")
            if not failure and status >= 500:
                failure = "HTTP_5XX"
            elif not failure and status >= 400:
                failure = "HTTP_4XX"
            elif not failure and len(body) > policy.max_response_bytes:
                failure = "SIZE_LIMIT"
            elif not failure and mime not in policy.allowed_mime_types:
                failure = "MIME_REJECTED"
            elif not failure and status not in {204, 205, 304} and not body:
                failure = "EMPTY_BODY"
            payload = TransportPayload(
                requested_url, current, status, mime, None if failure else body,
                self.name, failure, safe_headers, metadata)
            self._request_for_target[_canonical_url(requested_url)] = request_id
            retained_body = None if failure == "SIZE_LIMIT" else body
            return self._finish_request(request_id, payload, started, raw_body=retained_body)
        return TransportPayload(requested_url, current, 0, None, None, self.name, "UNKNOWN")


def tls_backend_change_allows_attempt(old: DirectHttpTransport,
                                      new: DirectHttpTransport) -> bool:
    """A new retry identity requires an actual supported backend transition."""
    return (old.tls_backend != new.tls_backend
            and {old.tls_backend, new.tls_backend}
            <= {TLS_BACKEND_CURRENT_CA, TLS_BACKEND_WINDOWS_SYSTEM_TRUST}
            and old.name != new.name)


@dataclass(frozen=True)
class NormalizedOfficialBody:
    title: str
    canonical_url: str
    visible_text: str
    date_candidates: tuple[str, ...]
    relevant_links: tuple[str, ...]
    bounded_excerpt: str


@dataclass(frozen=True)
class DynamicOfficialSource:
    """An explicit same-domain resource reference found in saved official HTML."""

    url: str
    source_location: str
    expected_content_type: str


_DYNAMIC_PATH = re.compile(
    r"(?:/api/|/wp-json/|ajax|\.json(?:[?#]|$)|\.xml(?:[?#]|$)|\.csv(?:[?#]|$))",
    re.IGNORECASE,
)
_QUOTED_RESOURCE = re.compile(r"[\"']((?:https://|/)[^\"'<>\\\s]+)[\"']", re.IGNORECASE)


def discover_official_dynamic_sources(body: bytes, final_url: str,
                                      official_domain: str) -> tuple[DynamicOfficialSource, ...]:
    """Extract explicit dynamic-resource references without guessing or network access."""
    soup = BeautifulSoup(body, "html.parser")
    discovered: dict[str, DynamicOfficialSource] = {}

    def add(value: str, location: str, expected: str, *, require_dynamic: bool = False) -> None:
        candidate = (value or "").strip()
        if not candidate or candidate.startswith(("#", "javascript:", "data:")):
            return
        absolute = urljoin(final_url, candidate)
        parsed = urlsplit(absolute)
        if parsed.scheme.lower() != "https" or not _is_official_host(parsed.hostname or "", official_domain):
            return
        if require_dynamic and not _DYNAMIC_PATH.search(parsed.path + ("?" + parsed.query if parsed.query else "")):
            return
        canonical = _canonical_url(absolute)
        discovered.setdefault(canonical, DynamicOfficialSource(canonical, location, expected))

    for index, script in enumerate(soup.find_all("script")):
        source = script.get("src")
        if source:
            add(str(source), f"script[{index}].src", "application/javascript")
        inline = script.string or script.get_text(" ", strip=False)
        for match in _QUOTED_RESOURCE.finditer(inline or ""):
            add(match.group(1), f"script[{index}].inline", "application/json", require_dynamic=True)

    for element_index, element in enumerate(soup.find_all(True)):
        for attribute, value in element.attrs.items():
            if not attribute.startswith("data-"):
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                add(str(item), f"element[{element_index}].{attribute}", "application/json",
                    require_dynamic=True)

    return tuple(discovered[url] for url in sorted(discovered))


def normalize_official_body(body: bytes, mime_type: str, final_url: str,
                            official_domain: str, *, text_limit: int = 20_000,
                            excerpt_limit: int = 2_000) -> NormalizedOfficialBody:
    if mime_type == "application/pdf":
        from pypdf import PdfReader
        pages = PdfReader(io.BytesIO(body)).pages[:12]
        text = " ".join((page.extract_text() or "") for page in pages)
        title, canonical, links = Path(urlsplit(final_url).path).name, final_url, []
    else:
        soup = BeautifulSoup(body, "html.parser")
        for node in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            node.decompose()
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        canonical_node = soup.find("link", rel=lambda value: value and "canonical" in value)
        canonical = urljoin(final_url, canonical_node.get("href")) if canonical_node else final_url
        if not _is_official_host(urlsplit(canonical).hostname or "", official_domain):
            canonical = final_url
        links = []
        for node in soup.find_all("a", href=True):
            candidate = urljoin(final_url, node["href"])
            host = urlsplit(candidate).hostname or ""
            if _is_official_host(host, official_domain):
                links.append(_canonical_url(candidate))
        text = soup.get_text(" ", strip=True)
    visible = re.sub(r"\s+", " ", text).strip()[:text_limit]
    dates = tuple(dict.fromkeys(_DATE.findall(visible)))
    return NormalizedOfficialBody(title[:500], _canonical_url(canonical), visible,
                                  dates[:20], tuple(dict.fromkeys(links))[:25], visible[:excerpt_limit])


@dataclass(frozen=True)
class OfficialSourceResolution:
    requested_url: str
    final_url: str
    status_code: int
    access_status: str
    mime_type: str | None
    normalized_content: NormalizedOfficialBody | None
    body_hash: str | None
    fetched_at: str
    fetch_method: str
    failure_reason: str | None


@dataclass(frozen=True)
class IdentityFailureRecord:
    security_code: str
    domain: str
    url: str
    transport: str
    failure_type: str
    attempted_at: str
    retry_count: int
    retry_allowed: bool


def _failure_transport_key(transport: str) -> str:
    aliases = {
        "direct_http/windows-system-trust-v1": "direct_http/openssl-windows-root-bundle-v1",
        "direct_http/windows-root-bundle-v1": "direct_http/openssl-windows-root-bundle-v1",
    }
    return aliases.get(transport, transport)


class IdentityFailureCache:
    def __init__(self, max_attempts_per_failure: int = 1) -> None:
        if max_attempts_per_failure < 1:
            raise OfficialSourceError("retry hard cap must be positive")
        self.max_attempts = max_attempts_per_failure
        self._records: dict[tuple[str, str, str, str, str], IdentityFailureRecord] = {}

    def record(self, *, security_code: str, domain: str, url: str, transport: str,
               failure_type: str, attempted_at: str) -> IdentityFailureRecord:
        if failure_type not in FAILURE_TYPES:
            failure_type = "UNKNOWN"
        key = (security_code, _domain(domain), _canonical_url(url),
               _failure_transport_key(transport), failure_type)
        prior = self._records.get(key)
        count = 1 if prior is None else prior.retry_count + 1
        value = IdentityFailureRecord(security_code, key[1], key[2], transport, failure_type,
                                      attempted_at, count, count < self.max_attempts)
        self._records[key] = value
        return value

    def retry_allowed(self, *, security_code: str, domain: str, url: str, transport: str) -> bool:
        prefix = (security_code, _domain(domain), _canonical_url(url),
                  _failure_transport_key(transport))
        matches = [value for key, value in self._records.items() if key[:4] == prefix]
        return not matches or all(value.retry_allowed for value in matches)

    def records(self) -> list[IdentityFailureRecord]:
        return list(self._records.values())

    def record_outcome_lost(self, *, security_code: str, domain: str, url: str,
                            transport: str, attempted_at: str) -> IdentityFailureRecord:
        """Persist a known attempt whose HTTP outcome was not durably captured."""
        return self.record(security_code=security_code, domain=domain, url=url,
                           transport=transport, failure_type="OUTCOME_LOST",
                           attempted_at=attempted_at)


class ArtifactStorageUnavailable(OfficialSourceError):
    """Raised before transport execution when no durable journal path is writable."""


def atomic_write_json(path: Path, value: Any) -> None:
    """Durably replace one JSON document without exposing a partial result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_bytes(path: Path, value: bytes) -> None:
    """Durably replace one binary artifact without exposing a partial body."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def writable_probe(path: Path) -> None:
    """Create, fsync, read back, and remove a probe before any HTTP is allowed."""
    path.mkdir(parents=True, exist_ok=True)
    probe = path / f".write-probe-{os.getpid()}-{uuid.uuid4().hex}"
    marker = uuid.uuid4().hex
    try:
        with probe.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(marker)
            stream.flush()
            os.fsync(stream.fileno())
        if probe.read_text(encoding="utf-8") != marker:
            raise OSError("writable probe read-back mismatch")
    finally:
        probe.unlink(missing_ok=True)


@dataclass
class DurableArtifactStore:
    """Authoritative local spool with best-effort copies to the primary artifact root."""
    primary_root: Path
    spool_root: Path
    primary_writable: bool
    using_fallback: bool
    primary_error: str | None = None
    mirror_errors: list[str] | None = None

    @classmethod
    def prepare(cls, primary_root: Path, *,
                fallback_factory: Callable[[], Path] | None = None,
                probe: Callable[[Path], None] = writable_probe) -> "DurableArtifactStore":
        primary_root = Path(primary_root)
        primary_writable, primary_error = True, None
        try:
            probe(primary_root)
        except Exception as exc:
            primary_writable = False
            primary_error = f"{type(exc).__name__}: {exc}"

        fallback_factory = fallback_factory or (
            lambda: Path(tempfile.mkdtemp(prefix="company-news-http-journal-")))
        fallback_root, fallback_error = None, None
        try:
            fallback_root = Path(fallback_factory())
            probe(fallback_root)
        except Exception as exc:
            fallback_error = f"{type(exc).__name__}: {exc}"

        if fallback_root is not None and fallback_error is None:
            return cls(primary_root, fallback_root, primary_writable, True,
                       primary_error, [])
        if primary_writable:
            return cls(primary_root, primary_root, True, False, None, [])
        raise ArtifactStorageUnavailable(
            f"ARTIFACT_STORAGE_UNAVAILABLE: primary={primary_error}; fallback={fallback_error}"
        )

    def write_json(self, relative_path: str | Path, value: Any) -> Path:
        relative = Path(relative_path)
        spool_path = self.spool_root / relative
        atomic_write_json(spool_path, value)
        if self.spool_root != self.primary_root:
            try:
                atomic_write_json(self.primary_root / relative, value)
            except Exception as exc:
                assert self.mirror_errors is not None
                self.mirror_errors.append(f"{relative}: {type(exc).__name__}: {exc}")
        return spool_path

    def write_bytes(self, relative_path: str | Path, value: bytes) -> Path:
        relative = Path(relative_path)
        spool_path = self.spool_root / relative
        atomic_write_bytes(spool_path, value)
        if self.spool_root != self.primary_root:
            try:
                atomic_write_bytes(self.primary_root / relative, value)
            except Exception as exc:
                assert self.mirror_errors is not None
                self.mirror_errors.append(f"{relative}: {type(exc).__name__}: {exc}")
        return spool_path

    def copy_spool_to_primary(self) -> bool:
        if self.spool_root == self.primary_root:
            return True
        try:
            writable_probe(self.primary_root)
            for source in self.spool_root.rglob("*"):
                if source.is_file():
                    relative = source.relative_to(self.spool_root)
                    if source.suffix == ".json":
                        atomic_write_json(self.primary_root / relative,
                                          json.loads(source.read_text(encoding="utf-8")))
                    else:
                        atomic_write_bytes(self.primary_root / relative, source.read_bytes())
            return True
        except Exception as exc:
            assert self.mirror_errors is not None
            self.mirror_errors.append(f"final-copy: {type(exc).__name__}: {exc}")
            return False


class HttpFetchJournal:
    """One atomically rewritten journal file per actual transport request."""
    schema_version = "company_news_http_fetch_journal_v1"

    def __init__(self, store: DurableArtifactStore, *, run_id: str, company: str,
                 clock: Callable[[], str] = _now) -> None:
        self.store, self.run_id, self.company, self.clock = store, run_id, company, clock
        self._records: dict[str, dict[str, Any]] = {}

    def _path(self, request_id: str) -> Path:
        return Path("http-journal") / f"{request_id}.json"

    def _persist(self, record: dict[str, Any]) -> Path:
        self._records[record["request_id"]] = copy.deepcopy(record)
        return self.store.write_json(self._path(record["request_id"]), record)

    def planned(self, *, request_id: str, transport: str, requested_url: str,
                attempt_number: int, parent_request_id: str | None = None,
                redirect_hop: int = 0, redirect_source_url: str | None = None,
                redirect_location: str | None = None,
                redirect_target_url: str | None = None) -> None:
        record = {
            "schema_version": self.schema_version, "run_id": self.run_id,
            "request_id": request_id, "company": self.company, "transport": transport,
            "requested_url": requested_url, "attempt_number": attempt_number,
            "parent_request_id": parent_request_id, "redirect_hop": redirect_hop,
            "redirect_source_url": redirect_source_url,
            "redirect_location": redirect_location,
            "redirect_target_url": redirect_target_url,
            "history": [{"phase": "PLANNED", "at": self.clock()}],
        }
        self._persist(record)

    def transition(self, request_id: str, phase: str, **values: Any) -> None:
        if phase not in HTTP_JOURNAL_PHASES:
            raise OfficialSourceError(f"invalid HTTP journal phase: {phase}")
        record = copy.deepcopy(self._records[request_id])
        record["history"].append({"phase": phase, "at": self.clock(), **values})
        self._persist(record)

    def start(self, *, request_id: str, transport: str, requested_url: str,
              attempt_number: int, parent_request_id: str | None = None,
              redirect_hop: int = 0, redirect_source_url: str | None = None,
              redirect_location: str | None = None,
              redirect_target_url: str | None = None) -> None:
        self.planned(request_id=request_id, transport=transport,
                     requested_url=requested_url, attempt_number=attempt_number,
                     parent_request_id=parent_request_id, redirect_hop=redirect_hop,
                     redirect_source_url=redirect_source_url,
                     redirect_location=redirect_location,
                     redirect_target_url=redirect_target_url)
        self.transition(request_id, "REQUEST_STARTED", started_at=self.clock())

    def response_received(self, request_id: str, payload: TransportPayload, *, elapsed: float,
                          access_status: str | None = None) -> None:
        metadata = dict(payload.transport_metadata or {})
        response_bytes = (len(payload.body) if payload.body is not None
                          else metadata.get("response_bytes"))
        body_sha256 = (hashlib.sha256(payload.body).hexdigest() if payload.body is not None
                       else metadata.get("body_sha256"))
        self.transition(
            request_id, "RESPONSE_RECEIVED", completed_at=self.clock(),
            final_url=payload.final_url, status_code=payload.status_code,
            safe_headers=dict(payload.safe_headers or {}), mime_type=payload.mime_type,
            response_bytes=response_bytes, body_sha256=body_sha256,
            access_status=access_status or (
                "BLOCKED" if payload.failure_reason or payload.body is None else "RECEIVED"),
            exception_category=payload.failure_reason, elapsed_seconds=elapsed,
            transport_metadata=metadata,
        )

    def normalized(self, request_id: str, *, normalized_chars: int, body_hash: str) -> None:
        self.transition(request_id, "NORMALIZED", normalized_chars=normalized_chars,
                        body_sha256=body_hash)

    def failed(self, request_id: str, *, category: str, detail: str | None = None) -> None:
        self.transition(request_id, "FAILED", exception_category=category,
                        detail=(detail or "")[:2000])

    def project_outcome_lost(self, *, request_id: str, transport: str, requested_url: str,
                             attempt_number: int, reason: str) -> None:
        self.start(request_id=request_id, transport=transport, requested_url=requested_url,
                   attempt_number=attempt_number)
        self.transition(request_id, "OUTCOME_LOST", exception_category="OUTCOME_LOST",
                        detail=reason[:2000])

    def _disk_records(self) -> tuple[list[dict[str, Any]], list[str]]:
        records, corrupt = [], []
        directory = self.store.spool_root / "http-journal"
        if not directory.exists():
            return records, corrupt
        for path in sorted(directory.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if (value.get("schema_version") != self.schema_version
                        or not isinstance(value.get("history"), list)):
                    raise ValueError("invalid journal schema")
                records.append(value)
            except Exception:
                corrupt.append(path.name)
        return records, corrupt

    def request_count(self) -> int:
        records, _ = self._disk_records()
        return sum(any(event.get("phase") == "REQUEST_STARTED" for event in row["history"])
                   for row in records)

    def incomplete_started(self) -> list[str]:
        records, _ = self._disk_records()
        terminal = {"RESPONSE_RECEIVED", "NORMALIZED", "FAILED", "OUTCOME_LOST"}
        return [row["request_id"] for row in records
                if any(event.get("phase") == "REQUEST_STARTED" for event in row["history"])
                and not any(event.get("phase") in terminal for event in row["history"])]

    def corrupt_records(self) -> list[str]:
        _, corrupt = self._disk_records()
        return corrupt


class JournaledTransport:
    """Write-ahead durability wrapper; the wrapped HTTP callable runs only after STARTED is durable."""

    def __init__(self, transport: OfficialFetchTransport, journal: HttpFetchJournal) -> None:
        self.inner, self.journal = transport, journal
        self.name = transport.name
        self._attempts = 0
        self._request_for_url: dict[str, str] = {}

    def fetch(self, requested_url: str, official_domain: str, policy: FetchPolicy) -> TransportPayload:
        self._attempts += 1
        request_id = f"request-{self._attempts:04d}-{uuid.uuid4().hex[:12]}"
        self.journal.start(request_id=request_id, transport=self.name,
                           requested_url=requested_url, attempt_number=self._attempts)
        self._request_for_url[_canonical_url(requested_url)] = request_id
        started = datetime.now(timezone.utc)
        try:
            payload = self.inner.fetch(requested_url, official_domain, policy)
        except Exception as exc:
            self.journal.failed(request_id, category=type(exc).__name__, detail=str(exc))
            raise
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        self.journal.response_received(request_id, payload, elapsed=elapsed)
        if payload.failure_reason or payload.body is None:
            self.journal.failed(request_id, category=payload.failure_reason or "UNKNOWN")
        return payload

    def record_normalized(self, requested_url: str, *, normalized_chars: int, body_hash: str) -> None:
        request_id = self._request_for_url[_canonical_url(requested_url)]
        self.journal.normalized(request_id, normalized_chars=normalized_chars, body_hash=body_hash)

    def record_normalization_failure(self, requested_url: str, exc: Exception) -> None:
        request_id = self._request_for_url[_canonical_url(requested_url)]
        self.journal.failed(request_id, category="NORMALIZATION", detail=str(exc))


class OfficialSourceResolver:
    def __init__(self, transport: OfficialFetchTransport, *, failure_cache: IdentityFailureCache | None = None,
                 clock: Callable[[], str] = _now) -> None:
        self.transport, self.failure_cache, self.clock = transport, failure_cache or IdentityFailureCache(), clock

    def resolve(self, identity: CompanyIdentityRecord, target_url: str,
                policy: FetchPolicy = FetchPolicy()) -> OfficialSourceResolution:
        if not self.failure_cache.retry_allowed(security_code=identity.security_code,
                                                domain=identity.official_domain, url=target_url,
                                                transport=self.transport.name):
            return OfficialSourceResolution(target_url, target_url, 0, "BLOCKED", None, None, None,
                                            self.clock(), self.transport.name, "RETRY_SUPPRESSED")
        try:
            payload = self.transport.fetch(target_url, identity.official_domain, policy)
        except OfficialSourceError as exc:
            reason = str(exc).split(":", 1)[0] if ":" in str(exc) else "UNKNOWN"
            self.failure_cache.record(security_code=identity.security_code, domain=identity.official_domain,
                                      url=target_url, transport=self.transport.name,
                                      failure_type=reason, attempted_at=self.clock())
            return OfficialSourceResolution(target_url, target_url, 0, "BLOCKED", None, None, None,
                                            self.clock(), self.transport.name, reason)
        if payload.failure_reason or payload.body is None:
            reason = payload.failure_reason or "UNKNOWN"
            self.failure_cache.record(security_code=identity.security_code, domain=identity.official_domain,
                                      url=target_url, transport=self.transport.name,
                                      failure_type=reason, attempted_at=self.clock())
            return OfficialSourceResolution(payload.requested_url, payload.final_url, payload.status_code,
                                            "BLOCKED", payload.mime_type, None, None, self.clock(),
                                            payload.fetch_method, reason)
        try:
            normalized = normalize_official_body(payload.body, payload.mime_type or "text/plain",
                                                 payload.final_url, identity.official_domain)
        except Exception as exc:
            callback = getattr(self.transport, "record_normalization_failure", None)
            if callback is not None:
                callback(target_url, exc)
            raise
        body_hash = hashlib.sha256(payload.body).hexdigest()
        callback = getattr(self.transport, "record_normalized", None)
        if callback is not None:
            callback(target_url, normalized_chars=len(normalized.visible_text), body_hash=body_hash)
        return OfficialSourceResolution(payload.requested_url, payload.final_url, payload.status_code,
                                        "VERIFIED", payload.mime_type, normalized,
                                        body_hash, self.clock(),
                                        payload.fetch_method, None)


def build_identity_evidence_card(result: OfficialSourceResolution, identity: CompanyIdentityRecord) -> dict[str, Any]:
    if result.access_status != "VERIFIED" or result.normalized_content is None or not result.body_hash:
        raise OfficialSourceError("verified body required for identity evidence")
    normalized = result.normalized_content
    if _normalized_name(identity.company_name) not in _normalized_name(normalized.visible_text):
        raise OfficialSourceError("official body does not identify the company")
    return {"url": normalized.canonical_url, "source_type": "company_official",
            "title": normalized.title, "body_excerpt": normalized.bounded_excerpt,
            "identity_support": identity.company_name, "body_hash": result.body_hash,
            "access_status": "VERIFIED"}


def promote_verified_identity(identity: CompanyIdentityRecord, result: OfficialSourceResolution,
                              *, verification_source: str) -> CompanyIdentityRecord:
    """Create a VERIFIED registry record only from accepted official body evidence."""
    card = build_identity_evidence_card(result, identity)
    method = ("REPOSITORY_VERIFIED_ARTIFACT" if result.fetch_method == "repository_existing"
              else "ACTUAL_OFFICIAL_BODY")
    record = CompanyIdentityRecord(
        security_code=identity.security_code, company_name=identity.company_name,
        normalized_name=_normalized_name(identity.company_name), official_domain=identity.official_domain,
        verified_official_url=card["url"], verification_source=verification_source,
        verification_method=method, verified_at=result.fetched_at, evidence_hash=result.body_hash,
        status="VERIFIED", evidence_excerpt=card["body_excerpt"])
    return validate_identity_record(record)


def model_context_from_resolution(result: OfficialSourceResolution,
                                  identity: CompanyIdentityRecord) -> dict[str, Any]:
    """Expose only a bounded card; raw/normalized full body never enters model context."""
    return {"company_identity_evidence": build_identity_evidence_card(result, identity)}


def identity_preflight(registry: CompanyIdentityRegistry, *, security_code: str,
                       company_name: str) -> dict[str, Any]:
    record = registry.usable(security_code, company_name)
    return {"status": "REUSE_VERIFIED" if record else "FETCH_REQUIRED",
            "identity_web_fetch_required": record is None,
            "identity_llm_worker_required": False if record else True,
            "record": record}


def identity_satisfies_validator(registry: CompanyIdentityRegistry | None, *, security_code: str,
                                 company_name: str | None, identity: Mapping[str, Any]) -> bool:
    if registry is None:
        return False
    record = registry.usable(security_code, company_name)
    return bool(record and identity.get("verified_ticker") == security_code
                and _canonical_url(str(identity.get("source_url", ""))) ==
                _canonical_url(record.verified_official_url or ""))


def apply_registry_identity(response: dict[str, Any], registry: CompanyIdentityRegistry,
                            assignment: Mapping[str, Any]) -> dict[str, Any]:
    record = registry.usable(str(assignment.get("ticker", "")), assignment.get("company_name"))
    if record is None:
        raise OfficialSourceError("no reusable VERIFIED identity")
    repaired = copy.deepcopy(response)
    strategy, payload = repaired["strategy_telemetry"], repaired["research_result"]
    url = _canonical_url(record.verified_official_url or "")
    row = {"url": url, "source_type": "company_official", "content_verified": True,
           "evidence_excerpt": record.evidence_excerpt}
    provenance = [item for item in strategy.get("source_provenance", [])
                  if _canonical_url(str(item.get("url", ""))) != url]
    provenance.append(row)
    strategy["source_provenance"] = provenance
    strategy["company_identity"] = {"verified_ticker": record.security_code, "source_url": url,
                                    "evidence": "Stable VERIFIED CompanyIdentityRegistry evidence reused."}
    payload["sources_checked_count"] = len(provenance)
    return repaired


def project_offline_state(*, security_code: str, company_name: str, official_domain: str,
                          failures: tuple[str, ...], verification_source: str) -> dict[str, Any]:
    """Project saved access failures without network or artifact mutation."""
    record = CompanyIdentityRecord(security_code, company_name, _normalized_name(company_name),
                                   official_domain, None, verification_source,
                                   None, None, None, "UNVERIFIED", None)
    window = OfficialWindowCoverage(security_code, "2026-08-25", "2026-08-31",
                                    "BLOCKED" if failures else "PARTIAL", (),
                                    failures or ("official window not yet checked",), ())
    return {"company_identity": record.to_dict(), "official_window": asdict(window),
            "search_snippets": "DISCOVERY_ONLY", "no_news_allowed": False,
            "web_calls": 0, "llm_calls": 0}
