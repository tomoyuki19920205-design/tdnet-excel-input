"""Offline-only tests for the official source resolver and identity registry."""
from __future__ import annotations

import json
import ssl
import base64
import hashlib
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
import requests

from tools import company_news_strategy_v4 as v4
from tools import company_news_official_source as official_source
from tools.company_news_official_source import (
    ArtifactStorageUnavailable,
    CompanyIdentityRecord,
    CompanyIdentityRegistry,
    DelegatedStaticResourcePolicy,
    DirectHttpTransport,
    DurableArtifactStore,
    FetchPolicy,
    HttpFetchJournal,
    IdentityFailureCache,
    IrNightlyKnownSuccessfulTransport,
    JournaledTransport,
    OfficialSourceError,
    OfficialSourceResolver,
    OfficialWindowCoverage,
    RepositoryExistingTransport,
    TLS_BACKEND_CURRENT_CA,
    TLS_BACKEND_WINDOWS_SYSTEM_TRUST,
    SCHANNEL_IMPLEMENTATION_FINGERPRINT,
    SCHANNEL_TRANSPORT_ID,
    TransportPayload,
    TlsTrustInitializationError,
    WindowsSchannelTransport,
    apply_registry_identity,
    atomic_write_json,
    build_identity_evidence_card,
    company_identity_registry_schema,
    create_verified_tls_context,
    derive_exact_https_identity_urls,
    discover_official_dynamic_sources,
    identity_invalidation_reasons,
    identity_preflight,
    model_context_from_resolution,
    parse_http_rejection_artifact,
    promote_verified_identity,
    project_offline_state,
    rejection_bodies_are_identical,
    tls_backend_change_allows_attempt,
    validate_fetch_url,
    validate_identity_record,
    validate_window_coverage,
    validate_verified_tls_context,
    windows_system_trust_context,
    writable_probe,
)


def test_identity_matches_canonical_japanese_name_in_structured_logo_marker():
    body = """<html><head><title>KOBELCO 神戸製鋼</title></head>
    <body><header><img class='site-logo' alt='株式会社神戸製鋼所'></header>
    <main>株主・投資家情報</main></body></html>""".encode()
    identity = CompanyIdentityRecord(
        "5406", "神戸製鋼所", "神戸製鋼所", "kobelco.co.jp", None, None,
        None, None, None, "UNVERIFIED", None)
    result = OfficialSourceResolver(RepositoryExistingTransport({
        "https://www.kobelco.co.jp/ir/": (body, "text/html", 200)})).resolve(
            identity, "https://www.kobelco.co.jp/ir/")
    promoted = promote_verified_identity(identity, result, verification_source="saved official body")
    assert promoted.status == "VERIFIED"


def test_identity_matches_existing_authoritative_english_name_generically():
    body = b"<html><body><footer>Copyright KOBE STEEL, LTD.</footer></body></html>"
    identity = CompanyIdentityRecord(
        "5406", "神戸製鋼所", "神戸製鋼所", "kobelco.co.jp", None, None,
        None, None, None, "UNVERIFIED", None)
    result = OfficialSourceResolver(RepositoryExistingTransport({
        "https://www.kobelco.co.jp/ir/": (body, "text/html", 200)})).resolve(
            identity, "https://www.kobelco.co.jp/ir/")
    card = build_identity_evidence_card(
        result, identity, authoritative_names=("Kobe Steel, Ltd.",))
    assert card["identity_support"] == "Kobe Steel, Ltd."


def test_unknown_brand_only_does_not_verify_without_authoritative_name():
    body = b"<html><head><title>KOBELCO Investor Relations</title></head></html>"
    identity = CompanyIdentityRecord(
        "5406", "神戸製鋼所", "神戸製鋼所", "kobelco.co.jp", None, None,
        None, None, None, "UNVERIFIED", None)
    result = OfficialSourceResolver(RepositoryExistingTransport({
        "https://www.kobelco.co.jp/ir/": (body, "text/html", 200)})).resolve(
            identity, "https://www.kobelco.co.jp/ir/")
    with pytest.raises(OfficialSourceError, match="does not identify"):
        build_identity_evidence_card(result, identity)


def test_saved_literal_https_identity_url_is_derived_without_scheme_guessing():
    body = b"<link rel='canonical' href='https://www.rise-i.co.jp/ir/faq.html'>"
    assert derive_exact_https_identity_urls(body, "rise-i.co.jp") == (
        "https://www.rise-i.co.jp/ir/faq.html",)
    assert derive_exact_https_identity_urls(
        b"<a href='http://www.rise-i.co.jp/ir/faq.html'>RISE</a>", "rise-i.co.jp") == ()
    assert derive_exact_https_identity_urls(
        b"<a href='https://external.example/'>external</a>", "rise-i.co.jp") == ()


def test_authoritative_legacy_http_identity_persists_transport_warning():
    url = "http://official.example/company"
    identity = verified_record(
        status="UNVERIFIED", verified_official_url=None, verification_source=None,
        verification_method=None, verified_at=None, evidence_hash=None, evidence_excerpt=None)
    result = OfficialSourceResolver(RepositoryExistingTransport({
        url: (b"<title>Example Works official</title>", "text/html", 200)})).resolve(identity, url)
    record = promote_verified_identity(
        identity, result, verification_source="IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH",
        url_authority="IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH", allow_legacy_http=True)
    assert record.status == "VERIFIED" and record.transport_assurance == "LEGACY_HTTP"
    assert record.transport_security_warning is True and record.tls_verified is False
    assert record.evidence_http_status == 200
    assert CompanyIdentityRegistry([record]).usable("X001", "Example Works") == record


def test_legacy_http_rejects_guessed_authority_company_mismatch_redirect_and_missing_hash():
    url = "http://official.example/company"
    identity = verified_record(
        status="UNVERIFIED", verified_official_url=None, verification_source=None,
        verification_method=None, verified_at=None, evidence_hash=None, evidence_excerpt=None)
    good = OfficialSourceResolver(RepositoryExistingTransport({
        url: (b"<title>Example Works official</title>", "text/html", 200)})).resolve(identity, url)
    with pytest.raises(OfficialSourceError, match="contract is not satisfied"):
        promote_verified_identity(identity, good, verification_source="guessed",
                                  url_authority="GUESSED", allow_legacy_http=True)
    mismatch = OfficialSourceResolver(RepositoryExistingTransport({
        url: (b"<title>Different Company</title>", "text/html", 200)})).resolve(identity, url)
    with pytest.raises(OfficialSourceError, match="does not identify"):
        promote_verified_identity(
            identity, mismatch, verification_source="IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH",
            url_authority="IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH", allow_legacy_http=True)
    redirected = official_source.OfficialSourceResolution(
        url, "http://evil.example/company", 200, good.access_status, good.mime_type,
        good.normalized_content, good.body_hash, good.fetched_at, good.fetch_method, None)
    with pytest.raises(OfficialSourceError, match="contract is not satisfied"):
        promote_verified_identity(
            identity, redirected, verification_source="IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH",
            url_authority="IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH", allow_legacy_http=True)
    missing_hash = official_source.OfficialSourceResolution(
        url, url, 200, good.access_status, good.mime_type,
        good.normalized_content, None, good.fetched_at, good.fetch_method, None)
    with pytest.raises(OfficialSourceError, match="verified body required"):
        promote_verified_identity(
            identity, missing_hash, verification_source="IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH",
            url_authority="IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH", allow_legacy_http=True)


PUBLIC_DNS = lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))]


def verified_record(**changes):
    values = dict(security_code="X001", company_name="Example Works",
                  normalized_name="exampleworks", official_domain="official.example",
                  verified_official_url="https://official.example/company", verification_source="official body",
                  verification_method="ACTUAL_OFFICIAL_BODY", verified_at="2026-09-06T00:00:00+00:00",
                  evidence_hash="a" * 64, status="VERIFIED", evidence_excerpt="Example Works official profile")
    values.update(changes)
    return CompanyIdentityRecord(**values)


def unverified_record():
    return CompanyIdentityRecord("6444", "サンデン株式会社", "サンデン", "sanden.co.jp",
                                 None, None, None, None, None, "UNVERIFIED", None)


class FakeResponse:
    def __init__(self, status=200, *, headers=None, chunks=()):
        self.status_code, self.headers, self._chunks = status, headers or {}, list(chunks)

    def iter_content(self, _size):
        return iter(self._chunks)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.mounts = []
        self.trust_env = True
        self.verify = True
        self.cookies = type("Cookies", (), {"clear": lambda self: None})()

    def mount(self, prefix, adapter):
        self.mounts.append((prefix, adapter))

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        pass


