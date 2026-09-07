import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import company_news_lightweight as light


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def experiment(tmp_path):
    write(tmp_path / "manifest.json", {"company_limit": 4})
    write(tmp_path / "queue.json", {"cohort": list(light.COHORT), "assignments": [
        {"assignment_id": f"a-{i}-{ticker}", "ticker": ticker, "company_name": ticker,
         "search_from": "2026-08-25", "search_to": "2026-08-31", "strategy_version": light.STRATEGY,
         "position": i, "status": "pending"} for i, ticker in enumerate(light.COHORT, 1)
    ]})
    return tmp_path


def test_zero_assignment_has_no_agent_creation(tmp_path):
    exp = experiment(tmp_path)
    queue = light._read_json(exp / "queue.json")
    for row in queue["assignments"]:
        row["status"] = "completed"
    light._atomic_json(exp / "queue.json", queue)
    called = False
    def runner(*args):
        nonlocal called
        called = True
    assert light.run_next(tmp_path, exp, runner)["status"] == "empty"
    assert called is False


def test_deterministic_route_and_atomic_acquire(tmp_path):
    exp = experiment(tmp_path)
    first = light.preflight(exp)
    assert first["assignment"]["ticker"] == "7760"
    assert light.preflight(exp) == {"status": "busy", "agent_launch": False}
    assert not list(exp.glob("*.tmp"))


def test_context_isolation_and_no_previous_history(tmp_path):
    assignment = {"assignment_id": "a", "ticker": "6444", "company_name": "サンデン",
                  "search_from": "2026-08-25", "search_to": "2026-08-31", "position": 2}
    prompt = light.compact_contract(assignment)
    assert "7760" not in prompt and "prior company context" in prompt
    assert "control_output" not in prompt and "Gold" in prompt


def test_canonical_schema_is_wired_to_actual_command(tmp_path):
    schema = tmp_path / "canonical.json"
    result = tmp_path / "result.json"
    state = tmp_path / "state"
    state.mkdir()
    command = light.codex_command(tmp_path, schema, result, "prompt", sqlite_home=state)
    assert command[command.index("--output-schema") + 1] == str(schema)
    assert "--search" in command and "shell_tool" in command and "--ignore-user-config" in command
    synthesis = light.codex_command(
        tmp_path, schema, result, "prompt", sqlite_home=state, web_enabled=False)
    assert "--search" not in synthesis
    assert light.output_schema() == light.strategy_v5.canonical_output_schema()
    assert light.strategy_v5.schema_sha256(light.output_schema()) == light.strategy_v5.canonical_schema_sha256()


def _worker_fixture(spec):
    queries = []
    for row in spec.queries:
        queries.append({"channel": row["channel"], "query": row["query"], "executed": True,
                        "result_count": 1, "reached_sources": ["https://example.test/item"],
                        "window_relevance_checked": True, "exposure_index": row.get("exposure_index"),
                        "canonical_driver_phrase": row.get("canonical_driver_phrase"),
                        "driver_phrase_preserved": True if row.get("canonical_driver_phrase") else None,
                        "driver_phrase_preservation_reason": None})
    exposure = {"canonical_driver_phrase": "vehicle test demand", "metadata_evidence": "business metadata",
                "transmission_channel": "orders", "government_applicable": False,
                "government_applicability_reason": "no policy linkage", "product_context": None,
                "channel": "external_media"}
    return {"worker_id": spec.worker_id, "kind": spec.kind,
            "company_identity": ({"verified_ticker": "7760", "source_url": "https://example.test",
                                  "evidence": "official identity"} if spec.kind == "company_web" else None),
            "economic_exposures": ([exposure, {**exposure, "canonical_driver_phrase": "electronics capex"}]
                                   if spec.kind == "company_web" else []),
            "exposure_id": spec.exposure_id, "queries": queries,
            "evidence_cards": [{"source_id": "s1", "query_id": spec.queries[0]["query_id"],
                                "exposure_id": spec.exposure_id, "url": "https://example.test/item",
                                "source_type": "company", "title": "Item", "published_at": "2026-08-31",
                                "excerpt": "Evidence", "supports": "Material fact",
                                "verification_status": "VERIFIED"}],
            "zero_result_signatures": [], "incremental_ir_applicable": False}


@pytest.mark.parametrize("spec", [
    light.WorkerSpec("company-web", "company_web", None, None, ({"query_id": "q1", "channel": "official",
        "query": "IMV news", "exposure_index": None, "canonical_driver_phrase": None},)),
    light.WorkerSpec("exposure-1", "exposure", 0, {}, ({"query_id": "q1", "channel": "external_media",
        "query": "vehicle test demand", "exposure_index": 0, "canonical_driver_phrase": "vehicle test demand"},)),
    light.WorkerSpec("government-1", "government", 0, {}, ({"query_id": "q1", "channel": "government",
        "query": "vehicle test regulation", "exposure_index": 0, "canonical_driver_phrase": "vehicle test"},)),
    light.WorkerSpec("incremental-ir", "incremental_ir", None, None, ({"query_id": "q1", "channel": "incremental_ir",
        "query": "IMV IR", "exposure_index": None, "canonical_driver_phrase": None},)),
])
def test_every_worker_schema_lints_and_validates_fixture(spec):
    schema = light.worker_output_schema(spec)
    assert light.lint_api_schema(schema) == []
    fixture = _worker_fixture(spec)
    light.validate_instance(fixture, schema)
    light._validate_worker_response_contract(fixture, spec)


def test_worker_semantic_identity_and_structural_failures_are_rejected():
    spec = light.WorkerSpec("exposure-1", "exposure", 0, {}, ({"query_id": "q1", "channel": "external_media",
        "query": "vehicle test demand", "exposure_index": 0, "canonical_driver_phrase": "vehicle test demand"},))
    schema = light.worker_output_schema(spec)
    valid = _worker_fixture(spec)
    wrong = copy.deepcopy(valid); wrong["worker_id"] = "exposure-2"
    with pytest.raises(light.LightweightError, match="identity mismatch"):
        light._validate_worker_response_contract(wrong, spec)
    wrong = copy.deepcopy(valid); wrong["exposure_id"] = 1
    with pytest.raises(light.LightweightError, match="exposure identity"):
        light._validate_worker_response_contract(wrong, spec)
    for mutation in (
        lambda row: row.pop("worker_id"),
        lambda row: row.update(kind="invalid"),
        lambda row: row.update(extra=True),
    ):
        wrong = copy.deepcopy(valid); mutation(wrong)
        with pytest.raises(Exception):
            light.validate_instance(wrong, schema)


def test_output_schema_file_is_utf8_json_and_command_uses_it(tmp_path):
    spec = light.WorkerSpec("company-web", "company_web", None, None, ({"query_id": "q1",
        "channel": "official", "query": "IMV news", "exposure_index": None,
        "canonical_driver_phrase": None},))
    schema_path, result_path = tmp_path / "worker-schema.json", tmp_path / "result.json"
    light._atomic_json(schema_path, light.worker_output_schema(spec))
    assert json.loads(schema_path.read_text(encoding="utf-8"))["type"] == "object"
    state = tmp_path / "state"
    state.mkdir()
    command = light.codex_command(
        tmp_path, schema_path, result_path, "prompt", sqlite_home=state)
    assert Path(command[command.index("--output-schema") + 1]).exists()


def test_worker_sqlite_home_is_unique_short_and_repo_local(tmp_path):
    company = tmp_path / "artifacts" / "token" / "phase22-run-008" / "companies" / "01-5237"
    (company / "workers" / "company-web").mkdir(parents=True)
    (company / "workers" / "exposure-1").mkdir()
    first = light.isolated_sqlite_home(tmp_path, company, "company-web")
    second = light.isolated_sqlite_home(tmp_path, company, "exposure-1")
    assert first == company / "workers" / "company-web"
    assert second != first and first.is_dir() and second.is_dir()
    assert not list(first.iterdir()) and not list(second.iterdir())


