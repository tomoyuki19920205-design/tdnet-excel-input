from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from tools import company_news_multi_runner as multi
from tools.company_news_official_source import (
    CompanyIdentityRecord,
    CompanyIdentityRegistry,
    NormalizedOfficialBody,
    OfficialSourceResolution,
    OfficialWindowCoverage,
)


def canonical_sha(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


def cohort_file(tmp_path: Path, count: int = 12, *, mutate=None) -> Path:
    rows = [{"position": i, "source_universe_position": i,
             "security_code": f"{1000 + i:04d}", "company_name": f"Company {i}",
             "bucket": "no_news", "historical_result_label": "no_news",
             "selection_reason": "saved", "source_artifact_reference": []}
            for i in range(1, count + 1)]
    if mutate:
        mutate(rows)
    hash_rows = [{key: row[key] for key in (
        "position", "source_universe_position", "security_code", "company_name", "bucket")}
        for row in rows]
    value = {"companies": rows, "cohort_sha256": canonical_sha(hash_rows),
             "source_universe_order_sha256": "a" * 64}
    path = tmp_path / "cohort.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def load(path: Path, count: int = 12, cap: int = 12):
    expected = json.loads(path.read_text())["cohort_sha256"]
    return multi.FrozenCohort.load(
        path, approved_company_count=count, max_companies=cap,
        expected_cohort_sha256=expected)


def verified_registry(ticker="1001", name="Company 1"):
    record = CompanyIdentityRecord(
        ticker, name, name.replace(" ", "").lower(), "example.test",
        "https://example.test/", "fixture", "REPOSITORY_VERIFIED_ARTIFACT",
        "2026-09-06T00:00:00+00:00", "b" * 64, "VERIFIED", f"{name} official")
    return CompanyIdentityRegistry([record])


def company_master(path: Path, rows):
    connection = sqlite3.connect(path)
    connection.execute("""CREATE TABLE company_ir_companies (
        ticker TEXT PRIMARY KEY, official_url TEXT, official_domain TEXT,
        discovery_status TEXT, source_origin TEXT, last_validated_at TEXT,
        last_http_status INTEGER)""")
    connection.executemany(
        "INSERT INTO company_ir_companies VALUES (?,?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()


def company_sources(path: Path, rows):
    connection = sqlite3.connect(path)
    connection.execute("""CREATE TABLE company_ir_sources (
        id INTEGER PRIMARY KEY, ticker TEXT, source_url TEXT, status TEXT,
        baseline_completed_at TEXT, last_checked_at TEXT, last_success_at TEXT,
        last_error TEXT, failure_count INTEGER, page_kind TEXT,
        discovered_from TEXT, provenance_url TEXT, is_external INTEGER,
        verified_from_official INTEGER)""")
    connection.executemany(
        "INSERT INTO company_ir_sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()


def complete_inputs(tmp_path: Path, cohort):
    records = []
    for company in cohort.companies:
        records.extend(verified_registry(company.security_code, company.company_name).to_dict()["records"])
    registry = CompanyIdentityRegistry([CompanyIdentityRecord(**row) for row in records])
    registry_path = tmp_path / "identity-registry.json"
    registry.save(registry_path)
    plan_path = tmp_path / "official-plan.json"
    plan_path.write_text(json.dumps(multi._official_plan(
        cohort, {row.security_code: row for row in (
            CompanyIdentityRecord(**value) for value in records)})), encoding="utf-8")
    return registry_path, plan_path


def resolution(url, *, status="VERIFIED", method="repository_existing"):
    body = NormalizedOfficialBody("Official", url, "Company 1 official body",
                                  (), (), "Company 1 official body") if status == "VERIFIED" else None
    return OfficialSourceResolution(url, url, 200 if body else 0, status, "text/html" if body else None,
                                    body, "c" * 64 if body else None,
                                    "2026-09-06T00:00:00+00:00", method,
                                    None if body else "ACCESS_DENIED")


def linked_resolution(url, links):
    body = NormalizedOfficialBody(
        "Company 1 official", url, "Company 1 official body", (), tuple(links),
        "Company 1 official body")
    return OfficialSourceResolution(
        url, url, 200, "VERIFIED", "text/html", body, "d" * 64,
        "2026-09-06T00:00:00+00:00", "repository_existing", None)


class FakeResolver:
    def __init__(self, outcome=None):
        self.outcome = outcome
        self.calls = []

    def resolve(self, identity, url, policy=None):
        self.calls.append((identity, url, policy))
        return self.outcome or resolution(url)


class UrlResolver:
    def __init__(self, outcomes):
        self.outcomes = dict(outcomes)
        self.calls = []

    def resolve(self, identity, url, policy=None):
        self.calls.append((identity, url, policy))
        return self.outcomes.get(url, resolution(url, status="BLOCKED"))


class IdentityResolver:
    def __init__(self, *, root_has_identity=True, blocked=False, identity_link=None):
        self.root_has_identity = root_has_identity
        self.blocked = blocked
        self.identity_link = identity_link
        self.calls = []

    def resolve(self, identity, url, policy=None):
        self.calls.append((identity, url, policy))
        if self.blocked:
            return OfficialSourceResolution(
                url, url, 0, "BLOCKED", None, None, None,
                "2026-09-06T00:00:00+00:00", "windows-schannel-v1", "TLS")
        is_root = len(self.calls) == 1
        text = identity.company_name if self.root_has_identity or not is_root else "generic landing page"
        links = (self.identity_link,) if is_root and self.identity_link else ()
        body = NormalizedOfficialBody(
            "Official corporate site", url, text, (), links, text)
        return OfficialSourceResolution(
            url, url, 200, "VERIFIED", "text/html", body, "d" * 64,
            "2026-09-06T00:00:00+00:00", "windows-schannel-v1", None)


class ResolverJournal:
    def __init__(self, resolver):
        self.resolver = resolver

    def request_count(self):
        return len(self.resolver.calls)


class NightlyResponse:
    def __init__(self, status=200, *, headers=None, body=b""):
        self.status_code, self.headers, self.body = status, headers or {}, body

    def iter_content(self, _size):
        return iter([self.body]) if self.body else iter(())


class NightlySession:
    def __init__(self, responses):
        self.responses, self.calls, self.trust_env, self.verify = list(responses), [], True, True
        self.cookies = type("Cookies", (), {"clear": lambda self: None})()
        self.headers = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def close(self):
        pass


class ReadyOfficial:
    def __init__(self, safe_at=None):
        self.safe_at = safe_at
        self.calls = []

    def prepare(self, assignment, company_dir):
        self.calls.append(dict(assignment))
        safe = assignment["position"] == self.safe_at
        window = OfficialWindowCoverage(
            assignment["ticker"], assignment["search_from"], assignment["search_to"],
            "PARTIAL" if safe else "COMPLETE",
            ("news",) if safe else ("news", "ir", "announcements"),
            ("blocked exact document",) if safe else (), ())
        return multi.OfficialPreflight(
            "SAFE_UNKNOWN" if safe else "READY", CompanyIdentityRegistry(), window, {},
            {"http_requests": 0, "response_bytes": 0, "transports": [],
             "fetch_failures": int(safe), "official_discovery_started": safe},
            window.gaps)


class ControlFailureOfficial:
    def __init__(self):
        self.calls = []

    def prepare(self, assignment, company_dir):
        self.calls.append(dict(assignment))
        gap = "official plan control wiring unavailable"
        window = OfficialWindowCoverage(
            assignment["ticker"], assignment["search_from"], assignment["search_to"],
            "BLOCKED", (), (gap,), ())
        return multi.OfficialPreflight(
            "CONTROL_FAILURE", CompanyIdentityRegistry(), window, {},
            {"http_requests": 0, "official_discovery_started": False}, (gap,))


class FakeExecutor:
    def __init__(self, tokens=100, marker=None):
        self.tokens, self.marker, self.calls = tokens, marker, []

    def __call__(self, root, company_dir, assignment, official, *, usage_checkpoint=None):
        self.calls.append(dict(assignment))
        total = self.tokens(assignment) if callable(self.tokens) else self.tokens
        telemetry = {"total_tokens": total, "worker_count": 3, "web_call_count": 2,
                     "cached_input_tokens": 40, "workers": [{"input_tokens": 50}]}
        if usage_checkpoint:
            usage_checkpoint(telemetry)
        return ({"research_result": {"items": []}, "strategy_telemetry": {}}, telemetry)


def prepare(tmp_path, count=12):
    path = cohort_file(tmp_path, count)
    cohort = load(path, count, count)
    run = tmp_path / "run"
    multi.prepare_run(cohort, run, search_from="2026-08-25", search_to="2026-08-31",
                      approved_company_count=count)
    return path, run


def controller(tmp_path, *, official=None, executor=None, auditor=None, count=12):
    _, run = prepare(tmp_path, count)
    value = multi.FrozenCohortController(
        root=tmp_path, run_dir=run, official=official or ReadyOfficial(),
        executor=executor or FakeExecutor(),
        auditor=auditor or (lambda response, company_dir: {"status": "PASS"}))
    return run, value


def test_loads_fixed_12_and_freezes_hash_order_and_count(tmp_path):
    path = cohort_file(tmp_path)
    value = load(path)
    assert len(value.companies) == 12
    assert [row.position for row in value.companies] == list(range(1, 13))
    assert value.cohort_sha256 == json.loads(path.read_text())["cohort_sha256"]


def test_sha_count_duplicate_order_and_cap_fail_closed(tmp_path):
    path = cohort_file(tmp_path)
    document = json.loads(path.read_text())
    document["cohort_sha256"] = "0" * 64
    path.write_text(json.dumps(document))
    with pytest.raises(multi.CohortIntegrityError, match="SHA"):
        load(path)
    with pytest.raises(multi.CohortIntegrityError, match="count"):
        multi.FrozenCohort.load(cohort_file(tmp_path), approved_company_count=11,
                                max_companies=12)
    duplicate = cohort_file(tmp_path, mutate=lambda rows: rows[1].update(security_code=rows[0]["security_code"]))
    with pytest.raises(multi.CohortIntegrityError, match="duplicate"):
        load(duplicate)
    changed = cohort_file(tmp_path, mutate=lambda rows: rows[1].update(source_universe_position=0))
    with pytest.raises(multi.CohortIntegrityError, match="order"):
        load(changed)
    with pytest.raises(multi.CohortIntegrityError, match="hard cap"):
        multi.FrozenCohort.load(cohort_file(tmp_path), approved_company_count=12,
                                max_companies=11)


def test_cohort_mutation_after_prepare_stops_before_execution(tmp_path):
    path, run = prepare(tmp_path)
    document = json.loads(path.read_text())
    document["note"] = "changed bytes"
    path.write_text(json.dumps(document))
    ctl = multi.FrozenCohortController(root=tmp_path, run_dir=run,
        official=ReadyOfficial(), executor=FakeExecutor(),
        auditor=lambda *_: {"status": "PASS"})
    with pytest.raises(multi.CohortIntegrityError, match="changed"):
        ctl.run_next()


def test_official_registry_and_plan_are_frozen_together(tmp_path):
    path = cohort_file(tmp_path, 1)
    cohort = load(path, 1, 1)
    registry, plan = complete_inputs(tmp_path, cohort)
    run = tmp_path / "run"
    manifest = multi.prepare_run(
        cohort, run, search_from="2026-08-25", search_to="2026-08-31",
        approved_company_count=1, identity_registry_path=registry,
        official_plan_path=plan)
    assert len(manifest["identity_registry_logical_sha256"]) == 64
    assert len(manifest["official_plan_logical_sha256"]) == 64
    assert multi.load_frozen_official_config(manifest) == (registry.resolve(), plan.resolve())
    document = json.loads(plan.read_text())
    document["companies"][0]["official_domain"] = "changed.example"
    plan.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(multi.CohortIntegrityError, match="metadata changed"):
        multi.load_frozen_official_config(manifest)


def test_offline_bootstrap_fails_closed_without_verified_identity(tmp_path):
    cohort = load(cohort_file(tmp_path), 12, 12)
    master = tmp_path / "company-master.db"
    company_master(master, [(row.security_code, f"https://{row.security_code}.example/",
                             f"{row.security_code}.example", "discovered", "company master",
                             "2026-08-20T00:00:00+00:00", 200)
                            for row in cohort.companies])
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    output = tmp_path / "bootstrap"
    report = multi.bootstrap_inputs(
        cohort, company_master_db=master, repository_artifacts_root=artifacts,
        output_dir=output, offline_only=True)
    assert report["status"] == "INCOMPLETE_OFFLINE_EVIDENCE"
    assert report["verified_count"] == 0 and report["unverified_count"] == 12
    assert all(not row["offline_bootstrap_complete"] for row in report["matrix"])
    assert not (output / "phase22-company-identity-registry.json").exists()
    assert not (output / "phase22-official-source-plan.json").exists()
    rendered = json.dumps(report).lower()
    assert not any(key in rendered for key in (
        "historical_result", "old_event", "gold_url", "selection_reason"))


def test_offline_bootstrap_reuses_verified_repository_identity_and_freezes(tmp_path):
    cohort = load(cohort_file(tmp_path, 1), 1, 1)
    master = tmp_path / "company-master.db"
    company_master(master, [("1001", "https://example.test/", "example.test",
                             "discovered", "company master",
                             "2026-08-20T00:00:00+00:00", 200)])
    artifacts = tmp_path / "artifacts" / "identity-preflight"
    artifacts.mkdir(parents=True)
    verified_registry().save(artifacts / "company-identity-registry.json")
    output = tmp_path / "bootstrap"
    report = multi.bootstrap_inputs(
        cohort, company_master_db=master,
        repository_artifacts_root=tmp_path / "artifacts",
        output_dir=output, offline_only=True)
    assert report["status"] == "READY" and report["verified_count"] == 1
    registry = output / "phase22-company-identity-registry.json"
    plan = output / "phase22-official-source-plan.json"
    multi.validate_frozen_inputs(cohort, registry, plan)
    frozen_plan = json.loads(plan.read_text())
    assert frozen_plan["resolver_policy"]["manual_url_input"] is False
    assert frozen_plan["resolver_policy"]["historical_result_input"] is False
    assert frozen_plan["family_urls"] == {} and frozen_plan["repository_artifacts"] == {}
    prepared = tmp_path / "prepared"
    multi.prepare_run(cohort, prepared, search_from="2026-08-25", search_to="2026-08-31",
                      approved_company_count=1, identity_registry_path=registry,
                      official_plan_path=plan)
    assert not (prepared / "companies").exists()
    pending = json.loads((prepared / "queue.json").read_text())["assignments"][0]
    assert pending["research_started"] is pending["model_started"] is False
    assert pending["token_gate"] == "NOT_MEASURED"
    dry = multi.dry_control_preflight(prepared)
    assert dry == {
        "status": "OFFICIAL_DISCOVERY_PENDING", "ticker": "1001",
            "identity_verified": True, "root_authority_available": True,
            "family_urls_frozen": False, "runtime_discovery_reachable": True,
            "ir_nightly_repository_authority_reachable": False,
            "known_successful_exact_source_candidates": [],
            "official_window_state": "PENDING", "research_started": False,
        "model_started": False, "http_requests": 0, "web_calls": 0, "model_calls": 0,
    }
    assert not (prepared / "companies").exists()


def test_schannel_bootstrap_verifies_12_freezes_and_prepares_without_research(tmp_path):
    cohort = load(cohort_file(tmp_path), 12, 12)
    master = tmp_path / "company-master.db"
    company_master(master, [(row.security_code, f"https://{row.security_code}.example/",
                             f"{row.security_code}.example", "discovered", "company master",
                             "2026-08-20T00:00:00+00:00", 200)
                            for row in cohort.companies])
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    resolvers = []
    def factory(company_dir, company):
        resolver = IdentityResolver()
        resolvers.append(resolver)
        return resolver, ResolverJournal(resolver)
    output, prepared = tmp_path / "bootstrap", tmp_path / "prepared"
    report = multi.bootstrap_identities_schannel(
        cohort, company_master_db=master, repository_artifacts_root=artifacts,
        output_dir=output, resolver_factory=factory, prepare_run_dir=prepared)
    assert report["status"] == "READY" and report["verified_count"] == 12
    assert report["http_requests"] == 12 and sum(len(row.calls) for row in resolvers) == 12
    assert len(report["registry_logical_sha256"]) == len(report["plan_file_sha256"]) == 64
    assert report["prepare_result"]["state"] == "PREPARED"
    assert not (prepared / "companies").exists()
    assert report["research_companies"] == report["web_calls"] == report["model_calls"] == 0


def test_schannel_bootstrap_follows_only_literal_identity_link_once(tmp_path):
    cohort = load(cohort_file(tmp_path, 1), 1, 1)
    master = tmp_path / "company-master.db"
    company_master(master, [("1001", "https://example.test/", "example.test",
                             "discovered", "company master",
                             "2026-08-20T00:00:00+00:00", 200)])
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    resolver = IdentityResolver(
        root_has_identity=False, identity_link="https://example.test/company/profile/")
    report = multi.bootstrap_identities_schannel(
        cohort, company_master_db=master, repository_artifacts_root=artifacts,
        output_dir=tmp_path / "bootstrap", resolver_factory=lambda *_: (
            resolver, ResolverJournal(resolver)))
    assert report["status"] == "READY" and report["http_requests"] == 2
    assert resolver.calls[1][1] == "https://example.test/company/profile/"
    assert resolver.calls[1][2].max_redirects == 0


def test_identity_bootstrap_rejects_guessed_path_and_preserves_failure_state(tmp_path):
    blocked = OfficialSourceResolution(
        "https://example.test/", "https://example.test/", 0, "BLOCKED", None,
        None, None, "2026-09-06T00:00:00+00:00", "windows-schannel-v1", "TLS")
    assert multi.literal_identity_page_candidate(blocked, "example.test") is None
    assert multi.deterministic_https_root("example.test") == "https://example.test/"
    with pytest.raises(multi.MultiRunnerError):
        multi.deterministic_https_root("https://example.test/about")
    cohort = load(cohort_file(tmp_path, 1), 1, 1)
    master = tmp_path / "company-master.db"
    company_master(master, [("1001", "https://example.test/", "example.test",
                             "discovered", "company master",
                             "2026-08-20T00:00:00+00:00", 200)])
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    unverified = IdentityResolver(root_has_identity=False)
    report = multi.bootstrap_identities_schannel(
        cohort, company_master_db=master, repository_artifacts_root=artifacts,
        output_dir=tmp_path / "unverified", resolver_factory=lambda *_: (
            unverified, ResolverJournal(unverified)))
    assert report["matrix"][0]["identity_state"] == "UNVERIFIED"
    assert report["registry_path"] is None
    tls_blocked = IdentityResolver(blocked=True)
    report = multi.bootstrap_identities_schannel(
        cohort, company_master_db=master, repository_artifacts_root=artifacts,
        output_dir=tmp_path / "blocked", resolver_factory=lambda *_: (
            tls_blocked, ResolverJournal(tls_blocked)))
    assert report["matrix"][0]["identity_state"] == "BLOCKED"
    assert report["matrix"][0]["failure"] == "TLS"


def test_identity_authority_separates_domain_exact_url_and_prefers_nightly_success(tmp_path):
    master = tmp_path / "master.db"
    company_master(master, [("1001", "https://www.example.test/", "example.test",
                             "discovered", "master", "2026-08-20T00:00:00+00:00", 200)])
    company_sources(master, [(1, "1001", "https://www.example.test/ir/", "active",
                              "2026-08-20", "2026-09-06", "2026-09-06", None, 0,
                              "top", "master", "https://www.example.test/", 0, 1)])
    authority = multi._load_company_master(master, ["1001"])["1001"]
    sources = multi._load_ir_nightly_sources(master, ["1001"])["1001"]
    selected = multi._select_identity_url_authority(authority, sources)
    assert authority["official_domain"] == "example.test"
    assert selected["identity_fetch_url"] == "https://www.example.test/ir/"
    assert selected["identity_fetch_url_source"] == "IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH"
    assert selected["identity_fetch_verification"] == "KNOWN_SUCCESSFUL_FETCH"
    assert selected["identity_fetch_url"] != multi.deterministic_https_root("example.test")


def test_ir_nightly_saved_body_hash_and_linkage_can_be_reused(tmp_path):
    root = tmp_path / "artifacts"
    saved = root / "ir-nightly"; saved.mkdir(parents=True)
    body = b"<html><title>Company 1</title><body>Company 1 official</body></html>"
    (saved / "body.html").write_bytes(body)
    (saved / "company-ir-nightly-verified-body.json").write_text(json.dumps({
        "schema_version": multi.IR_NIGHTLY_VERIFIED_BODY_SCHEMA,
        "ticker": "1001", "company_name": "Company 1",
        "official_domain": "example.test", "source_url": "https://www.example.test/ir/",
        "last_success_at": "2026-09-06T00:00:00+00:00", "body_path": "body.html",
        "body_sha256": hashlib.sha256(body).hexdigest(), "mime_type": "text/html",
    }), encoding="utf-8")
    records = multi._load_ir_nightly_verified_body_records(root)
    assert records["1001"].status == "VERIFIED"
    assert records["1001"].verification_method == "REPOSITORY_VERIFIED_ARTIFACT"
    assert records["1001"].evidence_hash == hashlib.sha256(body).hexdigest()


def test_configured_url_or_bad_saved_body_never_becomes_verified(tmp_path):
    root = tmp_path / "artifacts"
    saved = root / "ir-nightly"; saved.mkdir(parents=True)
    (saved / "body.html").write_text("unrelated company", encoding="utf-8")
    (saved / "company-ir-nightly-verified-body.json").write_text(json.dumps({
        "schema_version": multi.IR_NIGHTLY_VERIFIED_BODY_SCHEMA,
        "ticker": "1001", "company_name": "Company 1",
        "official_domain": "example.test", "source_url": "https://www.example.test/ir/",
        "last_success_at": "2026-09-06T00:00:00+00:00", "body_path": "body.html",
        "body_sha256": "0" * 64, "mime_type": "text/html",
    }), encoding="utf-8")
    assert multi._load_ir_nightly_verified_body_records(root) == {}


def test_offline_resolution_plan_uses_saved_www_only_and_never_calls_network(tmp_path, monkeypatch):
    cohort = load(cohort_file(tmp_path, 1), 1, 1)
    master = tmp_path / "master.db"
    company_master(master, [("1001", "https://www.example.test/", "example.test",
                             "discovered", "master", "2026-08-20T00:00:00+00:00", 200)])
    company_sources(master, [(1, "1001", "https://www.example.test/ir/", "active",
                              "2026-08-20", "2026-09-06", "2026-09-06", None, 0,
                              "top", "master", "https://www.example.test/", 0, 1)])
    calls = []
    monkeypatch.setattr(multi.OfficialSourceResolver, "resolve",
                        lambda *args, **kwargs: calls.append(args))
    artifacts = tmp_path / "artifacts"; artifacts.mkdir()
    plan = multi.generate_identity_resolution_plan(
        cohort, company_master_db=master, repository_artifacts_root=artifacts,
        output_path=tmp_path / "plan.json")
    row = plan["companies"][0]
    assert calls == [] and plan["http_calls"] == plan["web_calls"] == plan["model_calls"] == 0
    assert row["classification"] == "B_EXACT_AUTHORITATIVE_URL_ONE_GET"
    assert row["candidate_exact_url"] == "https://www.example.test/ir/"
    assert plan["contract"]["configured_url_is_verified_identity"] is False
    assert plan["contract"]["derived_root_failure_blocks_official_source_globally"] is False
    assert plan["contract"]["historical_benchmark_answer_input"] is False


def test_bare_domain_is_lowest_authority_and_not_globally_blocked(tmp_path):
    cohort = load(cohort_file(tmp_path, 1), 1, 1)
    master = tmp_path / "master.db"
    company_master(master, [("1001", None, "example.test", "discovered", "master",
                             "2026-08-20T00:00:00+00:00", None)])
    artifacts = tmp_path / "artifacts"; artifacts.mkdir()
    phase22e = tmp_path / "phase22e.json"
    phase22e.write_text(json.dumps({"matrix": [{"position": 1, "ticker": "1001",
        "deterministic_root_url": "https://example.test/", "failure": "DNS",
        "http_requests": 0}]}), encoding="utf-8")
    plan = multi.generate_identity_resolution_plan(
        cohort, company_master_db=master, repository_artifacts_root=artifacts,
        phase22e_report_path=phase22e, output_path=tmp_path / "plan.json")
    row = plan["companies"][0]
    assert row["source"] == "DERIVED_ROOT_URL"
    assert row["classification"] == "C_EXACT_AUTHORITATIVE_URL_MISSING"
    assert row["failure_clusters"] == ["D_DERIVED_HOST_DNS_FAILURE"]


def phase22g_plan(tmp_path, cohort, url):
    value = {
        "schema_version": multi.IDENTITY_RESOLUTION_PLAN_SCHEMA,
        "cohort_sha256": cohort.cohort_sha256,
        "ordered_tickers": [row.security_code for row in cohort.companies],
        "companies": [{
            "position": 1, "ticker": "1001", "company_name": "Company 1",
            "official_domain": "example.test", "candidate_exact_url": url,
            "source": "IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH",
        }],
    }
    path = tmp_path / "resolution-plan.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_known_successful_bootstrap_verifies_saves_reusable_body_and_prepares(tmp_path):
    cohort = load(cohort_file(tmp_path, 1), 1, 1)
    plan, plan_sha = phase22g_plan(tmp_path, cohort, "https://www.example.test/ir/")
    sessions = []
    def factory(company_dir, company):
        store = multi.DurableArtifactStore.prepare(company_dir / "identity-http")
        journal = multi.HttpFetchJournal(store, run_id="fixture", company=company.security_code)
        session = NightlySession([NightlyResponse(
            headers={"Content-Type": "text/html"}, body=b"Company 1 official identity")])
        sessions.append(session)
        return multi.OfficialSourceResolver(multi.IrNightlyKnownSuccessfulTransport(
            session_factory=lambda: session,
            dns_resolver=lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))],
            journal=journal)), journal
    output, prepared = tmp_path / "bootstrap", tmp_path / "prepared"
    report = multi.bootstrap_identities_ir_nightly(
        cohort, resolution_plan_path=plan, expected_resolution_plan_sha256=plan_sha,
        output_dir=output, prepare_run_dir=prepared, resolver_factory=factory,
        max_logical_fetches=1)
    assert report["status"] == "READY" and report["verified_count"] == 1
    assert report["logical_company_fetches"] == report["actual_http_requests"] == 1
    assert report["research_companies"] == report["model_calls"] == report["web_calls"] == 0
    assert report["prepare_result"] == {
        "state": "PREPARED", "run_dir": str(prepared.resolve()), "company_directories": 0}
    assert not (prepared / "companies").exists()
    assert sessions[0].calls[0][0] == "https://www.example.test/ir/"
    reused = multi._load_ir_nightly_verified_body_records(output)
    assert reused["1001"].status == "VERIFIED"
    assert multi._contains_forbidden_bootstrap_key(json.loads(
        (output / "phase22-company-identity-registry.json").read_text())) is False


