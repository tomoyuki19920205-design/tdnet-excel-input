"""Offline-only tests for the official source resolver and identity registry."""
from __future__ import annotations

import json

import pytest
import requests

from tools import company_news_strategy_v4 as v4
from tools.company_news_official_source import (
    CompanyIdentityRecord,
    CompanyIdentityRegistry,
    DirectHttpTransport,
    FetchPolicy,
    IdentityFailureCache,
    OfficialSourceError,
    OfficialSourceResolver,
    OfficialWindowCoverage,
    RepositoryExistingTransport,
    apply_registry_identity,
    build_identity_evidence_card,
    company_identity_registry_schema,
    identity_invalidation_reasons,
    identity_preflight,
    model_context_from_resolution,
    promote_verified_identity,
    project_offline_state,
    validate_fetch_url,
    validate_identity_record,
    validate_window_coverage,
)


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
        self.trust_env = True
        self.cookies = type("Cookies", (), {"clear": lambda self: None})()

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