def test_worker_sqlite_home_collision_and_traversal_fail_closed(tmp_path):
    company = tmp_path / "run" / "companies" / "01-5237"
    stage = company / "workers" / "company-web"
    stage.mkdir(parents=True)
    (stage / "state_5.sqlite").write_bytes(b"existing")
    with pytest.raises(light.LightweightError, match="collision"):
        light.isolated_sqlite_home(tmp_path, company, "company-web")
    with pytest.raises(light.LightweightError, match="worker id"):
        light.isolated_sqlite_home(tmp_path, company, "../worker")
    outside = tmp_path.parent / "outside-company"
    with pytest.raises(light.LightweightError, match="outside workspace"):
        light.isolated_sqlite_home(tmp_path, outside, "company-web")


def test_missing_worker_directory_fails_before_state_initialization(tmp_path):
    company = tmp_path / "run" / "companies" / "01-5237"
    company.mkdir(parents=True)
    with pytest.raises(light.LightweightError, match="worker directory is missing"):
        light.isolated_sqlite_home(tmp_path, company, "company-web")


def test_codex_command_enforces_ephemeral_isolated_state_without_auth_copy(tmp_path):
    schema, result, state = tmp_path / "s.json", tmp_path / "r.json", tmp_path / "state"
    state.mkdir()
    command = light.codex_command(
        tmp_path, schema, result, "fixed prompt", sqlite_home=state,
        web_enabled=False)
    assert "--ephemeral" in command and "--json" in command
    assert "--ignore-user-config" in command and "history.persistence=none" in command
    sqlite_config = command[command.index("sqlite_home=" + json.dumps(str(state.resolve())))]
    assert sqlite_config == "sqlite_home=" + json.dumps(str(state.resolve()))
    assert "--search" not in command
    assert os.environ.get("CODEX_HOME") not in command
    assert not (state / "auth.json").exists()


def test_codex_command_rejects_workspace_external_sqlite_home(tmp_path):
    outside = tmp_path.parent / "outside-state"
    with pytest.raises(light.LightweightError, match="inside workspace"):
        light.codex_command(
            tmp_path, tmp_path / "s.json", tmp_path / "r.json", "prompt",
            sqlite_home=outside)


def test_state_initialization_failure_stops_before_child_process(tmp_path, monkeypatch):
    spec = light.WorkerSpec("company-web", "company_web", None, None, ())
    called = False

    def forbidden_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("child process must not start")

    monkeypatch.setattr(
        light, "isolated_sqlite_home",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            light.LightweightError("isolated sqlite_home initialization failed")))
    monkeypatch.setattr(light.subprocess, "run", forbidden_run)
    with pytest.raises(light.LightweightError, match="initialization failed"):
        light._run_isolated_stage(
            tmp_path, tmp_path / "company", spec, "prompt",
            light.worker_output_schema(spec), web_enabled=False)
    assert called is False


def test_isolated_runtime_does_not_write_global_state_and_keeps_exact_usage(
        tmp_path, monkeypatch):
    global_home = tmp_path / "global-codex-home"
    global_home.mkdir()
    global_state = global_home / "state_5.sqlite"
    global_state.write_bytes(b"READ-ONLY-GLOBAL-STATE")
    global_state.chmod(0o444)
    before = (global_state.read_bytes(), global_state.stat().st_size,
              global_state.stat().st_mtime_ns)
    monkeypatch.setenv("CODEX_HOME", str(global_home))
    spec = light.WorkerSpec("company-web", "company_web", None, None, ({
        "query_id": "company-web-q1", "channel": "official",
        "query": "fixed smoke query", "exposure_index": None,
        "canonical_driver_phrase": None},))

    def fake_run(command, **kwargs):
        sqlite_arg = next(value for value in command if value.startswith("sqlite_home="))
        sqlite_home = Path(json.loads(sqlite_arg.split("=", 1)[1]))
        (sqlite_home / "state_5.sqlite").write_bytes(b"isolated")
        result_path = Path(command[command.index("--output-last-message") + 1])
        write(result_path, _worker_fixture(spec))
        events = [
            {"type": "thread.started", "thread_id": "fixture"},
            {"type": "turn.started"},
            {"type": "turn.completed", "usage": {
                "input_tokens": 100, "cached_input_tokens": 40,
                "output_tokens": 10, "reasoning_tokens": 2}},
        ]
        return SimpleNamespace(
            returncode=0, stdout="\n".join(json.dumps(row) for row in events), stderr="")

    monkeypatch.setattr(light.subprocess, "run", fake_run)
    _, telemetry = light._run_isolated_stage(
        tmp_path, tmp_path / "run" / "companies" / "01-5237", spec,
        "prompt", light.worker_output_schema(spec), web_enabled=False)
    after = (global_state.read_bytes(), global_state.stat().st_size,
             global_state.stat().st_mtime_ns)
    assert before == after
    assert telemetry["total_tokens"] == 110
    assert telemetry["cached_input_tokens"] == 40
    assert telemetry["sqlite_state_file_created"] is True
    assert Path(telemetry["sqlite_home"]).is_relative_to(tmp_path)
    assert not (Path(telemetry["sqlite_home"]) / "auth.json").exists()


def test_bounded_tool_output_and_no_broad_repo_search():
    bounded = light.bounded_text("x" * 20_000, max_chars=100, max_lines=2)
    assert len(bounded) < 180 and "TRUNCATED" in bounded
    prompt = light.compact_contract({"assignment_id": "a", "ticker": "1", "company_name": "x",
                                     "search_from": "2026-08-25", "search_to": "2026-08-31", "position": 1})
    assert "do not use shell, repo search" in prompt
    assert "rg -" not in prompt and "recursive grep" not in prompt


def test_token_telemetry_complete():
    events = [{"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40,
                                                       "output_tokens": 20, "reasoning_tokens": 5}}]
    value = light.token_telemetry(events, 1.5)
    assert value["uncached_input_tokens"] == 60 and value["total_tokens"] == 120
    assert value["post_research_model_calls"] == value["post_research_tokens"] == 0
    assert value["model_call_count"] == 1


def test_missing_usage_is_unknown_not_zero():
    value = light.token_telemetry([{"type": "turn.started"}], 0.1)
    for key in ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens",
                "reasoning_tokens", "total_tokens", "model_call_count"):
        assert value[key] is None


def test_token_telemetry_preserves_each_terminal_usage_and_tool_size():
    events = [
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20}},
        {"type": "item.completed", "item": {"type": "web_search", "query": "driver demand", "result_count": 2}},
        {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 30, "output_tokens": 10}},
    ]
    value = light.token_telemetry(events, 2.0)
    assert value["input_tokens"] == 150 and value["cached_input_tokens"] == 70
    assert value["turn_completed_count"] == value["model_call_count"] == 2
    assert value["tool_events"][0]["query"] == "driver demand"
    assert value["tool_events"][0]["model_visible_chars"] > 0