def test_known_successful_bootstrap_http_only_final_fails_closed_without_formal_files(tmp_path):
    cohort = load(cohort_file(tmp_path, 1), 1, 1)
    plan, plan_sha = phase22g_plan(tmp_path, cohort, "http://www.example.test/ir/")
    def factory(company_dir, company):
        store = multi.DurableArtifactStore.prepare(company_dir / "identity-http")
        journal = multi.HttpFetchJournal(store, run_id="fixture", company=company.security_code)
        session = NightlySession([NightlyResponse(
            headers={"Content-Type": "text/html"}, body=b"Company 1 official identity")])
        return multi.OfficialSourceResolver(multi.IrNightlyKnownSuccessfulTransport(
            session_factory=lambda: session,
            dns_resolver=lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 80))],
            journal=journal)), journal
    output, prepared = tmp_path / "bootstrap", tmp_path / "prepared"
    report = multi.bootstrap_identities_ir_nightly(
        cohort, resolution_plan_path=plan, expected_resolution_plan_sha256=plan_sha,
        output_dir=output, prepare_run_dir=prepared, resolver_factory=factory,
        max_logical_fetches=1)
    assert report["status"] == "PARTIAL"
    assert report["matrix"][0]["identity_state"] == "UNVERIFIED_HTTP_ONLY"
    assert report["verified_count"] == 0 and report["unverified_count"] == 1
    assert report["registry_path"] is report["official_plan_path"] is None
    assert not prepared.exists()