def v4_fixture():
    assignment = {"assignment_id": "a1", "ticker": "X001", "company_name": "Example Works",
                  "search_from": "2026-08-25", "search_to": "2026-08-31", "strategy_version": v4.VERSION}
    source = {"url": "https://media.example/news", "source_type": "external_media",
              "content_verified": True, "evidence_excerpt": "checked source"}
    telemetry = {
        "schema_version": v4.SCHEMA, "strategy_version": v4.VERSION, "assignment_id": "a1", "ticker": "X001",
        "company_identity": {"verified_ticker": "X001", "source_url": "https://official.example/company",
                             "evidence": "stable registry identity"},
        "economic_exposures": [{"driver": "customer demand", "metadata_evidence": "profile",
                                "transmission_channel": "orders"},
                               {"driver": "input prices", "metadata_evidence": "profile",
                                "transmission_channel": "margin"}],
        "queries": [{"channel": "official", "query": "official window", "executed": True},
                    {"channel": "counterparty", "query": "customer", "executed": True},
                    {"channel": "external_media", "query": "inputs", "executed": True}],
        "channel_reviews": [{"channel": "official", "status": "CHECKED", "evidence": "window", "exposure_index": None},
                            {"channel": "counterparty", "status": "CHECKED", "evidence": "customer", "exposure_index": 0},
                            {"channel": "external_media", "status": "CHECKED", "evidence": "inputs", "exposure_index": 1}],
        "regulatory_themes": [], "query_budget_exceeded": False, "documents": [], "coverage_gaps": [],
        "coverage_status": "COMPLETE", "source_provenance": [source], "candidate_provenance": [],
        "candidate_count": 0, "final_event_count": 0, "no_news_gate_pass": True,
        "window_review_complete": True, "stopped_after_first_event": False,
        "stage_elapsed_seconds": {"company_web": 1, "external_complement": 2},
        "total_research_elapsed_seconds": 3,
    }
    return assignment, telemetry, {"sources_checked_count": 1, "items": []}


def test_identity_registry_schema_contains_required_stable_fields():
    record = company_identity_registry_schema()["properties"]["records"]["items"]
    assert set(record["required"]) >= {"security_code", "company_name", "normalized_name", "official_domain",
                                       "verified_official_url", "verification_source", "verification_method",
                                       "verified_at", "evidence_hash", "status"}


def test_verified_identity_reuse_skips_per_run_identity_web_and_llm():
    state = identity_preflight(CompanyIdentityRegistry([verified_record()]),
                               security_code="X001", company_name="Example Works")
    assert state["status"] == "REUSE_VERIFIED"
    assert state["identity_web_fetch_required"] is state["identity_llm_worker_required"] is False


def test_unverified_identity_fails_closed():
    state = identity_preflight(CompanyIdentityRegistry([unverified_record()]),
                               security_code="6444", company_name="サンデン株式会社")
    assert state["status"] == "FETCH_REQUIRED" and state["identity_web_fetch_required"] is True


def test_search_snippet_cannot_create_verified_identity():
    with pytest.raises(OfficialSourceError, match="verification method"):
        validate_identity_record(verified_record(verification_method="SEARCH_SNIPPET"))


def test_actual_saved_official_body_can_become_bounded_verified_evidence():
    body = b"<html><head><title>Company</title></head><body>Example Works official profile</body></html>"
    resolver = OfficialSourceResolver(RepositoryExistingTransport({
        "https://official.example/company": (body, "text/html", 200)}), clock=lambda: "2026-09-06T00:00:00+00:00")
    result = resolver.resolve(verified_record(status="UNVERIFIED", verification_method=None,
                                              verified_at=None, evidence_hash=None),
                              "https://official.example/company")
    card = build_identity_evidence_card(result, verified_record())
    assert result.access_status == "VERIFIED" and card["source_type"] == "company_official"
    promoted = promote_verified_identity(verified_record(status="UNVERIFIED", verification_method=None,
                                                         verified_at=None, evidence_hash=None),
                                         result, verification_source="saved official body")
    assert promoted.status == "VERIFIED" and promoted.verification_method == "REPOSITORY_VERIFIED_ARTIFACT"


def test_verified_identity_plus_blocked_window_cannot_be_complete_or_no_news():
    assignment, telemetry, payload = v4_fixture()
    blocked = OfficialWindowCoverage("X001", "2026-08-25", "2026-08-31", "BLOCKED", (), ("HTTP_502",), ())
    with pytest.raises(Exception, match="official window"):
        v4.validate(telemetry, assignment, payload,
                    identity_registry=CompanyIdentityRegistry([verified_record()]),
                    official_window_coverage=blocked)


def test_verified_registry_identity_passes_without_per_run_identity_source_fetch():
    assignment, telemetry, payload = v4_fixture()
    complete = OfficialWindowCoverage("X001", "2026-08-25", "2026-08-31", "COMPLETE",
                                      ("news", "ir", "announcements"), (), ("a" * 64,))
    assert v4.validate(telemetry, assignment, payload,
                       identity_registry=CompanyIdentityRegistry([verified_record()]),
                       official_window_coverage=complete)


def test_pending_window_is_unchecked_and_not_complete():
    pending = OfficialWindowCoverage("X001", "2026-08-25", "2026-08-31", "PENDING")
    assert validate_window_coverage(pending).state == "PENDING"
    with pytest.raises(OfficialSourceError, match="must be unresolved"):
        validate_window_coverage(OfficialWindowCoverage(
            "X001", "2026-08-25", "2026-08-31", "PENDING", ("news",)))


def test_normalizer_retains_exact_navigation_links_for_runtime_discovery():
    body = b"""<html><head><title>Company 1</title></head><body>
        <header><a href='/news/'>News</a><a href='/ir/'>IR</a></header>
        <main>Company 1 official</main></body></html>"""
    normalized = official_source.normalize_official_body(
        body, "text/html", "https://example.test/", "example.test")
    assert normalized.relevant_links == (
        "https://example.test/news/", "https://example.test/ir/")


def test_http_502_is_blocked_and_classified():
    session = FakeSession([FakeResponse(502, headers={"Content-Type": "text/html"})])
    resolver = OfficialSourceResolver(DirectHttpTransport(session_factory=lambda: session,
                                                          dns_resolver=PUBLIC_DNS))
    result = resolver.resolve(verified_record(status="UNVERIFIED", verification_method=None,
                                              verified_at=None, evidence_hash=None),
                              "https://official.example/company")
    assert result.access_status == "BLOCKED" and result.failure_reason == "HTTP_5XX"


def test_timeout_is_blocked_and_classified():
    session = FakeSession([requests.Timeout()])
    result = OfficialSourceResolver(DirectHttpTransport(session_factory=lambda: session,
                                                        dns_resolver=PUBLIC_DNS)).resolve(
        verified_record(status="UNVERIFIED", verification_method=None, verified_at=None, evidence_hash=None),
        "https://official.example/company")
    assert result.access_status == "BLOCKED" and result.failure_reason == "TIMEOUT"


def test_duplicate_failed_url_is_suppressed_by_hard_retry_cap():
    session = FakeSession([FakeResponse(502)])
    cache = IdentityFailureCache(max_attempts_per_failure=1)
    resolver = OfficialSourceResolver(DirectHttpTransport(session_factory=lambda: session,
                                                          dns_resolver=PUBLIC_DNS), failure_cache=cache)
    identity = verified_record(status="UNVERIFIED", verification_method=None, verified_at=None, evidence_hash=None)
    resolver.resolve(identity, "https://official.example/company")
    second = resolver.resolve(identity, "https://official.example/company")
    assert second.failure_reason == "RETRY_SUPPRESSED" and len(session.calls) == 1


def test_alternate_official_subdomain_url_is_allowed():
    assert validate_fetch_url("https://www.official.example/company", "official.example", PUBLIC_DNS)


@pytest.mark.parametrize("url,domain", [("http://127.0.0.1/x", "127.0.0.1"),
                                         ("http://localhost/x", "localhost")])
def test_private_ip_and_localhost_are_rejected(url, domain):
    local = lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 80))]
    with pytest.raises(OfficialSourceError, match="non-public"):
        validate_fetch_url(url, domain, local)


def test_cross_domain_redirect_is_blocked():
    session = FakeSession([FakeResponse(302, headers={"Location": "https://evil.example/x"})])
    payload = DirectHttpTransport(session_factory=lambda: session, dns_resolver=PUBLIC_DNS).fetch(
        "https://official.example/company", "official.example", FetchPolicy())
    assert payload.failure_reason == "REDIRECT_BLOCKED"


def test_ir_nightly_transport_uses_verified_requests_exact_url_and_no_retry():
    session = FakeSession([FakeResponse(
        200, headers={"Content-Type": "text/html"}, chunks=[b"Example Works official"])])
    transport = IrNightlyKnownSuccessfulTransport(
        session_factory=lambda: session, dns_resolver=PUBLIC_DNS)
    result = OfficialSourceResolver(transport).resolve(
        verified_record(status="UNVERIFIED", verification_method=None,
                        verified_at=None, evidence_hash=None),
        "https://official.example/company", FetchPolicy(max_redirects=0))
    assert result.access_status == "VERIFIED"
    assert transport.name == "ir-nightly-known-successful-v1"
    assert len(session.calls) == 1 and session.trust_env is False and session.verify is True
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.calls[0][1]["headers"]["User-Agent"].startswith("tdnet-company-ir-monitor/")