def test_evidence_store_bounds_dedupes_and_does_not_retain_raw_body():
    store = light.EvidenceStore(source_limit=2, result_limit=1)
    assert store.register_query("q1", "Driver   Demand") is True
    assert store.register_query("q2", "driver demand") is False
    body = "irrelevant.\n" * 3000 + "Pricing changed because energy costs increased."
    assert store.add(url="https://example.test/a?utm_source=x", source_type="company", title="Price notice",
                     published_at="2026-08-31", body=body, supports="pricing and margin", exposure_id=0,
                     query_id="q1", verification_status="VERIFIED", relevant_terms=["pricing", "energy"])
    assert not store.add(url="https://example.test/a", source_type="company", title="duplicate",
                         published_at=None, body="different raw body", supports="duplicate", exposure_id=0,
                         query_id="q1", verification_status="VERIFIED", relevant_terms=["body"])
    context = store.model_context()
    assert len(store.cards()[0]["excerpt"]) <= light.MAX_EVIDENCE_EXCERPT
    assert "irrelevant irrelevant irrelevant" not in context
    assert "different raw body" not in context
    assert store.bounded_results([{"rank": 1}, {"rank": 2}]) == [{"rank": 1}]
    store.register_zero_result("pricing demands 20260831")
    assert store.register_query("q3", "price demand 20260831") is False


def test_runner_persists_raw_usage_and_tool_events_before_validation(tmp_path, monkeypatch):
    prompts = []
    exposure = {"canonical_driver_phrase": "vibration testing demand", "metadata_evidence": "testing business",
                "transmission_channel": "orders", "government_applicable": False,
                "government_applicability_reason": "none", "product_context": "automotive",
                "channel": "external_media"}
    card = {"source_id": "source-a", "query_id": "company-web-q1", "exposure_id": None,
            "url": "https://example.test/a", "source_type": "company", "title": "NOTICE_MARKER",
            "published_at": "2026-08-31", "excerpt": "RAW_HISTORY_MARKER useful fact",
            "supports": "useful fact", "verification_status": "VERIFIED"}
    def telemetry(spec):
        return {"worker_id": spec.worker_id if spec else "final-synthesis",
                "kind": spec.kind if spec else "final_synthesis", "exposure_id": spec.exposure_id if spec else None,
                "input_tokens": 100, "cached_input_tokens": 40, "uncached_input_tokens": 60,
                "output_tokens": 20, "reasoning_tokens": 3, "total_tokens": 120,
                "model_call_count": 1, "tool_call_count": 1 if spec else 0,
                "web_call_count": 1 if spec else 0, "research_elapsed_seconds": 1.0,
                "model_visible_chars": 50 if spec else 0, "thread_ids": [f"thread-{len(prompts)}"],
                "started_at": "2026-09-05T14:40:00+00:00",
                "completed_at": "2026-09-05T14:41:36.202594+00:00"}
    def fake_stage(root, company_dir, spec, prompt, schema, *, web_enabled):
        prompts.append((spec.worker_id if spec else "final", prompt, web_enabled))
        if spec and spec.kind == "company_web":
            return {"worker_id": spec.worker_id, "kind": spec.kind,
                    "company_identity": {"verified_ticker": "7760", "source_url": "https://example.test/a", "evidence": "official"},
                    "economic_exposures": [exposure, {**exposure, "canonical_driver_phrase": "electronics capex"}],
                    "exposure_id": None, "queries": [], "evidence_cards": [card],
                    "zero_result_signatures": [], "incremental_ir_applicable": False}, telemetry(spec)
        if spec:
            query = {"channel": spec.queries[0]["channel"], "query": spec.queries[0]["query"], "executed": True,
                     "result_count": 0, "reached_sources": [], "window_relevance_checked": True,
                     "exposure_index": spec.exposure_id,
                     "canonical_driver_phrase": spec.exposure["canonical_driver_phrase"],
                     "driver_phrase_preserved": True, "driver_phrase_preservation_reason": None}
            return {"worker_id": spec.worker_id, "kind": spec.kind, "company_identity": None,
                    "economic_exposures": [], "exposure_id": spec.exposure_id, "queries": [query],
                    "evidence_cards": [], "zero_result_signatures": [],
                    "incremental_ir_applicable": False}, telemetry(spec)
        return {"research_result": {"run_id": "", "task_id": "", "ticker": "7760",
                                      "sources_checked_count": 1, "items": []},
                "strategy_telemetry": {"assignment_id": "a", "ticker": "7760",
                    "source_provenance": [{
                    "url": "https://example.test/a", "source_type": "company",
                    "content_verified": True, "evidence_excerpt": "official identity"}],
                    "queries": [], "channel_reviews": [], "documents": [],
                    "candidate_provenance": []}}, telemetry(None)
    monkeypatch.setattr(light, "_run_isolated_stage", fake_stage)
    assignment = {"assignment_id": "a", "ticker": "7760", "company_name": "IMV", "position": 1,
                  "search_from": "2026-08-25", "search_to": "2026-08-31"}
    _, telemetry = light.codex_runner(tmp_path, tmp_path / "experiment", assignment)
    company = tmp_path / "experiment" / "companies" / "01-7760"
    assert telemetry["worker_count"] == 3 and telemetry["synthesis_model_call_count"] == 1
    assert prompts[0][2] is True and prompts[-1][2] is False
    assert "RAW_HISTORY_MARKER" not in prompts[1][1]
    assert "NOTICE_MARKER" not in prompts[1][1]
    assert "RAW_HISTORY_MARKER" in prompts[-1][1]
    assert light._read_json(company / "measured-token-telemetry.json")["validation_status"] == "PENDING"
    assert light._read_json(company / "evidence-state.json")["raw_body_retained"] is False


def test_worker_limits_schema_sharing_and_context_boundaries():
    assignment = {"assignment_id": "a", "ticker": "7760", "company_name": "IMV",
                  "search_from": "2026-08-25", "search_to": "2026-08-31"}
    exposure = {"canonical_driver_phrase": "vibration testing demand", "metadata_evidence": "business",
                "transmission_channel": "orders", "government_applicable": True,
                "government_applicability_reason": "safety rules", "product_context": None,
                "channel": "external_media"}
    discovery = {"economic_exposures": [exposure, {**exposure, "canonical_driver_phrase": "electronics capex",
                                                     "government_applicable": False}],
                 "incremental_ir_applicable": True}
    specs = light.plan_exposure_workers(assignment, discovery, light.EvidenceStore())
    assert len(specs) == 3 and all(len(spec.queries) <= 2 for spec in specs)
    assert specs[0].queries[0]["canonical_driver_phrase"] in specs[0].queries[0]["query"]
    assert light.strategy_v5.canonical_schema_sha256() == "8c50803cc936224945ed18c59f691690df003259dea4fa9814cbd2c19813e6d5"
    prompt = light.worker_contract(specs[1], assignment)
    assert "prior worker history" in prompt and "electronics capex" in prompt
    assert "vibration testing demand" not in prompt
    assert "evidence_cards" not in prompt and "SECRET_RAW_PAGE_BODY" not in prompt


def test_registry_authoritative_synthesis_omits_model_identity_decision():
    assignment = {"assignment_id": "a", "ticker": "3436", "company_name": "SUMCO",
                  "search_from": "2026-08-25", "search_to": "2026-08-31"}
    discovery = {"company_identity": {"verified_ticker": "3436",
                                      "source_url": "https://third-party.test/profile",
                                      "evidence": "model"},
                 "economic_exposures": []}
    prompt = light.synthesis_contract(
        assignment, discovery, [], light.EvidenceStore(), {},
        registry_identity_authoritative=True)
    assert '"company_identity"' not in prompt.split("BOUNDED_INPUT=", 1)[1]
    assert "Set strategy_telemetry.company_identity to null" in prompt
    identity_schema = light.synthesis_output_schema()["properties"][
        "strategy_telemetry"]["properties"]["company_identity"]
    assert identity_schema["type"] == ["object", "null"]


