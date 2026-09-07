"""Deterministic official-source resolution and stable company identity reuse.

The module has no import-time I/O. Network access is available only through an
explicit transport supplied to :class:`OfficialSourceResolver`.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import io
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
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

from lib.news_monitor import canonicalize_url as _canonical_document_url


IDENTITY_STATUSES = {"VERIFIED", "UNVERIFIED", "STALE"}
IDENTITY_TRANSPORT_ASSURANCES = {"HTTPS_VERIFIED", "LEGACY_HTTP", "REPOSITORY_VERIFIED"}
LEGACY_HTTP_AUTHORITIES = {"IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH", "COMPANY_MASTER"}
WINDOW_STATES = {"PENDING", "COMPLETE", "PARTIAL", "BLOCKED", "NOT_APPLICABLE"}
FAILURE_TYPES = {
    "HTTP_4XX", "HTTP_5XX", "TIMEOUT", "DNS", "TCP", "TLS", "TLS_CA_CHAIN", "TLS_HOSTNAME",
    "TLS_EXPIRED", "TLS_NOT_YET_VALID", "TLS_PROTOCOL", "TLS_HANDSHAKE", "TLS_UNKNOWN",
    "TLS_TRUST_INIT", "REDIRECT_BLOCKED",
    "MIME_REJECTED", "SIZE_LIMIT", "ACCESS_DENIED", "OUTCOME_LOST",
    "BODY_READ", "BODY_ENCODING", "NORMALIZATION", "EMPTY_BODY", "UNKNOWN",
}
TLS_BACKEND_CURRENT_CA = "current-ca-v1"
TLS_BACKEND_WINDOWS_SYSTEM_TRUST = "windows-system-trust-v1"
TLS_BACKEND_WINDOWS_ROOT_BUNDLE_SEMANTIC = "windows-root-bundle-v1"
SCHANNEL_TRANSPORT_ID = "windows-schannel-v1"
PYTHON_OPENSSL_SYSTEM_TRUST_TRANSPORT_ID = "python-openssl-system-trust-v1"
IR_NIGHTLY_KNOWN_SUCCESSFUL_TRANSPORT_ID = "ir-nightly-known-successful-v1"
SCHANNEL_IMPLEMENTATION_FINGERPRINT = "dotnet10-sslstream-schannel-cryptoapi-v5"
PYTHON_OPENSSL_IMPLEMENTATION_FINGERPRINT = "python-ssl-default-context-v1"
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
    transport_assurance: str | None = None
    transport_security_warning: bool = False
    tls_verified: bool | None = None
    url_authority: str | None = None
    evidence_http_status: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def company_identity_registry_schema() -> dict[str, Any]:
    nullable = lambda kind: {"type": [kind, "null"]}
    record = {
        "type": "object", "additionalProperties": False,
        "required": ["security_code", "company_name", "normalized_name", "official_domain",
                     "verified_official_url", "verification_source", "verification_method",
                     "verified_at", "evidence_hash", "status", "evidence_excerpt",
                     "transport_assurance", "transport_security_warning", "tls_verified",
                     "url_authority", "evidence_http_status"],
        "properties": {
            "security_code": {"type": "string"}, "company_name": {"type": "string"},
            "normalized_name": {"type": "string"}, "official_domain": {"type": "string"},
            "verified_official_url": nullable("string"), "verification_source": nullable("string"),
            "verification_method": nullable("string"), "verified_at": nullable("string"),
            "evidence_hash": nullable("string"), "status": {"type": "string", "enum": sorted(IDENTITY_STATUSES)},
            "evidence_excerpt": nullable("string"),
            "transport_assurance": {"type": ["string", "null"],
                                    "enum": [*sorted(IDENTITY_TRANSPORT_ASSURANCES), None]},
            "transport_security_warning": {"type": "boolean"},
            "tls_verified": {"type": ["boolean", "null"]},
            "url_authority": nullable("string"),
            "evidence_http_status": {"type": ["integer", "null"]},
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
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not _is_official_host(parsed.hostname, record.official_domain):
        raise OfficialSourceError("verified identity URL is outside the official domain")
    assurance = record.transport_assurance
    if assurance is None:
        assurance = ("REPOSITORY_VERIFIED" if record.verification_method == "REPOSITORY_VERIFIED_ARTIFACT"
                     else "HTTPS_VERIFIED") if parsed.scheme == "https" else None
    if assurance not in IDENTITY_TRANSPORT_ASSURANCES:
        raise OfficialSourceError("VERIFIED identity requires transport assurance")
    if assurance == "LEGACY_HTTP":
        if (parsed.scheme != "http" or record.url_authority not in LEGACY_HTTP_AUTHORITIES
                or record.evidence_http_status != 200 or not record.transport_security_warning
                or record.tls_verified is not False):
            raise OfficialSourceError("LEGACY_HTTP identity evidence is incomplete")
    elif (parsed.scheme != "https" or record.transport_security_warning
          or record.tls_verified is False):
        raise OfficialSourceError("secure identity assurance requires HTTPS")
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
    not_applicable_families: tuple[str, ...] = ()


def validate_window_coverage(value: OfficialWindowCoverage) -> OfficialWindowCoverage:
    if value.state not in WINDOW_STATES:
        raise OfficialSourceError("invalid official window state")
    if value.state == "COMPLETE":
        required = {"news", "ir", "announcements"}
        covered = set(value.checked_families) | set(value.not_applicable_families)
        if not required <= covered or value.gaps:
            raise OfficialSourceError("COMPLETE official window lacks required checks")
    if set(value.checked_families) & set(value.not_applicable_families):
        raise OfficialSourceError("official window family cannot be checked and non-applicable")
    if value.state == "PENDING" and (value.checked_families or value.gaps or value.evidence_hashes):
        raise OfficialSourceError("PENDING official window must be unresolved")
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
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        addresses = {row[4][0] for row in dns_resolver(
            parsed.hostname, parsed.port or default_port, type=socket.SOCK_STREAM)}
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
    if "hostname" in message or "doesn't match" in message or "does not match" in message:
        return "TLS_HOSTNAME"
    if "expired" in message:
        return "TLS_EXPIRED"
    if "not yet valid" in message:
        return "TLS_NOT_YET_VALID"
    if any(value in message for value in ("unable to get local issuer", "self-signed certificate",
                                           "unknown ca", "certificate verify failed")):
        return "TLS_CA_CHAIN"
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
                 system_context_factory: Callable[[], ssl.SSLContext] = windows_system_trust_context,
                 transport_name: str | None = None,
                 retain_raw_html: bool = False,
                 user_agent: str = "CompanyNewsOfficialResolver/1.0") -> None:
        if tls_backend not in {TLS_BACKEND_CURRENT_CA, TLS_BACKEND_WINDOWS_SYSTEM_TRUST}:
            raise TlsTrustInitializationError("TLS_TRUST_INIT: unsupported TLS backend")
        self.session_factory, self.dns_resolver = session_factory, dns_resolver
        self.journal = journal
        self.tls_backend = tls_backend
        self.transport_name = transport_name
        self.retain_raw_html = retain_raw_html
        self.user_agent = user_agent
        self._system_context = None
        if tls_backend == TLS_BACKEND_WINDOWS_SYSTEM_TRUST:
            self._system_context = validate_verified_tls_context(system_context_factory())
        self._attempts = 0
        self._request_for_target: dict[str, str] = {}

    @property
    def name(self) -> str:
        if self.transport_name:
            return self.transport_name
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

    def _start_request(self, target_url: str, wire_url: str, *,
                       parent_request_id: str | None = None, redirect_hop: int = 0,
                       redirect_source_url: str | None = None,
                       redirect_location: str | None = None) -> tuple[str | None, float]:
        started = time.monotonic()
        if self.journal is None:
            return None, started
        self._attempts += 1
        request_id = f"request-{self._attempts:04d}-{uuid.uuid4().hex[:12]}"
        self.journal.start(
            request_id=request_id, transport=self.name, requested_url=wire_url,
            attempt_number=self._attempts, parent_request_id=parent_request_id,
            redirect_hop=redirect_hop, redirect_source_url=redirect_source_url,
            redirect_location=redirect_location,
            redirect_target_url=wire_url if parent_request_id else None)
        self._request_for_target[_canonical_url(target_url)] = request_id
        return request_id, started

    def _finish_request(self, request_id: str | None, payload: TransportPayload,
                        started: float, *, access_status: str | None = None,
                        raw_body: bytes | None = None) -> TransportPayload:
        if self.journal is not None and request_id is not None:
            retained = payload.body if raw_body is None else raw_body
            if (self.retain_raw_html and retained is not None
                    and payload.mime_type in {"text/html", "application/xhtml+xml", "text/plain"}):
                relative = Path("raw-http") / f"{request_id}.body"
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
                    payload.failure_reason, payload.safe_headers, metadata)
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
        if hasattr(session, "headers"):
            session.headers.pop("Authorization", None)
            session.headers.pop("Cookie", None)
        try:
            parent_request_id = None
            redirect_source_url = None
            redirect_location = None
            for redirects in range(policy.max_redirects + 1):
                request_id, request_started = self._start_request(
                    requested_url, current, parent_request_id=parent_request_id,
                    redirect_hop=redirects, redirect_source_url=redirect_source_url,
                    redirect_location=redirect_location)
                try:
                    response = session.get(current, headers={"User-Agent": self.user_agent,
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
                    parent_request_id = request_id
                    redirect_source_url = current
                    redirect_location = location
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


class IrNightlyKnownSuccessfulTransport(DirectHttpTransport):
    """Requests transport restricted by callers to persisted IR Nightly exact URLs."""

    def __init__(self, *, session_factory: Callable[[], Any] = requests.Session,
                 dns_resolver: Callable[..., Any] = socket.getaddrinfo,
                 journal: "HttpFetchJournal | None" = None) -> None:
        super().__init__(
            session_factory=session_factory, dns_resolver=dns_resolver, journal=journal,
            tls_backend=TLS_BACKEND_CURRENT_CA,
            transport_name=IR_NIGHTLY_KNOWN_SUCCESSFUL_TRANSPORT_ID,
            retain_raw_html=True,
            user_agent="tdnet-company-ir-monitor/1.0 (+nightly; metadata-only)")


def python_openssl_system_trust_context(
        context_factory: Callable[..., ssl.SSLContext] = ssl.create_default_context,
        ) -> ssl.SSLContext:
    """Build the immutable verified TLS policy for the in-process transport."""
    context = context_factory(ssl.Purpose.SERVER_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return validate_verified_tls_context(context)


def _certificate_name(value: Any) -> list[list[list[str]]]:
    return [[[str(key), str(item)] for key, item in group] for group in (value or ())]


class PythonOpenSslSystemTrustTransport:
    """Strict in-process HTTPS transport using Python's default OpenSSL trust.

    The profile has no helper process and never falls back to another transport.
    A successful ``wrap_socket`` is the authority for both chain and hostname
    validation because the context is fixed to CERT_REQUIRED/check_hostname.
    """

    name = PYTHON_OPENSSL_SYSTEM_TRUST_TRANSPORT_ID
    implementation_fingerprint = PYTHON_OPENSSL_IMPLEMENTATION_FINGERPRINT

    def __init__(self, *, journal: "HttpFetchJournal | None" = None,
                 dns_resolver: Callable[..., Any] = socket.getaddrinfo,
                 socket_connector: Callable[..., Any] = socket.create_connection,
                 context_factory: Callable[..., ssl.SSLContext] = ssl.create_default_context,
                 response_factory: Callable[..., Any] = http.client.HTTPResponse,
                 retain_raw_html: bool = False,
                 wire_fetcher: Callable[[str, FetchPolicy], TransportPayload] | None = None,
                 ) -> None:
        self.journal = journal
        self.dns_resolver = dns_resolver
        self.socket_connector = socket_connector
        self.response_factory = response_factory
        self.retain_raw_html = retain_raw_html
        self.context = python_openssl_system_trust_context(context_factory)
        self.wire_fetcher = wire_fetcher or self._fetch_once
        self._attempts = 0
        self._request_for_target: dict[str, str] = {}

    def _context_provenance(self) -> dict[str, Any]:
        defaults = ssl.get_default_verify_paths()
        return {
            "transport_profile": self.name,
            "implementation_fingerprint": self.implementation_fingerprint,
            "python_version": platform.python_version(),
            "python_executable": str(Path(sys.executable).resolve()),
            "openssl_version": ssl.OPENSSL_VERSION,
            "ca_source": "ssl.create_default_context(Purpose.SERVER_AUTH)",
            "default_verify_paths": {
                "cafile": defaults.cafile, "capath": defaults.capath,
                "openssl_cafile_env": defaults.openssl_cafile_env,
                "openssl_cafile": defaults.openssl_cafile,
                "openssl_capath_env": defaults.openssl_capath_env,
                "openssl_capath": defaults.openssl_capath,
            },
            "cert_store_stats": self.context.cert_store_stats(),
            "verify_mode": "CERT_REQUIRED",
            "check_hostname": True,
            "minimum_tls_version": "TLSv1_2",
            "helper_process_started": False,
        }

    def _start_request(self, target_url: str, *, parent_request_id: str | None = None,
                       redirect_hop: int = 0, redirect_source_url: str | None = None,
                       redirect_location: str | None = None) -> tuple[str | None, float]:
        started = time.monotonic()
        if self.journal is None:
            return None, started
        self._attempts += 1
        request_id = f"request-{self._attempts:04d}-{uuid.uuid4().hex[:12]}"
        self.journal.start(
            request_id=request_id, transport=self.name, requested_url=target_url,
            attempt_number=self._attempts, parent_request_id=parent_request_id,
            redirect_hop=redirect_hop, redirect_source_url=redirect_source_url,
            redirect_location=redirect_location,
            redirect_target_url=target_url if parent_request_id else None)
        self._request_for_target[_canonical_url(target_url)] = request_id
        return request_id, started

    def _finish_request(self, request_id: str | None, payload: TransportPayload,
                        started: float, *, raw_body: bytes | None = None,
                        access_status: str | None = None) -> TransportPayload:
        if self.journal is not None and request_id is not None:
            retained = payload.body if raw_body is None else raw_body
            if (self.retain_raw_html and retained is not None
                    and payload.mime_type in {"text/html", "application/xhtml+xml", "text/plain"}):
                relative = Path("raw-http") / f"{request_id}.body"
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
                    payload.failure_reason, payload.safe_headers, metadata)
            self.journal.response_received(
                request_id, payload,
                elapsed=float((payload.transport_metadata or {}).get(
                    "elapsed_seconds", time.monotonic() - started)),
                access_status=access_status)
            if payload.failure_reason:
                self.journal.failed(request_id, category=payload.failure_reason)
        return payload

    def record_normalized(self, requested_url: str, *, normalized_chars: int,
                          body_hash: str) -> None:
        if self.journal is not None:
            self.journal.normalized(
                self._request_for_target[_canonical_url(requested_url)],
                normalized_chars=normalized_chars, body_hash=body_hash)

    def record_normalization_failure(self, requested_url: str, exc: Exception) -> None:
        if self.journal is not None:
            self.journal.failed(
                self._request_for_target[_canonical_url(requested_url)],
                category="NORMALIZATION", detail=str(exc))

    def _failure(self, requested_url: str, current: str, category: str,
                 started: float, stage: str, exc: BaseException,
                 metadata: Mapping[str, Any]) -> TransportPayload:
        detail = dict(metadata)
        detail.update({
            "failure_stage": stage,
            "exception_type": type(exc).__name__,
            "error_detail": str(exc)[:2000],
            "elapsed_seconds": time.monotonic() - started,
        })
        if isinstance(exc, ssl.SSLCertVerificationError):
            detail["certificate_verification"] = {
                "verify_code": getattr(exc, "verify_code", None),
                "verify_message": getattr(exc, "verify_message", None),
            }
        return TransportPayload(
            requested_url, current, 0, None, None, self.name, category, {}, detail)

    def _fetch_once(self, current: str, policy: FetchPolicy) -> TransportPayload:
        parsed = urlsplit(current)
        hostname = parsed.hostname or ""
        started = time.monotonic()
        metadata = self._context_provenance()
        metadata["sni"] = hostname
        timings: dict[str, float] = {}
        metadata["stage_timings_seconds"] = timings
        try:
            self.dns_resolver(hostname, 443, type=socket.SOCK_STREAM)
            timings["dns_complete"] = time.monotonic() - started
        except socket.timeout as exc:
            return self._failure(current, current, "TIMEOUT", started, "DNS", exc, metadata)
        except OSError as exc:
            return self._failure(current, current, "DNS", started, "DNS", exc, metadata)
        raw_socket = tls_socket = None
        try:
            try:
                raw_socket = self.socket_connector((hostname, 443), timeout=policy.timeout_seconds)
                timings["tcp_complete"] = time.monotonic() - started
            except (socket.timeout, TimeoutError) as exc:
                return self._failure(current, current, "TIMEOUT", started, "TCP", exc, metadata)
            except OSError as exc:
                return self._failure(current, current, "TCP", started, "TCP", exc, metadata)
            try:
                tls_socket = self.context.wrap_socket(raw_socket, server_hostname=hostname)
                raw_socket = None
                timings["tls_complete"] = time.monotonic() - started
            except ssl.SSLCertVerificationError as exc:
                return self._failure(
                    current, current, classify_tls_failure(exc), started, "CERTIFICATE",
                    exc, metadata)
            except (ssl.SSLError, OSError) as exc:
                category = classify_tls_failure(exc)
                if category == "TLS_UNKNOWN":
                    category = "TLS_HANDSHAKE"
                return self._failure(current, current, category, started, "TLS", exc, metadata)
            certificate_der = tls_socket.getpeercert(binary_form=True)
            certificate = tls_socket.getpeercert()
            cipher = tls_socket.cipher()
            metadata.update({
                "certificate_chain_validation": "PASS",
                "hostname_validation": "PASS",
                "peer_certificate": {
                    "der_sha256": hashlib.sha256(certificate_der).hexdigest(),
                    "subject": _certificate_name(certificate.get("subject")),
                    "issuer": _certificate_name(certificate.get("issuer")),
                    "subject_alt_name": [[str(k), str(v)] for k, v in certificate.get("subjectAltName", ())],
                    "not_before": certificate.get("notBefore"),
                    "not_after": certificate.get("notAfter"),
                },
                "negotiated_tls_version": tls_socket.version(),
                "cipher": {"name": cipher[0], "protocol": cipher[1], "bits": cipher[2]} if cipher else None,
                "alpn": tls_socket.selected_alpn_protocol(),
            })
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            request = (
                f"GET {target} HTTP/1.1\r\nHost: {hostname}\r\n"
                "User-Agent: CompanyNewsOfficialResolverPythonOpenSSL/1.0\r\n"
                "Accept: text/html,application/xhtml+xml,application/pdf,text/plain\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            tls_socket.sendall(request)
            response = self.response_factory(tls_socket)
            response.begin()
            status = int(response.status)
            headers = {str(key).lower(): str(value)[:1000] for key, value in response.getheaders()
                       if str(key).lower() in {"content-type", "content-length", "location", "etag", "last-modified"}}
            mime = headers.get("content-type", "").split(";", 1)[0].strip().lower() or None
            content_length = headers.get("content-length")
            if content_length and int(content_length) > policy.max_response_bytes:
                metadata.update(elapsed_seconds=time.monotonic() - started,
                                failure_stage="BODY", response_bytes=0)
                return TransportPayload(current, current, status, mime, None, self.name,
                                        "SIZE_LIMIT", headers, metadata)
            body = response.read(policy.max_response_bytes + 1)
            timings["body_complete"] = time.monotonic() - started
            metadata.update({
                "elapsed_seconds": time.monotonic() - started,
                "failure_stage": None,
                "response_bytes": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "request_header_profile": {
                    "host": hostname,
                    "user_agent": "CompanyNewsOfficialResolverPythonOpenSSL/1.0",
                    "accept": "text/html,application/xhtml+xml,application/pdf,text/plain",
                    "connection": "close",
                },
            })
            if len(body) > policy.max_response_bytes:
                return TransportPayload(current, current, status, mime, None, self.name,
                                        "SIZE_LIMIT", headers, metadata)
            return TransportPayload(current, current, status, mime, body, self.name,
                                    None, headers, metadata)
        except (socket.timeout, TimeoutError) as exc:
            return self._failure(current, current, "TIMEOUT", started, "HTTP", exc, metadata)
        except (http.client.HTTPException, OSError, ValueError) as exc:
            return self._failure(current, current, "BODY_READ", started, "HTTP", exc, metadata)
        finally:
            for stream in (tls_socket, raw_socket):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def fetch(self, requested_url: str, official_domain: str,
              policy: FetchPolicy) -> TransportPayload:
        if urlsplit(requested_url).scheme.lower() != "https":
            raise OfficialSourceError(
                "ACCESS_DENIED: python OpenSSL system-trust transport requires HTTPS")
        current = validate_fetch_url(requested_url, official_domain, self.dns_resolver)
        visited = {_canonical_url(current)}
        parent_request_id = redirect_source_url = redirect_location = None
        for redirects in range(policy.max_redirects + 1):
            request_id, started = self._start_request(
                current, parent_request_id=parent_request_id, redirect_hop=redirects,
                redirect_source_url=redirect_source_url,
                redirect_location=redirect_location)
            payload = self.wire_fetcher(current, policy)
            if payload.fetch_method != self.name:
                payload = TransportPayload(
                    payload.requested_url, payload.final_url, payload.status_code,
                    payload.mime_type, None, self.name, "UNKNOWN", payload.safe_headers,
                    {**(payload.transport_metadata or {}),
                     "provenance_error": "wire fetcher transport mismatch"})
            status = payload.status_code
            if status in range(300, 400) and not payload.failure_reason:
                location = (payload.safe_headers or {}).get("location")
                if not location or redirects == policy.max_redirects:
                    blocked = TransportPayload(
                        requested_url, current, status, payload.mime_type, None,
                        self.name, "REDIRECT_BLOCKED", payload.safe_headers,
                        payload.transport_metadata)
                    return self._finish_request(request_id, blocked, started,
                                                raw_body=payload.body)
                destination = urljoin(current, location)
                current_host = _domain(urlsplit(current).hostname or "")
                destination_host = _domain(urlsplit(destination).hostname or "")
                if (urlsplit(destination).scheme.lower() != "https"
                        or destination_host != current_host
                        or _canonical_url(destination) in visited):
                    blocked = TransportPayload(
                        requested_url, destination, status, payload.mime_type, None,
                        self.name, "REDIRECT_BLOCKED", payload.safe_headers,
                        payload.transport_metadata)
                    return self._finish_request(request_id, blocked, started,
                                                raw_body=payload.body)
                self._finish_request(request_id, payload, started, raw_body=payload.body,
                                     access_status="REDIRECT")
                parent_request_id = request_id
                redirect_source_url = current
                redirect_location = str(location)
                current = validate_fetch_url(destination, official_domain, self.dns_resolver)
                visited.add(_canonical_url(current))
                continue
            failure = payload.failure_reason
            if not failure and status >= 500:
                failure = "HTTP_5XX"
            elif not failure and status >= 400:
                failure = "HTTP_4XX"
            elif not failure and payload.body is not None and len(payload.body) > policy.max_response_bytes:
                failure = "SIZE_LIMIT"
            elif not failure and payload.mime_type not in policy.allowed_mime_types:
                failure = "MIME_REJECTED"
            elif not failure and status not in {204, 205, 304} and not payload.body:
                failure = "EMPTY_BODY"
            final = TransportPayload(
                requested_url, current, status, payload.mime_type,
                None if failure else payload.body, self.name, failure,
                payload.safe_headers, payload.transport_metadata)
            self._request_for_target[_canonical_url(requested_url)] = request_id
            return self._finish_request(
                request_id, final, started,
                raw_body=None if failure == "SIZE_LIMIT" else payload.body)
        return TransportPayload(requested_url, current, 0, None, None,
                                self.name, "UNKNOWN")


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
        argv = [executable, "-NoProfile", "-NonInteractive", "-File", str(self.helper_path)]
        completed = subprocess.run(
            argv,
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
        def environment_marker(name: str) -> dict[str, Any]:
            raw = os.environ.get(name)
            return {
                "present": raw is not None,
                "value_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()
                if raw is not None else None,
            }

        executable_path = Path(executable).resolve()
        try:
            executable_sha = hashlib.sha256(executable_path.read_bytes()).hexdigest()
        except OSError:
            executable_sha = None
        try:
            version = subprocess.run(
                [executable, "-NoProfile", "-NonInteractive", "-Command",
                 "$PSVersionTable.PSVersion.ToString()"],
                capture_output=True, text=True, encoding="utf-8", timeout=5, check=False,
            )
            executable_version = version.stdout.strip() if version.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            executable_version = None
        value["helper_exit_code"] = completed.returncode
        value["launcher_provenance"] = {
            "executable_path": str(executable_path),
            "executable_sha256": executable_sha,
            "executable_version": executable_version,
            "helper_path": str(self.helper_path.resolve()),
            "helper_sha256": hashlib.sha256(self.helper_path.read_bytes()).hexdigest(),
            "argv": argv,
            "timeout_seconds": float(request["timeout_seconds"]) + 5,
            "parent_process_id": os.getpid(),
            "parent_parent_process_id": os.getppid(),
            "execution_user": os.environ.get("USERNAME") or os.environ.get("USER"),
            "working_directory": str(Path.cwd().resolve()),
            "environment": {name: environment_marker(name) for name in (
                "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
            )},
            "proxy_behavior": "DIRECT_TCP_PROXY_ENV_NOT_CONSUMED",
        }
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
                "native_error": response.get("native_error"),
                "stage_timings_seconds": response.get("stage_timings_seconds") or {},
                "tls_policy": response.get("tls_policy") or {},
                "helper_exit_code": response.get("helper_exit_code"),
                "launcher_provenance": response.get("launcher_provenance") or {},
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
    identity_markers: tuple[str, ...] = ()


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


def _html_identity_markers(soup: BeautifulSoup) -> tuple[str, ...]:
    """Keep bounded, structured corporate markers before layout nodes are removed."""
    markers: list[str] = []

    def add(value: Any) -> None:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if text and text not in markers:
            markers.append(text[:1_000])

    if soup.title:
        add(soup.title.get_text(" ", strip=True))
    accepted_meta = {"description", "application-name", "og:site_name", "og:title"}
    for node in soup.find_all("meta"):
        key = str(node.get("property") or node.get("name") or "").casefold()
        if key in accepted_meta:
            add(node.get("content"))
    for node in soup.find_all("img", alt=True):
        descriptor = " ".join(str(node.get(key) or "") for key in ("src", "class", "id")).casefold()
        if "logo" in descriptor:
            add(node.get("alt"))
    for node in soup.find_all(attrs={"itemprop": re.compile(r"^(?:name|legalName|alternateName)$", re.I)}):
        add(node.get("content") or node.get_text(" ", strip=True))

    def json_names(value: Any, *, organization: bool = False) -> None:
        if isinstance(value, list):
            for item in value:
                json_names(item, organization=organization)
        elif isinstance(value, dict):
            kind = value.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            is_org = organization or any(str(item).casefold() in {"organization", "corporation"}
                                         for item in kinds)
            if is_org:
                for key in ("name", "legalName", "alternateName"):
                    add(value.get(key))
            for item in value.values():
                json_names(item, organization=is_org)

    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            json_names(json.loads(node.string or node.get_text(" ", strip=False)))
        except (TypeError, ValueError):
            continue
    for node in soup.find_all("footer"):
        add(node.get_text(" ", strip=True))
    return tuple(markers[:40])


def derive_exact_https_identity_urls(saved_body: bytes, official_domain: str) -> tuple[str, ...]:
    """Return only literal, same-domain HTTPS identity authorities from saved HTML."""
    soup = BeautifulSoup(saved_body, "html.parser")
    candidates: list[str] = []
    for node in soup.find_all("link"):
        if "canonical" in [str(item).casefold() for item in node.get("rel", [])]:
            candidates.append(str(node.get("href") or ""))
    for node in soup.find_all("meta"):
        key = str(node.get("property") or node.get("name") or "").casefold()
        if key == "og:url":
            candidates.append(str(node.get("content") or ""))
        if str(node.get("http-equiv") or "").casefold() == "refresh":
            match = re.search(r"(?i)(?:^|;)\s*url\s*=\s*([^;]+)", str(node.get("content") or ""))
            if match:
                candidates.append(match.group(1).strip(" \t\"'"))
    for node in soup.find_all("base", href=True):
        candidates.append(str(node["href"]))
    for node in soup.find_all(True):
        for attribute in ("href", "src", "action"):
            value = node.get(attribute)
            if isinstance(value, str) and value.casefold().startswith("https://"):
                candidates.append(value)
    decoded = saved_body.decode("utf-8", "ignore")
    candidates.extend(re.findall(r"(?i)https://[^\s\"'<>]+", decoded))
    accepted = []
    for value in candidates:
        parsed = urlsplit(value.strip())
        if parsed.scheme.casefold() != "https" or not parsed.hostname:
            continue
        if not _is_official_host(parsed.hostname, official_domain):
            continue
        canonical = _canonical_url(value.strip())
        if canonical not in accepted:
            accepted.append(canonical)
    return tuple(accepted)


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
        identity_markers = _html_identity_markers(soup)
        # Navigation/header links are often the only deterministic route from a
        # verified corporate root to its news and IR indexes.  Capture exact
        # same-domain hrefs before layout nodes are removed from visible text.
        links = []
        for node in soup.find_all("a", href=True):
            candidate = urljoin(final_url, node["href"])
            host = urlsplit(candidate).hostname or ""
            if _is_official_host(host, official_domain):
                links.append(_canonical_url(candidate))
        for node in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            node.decompose()
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        canonical_node = soup.find("link", rel=lambda value: value and "canonical" in value)
        canonical = urljoin(final_url, canonical_node.get("href")) if canonical_node else final_url
        if not _is_official_host(urlsplit(canonical).hostname or "", official_domain):
            canonical = final_url
        text = soup.get_text(" ", strip=True)
    if mime_type == "application/pdf":
        identity_markers = ()
    visible = re.sub(r"\s+", " ", text).strip()[:text_limit]
    dates = tuple(dict.fromkeys(_DATE.findall(visible)))
    return NormalizedOfficialBody(title[:500], _canonical_url(canonical), visible,
                                  dates[:20], tuple(dict.fromkeys(links))[:25], visible[:excerpt_limit],
                                  identity_markers)


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


def _authoritative_identity_support(normalized: NormalizedOfficialBody,
                                    identity: CompanyIdentityRecord,
                                    authoritative_names: tuple[str, ...]) -> str | None:
    names = (identity.company_name, *authoritative_names)
    bodies = (normalized.visible_text, *normalized.identity_markers)
    for name in names:
        needle = _normalized_name(name)
        if needle and any(needle in _normalized_name(body) for body in bodies):
            return name
    return None


def build_identity_evidence_card(result: OfficialSourceResolution, identity: CompanyIdentityRecord,
                                 *, authoritative_names: tuple[str, ...] = ()) -> dict[str, Any]:
    if result.access_status != "VERIFIED" or result.normalized_content is None or not result.body_hash:
        raise OfficialSourceError("verified body required for identity evidence")
    normalized = result.normalized_content
    support = _authoritative_identity_support(normalized, identity, authoritative_names)
    if support is None:
        raise OfficialSourceError("official body does not identify the company")
    return {"url": normalized.canonical_url, "source_type": "company_official",
            "title": normalized.title, "body_excerpt": normalized.bounded_excerpt,
            "identity_support": support, "body_hash": result.body_hash,
            "access_status": "VERIFIED"}


def promote_verified_identity(identity: CompanyIdentityRecord, result: OfficialSourceResolution,
                              *, verification_source: str,
                              authoritative_names: tuple[str, ...] = (),
                              url_authority: str | None = None,
                              allow_legacy_http: bool = False) -> CompanyIdentityRecord:
    """Create a VERIFIED registry record only from accepted official body evidence."""
    card = build_identity_evidence_card(result, identity, authoritative_names=authoritative_names)
    method = ("REPOSITORY_VERIFIED_ARTIFACT" if result.fetch_method == "repository_existing"
              else "ACTUAL_OFFICIAL_BODY")
    parsed = urlsplit(card["url"])
    legacy = parsed.scheme == "http"
    if legacy:
        if (not allow_legacy_http or result.status_code != 200
                or _canonical_url(result.requested_url) != _canonical_url(result.final_url)
                or url_authority not in LEGACY_HTTP_AUTHORITIES):
            raise OfficialSourceError("LEGACY_HTTP identity contract is not satisfied")
        assurance, warning, tls_verified = "LEGACY_HTTP", True, False
    else:
        assurance = "REPOSITORY_VERIFIED" if method == "REPOSITORY_VERIFIED_ARTIFACT" else "HTTPS_VERIFIED"
        warning, tls_verified = False, (True if assurance == "HTTPS_VERIFIED" else None)
    record = CompanyIdentityRecord(
        security_code=identity.security_code, company_name=identity.company_name,
        normalized_name=_normalized_name(identity.company_name), official_domain=identity.official_domain,
        verified_official_url=card["url"], verification_source=verification_source,
        verification_method=method, verified_at=result.fetched_at, evidence_hash=result.body_hash,
        status="VERIFIED", evidence_excerpt=card["body_excerpt"],
        transport_assurance=assurance, transport_security_warning=warning,
        tls_verified=tls_verified, url_authority=url_authority,
        evidence_http_status=result.status_code)
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
                and _canonical_document_url(str(identity.get("source_url", ""))) ==
                _canonical_document_url(record.verified_official_url or ""))


def _same_document_aliases(
        record: CompanyIdentityRecord,
        equivalences: Sequence[Mapping[str, Any]],
        ) -> dict[str, str]:
    """Validate resolver-proven aliases and map them to one document key."""
    identity_url = _canonical_document_url(record.verified_official_url or "")
    expected_hash = record.evidence_hash
    expected_domain = _domain(record.official_domain)
    edges: list[tuple[str, str, str]] = []
    for index, item in enumerate(equivalences):
        declared = _canonical_document_url(str(item.get("declared_url") or ""))
        resolved = _canonical_document_url(str(item.get("resolved_url") or ""))
        body_hash = item.get("body_sha256")
        if not declared or not resolved or not body_hash:
            raise OfficialSourceError(f"document equivalence[{index}] is incomplete")
        if not _SHA256.fullmatch(str(body_hash).lower()):
            raise OfficialSourceError(f"document equivalence[{index}] body hash is invalid")
        if not (_is_official_host(urlsplit(declared).hostname or "", expected_domain)
                and _is_official_host(urlsplit(resolved).hostname or "", expected_domain)):
            raise OfficialSourceError("document equivalence crosses official-domain authority")
        edges.append((declared, resolved, str(body_hash).lower()))
    connected = {identity_url}
    changed = True
    while changed:
        changed = False
        for declared, resolved, _body_hash in edges:
            if declared in connected or resolved in connected:
                before = len(connected)
                connected.update((declared, resolved))
                changed = changed or len(connected) != before
    relevant_hashes = {body_hash for declared, resolved, body_hash in edges
                       if declared in connected and resolved in connected}
    if len(relevant_hashes) > 1 or (relevant_hashes and expected_hash
            and str(expected_hash).lower() not in relevant_hashes):
        raise OfficialSourceError("document equivalence conflicts with identity body hash")
    key = min(connected)
    aliases = {url: key for url in connected} if len(connected) > 1 else {}
    return aliases


def _authority_excerpt(record: CompanyIdentityRecord, model_rows: Sequence[Mapping[str, Any]]) -> str:
    """Keep event/context evidence visible while identity evidence stays in its authority record."""
    model_evidence = sorted({
        str(row.get("evidence_excerpt") or "").strip()
        for row in model_rows if str(row.get("evidence_excerpt") or "").strip()
    })
    if model_evidence:
        value = " | ".join(model_evidence)
    else:
        value = str(record.evidence_excerpt or "").strip()
    if not value:
        raise OfficialSourceError("registry identity provenance evidence is empty")
    # Canonical schema bounds this projection. The complete Registry evidence,
    # body hash, and event Evidence Cards remain in their separate authorities.
    return value[:1000]


def apply_registry_identity_with_audit(
        response: dict[str, Any], registry: CompanyIdentityRegistry,
        assignment: Mapping[str, Any], *,
        document_equivalences: Sequence[Mapping[str, Any]] = (),
        evidence_cards: Sequence[Mapping[str, Any]] = (),
        ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply frozen identity and reconcile only proven same-document provenance.

    Identity authority and event/context linkage remain separate authorities.
    The canonical response receives one document row, while this function's
    audit return preserves every input role, evidence id, and differing field.
    """
    record = registry.usable(str(assignment.get("ticker", "")), assignment.get("company_name"))
    if record is None:
        raise OfficialSourceError("no reusable VERIFIED identity")
    repaired = copy.deepcopy(response)
    strategy, payload = repaired["strategy_telemetry"], repaired["research_result"]
    url = _canonical_document_url(record.verified_official_url or "")
    if not url or not _is_official_host(urlsplit(url).hostname or "", record.official_domain):
        raise OfficialSourceError("registry identity URL conflicts with official domain")
    aliases = _same_document_aliases(record, document_equivalences)
    identity_key = aliases.get(url, url)
    original = strategy.get("source_provenance", [])
    if not isinstance(original, list):
        raise OfficialSourceError("source provenance is missing")
    matches, provenance = [], []
    for index, item in enumerate(original):
        if not isinstance(item, Mapping):
            raise OfficialSourceError(f"source provenance[{index}] is invalid")
        normalized = {**item, "url": _canonical_document_url(str(item.get("url") or ""))}
        key = aliases.get(normalized["url"], normalized["url"])
        if key == identity_key:
            if item.get("content_verified") is not True:
                raise OfficialSourceError("identity-linked provenance is not body verified")
            supplied_hashes = {
                str(item.get(name)).lower() for name in ("body_hash", "body_sha256")
                if item.get(name)
            }
            if len(supplied_hashes) > 1 or (supplied_hashes and record.evidence_hash
                    and str(record.evidence_hash).lower() not in supplied_hashes):
                raise OfficialSourceError("identity-linked provenance body hash conflict")
            supplied_domains = {str(item.get("official_domain")).lower()
                                for item in (item,) if item.get("official_domain")}
            if supplied_domains and _domain(record.official_domain) not in supplied_domains:
                raise OfficialSourceError("identity-linked provenance official domain conflict")
            supplied_transport = item.get("transport_assurance")
            if (supplied_transport and record.transport_assurance
                    and supplied_transport != record.transport_assurance):
                raise OfficialSourceError("identity-linked provenance transport assurance conflict")
            matches.append((index, dict(item), normalized))
        else:
            provenance.append(normalized)
    authority_row = {
        "url": url, "source_type": "company_official", "content_verified": True,
        "evidence_excerpt": _authority_excerpt(record, [row[1] for row in matches]),
    }
    provenance.append(authority_row)
    strategy["source_provenance"] = provenance
    authority = {
        "ticker": record.security_code,
        "company_name": record.company_name,
        "official_domain": record.official_domain,
        "verified_identity_url": url,
        "identity_evidence_sha256": record.evidence_hash,
        "transport_assurance": record.transport_assurance,
        "registry_reference": "Formal CompanyIdentityRegistry",
    }
    strategy["company_identity"] = {
        "verified_ticker": record.security_code,
        "source_url": url,
        "evidence": json.dumps(authority, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")),
    }
    payload["sources_checked_count"] = len(provenance)
    cards_by_url: dict[str, list[str]] = {}
    for card in evidence_cards:
        try:
            card_url = _canonical_document_url(str(card.get("url") or ""))
        except (TypeError, ValueError):
            continue
        evidence_id = card.get("evidence_id")
        if evidence_id:
            cards_by_url.setdefault(aliases.get(card_url, card_url), []).append(str(evidence_id))
    authority_input = {
        "url": record.verified_official_url,
        "source_type": "company_official",
        "content_verified": True,
        "evidence_excerpt": record.evidence_excerpt,
    }
    differing = []
    for key in ("url", "source_type", "content_verified", "evidence_excerpt"):
        values = [row[1].get(key) for row in matches]
        values.append(authority_input.get(key))
        if len({json.dumps(value, ensure_ascii=False, sort_keys=True) for value in values}) > 1:
            differing.append({"field": key, "values": values})
    audit = {
        "schema_version": "company_news_authority_provenance_reconciliation_v1",
        "canonical_document_key": identity_key,
        "equivalence_basis": "resolver_redirect_body_hash" if aliases else "canonical_url",
        "same_document_verified": True,
        "records": [
            *[{"origin": "final_synthesis", "source_role": "event_or_context",
               "authority_tier": "MODEL_PROVENANCE", "input_index": index,
               "evidence_ids": sorted(set(cards_by_url.get(identity_key, []))), **row}
              for index, row, _normalized in matches],
            {"origin": "python_registry_injection", "source_role": "company_identity",
             "authority_tier": "FORMAL_COMPANY_IDENTITY_REGISTRY",
             "evidence_ids": sorted(set(cards_by_url.get(identity_key, []))),
             "url": record.verified_official_url, "canonical_url": url,
             "source_type": "company_official", "content_verified": True,
             "evidence_excerpt": record.evidence_excerpt,
             "body_sha256": record.evidence_hash,
             "transport_assurance": record.transport_assurance,
             "official_domain": record.official_domain}],
        "differing_fields": differing,
        "reconciled_source": authority_row,
        "identity_authority_preserved": True,
        "event_context_evidence_preserved_in_authorities": True,
        "model_identity_accepted": False,
    }
    return repaired, audit


def apply_registry_identity(response: dict[str, Any], registry: CompanyIdentityRegistry,
                            assignment: Mapping[str, Any]) -> dict[str, Any]:
    """Compatibility wrapper for frozen identity with authority-aware provenance."""
    repaired, _audit = apply_registry_identity_with_audit(response, registry, assignment)
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
