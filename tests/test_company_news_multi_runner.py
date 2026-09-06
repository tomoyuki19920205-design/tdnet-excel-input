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


class FakeResolver:
    def __init__(self, outcome=None):
        self.outcome = outcome
        self.calls = []

    def resolve(self, identity, url, policy=None):
        self.calls.append((identity, url, policy))
        return self.outcome or resolution(url)


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
            {"http_requests": 0, "response_bytes": 0, "transports": [], "fetch_failures": int(safe)},
            window.gaps)


class FakeExecutor:
    def __init__(self, tokens=100, marker=None):
        self.tokens, self.marker, self.calls = tokens, marker, []

    def __call__(self, root, company_dir, assignment, official):
        self.calls.append(dict(assignment))
        total = self.tokens(assignment) if callable(self.tokens) else self.tokens
        return ({"research_result": {"items": []}, "strategy_telemetry": {}},
                {"total_tokens": total, "worker_count": 3, "web_call_count": 2,
                 "cached_input_tokens": 40, "workers": [{"input_tokens": 50}]})


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
    assert queue["assignments"][0]["total_tokens"] == 0
    assert queue["assignments"][1]["status"] == "COMPLETED"


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


def test_official_missing_identity_is_safe_unknown_without_fetch(tmp_path):
    repo = FakeResolver()
    integration = multi.OfficialResolverIntegration(CompanyIdentityRegistry(), repo, None, {})
    value = integration.prepare({"ticker": "1001", "company_name": "Company 1",
        "search_from": "2026-08-25", "search_to": "2026-08-31"}, tmp_path)
    assert value.status == "SAFE_UNKNOWN" and value.window.state == "BLOCKED"
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