def test_legacy_http_offline_freeze_prepares_without_research_or_leakage(tmp_path):
    cohort = load(cohort_file(tmp_path, 1), 1, 1)
    plan, plan_sha = phase22g_plan(tmp_path, cohort, "http://www.example.test/ir/")
    def factory(company_dir, company):
        store = multi.DurableArtifactStore.prepare(company_dir / "identity-http")
        journal = multi.HttpFetchJournal(store, run_id="fixture", company=company.security_code)
        session = NightlySession([NightlyResponse(
            headers={"Content-Type": "text/html"},
            body=b"<title>Company 1 official identity</title>")])
        return multi.OfficialSourceResolver(multi.IrNightlyKnownSuccessfulTransport(
            session_factory=lambda: session,
            dns_resolver=lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 80))],
            journal=journal)), journal
    phase22g = tmp_path / "phase22g"
    first_prepare = tmp_path / "must-not-exist"
    initial = multi.bootstrap_identities_ir_nightly(
        cohort, resolution_plan_path=plan, expected_resolution_plan_sha256=plan_sha,
        output_dir=phase22g, prepare_run_dir=first_prepare, resolver_factory=factory,
        max_logical_fetches=1)
    assert initial["status"] == "PARTIAL" and not first_prepare.exists()
    phase22h = tmp_path / "phase22h"; phase22h.mkdir()
    phase22h_report = phase22h / "report.json"
    phase22h_report.write_text(json.dumps({
        "cohort_sha256": cohort.cohort_sha256,
        "resolution_plan_sha256": plan_sha,
        "phase22h": {},
    }), encoding="utf-8")
    output, prepared = tmp_path / "formal", tmp_path / "prepared"
    final = multi.finalize_legacy_http_identity_freeze(
        cohort, phase22g_dir=phase22g, phase22h_report_path=phase22h_report,
        expected_resolution_plan_sha256=plan_sha, output_dir=output,
        prepare_run_dir=prepared, search_from="2026-08-25", search_to="2026-08-31")
    assert final["status"] == "READY" and final["verified_count"] == 1
    assert final["http_requests"] == final["research_companies"] == 0
    assert final["model_calls"] == final["web_calls"] == 0
    assert final["leakage_findings"] == []
    assert final["prepare_result"]["state"] == "PREPARED"
    assert not (prepared / "companies").exists()
    registry = multi.CompanyIdentityRegistry.load(Path(final["registry_path"]))
    record = registry.get("1001")
    assert record.transport_assurance == "LEGACY_HTTP"
    assert record.transport_security_warning is True and record.tls_verified is False
    frozen_plan = json.loads(Path(final["official_plan_path"]).read_text())
    assert frozen_plan["companies"][0]["transport_assurance"] == "LEGACY_HTTP"
    assert frozen_plan["resolver_policy"]["current_content_https_only"] is True
    assert multi._contains_forbidden_bootstrap_key(frozen_plan) is False