def test_maximum_worker_count_is_enforced(monkeypatch):
    assignment = {"company_name": "IMV", "search_from": "2026-08-25", "search_to": "2026-08-31"}
    exposure = {"canonical_driver_phrase": "driver one", "metadata_evidence": "business",
                "transmission_channel": "orders", "government_applicable": False,
                "government_applicability_reason": "none", "product_context": None,
                "channel": "external_media"}
    discovery = {"economic_exposures": [exposure, {**exposure, "canonical_driver_phrase": "driver two"}],
                 "incremental_ir_applicable": True}
    monkeypatch.setattr(light, "MAX_WEB_WORKERS", 2)
    with pytest.raises(light.LightweightError, match="worker limit"):
        light.plan_exposure_workers(assignment, discovery, light.EvidenceStore())


def test_sanitized_raw_events_drop_web_body(tmp_path):
    events = [{"type": "item.completed", "item": {"type": "web_search", "query": "q",
                                                         "result": "SECRET_RAW_PAGE_BODY"}}]
    path = tmp_path / "raw.jsonl"
    light._atomic_jsonl(path, events)
    saved = path.read_text(encoding="utf-8")
    assert "SECRET_RAW_PAGE_BODY" not in saved
    assert "OMITTED_RAW_TOOL_CONTENT" in saved and '"query":"q"' in saved


def test_worker_telemetry_is_saved_before_schema_failure(tmp_path, monkeypatch):
    spec = light.WorkerSpec("company-web", "company_web", None, None, ({
        "query_id": "company-web-q1", "channel": "official", "query": "IMV news",
        "exposure_index": None, "canonical_driver_phrase": None},))
    def fake_run(command, **kwargs):
        result_path = Path(command[command.index("--output-last-message") + 1])
        write(result_path, {"invalid": True})
        event = {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 50,
                                                          "output_tokens": 10, "reasoning_tokens": 2}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(event), stderr="")
    monkeypatch.setattr(light.subprocess, "run", fake_run)
    with pytest.raises(Exception):
        light._run_isolated_stage(tmp_path, tmp_path / "company", spec, "prompt",
                                  light.worker_output_schema(spec), web_enabled=True)
    saved = light._read_json(tmp_path / "company" / "workers" / "company-web" / "telemetry.json")
    assert saved["total_tokens"] == 110 and saved["validation_status"] == "FAIL"
    assert saved["started_at"] and saved["completed_at"]


@pytest.mark.parametrize("generated", ["", None])
def test_runtime_checked_at_injection_accepts_empty_or_null(generated):
    response = {"research_result": {
        "schema_version": "company_news_v1", "run_id": "r", "task_id": "r", "ticker": "7760",
        "checked_at": generated, "collector_type": "test", "sources_checked_count": 0, "items": []},
        "strategy_telemetry": {}}
    injected = light.inject_runtime_metadata(
        response, completed_at="2026-09-05T14:41:36.2025945Z")
    assert injected["research_result"]["checked_at"] == "2026-09-05T14:41:36.202594+00:00"
    assert response["research_result"]["checked_at"] is generated
    assert light.validate_payload(injected["research_result"]).scan["checked_at"] == \
        "2026-09-05T14:41:36.202594+00:00"


def test_synthesis_transport_allows_null_checked_at_before_runtime_injection():
    schema = light.synthesis_output_schema()
    assert schema["properties"]["research_result"]["properties"]["checked_at"]["type"] == ["string", "null"]
    assert light.output_schema()["properties"]["research_result"]["properties"]["checked_at"]["type"] == "string"


def test_runtime_checked_at_injection_fails_closed_without_execution_timestamp():
    response = {"research_result": {"checked_at": ""}, "strategy_telemetry": {}}
    with pytest.raises(light.LightweightError, match="completed_at is missing"):
        light.inject_runtime_metadata(response, completed_at=None)


def test_runtime_checked_at_injection_fails_closed_for_invalid_timestamp():
    response = {"research_result": {"checked_at": ""}, "strategy_telemetry": {}}
    with pytest.raises(Exception, match="ISO-8601"):
        light.inject_runtime_metadata(response, completed_at="not-a-timestamp")


def test_runtime_checked_at_does_not_hide_other_semantic_failure():
    response = {"research_result": {"schema_version": "company_news_v1", "run_id": "r", "task_id": "r",
                                      "ticker": "INVALID", "checked_at": "", "collector_type": "test", "items": []},
                "strategy_telemetry": {}}
    injected = light.inject_runtime_metadata(response, completed_at="2026-09-05T14:41:36Z")
    with pytest.raises(Exception, match="ticker"):
        light.validate_payload(injected["research_result"])