def test_ir_nightly_transport_redirect_chain_is_durable_and_external_is_rejected(tmp_path):
    store = DurableArtifactStore.prepare(tmp_path / "same")
    journal = HttpFetchJournal(store, run_id="r", company="X001")
    session = FakeSession([
        FakeResponse(301, headers={"Location": "/company", "Content-Type": "text/html"}),
        FakeResponse(200, headers={"Content-Type": "text/html"},
                     chunks=[b"Example Works official"]),
    ])
    result = OfficialSourceResolver(IrNightlyKnownSuccessfulTransport(
        session_factory=lambda: session, dns_resolver=PUBLIC_DNS, journal=journal)).resolve(
        verified_record(status="UNVERIFIED", verification_method=None,
                        verified_at=None, evidence_hash=None),
        "https://official.example/", FetchPolicy(max_redirects=1))
    records, corrupt = journal._disk_records()
    assert result.access_status == "VERIFIED" and corrupt == [] and len(records) == 2
    assert records[1]["parent_request_id"] == records[0]["request_id"]
    assert records[1]["redirect_source_url"] == "https://official.example/"
    assert records[1]["redirect_location"] == "/company"
    assert list((store.spool_root / "raw-http").glob("*.body"))

    external = FakeSession([FakeResponse(302, headers={"Location": "https://evil.example/"})])
    payload = IrNightlyKnownSuccessfulTransport(
        session_factory=lambda: external, dns_resolver=PUBLIC_DNS).fetch(
        "https://official.example/", "official.example", FetchPolicy(max_redirects=1))
    assert payload.failure_reason == "REDIRECT_BLOCKED" and len(external.calls) == 1


def test_ir_nightly_transport_allows_saved_http_only_as_one_logical_fetch():
    session = FakeSession([FakeResponse(
        200, headers={"Content-Type": "text/html"}, chunks=[b"Example Works official"])])
    result = OfficialSourceResolver(IrNightlyKnownSuccessfulTransport(
        session_factory=lambda: session, dns_resolver=PUBLIC_DNS)).resolve(
        verified_record(status="UNVERIFIED", verification_method=None,
                        verified_at=None, evidence_hash=None,
                        verified_official_url="http://official.example/company"),
        "http://official.example/company", FetchPolicy(max_redirects=1))
    assert result.access_status == "VERIFIED" and result.final_url.startswith("http://")
    with pytest.raises(OfficialSourceError, match="contract is not satisfied"):
        promote_verified_identity(
            verified_record(status="UNVERIFIED", verification_method=None,
                            verified_at=None, evidence_hash=None),
            result, verification_source="HTTP-only fixture")


def test_ir_nightly_transport_http_to_https_redirect_can_verify():
    session = FakeSession([
        FakeResponse(301, headers={"Location": "https://official.example/company"}),
        FakeResponse(200, headers={"Content-Type": "text/html"},
                     chunks=[b"Example Works official"]),
    ])
    provisional = verified_record(
        status="UNVERIFIED", verification_method=None, verified_at=None,
        evidence_hash=None, verified_official_url="http://official.example/company")
    result = OfficialSourceResolver(IrNightlyKnownSuccessfulTransport(
        session_factory=lambda: session, dns_resolver=PUBLIC_DNS)).resolve(
        provisional, "http://official.example/company", FetchPolicy(max_redirects=1))
    promoted = promote_verified_identity(provisional, result, verification_source="fixture")
    assert promoted.status == "VERIFIED" and promoted.verified_official_url.startswith("https://")


def test_response_size_hard_cap_is_enforced():
    session = FakeSession([FakeResponse(200, headers={"Content-Type": "text/html"}, chunks=[b"x" * 11])])
    payload = DirectHttpTransport(session_factory=lambda: session, dns_resolver=PUBLIC_DNS).fetch(
        "https://official.example/company", "official.example", FetchPolicy(max_response_bytes=10))
    assert payload.failure_reason == "SIZE_LIMIT" and payload.body is None


def test_repository_transport_rejects_unapproved_mime():
    payload = RepositoryExistingTransport({
        "https://official.example/company": (b"binary", "application/octet-stream", 200)
    }).fetch("https://official.example/company", "official.example", FetchPolicy())
    assert payload.failure_reason == "MIME_REJECTED" and payload.body is None


def test_raw_body_is_not_exposed_to_model_context_and_excerpt_is_bounded():
    secret = "RAW_SECRET_AFTER_BOUND"
    body = ("<html><body>Example Works " + "x" * 3000 + secret + "</body></html>").encode()
    result = OfficialSourceResolver(RepositoryExistingTransport({
        "https://official.example/company": (body, "text/html", 200)})).resolve(
        verified_record(status="UNVERIFIED", verification_method=None, verified_at=None, evidence_hash=None),
        "https://official.example/company")
    context = model_context_from_resolution(result, verified_record())
    serialized = json.dumps(context)
    assert secret not in serialized
    assert len(context["company_identity_evidence"]["body_excerpt"]) <= 2000


def test_identity_invalidation_covers_master_domain_merger_and_explicit_changes():
    reasons = identity_invalidation_reasons(verified_record(), security_code="X002",
                                            company_name="Renamed Works", official_domain="new.example",
                                            merger_or_reorganization=True, explicit_reverification=True)
    assert set(reasons) == {"SECURITY_CODE_CHANGED", "COMPANY_NAME_CHANGED", "OFFICIAL_DOMAIN_CHANGED",
                            "MERGER_OR_REORGANIZATION", "EXPLICIT_REVERIFICATION"}


def test_registry_provenance_projection_is_deterministic_and_needs_no_fetch():
    response = {"research_result": {"sources_checked_count": 0},
                "strategy_telemetry": {"source_provenance": [], "company_identity": {}}}
    projected = apply_registry_identity(response, CompanyIdentityRegistry([verified_record()]),
                                        {"ticker": "X001", "company_name": "Example Works"})
    assert projected["research_result"]["sources_checked_count"] == 1
    assert projected["strategy_telemetry"]["source_provenance"][0]["content_verified"] is True


def test_complete_window_requires_all_official_families():
    with pytest.raises(OfficialSourceError, match="required checks"):
        validate_window_coverage(OfficialWindowCoverage("X001", "2026-01-01", "2026-01-02",
                                                        "COMPLETE", ("news",), (), ()))


def test_6444_offline_projection_remains_unverified_blocked_and_not_no_news():
    value = project_offline_state(
        security_code="6444", company_name="サンデン株式会社", official_domain="sanden.co.jp",
        failures=("IR index HTTP_502", "corporate profile TIMEOUT"),
        verification_source="saved_phase19d_phase19e")
    assert value["company_identity"]["status"] == "UNVERIFIED"
    assert value["official_window"]["state"] == "BLOCKED"
    assert value["search_snippets"] == "DISCOVERY_ONLY"
    assert value["no_news_allowed"] is False
    assert value["web_calls"] == value["llm_calls"] == 0


class CountingTransport:
    name = "direct_http"

    def __init__(self, body=b"<html><body>Example Works</body></html>"):
        self.body = body
        self.calls = 0

    def fetch(self, requested_url, _official_domain, _policy):
        self.calls += 1
        return TransportPayload(requested_url, requested_url, 200, "text/html", self.body,
                                self.name, safe_headers={"content-type": "text/html"})


def durable_store(tmp_path, *, primary=None, spool=None, probe=writable_probe):
    primary = primary or tmp_path / "primary"
    spool = spool or tmp_path / "spool"
    return DurableArtifactStore.prepare(primary, fallback_factory=lambda: spool, probe=probe)


def journal_for(tmp_path, **kwargs):
    return HttpFetchJournal(durable_store(tmp_path, **kwargs), run_id="run-1", company="6444")


def unverified_example():
    return verified_record(status="UNVERIFIED", verification_method=None, verified_at=None,
                           evidence_hash=None, evidence_excerpt=None)


def test_writable_artifact_preflight_allows_transport_only_after_probe(tmp_path):
    journal = journal_for(tmp_path)
    transport = CountingTransport()
    result = OfficialSourceResolver(JournaledTransport(transport, journal)).resolve(
        unverified_example(), "https://official.example/company")
    assert result.access_status == "VERIFIED"
    assert transport.calls == journal.request_count() == 1


def test_primary_permission_error_uses_prevalidated_fallback(tmp_path):
    primary, spool = tmp_path / "primary", tmp_path / "spool"

    def probe(path):
        if path == primary:
            raise PermissionError("primary blocked")
        writable_probe(path)

    store = durable_store(tmp_path, primary=primary, spool=spool, probe=probe)
    assert store.using_fallback and not store.primary_writable and store.spool_root == spool


def test_no_primary_or_fallback_storage_prevents_http_callable(tmp_path):
    transport = CountingTransport()
    with pytest.raises(ArtifactStorageUnavailable, match="ARTIFACT_STORAGE_UNAVAILABLE"):
        durable_store(tmp_path, probe=lambda _path: (_ for _ in ()).throw(PermissionError("blocked")))
    assert transport.calls == 0


