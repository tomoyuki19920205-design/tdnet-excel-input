import json
from pathlib import Path

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


def test_validation_failure_marks_failed_and_releases_guard(tmp_path, monkeypatch):
    exp = experiment(tmp_path)
    snap = light.preflight(exp)
    monkeypatch.setattr(light, "validate_payload", lambda payload: (_ for _ in ()).throw(ValueError("bad")))
    with pytest.raises(ValueError):
        light.finalize(exp, snap["run_token"], {"research_result": {}, "strategy_telemetry": {}}, {
            key: 0 for key in ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens",
                               "reasoning_tokens", "model_call_count", "tool_call_count", "web_call_count",
                               "research_elapsed_seconds", "post_research_model_calls", "post_research_tokens", "total_tokens")})
    assert not (exp / "guard.json").exists()
    assert light._read_json(exp / "queue.json")["assignments"][0]["status"] == "failed"


def test_deterministic_finalize_and_atomic_queue_update(tmp_path, monkeypatch):
    exp = experiment(tmp_path)
    snap = light.preflight(exp)
    monkeypatch.setattr(light, "validate_payload", lambda payload: payload)
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