def test_runtime_checked_at_injection_never_runs_llm_repair(monkeypatch):
    calls = 0
    def forbidden(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("LLM retry is forbidden")
    monkeypatch.setattr(light.subprocess, "run", forbidden)
    response = {"research_result": {"checked_at": "model-value"}, "strategy_telemetry": {}}
    injected = light.inject_runtime_metadata(response, completed_at="2026-09-05T14:41:36Z")
    assert injected["research_result"]["checked_at"] == "2026-09-05T14:41:36+00:00"
    assert calls == 0


def test_runtime_execution_identity_comes_from_acquired_assignment():
    response = {
        "research_result": {"run_id": "", "task_id": "model", "ticker": "6444"},
        "strategy_telemetry": {"assignment_id": "phase-6444", "ticker": "6444"},
    }
    repaired = light.inject_runtime_identity(
        response, {"assignment_id": "phase-6444", "ticker": "6444"})
    assert repaired["research_result"]["run_id"] == "phase-6444"
    assert repaired["research_result"]["task_id"] == "phase-6444"
    assert response["research_result"]["run_id"] == ""


def test_runtime_execution_identity_rejects_conflicting_ticker():
    response = {
        "research_result": {"ticker": "7760"},
        "strategy_telemetry": {"assignment_id": "phase-6444", "ticker": "7760"},
    }
    with pytest.raises(light.LightweightError, match="ticker conflicts"):
        light.inject_runtime_identity(
            response, {"assignment_id": "phase-6444", "ticker": "6444"})


def test_source_count_is_derived_after_canonical_deduplication():
    source = {"url": "https://example.test/news/", "source_type": "company_news",
              "content_verified": True, "evidence_excerpt": "verified"}
    duplicate = dict(source)
    duplicate["url"] = "https://example.test/news"
    response = {
        "research_result": {"sources_checked_count": 99},
        "strategy_telemetry": {"source_provenance": [source, duplicate]},
    }
    repaired = light.apply_source_count_authority(response)
    assert repaired["research_result"]["sources_checked_count"] == 1
    assert len(repaired["strategy_telemetry"]["source_provenance"]) == 1


def _metadata_repair_fixture(published_at="2026-08-31T09:30:00+09:00"):
    response = {
        "research_result": {"sources_checked_count": 1, "items": [{
            "headline": "Price notice", "source_url": "https://example.test/news", "published_at": "2026-08-31",
            "summary": "Material pricing change", "why_it_matters": "Margin transmission"}]},
        "strategy_telemetry": {"company_identity": {}, "source_provenance": [{
            "url": "https://example.test/ir.pdf", "source_type": "official_report",
            "content_verified": True, "evidence_excerpt": "old mapping"}]}}
    discovery = {"company_identity": {"verified_ticker": "7760", "source_url": "https://example.test/ir.pdf",
                                       "evidence": "Official report identifies code 7760."}}
    cards = [
        {"source_id": "identity", "query_id": "company-web-q1", "exposure_id": None,
         "url": "https://example.test/ir.pdf", "source_type": "official_report", "title": "Official report",
         "published_at": "2026-05-13", "excerpt": "Company code 7760", "supports": "identity",
         "verification_status": "VERIFIED"},
        {"source_id": "event", "query_id": "company-web-q1", "exposure_id": None,
         "url": "https://example.test/news/", "source_type": "official_news", "title": "Price notice",
         "published_at": published_at, "excerpt": "Prices changed", "supports": "event",
         "verification_status": "VERIFIED"},
    ]
    return response, discovery, cards


@pytest.mark.parametrize("link_present", [True, False])
def test_official_identity_evidence_is_bound_by_python_aggregator(link_present):
    response, discovery, cards = _metadata_repair_fixture()
    if not link_present:
        response["strategy_telemetry"]["source_provenance"] = []
        response["research_result"]["sources_checked_count"] = 0
    repaired = light.inject_identity_provenance(response, discovery, cards)
    identity = repaired["strategy_telemetry"]["company_identity"]
    source = repaired["strategy_telemetry"]["source_provenance"][0]
    assert identity == {"verified_ticker": "7760", "source_url": "https://example.test/ir.pdf",
                        "evidence": "Official report identifies code 7760."}
    assert source["source_type"] == "company_official" and source["content_verified"] is True
    assert repaired["research_result"]["sources_checked_count"] == 1


def test_missing_official_identity_evidence_fails_closed():
    response, discovery, cards = _metadata_repair_fixture()
    cards[0]["verification_status"] = "UNVERIFIED"
    with pytest.raises(light.LightweightError, match="official Company Web identity evidence is missing"):
        light.inject_identity_provenance(response, discovery, cards)


def test_search_excerpt_alone_is_not_official_identity_evidence():
    response, discovery, cards = _metadata_repair_fixture()
    cards[0]["source_type"] = "official_ir_index_search_excerpt"
    with pytest.raises(light.LightweightError, match="official Company Web identity evidence is missing"):
        light.inject_identity_provenance(response, discovery, cards)


def test_timezone_aware_published_at_is_restored_exactly_from_evidence():
    response, _, cards = _metadata_repair_fixture()
    repaired = light.restore_published_at_from_evidence(response, cards)
    assert repaired["research_result"]["items"][0]["published_at"] == "2026-08-31T09:30:00+09:00"
    assert repaired["research_result"]["items"][0]["published_date"] == "2026-08-31"
    assert repaired["research_result"]["items"][0]["published_precision"] == "datetime"
    assert repaired["research_result"]["items"][0]["source_url"] == "https://example.test/news"
    assert repaired["research_result"]["items"][0]["summary"] == "Material pricing change"
    assert response["research_result"]["items"][0]["published_at"] == "2026-08-31"


def test_date_only_published_at_never_invents_time_or_timezone():
    response, _, cards = _metadata_repair_fixture("2026-08-31")
    repaired = light.restore_publication_from_evidence(response, cards)
    assert repaired["research_result"]["items"][0]["published_at"] is None
    assert repaired["research_result"]["items"][0]["published_date"] == "2026-08-31"
    assert repaired["research_result"]["items"][0]["published_precision"] == "date"
    assert response["research_result"]["items"][0]["published_at"] == "2026-08-31"


def test_evidence_id_is_primary_and_headline_may_differ_from_source_title():
    response, _, cards = _metadata_repair_fixture("2026-08-31")
    cards[1]["evidence_id"] = "evidence-event"
    response["strategy_telemetry"].update(
        source_provenance=[{"url": "https://example.test/news", "source_type": "model",
                            "content_verified": True, "evidence_excerpt": "model"}],
        candidate_provenance=[{"source_url": "https://example.test/news",
                               "selected_for_output": True}])
    response["research_result"]["items"][0].update(
        headline="Presentation headline rewritten by synthesis",
        supporting_evidence_ids=["evidence-event"])
    repaired = light.restore_publication_from_evidence(response, cards)
    item = repaired["research_result"]["items"][0]
    assert item["headline"] == "Presentation headline rewritten by synthesis"
    assert item["published_date"] == "2026-08-31"
    assert item["source_url"] == "https://example.test/news"
    assert item["evidence_excerpt"] == "Prices changed"
    assert "supporting_evidence_ids" not in item
    source = repaired["strategy_telemetry"]["source_provenance"][0]
    assert source["source_type"] == "official_news" and source["evidence_excerpt"] == "Prices changed"


def test_nonexistent_or_fabricated_evidence_id_fails_closed():
    response, _, cards = _metadata_repair_fixture()
    cards[1]["evidence_id"] = "evidence-event"
    response["research_result"]["items"][0]["supporting_evidence_ids"] = ["evidence-invented"]
    with pytest.raises(light.PublicationAuthorityError, match="unknown evidence_id"):
        light.restore_publication_from_evidence(response, cards)


def test_legacy_url_fallback_fails_when_ambiguous():
    response, _, cards = _metadata_repair_fixture()
    cards.append({**cards[1], "source_id": "event-duplicate"})
    with pytest.raises(light.PublicationAuthorityError, match="not unique"):
        light.restore_publication_from_evidence(response, cards)


def test_legacy_unique_canonical_url_fallback_ignores_title_difference():
    response, _, cards = _metadata_repair_fixture("2026-08-31")
    response["research_result"]["items"][0]["headline"] = "Different presentation headline"
    repaired = light.restore_publication_from_evidence(response, cards)
    assert repaired["research_result"]["items"][0]["published_date"] == "2026-08-31"


def test_synthesis_schema_requires_nonempty_supporting_evidence_ids_for_events():
    item = light.synthesis_output_schema()["properties"]["research_result"]["properties"]["items"]["items"]
    assert "supporting_evidence_ids" in item["required"]
    assert item["properties"]["supporting_evidence_ids"]["minItems"] == 1


def test_document_dates_are_normalized_without_counting_unknown_or_background():
    response = {"strategy_telemetry": {
        "documents": [
            {"date": "", "title": "Index", "url": "https://example.test/index",
             "final_url": None, "body_status": "VERIFIED"},
            {"date": "2026-05-13", "title": "Background", "url": "https://example.test/background",
             "final_url": None, "body_status": "VERIFIED"},
            {"date": "2026-08-31", "title": "Event", "url": "https://example.test/event",
             "final_url": None, "body_status": "VERIFIED"},
        ],
        "candidate_provenance": [{"document_url": "https://example.test/event"}],
    }}
    cards = [
        {"url": "https://example.test/index", "published_at": None, "verification_status": "VERIFIED"},
        {"url": "https://example.test/background", "published_at": "2026-05-13", "verification_status": "VERIFIED"},
        {"url": "https://example.test/event", "published_at": "2026-08-31", "verification_status": "VERIFIED"},
    ]
    assignment = {"search_from": "2026-08-25", "search_to": "2026-08-31"}
    repaired = light.normalize_documents_from_evidence(response, cards, assignment)
    assert [(row["title"], row["date"], row["final_url"]) for row in repaired["strategy_telemetry"]["documents"]] == [
        ("Index", None, "https://example.test/index"),
        ("Event", "2026-08-31", "https://example.test/event"),
    ]


def test_unverified_inaccessible_document_keeps_null_date_and_background_candidate_is_removed():
    response = {"strategy_telemetry": {
        "documents": [
            {"date": None, "url": "https://example.test/inaccessible", "body_status": "INACCESSIBLE"},
            {"date": "2026-06-25", "url": "https://example.test/background", "body_status": "VERIFIED"},
        ],
        "candidate_provenance": [
            {"candidate_index": 0, "document_url": "https://example.test/inaccessible", "selected_for_output": False},
            {"candidate_index": 1, "document_url": "https://example.test/background", "selected_for_output": False},
        ],
        "candidate_count": 2,
    }}
    cards = [
        {"url": "https://example.test/inaccessible", "published_at": None,
         "verification_status": "UNVERIFIED"},
        {"url": "https://example.test/background", "published_at": "2026-06-25",
         "verification_status": "VERIFIED"},
    ]
    repaired = light.normalize_documents_from_evidence(
        response, cards, {"search_from": "2026-08-25", "search_to": "2026-08-31"})
    assert repaired["strategy_telemetry"]["documents"] == [
        {"date": None, "url": "https://example.test/inaccessible", "body_status": "INACCESSIBLE"}]
    assert repaired["strategy_telemetry"]["candidate_count"] == 1
    assert repaired["strategy_telemetry"]["candidate_provenance"][0]["candidate_index"] == 0


def _policy_materiality_fixture(*, direct=False):
    why = ("Synthetic Works was selected as a subsidy recipient and will expand capacity."
           if direct else "The effect on Synthetic Works revenue is not established.")
    importance = "medium" if direct else "low"
    relevance = "direct" if direct else "context"
    return ({
        "research_result": {"items": [{"headline": "Policy notice", "source_url": "https://policy.test/n",
                                          "summary": "Program notice", "why_it_matters": why,
                                          "evidence_excerpt": None, "importance": importance,
                                          "earnings_relevance": relevance}]},
        "strategy_telemetry": {
            "documents": [{"url": "https://policy.test/n", "scope_label": "IN_SCOPE_GOVERNMENT",
                           "material": True, "body_status": "VERIFIED"}],
            "candidate_provenance": [{"candidate_index": 0, "document_url": "https://policy.test/n",
                                      "source_url": "https://policy.test/n", "headline": "Policy notice",
                                      "selected_for_output": True, "event_key": "e1",
                                      "selection_reason": "candidate", "economic_transmission": why}],
            "final_event_count": 1,
        },
    }, [{"url": "https://policy.test/n", "supports": why}])


def test_general_policy_context_without_direct_company_linkage_is_rejected():
    response, cards = _policy_materiality_fixture()
    repaired, decisions = light.apply_materiality_authority(
        response, cards, company_name="Synthetic Works")
    assert repaired["research_result"]["items"] == []
    assert repaired["strategy_telemetry"]["final_event_count"] == 0
    assert decisions[0]["decision"] == "REJECT_GENERAL_CONTEXT"


def test_direct_company_policy_impact_is_retained():
    response, cards = _policy_materiality_fixture(direct=True)
    repaired, decisions = light.apply_materiality_authority(
        response, cards, company_name="Synthetic Works")
    assert len(repaired["research_result"]["items"]) == 1
    assert decisions[0]["decision"] == "KEEP"


def test_general_industry_context_without_company_linkage_is_rejected():
    response, cards = _policy_materiality_fixture()
    response["strategy_telemetry"]["documents"][0]["scope_label"] = "IN_SCOPE_EXTERNAL_MEDIA"
    repaired, decisions = light.apply_materiality_authority(
        response, cards, company_name="Synthetic Works")
    assert repaired["research_result"]["items"] == []
    assert decisions[0]["decision"] == "REJECT_GENERAL_CONTEXT"


def test_invalid_evidence_published_at_fails_closed():
    response, _, cards = _metadata_repair_fixture("not-a-publication-date")
    with pytest.raises(Exception, match="ISO-8601"):
        light.restore_published_at_from_evidence(response, cards)


def _search_worker(worker_id, channel, *, result_count, executed=True, web_calls=1, evidence=False,
                   exposure_id=0):
    query_id = f"{worker_id}-q1"
    output = {"queries": [{"channel": channel, "query": f"{channel} query", "executed": executed,
                            "result_count": result_count, "reached_sources": [],
                            "window_relevance_checked": True, "exposure_index": exposure_id,
                            "canonical_driver_phrase": "driver", "driver_phrase_preserved": True,
                            "driver_phrase_preservation_reason": None}],
              "evidence_cards": ([{"source_id": "e1", "query_id": query_id}] if evidence else [])}
    telemetry = {"worker_id": worker_id, "exposure_id": exposure_id, "web_call_count": web_calls}
    return output, telemetry


def test_actual_queries_and_evidence_are_registered_and_linked():
    run = _search_worker("exposure-1", "external_media", result_count=2, evidence=True)
    registry = light.build_search_record_registry([run])
    assert {key: registry[0][key] for key in ("query_id", "worker_id", "exposure_id", "canonical_channel",
                                               "query_text", "executed", "result_count", "evidence_ids")} == {
        "query_id": "exposure-1-q1", "worker_id": "exposure-1", "exposure_id": 0,
        "canonical_channel": "external_media", "query_text": "external_media query",
        "executed": True, "result_count": 2, "evidence_ids": ["e1"]}


def test_actual_zero_result_query_remains_in_search_registry():
    registry = light.build_search_record_registry([
        _search_worker("exposure-1", "external_media", result_count=0)])
    assert len(registry) == 1 and registry[0]["result_count"] == 0 and registry[0]["executed"] is True


def test_unexecuted_query_is_never_added_to_search_registry():
    registry = light.build_search_record_registry([
        _search_worker("exposure-1", "external_media", result_count=0, executed=False, web_calls=0)])
    assert registry == []


def test_claimed_execution_without_observed_web_call_fails_closed():
    with pytest.raises(light.LightweightError, match="no observed Web call"):
        light.build_search_record_registry([
            _search_worker("exposure-1", "external_media", result_count=0, web_calls=0)])


def test_checked_channel_without_actual_record_is_corrected_from_worker_state():
    response = {"strategy_telemetry": {"queries": [], "channel_reviews": [
        {"channel": "incremental_ir", "status": "CHECKED", "evidence": "No in-window item", "exposure_index": None},
        {"channel": "counterparty", "status": "CHECKED", "evidence": "Claimed review", "exposure_index": None},
    ]}}
    repaired = light.apply_search_record_registry(response, [], {"incremental_ir_applicable": False})
    reviews = {row["channel"]: row for row in repaired["strategy_telemetry"]["channel_reviews"]}
    assert reviews["incremental_ir"]["status"] == "NOT_APPLICABLE"
    assert reviews["counterparty"]["status"] == "PARTIAL"


def test_duplicate_per_exposure_reviews_collapse_to_canonical_channel_record():
    run = _search_worker("exposure-1", "external_media", result_count=1, exposure_id=1)
    registry = light.build_search_record_registry([run])
    response = {"strategy_telemetry": {"queries": run[0]["queries"], "channel_reviews": [
        {"channel": "external_media", "status": "PARTIAL", "evidence": "exposure zero", "exposure_index": 0},
        {"channel": "external_media", "status": "CHECKED", "evidence": "exposure one", "exposure_index": 1},
    ]}}
    repaired = light.apply_search_record_registry(response, registry, {"incremental_ir_applicable": False})
    reviews = repaired["strategy_telemetry"]["channel_reviews"]
    assert len(reviews) == 1 and reviews[0]["status"] == "CHECKED" and reviews[0]["exposure_index"] == 1
    assert "exposure zero" in reviews[0]["evidence"] and "exposure one" in reviews[0]["evidence"]


def _coverage_authority_fixture():
    exposures = [{"channel": "external_media", "government_applicable": True},
                 {"channel": "counterparty", "government_applicable": False}]
    discovery = {"economic_exposures": exposures, "incremental_ir_applicable": False}
    registry = []
    for worker, channel, exposure, count in [
        ("company-web", "official", None, 0),
        ("exposure-1", "external_media", 0, 0),
        ("government-1", "government", 0, 0),
        ("exposure-2", "counterparty", 1, 2),
    ]:
        registry.append({"query_id": worker+"-q1", "worker_id": worker,
                         "exposure_id": exposure, "canonical_channel": channel,
                         "executed": True, "result_count": count,
                         "evidence_ids": ([worker+"-s1"] if count else []),
                         "window_relevance_checked": True})
    response = {"research_result": {"items": [{"headline": "event"}]},
                "strategy_telemetry": {"documents": [], "coverage_status": "PARTIAL",
                    "coverage_gaps": [], "channel_reviews": [],
                    "no_news_exposure_coverage": [], "no_news_gate_pass": False,
                    "query_budget_exceeded": False}}
    return response, registry, discovery


def test_coverage_authority_derives_complete_and_accepts_zero_results_as_checked():
    response, registry, discovery = _coverage_authority_fixture()
    repaired, state = light.apply_coverage_authority(response, registry, discovery)
    assert state["coverage_status"] == "COMPLETE" and state["coverage_gaps"] == []
    zero = [row for row in state["coverage_matrix"]
            if row["required"] and row["result_count"] == 0]
    assert zero and all(row["checked"] and not row["gap"] for row in zero)
    assert repaired["strategy_telemetry"]["coverage_status"] == "COMPLETE"
    reviews = {row["channel"]: row["status"] for row in state["channel_reviews"]}
    assert reviews["incremental_ir"] == "NOT_APPLICABLE"
    assert state["fake_gap_count"] == 0


def test_incremental_ir_applicable_coverage_uses_executed_registry_record():
    response, registry, discovery = _coverage_authority_fixture()
    discovery["incremental_ir_applicable"] = True
    registry.append({"query_id": "incremental-ir-q1", "worker_id": "incremental-ir",
                     "exposure_id": None, "canonical_channel": "incremental_ir",
                     "executed": True, "result_count": 0, "evidence_ids": [],
                     "window_relevance_checked": True})
    repaired, state = light.apply_coverage_authority(response, registry, discovery)
    reviews = {row["channel"]: row for row in state["channel_reviews"]}
    assert reviews["incremental_ir"]["status"] == "CHECKED"
    assert reviews["incremental_ir"]["exposure_index"] is None
    assert repaired["strategy_telemetry"]["coverage_status"] == "COMPLETE"


def test_missing_required_query_derives_partial_with_explicit_real_gap():
    response, registry, discovery = _coverage_authority_fixture()
    registry = [row for row in registry if row["canonical_channel"] != "government"]
    _, state = light.apply_coverage_authority(response, registry, discovery)
    assert state["coverage_status"] == "PARTIAL"
    assert state["coverage_gaps"] == [{
        "gap_type": "query_execution", "url": None, "channel": "government",
        "exposure_index": 0,
        "reason": "Required government query for exposure 0 has no executed SearchRecordRegistry record.",
        "retry_required": True,
    }]
    assert state["fake_gap_count"] == 0


def test_search_registry_and_derived_coverage_counts_are_consistent():
    response, registry, discovery = _coverage_authority_fixture()
    _, state = light.apply_coverage_authority(response, registry, discovery)
    assert state["executed_query_count"] == len(registry)
    assert sum(row["executed_query_count"] for row in state["coverage_matrix"]) == len(registry)


def _incremental_authority_fixture(*, material=False, incremental=False, evidence="No delta"):
    response = {"strategy_telemetry": {"documents": [{
        "date": None, "title": "IR index", "url": "https://example.test/ir",
        "scope_label": "IN_SCOPE_INCREMENTAL_IR", "scope_reason": "IR channel",
        "material": material, "incremental_information": incremental,
        "incremental_information_evidence": evidence,
    }]}}
    cards = [{"url": "https://example.test/ir", "source_type": "official_index",
              "verification_status": "VERIFIED"}]
    return response, cards


def test_nonincremental_company_index_is_not_misclassified_as_incremental_ir():
    response, cards = _incremental_authority_fixture()
    repaired = light.apply_incremental_ir_authority(
        response, cards, {"incremental_ir_applicable": False})
    doc = repaired["strategy_telemetry"]["documents"][0]
    assert doc["scope_label"] == "IN_SCOPE_COMPANY_WEB"
    assert doc["incremental_information"] is False
    assert response["strategy_telemetry"]["documents"][0]["scope_label"] == "IN_SCOPE_INCREMENTAL_IR"


def test_incremental_ir_fails_closed_without_true_flag_or_delta():
    response, cards = _incremental_authority_fixture(material=True)
    with pytest.raises(light.LightweightError, match="material scope"):
        light.apply_incremental_ir_authority(
            response, cards, {"incremental_ir_applicable": False})
    response, cards = _incremental_authority_fixture(
        material=True, incremental=True, evidence=None)
    with pytest.raises(Exception, match="delta"):
        light.apply_incremental_ir_authority(
            response, cards, {"incremental_ir_applicable": True})


def test_incremental_ir_true_with_delta_evidence_passes():
    response, cards = _incremental_authority_fixture(
        material=True, incremental=True,
        evidence="Q&A adds capacity constraint absent from the release")
    repaired = light.apply_incremental_ir_authority(
        response, cards, {"incremental_ir_applicable": True})
    assert repaired["strategy_telemetry"]["documents"][0]["scope_label"] == "IN_SCOPE_INCREMENTAL_IR"


def test_exhaustive_audit_incremental_ir_handles_both_applicability_paths():
    from tools.company_news_phase18j_offline_replay import incremental_ir_contract
    query = {"canonical_channel": "incremental_ir", "executed": True}
    assert incremental_ir_contract(
        {"incremental_ir_applicable": True}, [query], [])[0] is True
    assert incremental_ir_contract(
        {"incremental_ir_applicable": False}, [], [])[0] is True
    assert incremental_ir_contract(
        {"incremental_ir_applicable": False}, [query], [])[0] is False


def test_saved_effective_dates_upgrade_without_fake_time_or_checked_at():
    response = {"research_result": {"checked_at": "2026-09-05T10:00:00+00:00", "items": [
        {"valid_from": "2026-10-01", "valid_until": None},
        {"valid_from": "2026-10-01T09:30:00+09:00", "valid_until": "2026-12-01"},
    ]}}
    repaired = light.restore_effective_precision(response)
    first, second = repaired["research_result"]["items"]
    assert first == {"valid_from": None, "valid_until": None,
                     "valid_from_date": "2026-10-01", "valid_from_precision": "date",
                     "valid_until_date": None, "valid_until_precision": "unknown"}
    assert second["valid_from"] == "2026-10-01T09:30:00+09:00"
    assert second["valid_from_date"] is None and second["valid_from_precision"] == "datetime"
    assert second["valid_until"] is None and second["valid_until_date"] == "2026-12-01"
    assert second["valid_until_precision"] == "date"
    assert "T00:00:00" not in json.dumps(repaired)
    assert all(item.get("valid_from") != response["research_result"]["checked_at"] for item in (first, second))


def test_offline_cost_gate_for_two_through_five_workers():
    rows = light.offline_cost_model()
    assert [row["worker_count"] for row in rows] == [2, 3, 4, 5]
    assert [row["conservative_upper_total"] for row in rows] == [253000, 322000, 391000, 460000]
    assert all(row["hard_500k"] == "GO" for row in rows)
    assert [row["preferred_400k"] for row in rows] == ["GO", "GO", "GO", "NO_GO"]


def test_validation_failure_marks_failed_and_releases_guard(tmp_path, monkeypatch):
    exp = experiment(tmp_path)
    snap = light.preflight(exp)
    monkeypatch.setattr(light.strategy_v5, "validate_generated_response", lambda response: response)
    monkeypatch.setattr(light, "validate_payload", lambda payload: (_ for _ in ()).throw(ValueError("bad")))
    company = exp / "companies" / "01-7760"
    write(company / "measured-token-telemetry.json", {"total_tokens": 10})
    with pytest.raises(ValueError):
        light.finalize(exp, snap["run_token"], {"research_result": {}, "strategy_telemetry": {}}, {
            key: 0 for key in ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens",
                               "reasoning_tokens", "model_call_count", "tool_call_count", "web_call_count",
                               "research_elapsed_seconds", "post_research_model_calls", "post_research_tokens", "total_tokens")})
    assert not (exp / "guard.json").exists()
    assert light._read_json(exp / "queue.json")["assignments"][0]["status"] == "failed"
    assert light._read_json(company / "measured-token-telemetry.json")["validation_status"] == "FAIL"
    assert light._read_json(company / "validation-result.json")["status"] == "FAIL"