def test_request_started_must_persist_before_http_callable():
    transport = CountingTransport()

    class BrokenJournal:
        def start(self, **_kwargs):
            raise PermissionError("journal unavailable")

    with pytest.raises(PermissionError, match="journal unavailable"):
        JournaledTransport(transport, BrokenJournal()).fetch(
            "https://official.example/company", "official.example", FetchPolicy())
    assert transport.calls == 0


def test_response_metadata_survives_normalization_failure(tmp_path, monkeypatch):
    journal = journal_for(tmp_path)
    body = b"<html><body>RAW-BODY</body></html>"
    transport = JournaledTransport(CountingTransport(body), journal)
    monkeypatch.setattr(official_source, "normalize_official_body",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("normalizer failed")))
    with pytest.raises(RuntimeError, match="normalizer failed"):
        OfficialSourceResolver(transport).resolve(
            unverified_example(), "https://official.example/company")
    record = journal._disk_records()[0][0]
    received = next(row for row in record["history"] if row["phase"] == "RESPONSE_RECEIVED")
    assert received["status_code"] == 200
    assert received["response_bytes"] == len(body)
    assert received["body_sha256"]
    assert record["history"][-1]["phase"] == "FAILED"


def test_post_response_primary_failure_keeps_fallback_spool(tmp_path, monkeypatch):
    primary, spool = tmp_path / "primary", tmp_path / "spool"
    store = durable_store(tmp_path, primary=primary, spool=spool)
    real_atomic = official_source.atomic_write_json

    def fail_primary(path, value):
        if primary in path.parents:
            raise PermissionError("primary failed after response")
        real_atomic(path, value)

    monkeypatch.setattr(official_source, "atomic_write_json", fail_primary)
    journal = HttpFetchJournal(store, run_id="run-1", company="6444")
    OfficialSourceResolver(JournaledTransport(CountingTransport(), journal)).resolve(
        unverified_example(), "https://official.example/company")
    records, corrupt = journal._disk_records()
    assert records[0]["history"][-1]["phase"] == "NORMALIZED"
    assert not corrupt and store.mirror_errors