def test_legacy_http_identity_does_not_complete_window_or_verify_document():
    record = CompanyIdentityRecord(
        "1001", "Company 1", "company1", "example.test",
        "http://www.example.test/ir/", "IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH",
        "REPOSITORY_VERIFIED_ARTIFACT", "2026-09-06T00:00:00+00:00", "a" * 64,
        "VERIFIED", "Company 1 official identity", "LEGACY_HTTP", True, False,
        "IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH", 200)
    registry = CompanyIdentityRegistry([record])
    blocked = FakeResolver(resolution("http://www.example.test/news", status="BLOCKED"))
    integration = multi.OfficialResolverIntegration(registry, blocked, None, {})
    preflight = integration.prepare({
        "ticker": "1001", "company_name": "Company 1",
        "search_from": "2026-08-25", "search_to": "2026-08-31"}, Path("."))
    assert preflight.status == "OFFICIAL_DISCOVERY_PENDING"
    assert preflight.window.state == "PENDING" and preflight.runtime_discovery is not None
    document = integration.fetch_document(
        record, title="価格改定", url="http://www.example.test/news")
    assert document["provenance_state"] == "ACCESS_FAILED"


def test_prepare_rejects_incomplete_frozen_inputs(tmp_path):
    cohort = load(cohort_file(tmp_path, 1), 1, 1)
    registry = tmp_path / "empty-registry.json"
    registry.write_text(json.dumps({
        "schema_version": "company_identity_registry_v1", "records": []}), encoding="utf-8")
    plan = tmp_path / "empty-plan.json"
    plan.write_text(json.dumps({"schema_version": multi.OFFICIAL_PLAN_SCHEMA,
        "cohort_sha256": cohort.cohort_sha256, "company_count": 1,
        "ordered_tickers": ["1001"], "companies": []}), encoding="utf-8")
    with pytest.raises(multi.CohortIntegrityError, match="registry cohort/count mismatch"):
        multi.prepare_run(cohort, tmp_path / "run", search_from="2026-08-25",
                          search_to="2026-08-31", approved_company_count=1,
                          identity_registry_path=registry, official_plan_path=plan)


def test_mock_12_company_simulation_is_ordered_isolated_and_atomic(tmp_path):
    official, executor = ReadyOfficial(), FakeExecutor()
    run, ctl = controller(tmp_path, official=official, executor=executor)
    for _ in range(12):
        assert ctl.run_next()["status"] == "COMPLETED"
    assert ctl.run_next()["status"] == "EMPTY"
    report = multi.aggregate(run)
    assert report["state"] == "COMPLETED" and report["completed"] == 12
    assert report["total_tokens"] == 1200
    assert [row["position"] for row in executor.calls] == list(range(1, 13))
    for index in range(1, 13):
        audit = json.loads(next((run / "companies" / f"{index:02d}-{1000+index:04d}").glob(
            "runtime-input-audit.json")).read_text())
        assert audit["previous_company_marker_hits"] == []
        assert audit["previous_company_payload_included"] is False
    assert not (run / "guard.json").exists()
    assert not list(run.rglob("*.tmp"))


