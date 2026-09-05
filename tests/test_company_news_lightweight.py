import json
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
    command = light.codex_command(tmp_path, schema, result, "prompt")
    assert command[command.index("--output-schema") + 1] == str(schema)
    assert "--search" in command and "shell_tool" in command and "--ignore-user-config" in command
    synthesis = light.codex_command(tmp_path, schema, result, "prompt", web_enabled=False)
    assert "--search" not in synthesis
    assert light.output_schema() == light.strategy_v5.canonical_output_schema()
    assert light.strategy_v5.schema_sha256(light.output_schema()) == light.strategy_v5.canonical_schema_sha256()


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
                "model_visible_chars": 50 if spec else 0, "thread_ids": [f"thread-{len(prompts)}"]}
    def fake_stage(root, company_dir, spec, prompt, schema, *, web_enabled):
        prompts.append((spec.worker_id if spec else "final", prompt, web_enabled))
        if spec and spec.kind == "company_web":
            return {"worker_id": spec.worker_id, "kind": spec.kind,
                    "company_identity": {"verified_ticker": "7760", "source_url": "https://imv.test", "evidence": "official"},
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
        return {"research_result": {}, "strategy_telemetry": {}}, telemetry(None)
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
    assert light.strategy_v5.canonical_schema_sha256() == "28dde3510e724231d43ed865c85accc96c2970f00fa8409ae155df31823c73c3"
    prompt = light.worker_contract(specs[1], assignment)
    assert "prior worker history" in prompt and "electronics capex" in prompt
    assert "vibration testing demand" not in prompt
    assert "evidence_cards" not in prompt and "SECRET_RAW_PAGE_BODY" not in prompt


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