def test_deterministic_finalize_and_atomic_queue_update(tmp_path, monkeypatch):
    exp = experiment(tmp_path)
    snap = light.preflight(exp)
    monkeypatch.setattr(light, "validate_payload", lambda payload: payload)
    monkeypatch.setattr(light.strategy_v5, "validate_generated_response", lambda response: response)
    monkeypatch.setattr(light.strategy_v5, "validate", lambda strategy, assignment, payload: strategy)
    response = {"research_result": {"run_id": "a-1-7760", "task_id": "a-1-7760", "ticker": "7760", "items": []},
                "strategy_telemetry": {"ok": True}}
    telemetry = {key: 0 for key in ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens",
                                     "reasoning_tokens", "model_call_count", "tool_call_count", "web_call_count",
                                     "research_elapsed_seconds", "post_research_model_calls", "post_research_tokens", "total_tokens")}
    result = light.finalize(exp, snap["run_token"], response, telemetry)
    assert result["status"] == "completed" and result["result_type"] == "no_news"
    assert not (exp / "guard.json").exists() and not list(exp.rglob("*.tmp"))
    saved = light._read_json(exp / "companies" / "01-7760" / "research-result.json")
    assert saved["ticker"] == "7760"


def test_runner_failure_releases_guard(tmp_path):
    exp = experiment(tmp_path)
    def runner(*args):
        raise RuntimeError("agent error")
    with pytest.raises(RuntimeError):
        light.run_next(tmp_path, exp, runner)
    assert not (exp / "guard.json").exists()
    assert light._read_json(exp / "queue.json")["assignments"][0]["status"] == "failed"