def test_safe_unknown_continues_without_model_call(tmp_path):
    official, executor = ReadyOfficial(safe_at=1), FakeExecutor()
    run, ctl = controller(tmp_path, official=official, executor=executor)
    assert ctl.run_next()["status"] == "SAFE_UNKNOWN"
    assert executor.calls == []
    assert ctl.run_next()["status"] == "COMPLETED"
    queue = json.loads((run / "queue.json").read_text())
    assert queue["assignments"][0]["total_tokens"] is None
    assert queue["assignments"][0]["token_gate"] == "NOT_MEASURED"
    assert queue["assignments"][1]["status"] == "COMPLETED"


def transport_journal(url, *, failure="TLS_HANDSHAKE", stage="TLS", message="handshake failed",
                      status=0):
    return {
        "requested_url": url,
        "transport": "windows-schannel-v1",
        "history": [{
            "phase": "RESPONSE_RECEIVED",
            "status_code": status,
            "exception_category": failure or None,
            "transport_metadata": {
                "failure_stage": stage,
                "error_detail": message,
                "exception_chain": [message] if message else [],
                "certificate_summary": {},
            },
        }],
    }


@pytest.mark.parametrize(("failure", "stage", "message", "status", "scope", "signature"), [
    ("DNS", "DNS", "No such host is known", 0, "DOMAIN_OR_REMOTE", None),
    ("TCP", "TCP", "socket access is denied 10013", 0, "LOCAL_SYSTEMIC",
     "LOCAL_SOCKET_ACCESS_DENIED"),
    ("TLS_HANDSHAKE", "TLS", "Authentication failed", 0, "DOMAIN_OR_REMOTE", None),
    ("TLS_CA_CHAIN", "TLS", "UntrustedRoot", 0, "DOMAIN_CERTIFICATE", None),
    ("TIMEOUT", "TLS", "operation timed out", 0, "AMBIGUOUS_TIMEOUT", None),
    (None, "HTTP", "", 403, "REMOTE_HTTP", None),
])
def test_transport_failure_stage_taxonomy(failure, stage, message, status, scope, signature):
    value = multi.classify_transport_failure_evidence(transport_journal(
        "https://official.example/", failure=failure, stage=stage,
        message=message, status=status))
    assert value["scope"] == scope
    assert value["systemic_signature"] == signature
    assert value["failure_class"] == ("HTTP_4XX" if status == 403 else failure)


def test_schannel_no_credentials_is_local_systemic_signature():
    value = multi.classify_transport_failure_evidence(transport_journal(
        "https://official.example/",
        message="Win32Exception: セキュリティ パッケージで利用できる資格情報がありません"))
    assert value["scope"] == "LOCAL_SYSTEMIC"
    assert value["systemic_signature"] == "SCHANNEL_SEC_E_NO_CREDENTIALS"


def test_phase22q_run004_success_run005_failure_fixture_attributes_local_boundary():
    fixture = json.loads((Path(__file__).parent / "fixtures" /
        "company_news_phase22q_5237_transport_comparison.json").read_text(encoding="utf-8"))
    success, failed, probe = fixture["run004"], fixture["run005"], fixture["independent_probe"]
    assert success["requested_url"] == failed["requested_url"] == probe["requested_url"]
    assert success["transport"] == failed["transport"] == probe["transport"]
    assert success["implementation_fingerprint"] == failed["implementation_fingerprint"]
    assert success["status_code"] == probe["status_code"] == 200
    assert success["body_sha256"] == probe["body_sha256"]
    classified = multi.classify_transport_failure_evidence({
        "requested_url": failed["requested_url"], "transport": failed["transport"],
        "history": [{"phase": "RESPONSE_RECEIVED", "status_code": failed["status_code"],
            "exception_category": failed["failure_class"], "transport_metadata": {
                "failure_stage": failed["failure_stage"],
                "exception_chain": failed["exception_chain"]}}]})
    assert classified["systemic_signature"] == "SCHANNEL_SEC_E_NO_CREDENTIALS"
    assert classified["scope"] == "LOCAL_SYSTEMIC"


class JournalSafeOfficial:
    def __init__(self, failure="TLS_HANDSHAKE", message="SEC_E_NO_CREDENTIALS 0x8009030E"):
        self.failure, self.message = failure, message

    def prepare(self, assignment, company_dir):
        journal_dir = company_dir / "official-http" / "http-journal"
        journal_dir.mkdir(parents=True)
        value = transport_journal(
            f"https://{assignment['ticker']}.example/", failure=self.failure,
            stage="TLS", message=self.message)
        (journal_dir / "request-0001.json").write_text(json.dumps(value), encoding="utf-8")
        window = OfficialWindowCoverage(
            assignment["ticker"], assignment["search_from"], assignment["search_to"],
            "BLOCKED", (), (self.failure,), ())
        return multi.OfficialPreflight(
            "SAFE_UNKNOWN", CompanyIdentityRegistry(), window, {},
            {"http_requests": 1, "official_discovery_started": True}, window.gaps)


def test_three_consecutive_distinct_domains_trip_run_level_circuit_breaker(tmp_path):
    run, ctl = controller(tmp_path, official=JournalSafeOfficial(), count=5)
    assert ctl.run_next()["status"] == "SAFE_UNKNOWN"
    assert ctl.run_next()["status"] == "SAFE_UNKNOWN"
    assert ctl.run_next()["status"] == "INFRA_STOP"
    manifest = json.loads((run / "manifest.json").read_text())
    rows = json.loads((run / "queue.json").read_text())["assignments"]
    assert manifest["state"] == "STOPPED"
    assert manifest["transport_circuit_breaker"] == {
        "threshold": 3,
        "signature": "SCHANNEL_SEC_E_NO_CREDENTIALS",
        "consecutive_distinct_domains": ["1001.example", "1002.example", "1003.example"],
        "triggered_at": manifest["transport_circuit_breaker"]["triggered_at"],
    }
    assert [row["status"] for row in rows] == [
        "SAFE_UNKNOWN", "SAFE_UNKNOWN", "INFRA_STOP", "PENDING", "PENDING"]
    with pytest.raises(multi.ResumeRequired):
        ctl.run_next()
    report = multi.aggregate(run)
    assert report["overall_evaluation"] == "BLOCKED"
    assert report["live_research_e2e"] == "BLOCKED"


def test_domain_certificate_failures_do_not_trip_systemic_circuit_breaker(tmp_path):
    run, ctl = controller(tmp_path, official=JournalSafeOfficial(
        failure="TLS_CA_CHAIN", message="UntrustedRoot"), count=3)
    assert [ctl.run_next()["status"] for _ in range(3)] == [
        "SAFE_UNKNOWN", "SAFE_UNKNOWN", "SAFE_UNKNOWN"]
    assert json.loads((run / "manifest.json").read_text())["state"] == "COMPLETED"


def test_run005_style_all_transport_blocked_is_not_overall_pass(tmp_path):
    run, ctl = controller(tmp_path, official=ReadyOfficial(safe_at=1), count=1)
    assert ctl.run_next()["status"] == "SAFE_UNKNOWN"
    report = multi.aggregate(run)
    assert report["queue_fail_closed_behavior"] == "PASS"
    assert report["publication_safety"] == "PASS"
    assert report["token_efficiency"] == "NOT_MEASURED"
    assert report["live_research_e2e"] == "BLOCKED"
    assert report["live_provenance_reconciliation"] == "NOT_REACHED"
    assert report["overall_evaluation"] == "BLOCKED"


def test_common_control_defect_stops_first_company_and_preserves_remaining_queue(tmp_path):
    official, executor = ControlFailureOfficial(), FakeExecutor()
    run, ctl = controller(tmp_path, official=official, executor=executor)
    result = ctl.run_next()
    assert result["status"] == "INFRA_STOP" and executor.calls == []
    manifest = json.loads((run / "manifest.json").read_text())
    queue = json.loads((run / "queue.json").read_text())["assignments"]
    assert manifest["state"] == "STOPPED"
    assert queue[0]["status"] == "INFRA_STOP"
    assert all(row["status"] == "PENDING" for row in queue[1:])
    assert queue[0]["research_started"] is queue[0]["model_started"] is False


def test_unstarted_zero_token_is_not_a_token_or_quality_sample(tmp_path):
    official, executor = ReadyOfficial(safe_at=1), FakeExecutor()
    run, ctl = controller(tmp_path, official=official, executor=executor, count=2)
    ctl.run_next()
    ctl.run_next()
    report = multi.aggregate(run)
    assert report["research_samples"] == 1 and report["not_measured"] == 1
    assert report["within_hard_gate"] == report["within_preferred_gate"] == 1
    assert report["ordered_results"][0]["token_gate"] == "NOT_MEASURED"