def test_atomic_json_write_is_valid_and_leaves_no_partial_file(tmp_path):
    target = tmp_path / "telemetry.json"
    atomic_write_json(target, {"ok": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    assert list(tmp_path.glob("*.tmp")) == []


def test_corrupt_journal_is_not_counted_as_formal_result(tmp_path):
    journal = journal_for(tmp_path)
    path = journal.store.spool_root / "http-journal" / "corrupt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{partial", encoding="utf-8")
    assert journal.request_count() == 0
    assert journal.corrupt_records() == ["corrupt.json"]


def test_outcome_lost_suppresses_same_url_same_transport_retry():
    cache = IdentityFailureCache()
    cache.record_outcome_lost(security_code="6444", domain="sanden.co.jp",
                              url="https://www.sanden.co.jp/company/about/",
                              transport="direct_http", attempted_at="2026-09-06T00:00:00+00:00")
    assert not cache.retry_allowed(security_code="6444", domain="sanden.co.jp",
                                   url="https://www.sanden.co.jp/company/about/",
                                   transport="direct_http")
    assert cache.records()[0].failure_type == "OUTCOME_LOST"


def test_outcome_lost_allows_different_url_under_policy():
    cache = IdentityFailureCache()
    cache.record_outcome_lost(security_code="6444", domain="sanden.co.jp",
                              url="https://www.sanden.co.jp/company/about/",
                              transport="direct_http", attempted_at="2026-09-06T00:00:00+00:00")
    assert cache.retry_allowed(security_code="6444", domain="sanden.co.jp",
                               url="https://www.sanden.co.jp/ir/index.html",
                               transport="direct_http")


def test_outcome_lost_keeps_transport_specific_retry_semantics():
    cache = IdentityFailureCache()
    cache.record_outcome_lost(security_code="6444", domain="sanden.co.jp",
                              url="https://www.sanden.co.jp/company/about/",
                              transport="direct_http", attempted_at="2026-09-06T00:00:00+00:00")
    assert cache.retry_allowed(security_code="6444", domain="sanden.co.jp",
                               url="https://www.sanden.co.jp/company/about/",
                               transport="repository_existing")


def test_request_count_is_recovered_from_durable_journal(tmp_path):
    journal = journal_for(tmp_path)
    journal.start(request_id="request-1", transport="direct_http",
                  requested_url="https://official.example/a", attempt_number=1)
    journal.failed("request-1", category="TIMEOUT")
    journal.start(request_id="request-2", transport="direct_http",
                  requested_url="https://official.example/b", attempt_number=2)
    journal.failed("request-2", category="HTTP_5XX")
    recovered = HttpFetchJournal(journal.store, run_id="run-1", company="6444")
    assert recovered.request_count() == 2


def test_direct_http_redirect_journals_each_wire_request(tmp_path):
    journal = journal_for(tmp_path)
    session = FakeSession([
        FakeResponse(302, headers={"Location": "/company/final"}),
        FakeResponse(200, headers={"Content-Type": "text/html"},
                     chunks=[b"<html><body>Example Works</body></html>"]),
    ])
    transport = DirectHttpTransport(session_factory=lambda: session,
                                    dns_resolver=PUBLIC_DNS, journal=journal)
    result = OfficialSourceResolver(transport).resolve(
        unverified_example(), "https://official.example/company")
    assert result.access_status == "VERIFIED"
    assert len(session.calls) == journal.request_count() == 2
    records = journal._disk_records()[0]
    assert any(event.get("access_status") == "REDIRECT"
               for event in records[0]["history"])
    assert records[1]["history"][-1]["phase"] == "NORMALIZED"


def test_started_without_terminal_state_is_detected_after_crash(tmp_path):
    journal = journal_for(tmp_path)
    journal.start(request_id="request-crashed", transport="direct_http",
                  requested_url="https://official.example/a", attempt_number=1)
    assert journal.incomplete_started() == ["request-crashed"]


def test_outcome_lost_projection_is_terminal_and_counted(tmp_path):
    journal = journal_for(tmp_path)
    journal.project_outcome_lost(
        request_id="phase19g-company-about", transport="direct_http",
        requested_url="https://www.sanden.co.jp/company/about/", attempt_number=1,
        reason="TELEMETRY_PERSISTENCE_FAILURE")
    assert journal.request_count() == 1
    assert journal.incomplete_started() == []
    record = journal._disk_records()[0][0]
    assert record["history"][-1]["phase"] == "OUTCOME_LOST"


def test_http_journal_does_not_fabricate_search_registry_rows(tmp_path):
    journal = journal_for(tmp_path)
    journal.project_outcome_lost(
        request_id="lost", transport="direct_http",
        requested_url="https://www.sanden.co.jp/company/about/", attempt_number=1,
        reason="TELEMETRY_PERSISTENCE_FAILURE")
    serialized = json.dumps(journal._disk_records()[0], ensure_ascii=False)
    assert "query_text" not in serialized and "result_count" not in serialized


def test_raw_body_is_absent_from_http_journal(tmp_path):
    journal = journal_for(tmp_path)
    secret = "RAW-SECRET-NEVER-PERSIST"
    OfficialSourceResolver(JournaledTransport(CountingTransport(
        f"<html><body>Example Works {secret}</body></html>".encode()), journal)).resolve(
            unverified_example(), "https://official.example/company")
    serialized = json.dumps(journal._disk_records()[0], ensure_ascii=False)
    assert secret not in serialized
    assert "body_sha256" in serialized


def test_phase19g_6444_lost_attempt_projection_and_retry_suppression(tmp_path):
    journal = journal_for(tmp_path)
    url = "https://www.sanden.co.jp/company/about/"
    journal.project_outcome_lost(request_id="phase19g-lost", transport="direct_http",
                                 requested_url=url, attempt_number=1,
                                 reason="TELEMETRY_PERSISTENCE_FAILURE")
    cache = IdentityFailureCache()
    cache.record_outcome_lost(security_code="6444", domain="sanden.co.jp", url=url,
                              transport="direct_http", attempted_at="2026-09-06T02:50:00+00:00")
    assert journal.request_count() == 1
    assert not cache.retry_allowed(security_code="6444", domain="sanden.co.jp",
                                   url=url, transport="direct_http")


def _certificate_material(tmp_path, *, hostname="official.example", expired=False):
    cryptography = pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Phase19j Test CA")])
    ca_cert = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
               .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(days=30)).not_valid_after(now + timedelta(days=30))
               .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
               .sign(ca_key, hashes.SHA256()))
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    not_before = now - timedelta(days=10)
    not_after = now - timedelta(days=1) if expired else now + timedelta(days=10)
    leaf_cert = (x509.CertificateBuilder().subject_name(leaf_name).issuer_name(ca_name)
                 .public_key(leaf_key.public_key()).serial_number(x509.random_serial_number())
                 .not_valid_before(not_before).not_valid_after(not_after)
                 .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
                 .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                 .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                 .sign(ca_key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "server.pem", tmp_path / "server-key.pem"
    cert_path.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(leaf_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_path, key_path)
    return ca_cert.public_bytes(serialization.Encoding.PEM).decode("ascii"), server


def _memory_tls_handshake(client_context, server_context, hostname):
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(client_in, client_out, server_hostname=hostname)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    client_done = server_done = False
    for _ in range(100):
        if not client_done:
            try:
                client.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                pass
        pending = client_out.read()
        if pending:
            server_in.write(pending)
        if not server_done:
            try:
                server.do_handshake()
                server_done = True
            except ssl.SSLWantReadError:
                pass
        pending = server_out.read()
        if pending:
            client_in.write(pending)
        if client_done and server_done:
            return
    raise RuntimeError("in-memory TLS handshake did not finish")


def test_verified_tls_context_accepts_valid_trusted_certificate(tmp_path):
    ca_pem, server = _certificate_material(tmp_path)
    client = create_verified_tls_context(cadata=ca_pem)
    _memory_tls_handshake(client, server, "official.example")
    assert client.verify_mode == ssl.CERT_REQUIRED and client.check_hostname


def test_verified_tls_context_rejects_untrusted_ca(tmp_path):
    trusted_dir, untrusted_dir = tmp_path / "trusted", tmp_path / "untrusted"
    trusted_dir.mkdir()
    untrusted_dir.mkdir()
    trusted_ca, _ = _certificate_material(trusted_dir)
    _, untrusted_server = _certificate_material(untrusted_dir)
    with pytest.raises(ssl.SSLCertVerificationError):
        _memory_tls_handshake(create_verified_tls_context(cadata=trusted_ca),
                              untrusted_server, "official.example")


def test_verified_tls_context_rejects_hostname_mismatch(tmp_path):
    ca_pem, server = _certificate_material(tmp_path, hostname="official.example")
    with pytest.raises(ssl.SSLCertVerificationError):
        _memory_tls_handshake(create_verified_tls_context(cadata=ca_pem),
                              server, "wrong.example")


def test_verified_tls_context_rejects_expired_certificate(tmp_path):
    ca_pem, server = _certificate_material(tmp_path, expired=True)
    with pytest.raises(ssl.SSLCertVerificationError):
        _memory_tls_handshake(create_verified_tls_context(cadata=ca_pem),
                              server, "official.example")


def test_windows_system_trust_context_keeps_verification_enabled():
    context = windows_system_trust_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_system_trust_initialization_failure_prevents_request_callable():
    session = FakeSession([FakeResponse(200)])

    def fail_context():
        raise TlsTrustInitializationError("TLS_TRUST_INIT: fixture")

    with pytest.raises(TlsTrustInitializationError, match="TLS_TRUST_INIT"):
        DirectHttpTransport(session_factory=lambda: session,
                            tls_backend=TLS_BACKEND_WINDOWS_SYSTEM_TRUST,
                            system_context_factory=fail_context)
    assert session.calls == []


def test_insecure_context_and_verify_false_session_are_rejected_before_http():
    insecure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    with pytest.raises(TlsTrustInitializationError, match="verification are required"):
        validate_verified_tls_context(insecure)
    session = FakeSession([FakeResponse(200)])
    session.verify = False
    transport = DirectHttpTransport(session_factory=lambda: session, dns_resolver=PUBLIC_DNS)
    with pytest.raises(TlsTrustInitializationError, match="verify=False"):
        transport.fetch("https://official.example/", "official.example", FetchPolicy())
    assert session.calls == []


@pytest.mark.parametrize("message,expected", [
    ("certificate verify failed: unable to get local issuer certificate", "TLS_CA_CHAIN"),
    ("hostname mismatch", "TLS_HOSTNAME"),
    ("certificate has expired", "TLS_EXPIRED"),
    ("certificate is not yet valid", "TLS_NOT_YET_VALID"),
    ("unsupported protocol version", "TLS_PROTOCOL"),
    ("handshake failure", "TLS_HANDSHAKE"),
    ("opaque TLS error", "TLS_UNKNOWN"),
])
def test_tls_failure_taxonomy(message, expected):
    assert official_source.classify_tls_failure(requests.exceptions.SSLError(message)) == expected


def test_backend_version_provenance_requires_real_supported_change():
    context = create_verified_tls_context()
    old = DirectHttpTransport(tls_backend=TLS_BACKEND_CURRENT_CA)
    new = DirectHttpTransport(tls_backend=TLS_BACKEND_WINDOWS_SYSTEM_TRUST,
                              system_context_factory=lambda: context)
    another_new = DirectHttpTransport(tls_backend=TLS_BACKEND_WINDOWS_SYSTEM_TRUST,
                                      system_context_factory=lambda: context)
    assert old.name == "direct_http"
    assert new.name == "direct_http/windows-system-trust-v1"
    assert tls_backend_change_allows_attempt(old, new)
    assert not tls_backend_change_allows_attempt(new, another_new)
    with pytest.raises(TlsTrustInitializationError, match="unsupported TLS backend"):
        DirectHttpTransport(tls_backend="renamed-only")


def test_backend_change_preserves_old_failure_and_allows_only_new_identity():
    cache = IdentityFailureCache()
    url = "https://www.sanden.co.jp/"
    cache.record(security_code="6444", domain="sanden.co.jp", url=url,
                 transport="direct_http", failure_type="TLS_CA_CHAIN",
                 attempted_at="2026-09-06T03:22:53+00:00")
    context = create_verified_tls_context()
    new = DirectHttpTransport(tls_backend=TLS_BACKEND_WINDOWS_SYSTEM_TRUST,
                              system_context_factory=lambda: context)
    assert not cache.retry_allowed(security_code="6444", domain="sanden.co.jp",
                                   url=url, transport="direct_http")
    assert cache.retry_allowed(security_code="6444", domain="sanden.co.jp",
                               url=url, transport=new.name)
    assert cache.records()[0].failure_type == "TLS_CA_CHAIN"


def test_system_trust_transport_mounts_verified_adapter_and_keeps_journal(tmp_path):
    context = create_verified_tls_context()
    session = FakeSession([FakeResponse(200, headers={"Content-Type": "text/html"},
                                                chunks=[b"<html><body>Example Works</body></html>"])])
    journal = journal_for(tmp_path)
    transport = DirectHttpTransport(
        session_factory=lambda: session, dns_resolver=PUBLIC_DNS, journal=journal,
        tls_backend=TLS_BACKEND_WINDOWS_SYSTEM_TRUST,
        system_context_factory=lambda: context)
    result = OfficialSourceResolver(transport).resolve(
        unverified_example(), "https://official.example/")
    assert result.access_status == "VERIFIED"
    assert session.mounts[0][0] == "https://"
    assert journal.request_count() == 1
    assert journal._disk_records()[0][0]["history"][-1]["phase"] == "NORMALIZED"


def schannel_response(*, body=b"<html><body>Example Works official profile</body></html>",
                      status=200, mime="text/html", result="RESPONSE",
                      failure=None, safe_headers=None):
    return {
        "schema_version": "company_news_schannel_helper_v1",
        "result": result,
        "transport_id": SCHANNEL_TRANSPORT_ID,
        "implementation_fingerprint": SCHANNEL_IMPLEMENTATION_FINGERPRINT,
        "requested_url": "https://official.example/",
        "final_url": "https://official.example/",
        "status_code": status,
        "mime_type": mime,
        "response_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "body_base64": base64.b64encode(body).decode(),
        "safe_headers": safe_headers or {},
        "certificate_summary": {
            "ssl_policy_errors": ["None"], "hostname_validation": "PASS",
            "leaf": {"subject": "CN=official.example", "issuer": "CN=Fixture CA"},
            "chain_elements": [], "chain_status": [],
        },
        "elapsed_seconds": 0.01,
        "failure_class": failure,
    }


def schannel_response_for(request, **kwargs):
    response = schannel_response(**kwargs)
    response["requested_url"] = request["url"]
    response["final_url"] = request["url"]
    return response


def test_schannel_transport_has_native_identity_and_fingerprint():
    transport = WindowsSchannelTransport(helper_runner=lambda request: schannel_response())
    assert transport.name == "windows-schannel-v1"
    assert transport.implementation_fingerprint == SCHANNEL_IMPLEMENTATION_FINGERPRINT


def test_schannel_valid_chain_fixture_normalizes_and_journals(tmp_path):
    calls = []
    journal = journal_for(tmp_path)
    transport = WindowsSchannelTransport(
        journal=journal, dns_resolver=PUBLIC_DNS,
        helper_runner=lambda request: calls.append(request) or schannel_response())
    result = OfficialSourceResolver(transport).resolve(
        unverified_example(), "https://official.example/", FetchPolicy(max_redirects=0))
    assert result.access_status == "VERIFIED"
    assert result.fetch_method == SCHANNEL_TRANSPORT_ID
    assert len(calls) == 1 and calls[0]["allowed_host"] == "official.example"
    record = journal._disk_records()[0][0]
    assert record["history"][-1]["phase"] == "NORMALIZED"
    received = next(row for row in record["history"] if row["phase"] == "RESPONSE_RECEIVED")
    assert received["transport_metadata"]["implementation_fingerprint"] == SCHANNEL_IMPLEMENTATION_FINGERPRINT


def test_schannel_http_200_empty_body_is_classified_without_crash(tmp_path):
    journal = journal_for(tmp_path)
    payload = WindowsSchannelTransport(
        journal=journal, dns_resolver=PUBLIC_DNS,
        helper_runner=lambda request: schannel_response(body=b"", status=200)).fetch(
            "https://official.example/", "official.example", FetchPolicy(max_redirects=0))
    assert payload.status_code == 200
    assert payload.failure_reason == "EMPTY_BODY"
    assert payload.body is None
    record = journal._disk_records()[0][0]
    received = next(row for row in record["history"] if row["phase"] == "RESPONSE_RECEIVED")
    assert received["status_code"] == 200
    assert received["exception_category"] == "EMPTY_BODY"


@pytest.mark.parametrize("status", [204, 205])
def test_schannel_valid_no_body_status_is_preserved(status):
    response = schannel_response(body=b"", status=status)
    payload = WindowsSchannelTransport(
        dns_resolver=PUBLIC_DNS, helper_runner=lambda request: response).fetch(
            "https://official.example/", "official.example", FetchPolicy(max_redirects=0))
    assert payload.status_code == status
    assert payload.failure_reason is None
    assert payload.body == b""


def test_schannel_null_body_representation_is_deterministic():
    response = schannel_response(body=b"", status=204)
    response["body_base64"] = None
    payload = WindowsSchannelTransport(
        dns_resolver=PUBLIC_DNS, helper_runner=lambda request: response).fetch(
            "https://official.example/", "official.example", FetchPolicy(max_redirects=0))
    assert payload.status_code == 204
    assert payload.failure_reason is None
    assert payload.body == b""


def test_schannel_body_encoding_failure_keeps_http_metadata(tmp_path):
    journal = journal_for(tmp_path)
    response = schannel_response(body=b"expected", status=200,
                                 safe_headers={"content-length": "8"})
    response["body_base64"] = "%%%invalid-base64%%%"
    payload = WindowsSchannelTransport(
        journal=journal, dns_resolver=PUBLIC_DNS,
        helper_runner=lambda request: response).fetch(
            "https://official.example/", "official.example", FetchPolicy(max_redirects=0))
    assert payload.status_code == 200
    assert payload.failure_reason == "BODY_ENCODING"
    assert payload.safe_headers == {"content-length": "8"}
    record = journal._disk_records()[0][0]
    received = next(row for row in record["history"] if row["phase"] == "RESPONSE_RECEIVED")
    assert received["status_code"] == 200
    assert received["safe_headers"] == {"content-length": "8"}
    assert record["history"][-1]["exception_category"] == "BODY_ENCODING"


def test_schannel_helper_failure_after_response_keeps_status_headers_and_stage(tmp_path):
    journal = journal_for(tmp_path)
    response = schannel_response(result="FAILURE", failure="BODY_ENCODING",
                                 body=b"", status=200, mime="text/html",
                                 safe_headers={"content-type": "text/html"})
    payload = WindowsSchannelTransport(
        journal=journal, dns_resolver=PUBLIC_DNS,
        helper_runner=lambda request: response).fetch(
            "https://official.example/", "official.example", FetchPolicy(max_redirects=0))
    assert payload.status_code == 200
    assert payload.mime_type == "text/html"
    assert payload.failure_reason == "BODY_ENCODING"
    record = journal._disk_records()[0][0]
    received = next(row for row in record["history"] if row["phase"] == "RESPONSE_RECEIVED")
    assert received["status_code"] == 200
    assert received["safe_headers"] == {"content-type": "text/html"}


def test_schannel_normalization_failure_keeps_received_http_result(tmp_path, monkeypatch):
    journal = journal_for(tmp_path)
    transport = WindowsSchannelTransport(
        journal=journal, dns_resolver=PUBLIC_DNS,
        helper_runner=lambda request: schannel_response(status=200))
    monkeypatch.setattr(
        official_source, "normalize_official_body",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad normalization")))
    with pytest.raises(RuntimeError, match="bad normalization"):
        OfficialSourceResolver(transport).resolve(
            unverified_example(), "https://official.example/", FetchPolicy(max_redirects=0))
    record = journal._disk_records()[0][0]
    received = next(row for row in record["history"] if row["phase"] == "RESPONSE_RECEIVED")
    assert received["status_code"] == 200
    assert record["history"][-1]["exception_category"] == "NORMALIZATION"


def test_schannel_journal_primary_and_spool_sha_match(tmp_path):
    primary, spool = tmp_path / "primary", tmp_path / "spool"
    store = durable_store(tmp_path, primary=primary, spool=spool)
    journal = HttpFetchJournal(store, run_id="run-1", company="6444")
    OfficialSourceResolver(WindowsSchannelTransport(
        journal=journal, dns_resolver=PUBLIC_DNS,
        helper_runner=lambda request: schannel_response())).resolve(
            unverified_example(), "https://official.example/", FetchPolicy(max_redirects=0))
    relative = next((spool / "http-journal").glob("*.json")).relative_to(spool)
    assert (primary / relative).read_bytes() == (spool / relative).read_bytes()


@pytest.mark.parametrize("failure,hostname", [
    ("TLS_CA_CHAIN", "PASS"),
    ("TLS_HOSTNAME", "FAIL"),
])
def test_schannel_rejects_untrusted_chain_and_hostname_mismatch(failure, hostname):
    response = schannel_response(result="FAILURE", failure=failure, body=b"", status=0, mime=None)
    response["certificate_summary"]["hostname_validation"] = hostname
    result = OfficialSourceResolver(WindowsSchannelTransport(
        dns_resolver=PUBLIC_DNS, helper_runner=lambda request: response)).resolve(
        unverified_example(), "https://official.example/")
    assert result.access_status == "BLOCKED" and result.failure_reason == failure


def test_schannel_rejects_http_and_private_destinations_before_helper():
    calls = []
    transport = WindowsSchannelTransport(
        dns_resolver=lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
        helper_runner=lambda request: calls.append(request) or schannel_response())
    with pytest.raises(OfficialSourceError, match="requires HTTPS"):
        transport.fetch("http://official.example/", "official.example", FetchPolicy())
    with pytest.raises(OfficialSourceError, match="non-public"):
        transport.fetch("https://official.example/", "official.example", FetchPolicy())
    assert calls == []


def test_schannel_cross_domain_redirect_is_blocked_without_second_request():
    calls = []
    response = schannel_response(status=302, body=b"", mime="text/html",
                                 safe_headers={"location": "https://outside.example/"})
    payload = WindowsSchannelTransport(
        dns_resolver=PUBLIC_DNS,
        helper_runner=lambda request: calls.append(request) or response).fetch(
            "https://official.example/", "official.example", FetchPolicy(max_redirects=2))
    assert payload.failure_reason == "REDIRECT_BLOCKED" and len(calls) == 1
    assert payload.body is None


@pytest.mark.parametrize("status", [301, 302])
def test_schannel_same_domain_https_redirect_follows_one_hop(status, tmp_path):
    calls = []
    journal = journal_for(tmp_path)

    def helper(request):
        calls.append(request["url"])
        if len(calls) == 1:
            return schannel_response_for(
                request, status=status, body=b"",
                safe_headers={"location": "/next", "content-length": "0"})
        return schannel_response_for(request)

    result = OfficialSourceResolver(WindowsSchannelTransport(
        journal=journal, dns_resolver=PUBLIC_DNS, helper_runner=helper)).resolve(
            unverified_example(), "https://official.example/", FetchPolicy(max_redirects=1))
    assert result.access_status == "VERIFIED"
    assert result.final_url == "https://official.example/next"
    assert calls == ["https://official.example/", "https://official.example/next"]
    records = sorted(journal._disk_records()[0], key=lambda row: row["attempt_number"])
    assert records[0]["parent_request_id"] is None
    original = next(row for row in records[0]["history"] if row["phase"] == "RESPONSE_RECEIVED")
    assert original["status_code"] == status and original["access_status"] == "REDIRECT"
    assert records[1]["parent_request_id"] == records[0]["request_id"]
    assert records[1]["redirect_hop"] == 1
    assert records[1]["redirect_source_url"] == "https://official.example/"
    assert records[1]["redirect_location"] == "/next"
    assert records[1]["redirect_target_url"] == "https://official.example/next"
    child = next(row for row in records[1]["history"] if row["phase"] == "RESPONSE_RECEIVED")
    assert child["status_code"] == 200


@pytest.mark.parametrize("location", [
    "https://outside.example/next",
    "http://official.example/next",
])
def test_schannel_redirect_rejects_external_domain_and_http_downgrade(location):
    calls = []

    def helper(request):
        calls.append(request["url"])
        return schannel_response_for(request, status=301, body=b"",
                                     safe_headers={"location": location})

    payload = WindowsSchannelTransport(
        dns_resolver=PUBLIC_DNS, helper_runner=helper).fetch(
            "https://official.example/", "official.example", FetchPolicy(max_redirects=1))
    assert payload.failure_reason == "REDIRECT_BLOCKED"
    assert len(calls) == 1


def test_schannel_redirect_revalidates_private_destination_before_child_request(tmp_path):
    calls = []

    def resolver(host, *_args, **_kwargs):
        address = "127.0.0.1" if host == "private.official.example" else "93.184.216.34"
        return [(2, 1, 6, "", (address, 443))]

    def helper(request):
        calls.append(request["url"])
        return schannel_response_for(
            request, status=301, body=b"",
            safe_headers={"location": "https://private.official.example/next"})

    journal = journal_for(tmp_path)
    with pytest.raises(OfficialSourceError, match="non-public"):
        WindowsSchannelTransport(
            journal=journal, dns_resolver=resolver, helper_runner=helper).fetch(
                "https://official.example/", "official.example", FetchPolicy(max_redirects=1))
    assert len(calls) == 1
    assert journal.request_count() == 1


def test_schannel_redirect_loop_is_blocked_without_child_request():
    calls = []

    def helper(request):
        calls.append(request["url"])
        return schannel_response_for(request, status=301, body=b"",
                                     safe_headers={"location": "/"})

    payload = WindowsSchannelTransport(
        dns_resolver=PUBLIC_DNS, helper_runner=helper).fetch(
            "https://official.example/", "official.example", FetchPolicy(max_redirects=1))
    assert payload.failure_reason == "REDIRECT_BLOCKED"
    assert len(calls) == 1


def test_schannel_second_redirect_is_blocked_without_third_request(tmp_path):
    calls = []
    journal = journal_for(tmp_path)

    def helper(request):
        calls.append(request["url"])
        location = "/second" if len(calls) == 1 else "/third"
        return schannel_response_for(request, status=302, body=b"",
                                     safe_headers={"location": location})

    payload = WindowsSchannelTransport(
        journal=journal, dns_resolver=PUBLIC_DNS, helper_runner=helper).fetch(
            "https://official.example/", "official.example", FetchPolicy(max_redirects=1))
    assert payload.failure_reason == "REDIRECT_BLOCKED"
    assert calls == ["https://official.example/", "https://official.example/second"]
    assert journal.request_count() == 2


def test_schannel_raw_html_is_durable_and_hash_matches(tmp_path):
    primary, spool = tmp_path / "primary", tmp_path / "spool"
    store = durable_store(tmp_path, primary=primary, spool=spool)
    journal = HttpFetchJournal(store, run_id="raw", company="6444")
    body = b"<html><body>Example Works retained source</body></html>"
    result = OfficialSourceResolver(WindowsSchannelTransport(
        journal=journal, dns_resolver=PUBLIC_DNS, retain_raw_html=True,
        helper_runner=lambda request: schannel_response_for(request, body=body))).resolve(
            unverified_example(), "https://official.example/", FetchPolicy(max_redirects=0))
    assert result.access_status == "VERIFIED"
    record = journal._disk_records()[0][0]
    received = next(row for row in record["history"] if row["phase"] == "RESPONSE_RECEIVED")
    relative = received["transport_metadata"]["raw_body_artifact"]
    assert received["transport_metadata"]["raw_body_sha256"] == hashlib.sha256(body).hexdigest()
    assert (spool / relative).read_bytes() == body
    assert (primary / relative).read_bytes() == body


def test_schannel_empty_html_body_is_retained(tmp_path):
    journal = journal_for(tmp_path)
    payload = WindowsSchannelTransport(
        journal=journal, dns_resolver=PUBLIC_DNS, retain_raw_html=True,
        helper_runner=lambda request: schannel_response_for(request, body=b"", status=200)).fetch(
            "https://official.example/", "official.example", FetchPolicy(max_redirects=0))
    assert payload.failure_reason == "EMPTY_BODY"
    received = next(row for row in journal._disk_records()[0][0]["history"]
                    if row["phase"] == "RESPONSE_RECEIVED")
    relative = received["transport_metadata"]["raw_body_artifact"]
    assert (journal.store.spool_root / relative).read_bytes() == b""


def test_schannel_oversized_html_is_not_retained(tmp_path):
    journal = journal_for(tmp_path)
    payload = WindowsSchannelTransport(
        journal=journal, dns_resolver=PUBLIC_DNS, retain_raw_html=True,
        helper_runner=lambda request: schannel_response_for(request, body=b"123456")).fetch(
            "https://official.example/", "official.example", FetchPolicy(
                max_redirects=0, max_response_bytes=5))
    assert payload.failure_reason == "SIZE_LIMIT"
    assert not (journal.store.spool_root / "raw-http").exists()


def test_schannel_normalization_failure_keeps_raw_html(tmp_path, monkeypatch):
    journal = journal_for(tmp_path)
    body = b"<html><body>Example Works raw before normalization</body></html>"
    transport = WindowsSchannelTransport(
        journal=journal, dns_resolver=PUBLIC_DNS, retain_raw_html=True,
        helper_runner=lambda request: schannel_response_for(request, body=body))
    monkeypatch.setattr(
        official_source, "normalize_official_body",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("normalizer failed")))
    with pytest.raises(RuntimeError, match="normalizer failed"):
        OfficialSourceResolver(transport).resolve(
            unverified_example(), "https://official.example/", FetchPolicy(max_redirects=0))
    received = next(row for row in journal._disk_records()[0][0]["history"]
                    if row["phase"] == "RESPONSE_RECEIVED")
    relative = received["transport_metadata"]["raw_body_artifact"]
    assert (journal.store.spool_root / relative).read_bytes() == body


def test_schannel_primary_raw_write_failure_keeps_spool(tmp_path, monkeypatch):
    primary, spool = tmp_path / "primary", tmp_path / "spool"
    store = durable_store(tmp_path, primary=primary, spool=spool)
    real_write = official_source.atomic_write_bytes

    def fail_primary(path, value):
        if primary in path.parents:
            raise PermissionError("primary raw blocked")
        real_write(path, value)

    monkeypatch.setattr(official_source, "atomic_write_bytes", fail_primary)
    journal = HttpFetchJournal(store, run_id="raw", company="6444")
    body = b"<html><body>Example Works spool survives</body></html>"
    OfficialSourceResolver(WindowsSchannelTransport(
        journal=journal, dns_resolver=PUBLIC_DNS, retain_raw_html=True,
        helper_runner=lambda request: schannel_response_for(request, body=body))).resolve(
            unverified_example(), "https://official.example/", FetchPolicy(max_redirects=0))
    received = next(row for row in journal._disk_records()[0][0]["history"]
                    if row["phase"] == "RESPONSE_RECEIVED")
    relative = received["transport_metadata"]["raw_body_artifact"]
    assert (spool / relative).read_bytes() == body
    assert store.mirror_errors


def test_schannel_redirect_journal_keeps_zero_body_and_hash(tmp_path):
    journal = journal_for(tmp_path)
    response = schannel_response(status=301, body=b"", mime="text/html",
                                 safe_headers={"content-length": "0", "location": "/next"})
    payload = WindowsSchannelTransport(
        journal=journal, dns_resolver=PUBLIC_DNS,
        helper_runner=lambda request: response).fetch(
            "https://official.example/", "official.example", FetchPolicy(max_redirects=0))
    assert payload.failure_reason == "REDIRECT_BLOCKED"
    received = next(row for row in journal._disk_records()[0][0]["history"]
                    if row["phase"] == "RESPONSE_RECEIVED")
    assert received["status_code"] == 301
    assert received["response_bytes"] == 0
    assert received["body_sha256"] == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("response,expected", [
    (schannel_response(body=b"123456"), "SIZE_LIMIT"),
    (schannel_response(body=b"ok", mime="application/octet-stream"), "MIME_REJECTED"),
])
def test_schannel_enforces_size_and_mime(response, expected):
    payload = WindowsSchannelTransport(
        dns_resolver=PUBLIC_DNS, helper_runner=lambda request: response).fetch(
            "https://official.example/", "official.example", FetchPolicy(max_response_bytes=5))
    assert payload.failure_reason == expected and payload.body is None


def test_schannel_timeout_is_classified_and_durably_failed(tmp_path):
    def timeout(_request):
        raise subprocess.TimeoutExpired("pwsh", 1)

    journal = journal_for(tmp_path)
    result = OfficialSourceResolver(WindowsSchannelTransport(
        journal=journal, dns_resolver=PUBLIC_DNS, helper_runner=timeout)).resolve(
            unverified_example(), "https://official.example/")
    assert result.failure_reason == "TIMEOUT"
    assert journal._disk_records()[0][0]["history"][-1]["phase"] == "FAILED"


def test_failure_cache_alias_cannot_bypass_but_schannel_is_distinct():
    cache = IdentityFailureCache()
    values = dict(security_code="6444", domain="sanden.co.jp",
                  url="https://www.sanden.co.jp/", failure_type="TLS_CA_CHAIN",
                  attempted_at="2026-09-06T00:00:00+00:00")
    cache.record(transport="direct_http/windows-system-trust-v1", **values)
    assert not cache.retry_allowed(transport="direct_http/windows-root-bundle-v1",
                                   security_code="6444", domain="sanden.co.jp",
                                   url="https://www.sanden.co.jp/")
    assert cache.retry_allowed(transport=SCHANNEL_TRANSPORT_ID,
                               security_code="6444", domain="sanden.co.jp",
                               url="https://www.sanden.co.jp/")


def test_schannel_model_context_is_bounded_and_contains_no_raw_body(tmp_path):
    body = b"<html><body>Example Works " + (b"x" * 5000) + b" RAW_TAIL_SECRET</body></html>"
    result = OfficialSourceResolver(WindowsSchannelTransport(
        dns_resolver=PUBLIC_DNS,
        helper_runner=lambda request: schannel_response(body=body))).resolve(
            unverified_example(), "https://official.example/")
    context = model_context_from_resolution(result, unverified_example())
    encoded = json.dumps(context)
    assert "RAW_TAIL_SECRET" not in encoded
    assert len(context["company_identity_evidence"]["body_excerpt"]) <= 2000


def test_schannel_helper_source_accepts_only_no_certificate_errors():
    source = official_source.Path(official_source.__file__).with_name(
        "company_news_windows_schannel_fetch.ps1").read_text(encoding="utf-8")
    assert "return $errors -eq [Net.Security.SslPolicyErrors]::None" in source
    assert "DangerousAcceptAnyServerCertificateValidator" not in source
    assert "DisableCertificateDownloads = $true" in source
    assert "[byte[]]::new(0)" in source
    assert "'BODY_ENCODING'" in source
    assert "status_code = $statusCode" in source


def test_dynamic_source_discovery_uses_only_explicit_same_domain_references():
    body = b"""<html><body data-feed='/wp-json/company/v1/ir'>
      <script src='/assets/ir-list.js'></script>
      <script>fetch('/api/ir/news.json'); fetch('https://outside.example/api/news.json')</script>
      <div data-label='/ordinary/page'></div>
    </body></html>"""
    found = discover_official_dynamic_sources(
        body, "https://www.sanden.co.jp/ir/news/", "sanden.co.jp")
    assert [item.url for item in found] == [
        "https://www.sanden.co.jp/api/ir/news.json",
        "https://www.sanden.co.jp/assets/ir-list.js",
        "https://www.sanden.co.jp/wp-json/company/v1/ir",
    ]
    assert all(item.source_location for item in found)


def test_dynamic_source_discovery_does_not_guess_from_static_shell():
    body = "<html><title>IR News</title><body>2026 IR news list</body></html>".encode()
    assert discover_official_dynamic_sources(
        body, "https://www.sanden.co.jp/ir/news/", "sanden.co.jp") == ()


def delegated_policy(**changes):
    values = dict(
        parent_identity=verified_record(
            official_domain="sanden.co.jp",
            verified_official_url="https://www.sanden.co.jp/"),
        parent_url="https://www.sanden.co.jp/ir/news/",
        parent_body_sha256="b" * 64,
        embedded_child_urls=("https://cdn.example/resources/company/list.js",),
        expected_mime_types=("application/javascript", "text/javascript"),
        accept="application/javascript,text/javascript;q=0.9,*/*;q=0.1",
    )
    values.update(changes)
    return DelegatedStaticResourcePolicy(**values)


def test_rejection_artifact_parser_is_bounded_and_extracts_evidence():
    body = b"<html><title>Request Rejected</title><body>Access denied by CloudFront. Reference ID: ABCDEF123456 cookie required</body></html>"
    result = parse_http_rejection_artifact(body, max_text_chars=80)
    assert result["title"] == "Request Rejected"
    assert "cloudfront" in result["provider_markers"]
    assert result["requirement_markers"]["cookie"] is True
    assert len(result["body_text"]) <= 80


def test_identical_rejection_responses_use_body_hash():
    assert rejection_bodies_are_identical([b"same", b"same"])
    assert not rejection_bodies_are_identical([b"same", b"different"])


def test_delegated_policy_requires_verified_parent_and_exact_embedded_child():
    assert delegated_policy().headers_for("https://cdn.example/resources/company/list.js")["accept"].startswith("application/javascript")
    with pytest.raises(OfficialSourceError, match="verified official"):
        delegated_policy(parent_identity=verified_record(status="UNVERIFIED")).headers_for(
            "https://cdn.example/resources/company/list.js")
    with pytest.raises(OfficialSourceError, match="exact embedded"):
        delegated_policy().headers_for("https://cdn.example/resources/company/other.js")


def test_delegated_policy_rejects_arbitrary_referer():
    with pytest.raises(OfficialSourceError, match="Referer"):
        delegated_policy(referer="https://unrelated.example/").headers_for(
            "https://cdn.example/resources/company/list.js")


def test_delegated_policy_accept_must_cover_expected_resource_mime():
    with pytest.raises(OfficialSourceError, match="Accept"):
        delegated_policy(accept="text/html").headers_for(
            "https://cdn.example/resources/company/list.js")


def test_delegated_policy_is_scoped_to_one_transport_and_preserves_ua():
    calls = []
    policy = delegated_policy()
    transport = WindowsSchannelTransport(
        delegated_policy=policy, dns_resolver=PUBLIC_DNS,
        helper_runner=lambda request: calls.append(request) or schannel_response_for(
            request, body=b"const data = [];", mime="application/javascript"))
    payload = transport.fetch(
        "https://cdn.example/resources/company/list.js", "cdn.example",
        FetchPolicy(max_redirects=0, allowed_mime_types=frozenset({"application/javascript"})))
    assert payload.failure_reason is None
    assert calls[0]["request_headers"] == {"accept": policy.accept}
    assert "user-agent" not in calls[0]["request_headers"]
    ordinary_calls = []
    WindowsSchannelTransport(
        dns_resolver=PUBLIC_DNS,
        helper_runner=lambda request: ordinary_calls.append(request) or schannel_response_for(request),
    ).fetch("https://official.example/", "official.example", FetchPolicy(max_redirects=0))
    assert ordinary_calls[0]["request_headers"] == {}


def test_delegated_policy_fails_closed_for_redirect_cookie_auth_and_captcha():
    evidence = parse_http_rejection_artifact(
        b"<html><body>Authentication and cookie required. CAPTCHA challenge.</body></html>")
    assert evidence["requirement_markers"] == {
        "cookie": True, "authentication": True, "captcha": True,
        "waf_or_anti_automation": False,
    }
    with pytest.raises(OfficialSourceError, match="cannot follow redirects"):
        WindowsSchannelTransport(
            delegated_policy=delegated_policy(), dns_resolver=PUBLIC_DNS,
            helper_runner=lambda request: pytest.fail("must fail before helper"),
        ).fetch("https://cdn.example/resources/company/list.js", "cdn.example", FetchPolicy(max_redirects=1))


def test_delegated_rejection_parser_and_policy_do_not_invoke_model_or_store_raw_in_context():
    source = official_source.Path(official_source.__file__).read_text(encoding="utf-8")
    assert "parse_http_rejection_artifact" in source
    assert "model_context_from_resolution(parse_http_rejection_artifact" not in source


def test_schannel_unknown_retains_stage_and_becomes_handshake():
    response = {"failure_class": "TLS_UNKNOWN", "failure_stage": "TLS",
                "error_detail": "Exception calling AuthenticateAsClient: Authentication failed",
                "exception_chain": ["AuthenticationException: handshake failed"],
                "certificate_summary": {"hostname_validation": "UNKNOWN", "chain_status": []}}
    assert official_source.classify_schannel_failure(response) == "TLS_HANDSHAKE"


def test_schannel_chain_and_hostname_take_priority():
    base = {"failure_class": "TLS_UNKNOWN", "failure_stage": "TLS",
            "error_detail": "authentication failed"}
    assert official_source.classify_schannel_failure({
        **base, "certificate_summary": {"hostname_validation": "FAIL", "chain_status": []}
    }) == "TLS_HOSTNAME"
    assert official_source.classify_schannel_failure({
        **base, "certificate_summary": {"hostname_validation": "PASS",
                                         "chain_status": [{"status": "UntrustedRoot"}]}
    }) == "TLS_CA_CHAIN"


def test_exact_document_url_requires_literal_saved_official_href():
    body = b'<ul><li><a href="/en/news/42">Recall statement</a></li></ul>'
    assert official_source.derive_exact_document_url(
        body, "https://official.example/en/news", "Recall statement", "official.example"
    ) == "https://official.example/en/news/42"


def test_exact_document_url_rejects_guessed_or_external_url():
    body = b'<p>Recall statement</p><a href="https://evil.example/news/42">other</a>'
    assert official_source.derive_exact_document_url(
        body, "https://official.example/en/news", "Recall statement",
        "official.example") is None
    assert official_source.derive_exact_document_url(
        b"<p>Recall statement</p>", "https://official.example/en/news",
        "Recall statement", "official.example") is None