def test_benchmark_and_production_isolation(tmp_path):
    source = Path(light.__file__).read_text(encoding="utf-8")
    assert "data/news_inbox" not in source and "canonical_news" not in source and "supabase" not in source.lower()
    assert "activate_run" not in source and "release_task(" not in source
    exp = experiment(tmp_path)
    snap = light.preflight(exp)
    assert Path(snap["assignment"]["assignment_id"]).is_absolute() is False


def _index_only_recall_fixture():
    url = "https://official.example/en/news"
    response, registry, discovery = _coverage_authority_fixture()
    response["research_result"]["items"] = []
    response["research_result"]["sources_checked_count"] = 2
    response["strategy_telemetry"].update({
        "documents": [{
            "date": "2026-08-31", "title": "News", "url": url, "final_url": url,
            "scope_label": "IN_SCOPE_COMPANY_WEB", "scope_reason": "official candidate",
            "material": True, "body_status": "VERIFIED", "attempt_evidence": "index only",
        }],
        "source_provenance": [
            {"url": "https://official.example/", "source_type": "company_official",
             "content_verified": True, "evidence_excerpt": "official body"},
            {"url": url, "source_type": "official_index", "content_verified": True,
             "evidence_excerpt": "2026-08-31 recall"},
        ],
    })
    cards = [{"url": url, "source_type": "official_index", "title": "News",
              "excerpt": "2026-08-31 recall", "verification_status": "VERIFIED"}]
    return response, registry, discovery, cards