def test_existing_all_unstarted_run_audit_is_external_and_invalid(tmp_path):
    _, run = prepare(tmp_path, count=2)
    manifest_path, queue_path = run / "manifest.json", run / "queue.json"
    manifest = json.loads(manifest_path.read_text())
    queue = json.loads(queue_path.read_text())
    manifest["state"] = "COMPLETED"
    for row in queue["assignments"]:
        row.update(status="SAFE_UNKNOWN", total_tokens=0)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    before = (manifest_path.read_bytes(), queue_path.read_bytes())
    output = tmp_path / "audit.json"
    audit = multi.audit_unstarted_run(run, output)
    assert audit["classification"] == "INVALID_FOR_TOKEN_QUALITY_EVALUATION"
    assert audit["research_started_count"] == 0
    assert before == (manifest_path.read_bytes(), queue_path.read_bytes())


def test_token_and_semantic_gates_stop_before_next_company(tmp_path):
    run, ctl = controller(tmp_path, executor=FakeExecutor(tokens=500001))
    assert ctl.run_next()["status"] == "COMPLETED"
    assert json.loads((run / "manifest.json").read_text())["state"] == "STOPPED"
    with pytest.raises(multi.ResumeRequired):
        ctl.run_next()
    queue = json.loads((run / "queue.json").read_text())
    assert queue["assignments"][1]["status"] == "PENDING"

    other = tmp_path / "semantic"; other.mkdir()
    run2, ctl2 = controller(other, auditor=lambda *_: {"status": "FAIL"})
    assert ctl2.run_next()["status"] == "FAILED"
    assert json.loads((run2 / "manifest.json").read_text())["state"] == "FAILED"
    failed = json.loads((run2 / "queue.json").read_text())["assignments"][0]
    assert failed["token_state"] == "MEASURED" and failed["total_tokens"] == 100
    report = multi.aggregate(run2)
    assert report["total_tokens"] == 100 and report["quality_pass_samples"] == 0


def test_publication_contract_failure_is_infra_stop_with_tokens_and_guard_release(tmp_path):
    class PublicationFailure(FakeExecutor):
        def __call__(self, root, company_dir, assignment, official, *, usage_checkpoint=None):
            telemetry = {"total_tokens": 268060, "worker_count": 4,
                         "web_call_count": 9, "cached_input_tokens": 136704,
                         "workers": [{"input_tokens": 102406}]}
            usage_checkpoint(telemetry)
            raise multi.lightweight.PublicationAuthorityError("unknown evidence_id")
    run, ctl = controller(tmp_path, executor=PublicationFailure(), count=2)
    assert ctl.run_next()["status"] == "INFRA_STOP"
    rows = json.loads((run / "queue.json").read_text())["assignments"]
    assert rows[0]["token_state"] == "MEASURED" and rows[0]["total_tokens"] == 268060
    assert rows[1]["status"] == "PENDING"
    assert not (run / "guard.json").exists()


def test_partial_and_unknown_failure_usage_are_never_coerced_to_zero(tmp_path):
    class PartialFailure:
        def __call__(self, root, company_dir, assignment, official, *, usage_checkpoint=None):
            worker = company_dir / "workers" / "company-web"
            worker.mkdir(parents=True)
            (worker / "telemetry.json").write_text(json.dumps({
                "kind": "company_web", "total_tokens": 10, "input_tokens": 8,
                "cached_input_tokens": 0, "uncached_input_tokens": 8,
                "output_tokens": 2, "reasoning_tokens": 0, "model_call_count": 1,
                "tool_call_count": 1, "web_call_count": 1,
                "research_elapsed_seconds": 1, "model_visible_chars": 1}), encoding="utf-8")
            incomplete = company_dir / "workers" / "exposure-1"
            incomplete.mkdir(parents=True)
            (incomplete / "telemetry.json").write_text(json.dumps({
                "kind": "exposure", "total_tokens": None}), encoding="utf-8")
            raise RuntimeError("provenance failure")
    run, ctl = controller(tmp_path, executor=PartialFailure(), count=1)
    assert ctl.run_next()["status"] == "FAILED"
    row = json.loads((run / "queue.json").read_text())["assignments"][0]
    assert row["token_state"] == "PARTIAL_USAGE"
    assert row["total_tokens"] is None and row["observed_total_tokens"] == 10
    assert multi.aggregate(run)["partial_usage"] == 1


def test_failed_run_consumption_audit_is_external_and_includes_exact_usage(tmp_path):
    _, run = prepare(tmp_path, count=2)
    queue_path, manifest_path = run / "queue.json", run / "manifest.json"
    queue = json.loads(queue_path.read_text())
    queue["assignments"][0].update(status="FAILED", research_started=True)
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    manifest = json.loads(manifest_path.read_text()); manifest["state"] = "FAILED"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    worker = run / "companies" / "01-1001" / "workers" / "company-web"
    worker.mkdir(parents=True)
    (worker / "telemetry.json").write_text(json.dumps({
        "kind": "company_web", "total_tokens": 123, "input_tokens": 100,
        "cached_input_tokens": 40, "uncached_input_tokens": 60, "output_tokens": 23,
        "reasoning_tokens": 3, "model_call_count": 1, "tool_call_count": 1,
        "web_call_count": 1, "research_elapsed_seconds": 1, "model_visible_chars": 1}),
        encoding="utf-8")
    before = manifest_path.read_bytes(), queue_path.read_bytes()
    report = multi.audit_consumed_usage(run, tmp_path / "audit.json")
    assert report["total_consumed_tokens"] == 123
    assert report["companies"][0]["token_state"] == "MEASURED"
    assert report["quality_classification"] == "INVALID_FOR_PHASE22_QUALITY_EVALUATION"
    assert before == (manifest_path.read_bytes(), queue_path.read_bytes())


def test_resume_disabled_by_default_and_requires_explicit_command(tmp_path):
    run, ctl = controller(tmp_path, executor=FakeExecutor(tokens=500001))
    ctl.run_next()
    with pytest.raises(multi.ResumeRequired):
        ctl.run_next()
    assert multi.approve_resume(run)["resume_approved"] is True


def test_artifact_collision_fails_and_releases_guard(tmp_path):
    run, ctl = controller(tmp_path)
    (run / "companies" / "01-1001").mkdir(parents=True)
    result = ctl.run_next()
    assert result["reason"] == "artifact collision"
    assert not (run / "guard.json").exists()


def test_existing_guard_blocks_acquisition_without_queue_mutation(tmp_path):
    run, ctl = controller(tmp_path)
    guard = run / "guard.json"
    guard.write_text('{"owner":"other"}', encoding="utf-8")
    before = (run / "queue.json").read_bytes()
    with pytest.raises(multi.MultiRunnerError, match="guard already exists"):
        ctl.run_next()
    assert (run / "queue.json").read_bytes() == before
    assert guard.exists()


def test_official_resolver_registry_reuse_and_window_wiring(tmp_path):
    registry = verified_registry()
    repo = FakeResolver()
    integration = multi.OfficialResolverIntegration(registry, repo, None, {
        "1001": {"news": "https://example.test/news", "ir": "https://example.test/ir",
                 "announcements": "https://example.test/notices"}})
    assignment = {"ticker": "1001", "company_name": "Company 1",
                  "search_from": "2026-08-25", "search_to": "2026-08-31"}
    value = integration.prepare(assignment, tmp_path)
    assert value.status == "READY" and value.window.state == "COMPLETE"
    assert len(repo.calls) == 3 and value.telemetry["http_requests"] == 0
    assert value.model_context["company_identity"]["verified_ticker"] == "1001"


def test_empty_frozen_family_map_enters_runtime_root_discovery_without_fetch(tmp_path):
    registry, repo = verified_registry(), FakeResolver()
    plans = {"1001": {"verified_root_url": "https://example.test/",
                       "allowed_hosts": ["example.test"]}}
    integration = multi.OfficialResolverIntegration(registry, repo, None, {}, plans)
    assignment = {"ticker": "1001", "company_name": "Company 1",
                  "search_from": "2026-08-25", "search_to": "2026-08-31"}
    value = integration.prepare(assignment, tmp_path)
    assert value.status == "OFFICIAL_DISCOVERY_PENDING"
    assert value.window.state == "PENDING" and value.runtime_discovery is not None
    assert repo.calls == []


