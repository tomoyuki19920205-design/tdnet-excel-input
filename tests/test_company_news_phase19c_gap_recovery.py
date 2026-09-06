from pathlib import Path

import pytest

from tools import company_news_phase19c_gap_recovery as phase19c
from tools.company_news_canonical_schema import validate_instance


def test_recovery_schemas_are_strict_and_bounded():
    for worker_id, spec in phase19c.SPECS.items():
        schema = phase19c.output_schema(spec)
        assert schema["additionalProperties"] is False
        assert schema["properties"]["worker_id"] == {"type": "string", "const": worker_id}
        assert schema["properties"]["target"]["type"] == "string"
        assert schema["properties"]["calls"]["items"]["properties"]["executed"]["type"] == "boolean"
        assert schema["properties"]["calls"]["maxItems"] == 2
        assert schema["properties"]["evidence_cards"]["maxItems"] == 8
        assert phase19c.strict_schema_errors(schema) == []


def test_every_schema_serializes_parses_lints_and_accepts_only_valid_fixture():
    import json

    for spec in phase19c.SPECS.values():
        schema = json.loads(json.dumps(phase19c.output_schema(spec)))
        assert phase19c.strict_schema_errors(schema) == []
        fixture = phase19c.valid_fixture(spec)
        validate_instance(fixture, schema)
        with pytest.raises(Exception):
            validate_instance({**fixture, "unexpected": True}, schema)

    telemetry_schema = json.loads(json.dumps(phase19c.recovery_telemetry_schema()))
    assert phase19c.strict_schema_errors(telemetry_schema) == []
    telemetry = {"worker_id": "official-verification", "input_tokens": 1,
                 "cached_input_tokens": 0, "uncached_input_tokens": 1, "output_tokens": 1,
                 "reasoning_tokens": 0, "total_tokens": 2, "web_call_count": 1,
                 "tool_call_count": 1, "research_elapsed_seconds": 0.1, "validation_status": "PASS"}
    validate_instance(telemetry, telemetry_schema)
    with pytest.raises(Exception):
        validate_instance({**telemetry, "validation_status": "INVALID"}, telemetry_schema)

    synthesis_schema = json.loads(json.dumps(phase19c.optional_synthesis_schema()))
    assert phase19c.strict_schema_errors(synthesis_schema) == []
    validate_instance({"result": "UNKNOWN", "events": [], "reason": "fixture"}, synthesis_schema)
    with pytest.raises(Exception):
        validate_instance({"result": "UNKNOWN", "events": []}, synthesis_schema)


def test_prompt_is_targeted_and_forbids_retry():
    for spec in phase19c.SPECS.values():
        value = phase19c.prompt(spec)
        assert f"TARGET={spec.target}" in value
        assert "at most TWO Web tool calls" in value
        assert "retry a failed call" in value
        assert "research another company" in value


def test_cumulative_tokens_requires_exact_values(tmp_path: Path):
    worker = tmp_path / "workers" / "a"
    worker.mkdir(parents=True)
    (worker / "telemetry.json").write_text('{"total_tokens":123}', encoding="utf-8")
    assert phase19c.cumulative_tokens(tmp_path) == 123


def test_command_uses_the_current_schema_path(tmp_path: Path):
    schema_path = tmp_path / "current-output-schema.json"
    result_path = tmp_path / "last-message.json"
    command = phase19c.lightweight.codex_command(
        tmp_path, schema_path, result_path, "fixture", web_enabled=True)
    index = command.index("--output-schema")
    assert Path(command[index + 1]) == schema_path
    assert "phase19c-gap-recovery" not in command[index + 1]