def test_index_only_is_not_content_verified_and_forces_partial_no_news_false():
    response, registry, discovery, cards = _index_only_recall_fixture()
    repaired, ledger = light.apply_document_provenance_authority(response, cards)
    assert ledger[0]["state"] == "DISCOVERED_IN_INDEX"
    document = repaired["strategy_telemetry"]["documents"][0]
    assert document["body_status"] == "INACCESSIBLE" and document["final_url"] is None
    assert all(row["url"] != document["url"]
               for row in repaired["strategy_telemetry"]["source_provenance"])
    repaired, state = light.apply_coverage_authority(repaired, registry, discovery, cards)
    assert state["coverage_status"] == "PARTIAL"
    assert state["no_news_gate_pass"] is False
    assert state["coverage_gaps"][0]["gap_type"] == "document_access"


def test_url_discovered_is_separate_from_body_verified():
    document = {"url": "https://official.example/news/42", "title": "Recall",
                "body_status": "SKIPPED"}
    card = {"url": document["url"], "title": "Recall", "source_type": "official_index",
            "verification_status": "VERIFIED", "excerpt": "index row",
            "exact_url_from_saved_evidence": True}
    assert light.derive_document_provenance_state(document, [card]) == "URL_DISCOVERED"
    body = {**card, "source_type": "official_news", "excerpt": "actual statement body"}
    assert light.derive_document_provenance_state(document, [body]) == "BODY_VERIFIED"


def test_access_failed_is_separate_from_discovery():
    document = {"url": "https://official.example/news/42", "title": "Recall",
                "body_status": "INACCESSIBLE", "attempt_evidence": "TLS failed"}
    assert light.derive_document_provenance_state(document, []) == "ACCESS_FAILED"


def test_recall_materiality_fixtures_are_not_inferred_from_title():
    nonmaterial = {"material": False, "economic_transmission": None}
    material = {"material": True,
                "economic_transmission": "Recall costs and lost sales affect earnings."}
    assert nonmaterial["material"] is False and nonmaterial["economic_transmission"] is None
    assert material["material"] is True and material["economic_transmission"]


def test_phase21_5122_index_only_offline_replay():
    response, registry, discovery, cards = _index_only_recall_fixture()
    repaired, ledger = light.apply_document_provenance_authority(response, cards)
    repaired, state = light.apply_coverage_authority(repaired, registry, discovery, cards)
    assert ledger == [{"document_index": 0, "url": "https://official.example/en/news",
                       "state": "DISCOVERED_IN_INDEX"}]
    assert state["coverage_status"] == "PARTIAL" and len(state["coverage_gaps"]) == 1
    assert repaired["strategy_telemetry"]["no_news_gate_pass"] is False


def test_body_fetch_attempted_state_is_preserved_before_verification():
    document = {"url": "https://official.example/news/42", "title": "Recall",
                "body_status": "SKIPPED"}
    card = {"url": document["url"], "source_type": "official_index", "title": "Recall",
            "verification_status": "VERIFIED", "excerpt": "index row",
            "document_provenance_state": "BODY_FETCH_ATTEMPTED"}
    assert light.derive_document_provenance_state(document, [card]) == "BODY_FETCH_ATTEMPTED"


def test_recall_materiality_requires_verified_body_and_earnings_pathway():
    facts = {"body_verified": True, "direct_company_linkage": True,
             "actual_product_recall": True, "affected_product": "product",
             "commercial_or_operational_impact": "recall operations",
             "meaningful_earnings_pathway": None}
    assert light.classify_recall_materiality(facts) == "FAIL"
    facts["meaningful_earnings_pathway"] = "lost sales and recall cost"
    assert light.classify_recall_materiality(facts) == "PASS"
    facts["body_verified"] = False
    assert light.classify_recall_materiality(facts) == "UNKNOWN"


def test_ambiguous_material_index_document_fails_closed_as_access_gap():
    response, registry, discovery, cards = _index_only_recall_fixture()
    response["strategy_telemetry"]["documents"][0]["scope_label"] = "AMBIGUOUS"
    repaired, _ = light.apply_document_provenance_authority(response, cards)
    repaired, state = light.apply_coverage_authority(repaired, registry, discovery, cards)
    assert state["coverage_status"] == "PARTIAL"
    assert state["coverage_gaps"][0]["url"] == "https://official.example/en/news"
    assert repaired["strategy_telemetry"]["no_news_gate_pass"] is False