def test_runtime_root_discovery_resolves_news_ir_and_consolidated_announcements(tmp_path):
    registry = verified_registry()
    root = "https://example.test/"
    news, ir = "https://example.test/news/", "https://example.test/ir/"
    repo = UrlResolver({root: linked_resolution(root, (news, ir)),
                        news: resolution(news), ir: resolution(ir)})
    plans = {"1001": {"verified_root_url": root, "allowed_hosts": ["example.test"]}}
    integration = multi.OfficialResolverIntegration(registry, repo, None, {}, plans)
    assignment = {"ticker": "1001", "company_name": "Company 1",
                  "search_from": "2026-08-25", "search_to": "2026-08-31"}
    discovered = integration.prepare(assignment, tmp_path).runtime_discovery()
    assert discovered.status == "READY" and discovered.window.state == "COMPLETE"
    assert set(discovered.window.checked_families) == {"news", "ir", "announcements"}
    assert [call[1] for call in repo.calls] == [root, news, ir]
    assert discovered.telemetry["official_discovery_started"] is True


def test_frozen_registry_identity_overrides_third_party_synthesis_identity():
    response = {
        "research_result": {"sources_checked_count": 1},
        "strategy_telemetry": {
            "company_identity": {"verified_ticker": "1001",
                                 "source_url": "https://third-party.test/profile",
                                 "evidence": "model selected"},
            "source_provenance": [{
                "url": "https://third-party.test/profile", "source_type": "company_profile",
                "content_verified": True, "evidence_excerpt": "third-party profile"}],
        },
    }
    repaired = multi.apply_registry_identity(response, verified_registry(), {
        "ticker": "1001", "company_name": "Company 1"})
    identity = repaired["strategy_telemetry"]["company_identity"]
    assert identity["source_url"] == "https://example.test/"
    assert "Formal CompanyIdentityRegistry" in identity["evidence"]
    assert any(row["url"] == "https://third-party.test/profile"
               and row["source_type"] == "company_profile"
               for row in repaired["strategy_telemetry"]["source_provenance"])
    assert any(row["url"] == "https://example.test/"
               and row["source_type"] == "company_official"
               for row in repaired["strategy_telemetry"]["source_provenance"])


def test_frozen_registry_identity_does_not_require_synthesis_identity():
    response = {"research_result": {"sources_checked_count": 0},
                "strategy_telemetry": {"company_identity": None,
                                       "source_provenance": []}}
    repaired = multi.apply_registry_identity(response, verified_registry(), {
        "ticker": "1001", "company_name": "Company 1"})
    assert repaired["strategy_telemetry"]["company_identity"]["verified_ticker"] == "1001"


def test_registry_identity_reconciles_query_order_and_preserves_both_roles():
    registry = verified_registry()
    record = registry.get("1001")
    registry = CompanyIdentityRegistry([CompanyIdentityRecord(**{
        **record.to_dict(), "verified_official_url": "HTTPS://EXAMPLE.TEST:443/?b=2&a=1"})])
    response = {"research_result": {"sources_checked_count": 1},
                "strategy_telemetry": {"company_identity": None, "source_provenance": [{
                    "url": "https://example.test/?a=1&b=2", "source_type": "official_news",
                    "content_verified": True, "evidence_excerpt": "event context"}]}}
    repaired, audit = multi.apply_registry_identity_with_audit(
        response, registry, {"ticker": "1001", "company_name": "Company 1"},
        evidence_cards=[{"url": "https://example.test/?b=2&a=1",
                         "evidence_id": "evidence-event"}])
    sources = repaired["strategy_telemetry"]["source_provenance"]
    assert sources == [{"url": "https://example.test/?a=1&b=2",
                        "source_type": "company_official", "content_verified": True,
                        "evidence_excerpt": "event context"}]
    assert [row["source_role"] for row in audit["records"]] == [
        "event_or_context", "company_identity"]
    assert audit["records"][0]["evidence_ids"] == ["evidence-event"]
    assert {row["field"] for row in audit["differing_fields"]} == {
        "url", "source_type", "evidence_excerpt"}
    assert audit["model_identity_accepted"] is False


def test_registry_identity_reconciles_same_host_redirect_only_with_body_hash():
    registry = verified_registry()
    record = registry.get("1001")
    registry = CompanyIdentityRegistry([CompanyIdentityRecord(**{
        **record.to_dict(), "verified_official_url": "https://example.test/declared"})])
    response = {"research_result": {"sources_checked_count": 1},
                "strategy_telemetry": {"company_identity": None, "source_provenance": [{
                    "url": "https://example.test/final", "source_type": "official_news",
                    "content_verified": True, "evidence_excerpt": "resolved body"}]}}
    repaired, audit = multi.apply_registry_identity_with_audit(
        response, registry, {"ticker": "1001", "company_name": "Company 1"},
        document_equivalences=[{
            "declared_url": "https://example.test/declared",
            "resolved_url": "https://example.test/final", "body_sha256": "b" * 64}])
    assert len(repaired["strategy_telemetry"]["source_provenance"]) == 1
    assert audit["equivalence_basis"] == "resolver_redirect_body_hash"


@pytest.mark.parametrize("mutation,pattern", [
    ({"body_sha256": "c" * 64}, "body hash conflict"),
    ({"transport_assurance": "LEGACY_HTTP"}, "transport assurance conflict"),
    ({"official_domain": "evil.test"}, "official domain conflict"),
])
def test_registry_identity_true_metadata_conflicts_fail_closed(mutation, pattern):
    source = {"url": "https://example.test/", "source_type": "official_news",
              "content_verified": True, "evidence_excerpt": "body", **mutation}
    response = {"research_result": {"sources_checked_count": 1},
                "strategy_telemetry": {"company_identity": None,
                                       "source_provenance": [source]}}
    base = verified_registry().get("1001")
    registry = CompanyIdentityRegistry([CompanyIdentityRecord(**{
        **base.to_dict(), "transport_assurance": "HTTPS_VERIFIED"})])
    with pytest.raises(Exception, match=pattern):
        multi.apply_registry_identity_with_audit(
            response, registry, {"ticker": "1001", "company_name": "Company 1"})


def test_candidate_selection_keeps_unique_primary_and_retains_supporting_row():
    response = {"research_result": {"items": [{}]}, "strategy_telemetry": {
        "candidate_provenance": [
            {"document_url": "https://media.test/story", "source_url": "https://gov.test/data",
             "headline": "Event", "event_key": "event", "selected_for_output": True,
             "selection_reason": "support"},
            {"document_url": "https://gov.test/data", "source_url": "https://gov.test/data",
             "headline": "Event", "event_key": "event", "selected_for_output": True,
             "selection_reason": "primary"}]}}
    repaired = multi.lightweight.apply_candidate_selection_authority(response)
    rows = repaired["strategy_telemetry"]["candidate_provenance"]
    assert [row["selected_for_output"] for row in rows] == [False, True]
    assert "supporting context" in rows[0]["selection_reason"]
    assert repaired["strategy_telemetry"]["final_event_count"] == 1


@pytest.mark.parametrize("ticker,name", [("9999", "Company 1"), ("1001", "Wrong Name")])
def test_frozen_registry_identity_mismatch_fails_closed(ticker, name):
    with pytest.raises(Exception, match="no reusable VERIFIED identity"):
        multi.apply_registry_identity(
            {"research_result": {}, "strategy_telemetry": {}}, verified_registry(),
            {"ticker": ticker, "company_name": name})


def test_ir_nightly_known_successful_source_is_exact_ir_family_authority(tmp_path):
    database = tmp_path / "company_ir_monitor.db"
    company_sources(database, [
        (1, "1001", "https://example.test/cgi-bin/topics/index.cgi?mode=list&genre=3",
         "active", "baseline", "checked", "2026-09-06T10:00:00+09:00", None, 0,
         "top", "official discovery", "https://example.test/", 0, 1),
        (2, "1001", "https://evil.test/ir", "active", "baseline", "checked",
         "2026-09-06T10:00:00+09:00", None, 0, "top", "unknown", None, 1, 0),
    ])
    urls, authority = multi.load_ir_nightly_repository_source_authority(
        database, verified_registry())
    expected = "https://example.test/cgi-bin/topics/index.cgi?mode=list&genre=3"
    assert urls == {"1001": {"ir": expected}}
    assert authority["1001"][0]["authority_source_type"] == \
        "IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH"
    assert authority["1001"][0]["current_body_verified"] is False
    assert authority["1001"][0]["current_window_complete"] is False


def test_repository_exact_ir_source_fills_root_link_gap_without_guessing(tmp_path):
    registry = verified_registry()
    root, news = "https://example.test/", "https://example.test/news/"
    exact_ir = "https://example.test/cgi-bin/topics/index.cgi?mode=list&genre=3"
    repo = UrlResolver({root: linked_resolution(root, (news,)),
                        news: resolution(news), exact_ir: resolution(exact_ir)})
    plans = {"1001": {"verified_root_url": root, "allowed_hosts": ["example.test"]}}
    source_rows = [{"family": "ir", "url": exact_ir,
                    "authority_source_type": "IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH",
                    "current_body_verified": False, "current_window_complete": False}]
    integration = multi.OfficialResolverIntegration(
        registry, repo, None, {}, plans, {"1001": {"ir": exact_ir}},
        {"1001": source_rows})
    assignment = {"ticker": "1001", "company_name": "Company 1",
                  "search_from": "2026-08-25", "search_to": "2026-08-31"}
    discovered = integration.prepare(assignment, tmp_path).runtime_discovery()
    assert discovered.status == "READY" and discovered.window.state == "COMPLETE"
    assert discovered.telemetry["discovered_family_urls"]["ir"] == exact_ir
    assert discovered.telemetry["repository_source_authority"] == source_rows
    assert [call[1] for call in repo.calls] == [root, news, exact_ir]


def test_runtime_discovery_marks_announcements_non_applicable_and_required_gap(tmp_path):
    registry = verified_registry()
    root, ir = "https://example.test/", "https://example.test/ir/"
    repo = UrlResolver({root: linked_resolution(root, (ir,)), ir: resolution(ir)})
    plans = {"1001": {"verified_root_url": root, "allowed_hosts": ["example.test"]}}
    integration = multi.OfficialResolverIntegration(registry, repo, None, {}, plans)
    assignment = {"ticker": "1001", "company_name": "Company 1",
                  "search_from": "2026-08-25", "search_to": "2026-08-31"}
    discovered = integration.prepare(assignment, tmp_path).runtime_discovery()
    assert discovered.status == "SAFE_UNKNOWN" and discovered.window.state == "PARTIAL"
    assert discovered.window.not_applicable_families == ("announcements",)
    assert discovered.gaps == ("news: no exact URL discovered from verified root",)
    assert [call[1] for call in repo.calls] == [root, ir]


def test_missing_allowed_hosts_is_control_failure_before_discovery(tmp_path):
    registry, repo = verified_registry(), FakeResolver()
    integration = multi.OfficialResolverIntegration(
        registry, repo, None, {}, {"1001": {"verified_root_url": "https://example.test/",
                                             "allowed_hosts": []}})
    value = integration.prepare({"ticker": "1001", "company_name": "Company 1",
        "search_from": "2026-08-25", "search_to": "2026-08-31"}, tmp_path)
    assert value.status == "CONTROL_FAILURE" and repo.calls == []


def test_official_missing_identity_is_control_failure_without_fetch(tmp_path):
    repo = FakeResolver()
    integration = multi.OfficialResolverIntegration(CompanyIdentityRegistry(), repo, None, {})
    value = integration.prepare({"ticker": "1001", "company_name": "Company 1",
        "search_from": "2026-08-25", "search_to": "2026-08-31"}, tmp_path)
    assert value.status == "CONTROL_FAILURE" and value.window.state == "BLOCKED"
    assert repo.calls == []


def test_ir_nightly_and_tdnet_skip_body_fetch_but_news_uses_non_llm_resolver():
    registry, resolver = verified_registry(), FakeResolver()
    integration = multi.OfficialResolverIntegration(registry, resolver, None, {})
    identity = registry.get("1001")
    nightly = integration.fetch_document(identity, title="2026年決算説明資料",
                                          url="https://example.test/briefing.pdf")
    tdnet = integration.fetch_document(identity, title="第三者割当",
                                       url="https://example.test/tdnet", tdnet_covered=True)
    news = integration.fetch_document(identity, title="価格改定",
                                      url="https://example.test/price")
    assert nightly["scope_label"] == "OUT_OF_SCOPE_IR_NIGHTLY"
    assert tdnet["scope_label"] == "OUT_OF_SCOPE_TDNET"
    assert nightly["http_requests"] == tdnet["http_requests"] == 0
    assert news["provenance_state"] == "BODY_VERIFIED" and len(resolver.calls) == 1


def test_index_only_never_becomes_body_verified_and_delegated_is_exact():
    blocked = FakeResolver(resolution("https://example.test/item", status="BLOCKED"))
    registry = verified_registry()
    integration = multi.OfficialResolverIntegration(registry, blocked, None, {})
    value = integration.fetch_document(registry.get("1001"), title="New product",
                                       url="https://example.test/item")
    assert value["provenance_state"] == "ACCESS_FAILED"
    delegated = FakeResolver()
    result = integration.resolve_delegated(
        resolver=delegated, identity=registry.get("1001"),
        exact_embedded_url="https://provider.test/list.js")
    assert result.access_status == "VERIFIED"
    assert delegated.calls[0][2].max_redirects == 0


def test_canonical_execution_fetches_only_unverified_in_scope_document(tmp_path, monkeypatch):
    company_dir = tmp_path / "run" / "companies" / "01-1001"
    (company_dir / "workers" / "company-web").mkdir(parents=True)
    (company_dir / "evidence-state.json").write_text(json.dumps({
        "search_record_registry": [], "cards": []}))
    (company_dir / "workers" / "company-web" / "last-message.json").write_text("{}")
    response = {"research_result": {"items": [], "sources_checked_count": 1},
                "strategy_telemetry": {
                    "company_identity": {"verified_ticker": "1001", "source_url": "https://example.test/"},
                    "source_provenance": [{"url": "https://example.test/", "source_type": "company_official",
                                           "content_verified": True, "evidence_excerpt": "Company 1 official"}],
                    "documents": [{"title": "価格改定", "url": "https://example.test/price",
                                   "scope_label": "IN_SCOPE_COMPANY_WEB", "body_status": "INACCESSIBLE"}]}}
    monkeypatch.setattr(multi.lightweight, "run_company", lambda *args, **kwargs: (
        response, {"total_tokens": 10, "non_llm": {}}))
    monkeypatch.setattr(multi.lightweight, "apply_coverage_authority",
                        lambda value, *args: (value, {}))
    monkeypatch.setattr(multi.lightweight.strategy_v5, "validate_generated_response", lambda value: value)
    monkeypatch.setattr(multi.lightweight, "validate_payload", lambda value: value)
    monkeypatch.setattr(multi.lightweight.strategy_v5, "validate", lambda *args, **kwargs: True)
    fetches = []
    def fetcher(**kwargs):
        fetches.append(kwargs)
        return {"scope_label": "IN_SCOPE_COMPANY_WEB", "scope_reason": "in scope",
                "body_fetch_required": True, "provenance_state": "BODY_VERIFIED",
                "http_requests": 1, "resolution": resolution(kwargs["url"], method="windows-schannel-v1")}
    window = OfficialWindowCoverage("1001", "2026-08-25", "2026-08-31", "COMPLETE",
                                    ("news", "ir", "announcements"), (), ())
    official = multi.OfficialPreflight("READY", verified_registry(), window, {},
                                      {"http_requests": 3}, (), fetcher)
    assignment = {"assignment_id": "a", "ticker": "1001", "company_name": "Company 1",
                  "search_from": "2026-08-25", "search_to": "2026-08-31",
                  "strategy_version": multi.lightweight.STRATEGY, "position": 1}
    saved, telemetry = multi.execute_lightweight_company(tmp_path, company_dir, assignment, official)
    assert len(fetches) == 1
    assert saved["strategy_telemetry"]["documents"][0]["body_status"] == "VERIFIED"
    states = json.loads((company_dir / "official-document-provenance.json").read_text())
    assert states["states"][0]["state"] == "BODY_VERIFIED"
    assert states["states"][0]["state_history"] == [
        "DISCOVERED_IN_INDEX", "URL_DISCOVERED", "BODY_FETCH_ATTEMPTED", "BODY_VERIFIED"]
    assert telemetry["non_llm"]["http_requests"] == 4


def test_legacy_phase1_fixture_and_production_isolation_remain():
    assert multi.lightweight.COHORT == ("7760", "6444", "4883", "5122")
    source = Path(multi.__file__).read_text(encoding="utf-8")
    assert "data/news_inbox" not in source and "supabase" not in source.lower()
    assert "production_routing\": False" in source
    assert "OfficialSourceResolver" in source and "WindowsSchannelTransport" in source
    assert multi.DOCUMENT_PROVENANCE_STATES == (
        "DISCOVERED_IN_INDEX", "URL_DISCOVERED", "BODY_FETCH_ATTEMPTED",
        "BODY_VERIFIED", "ACCESS_FAILED")


def test_cli_json_is_cp932_safe_and_keeps_native_artifact_encoding(tmp_path):
    artifact = tmp_path / "report.json"
    value = {"company": "神戸製鋼所", "marker": "⼈"}
    artifact.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    before = artifact.read_bytes()
    rendered = multi.render_cli_json(value)
    rendered.encode("cp932")
    assert "\\u" in rendered
    assert artifact.read_bytes() == before


def test_multi_runner_name_normalization_matches_registry_nfkc_contract():
    assert multi._normalized_company_name(" ＳＵＭＣＯ 株式会社 ") == "sumco"
