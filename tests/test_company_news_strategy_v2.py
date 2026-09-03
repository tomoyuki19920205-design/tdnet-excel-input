import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import tools.company_news_batch_benchmark as benchmark


BENCHMARK_ID = "batchbench-strategy-v2-test"
FIXED_TIME = datetime(2026, 9, 3, 3, 0, tzinfo=timezone.utc)


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _prepare_workspace(root: Path) -> list[dict]:
    companies = [{
        "ticker": f"X{index:03d}",
        "company_name": f"Example Manufacturing {index}",
        "sector": f"Synthetic Sector {index % 4}",
        "size_bucket": "mid",
        "prior_news_bucket": "unscanned",
    } for index in range(192)]
    cohort = {
        "schema_version": benchmark.BENCHMARK_SCHEMA,
        "benchmark_mode": True,
        "benchmark_id": BENCHMARK_ID,
        "search_from": "2026-08-25",
        "search_to": "2026-08-31",
        "reference_tickers": [company["ticker"] for company in companies[:48]],
        "companies": companies,
    }
    directory = root / "data" / "news_work" / "batch_benchmark" / BENCHMARK_ID
    _write(directory / "cohort.json", cohort)
    prompt = root / "data" / "news_work" / "SCHEDULED_TASK_BATCH_BENCHMARK_PROMPT.txt"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text(
        "company_news_research_strategy_v2 official_first targeted_second_pass "
        "site:go.jp driver_groups no_news_gate_pass stopped_after_first_event",
        encoding="utf-8",
    )
    benchmark._prepare_named_run(
        root, BENCHMARK_ID, cohort, companies, 4, "batch04",
        reference=False, now=FIXED_TIME,
    )
    return companies


def _valid_telemetry(*, event_count: int = 0) -> dict:
    provenance = [{
        "candidate_index": index,
        "stage": "official_first" if index == 0 else "targeted_second_pass",
        "selected_for_output": index < event_count,
        "headline": f"Candidate {index}",
        "source_url": f"https://example.com/candidate/{index}",
    } for index in range(event_count)]
    return {
        "schema_version": benchmark.STRATEGY_V2_TELEMETRY_SCHEMA,
        "strategy_version": benchmark.STRATEGY_V2_VERSION,
        "assignment_id": "assignment-1",
        "ticker": "X001",
        "official_queries_attempted": [
            '"Example Manufacturing" X001 IR お知らせ 適時開示',
            '"Example Manufacturing" X001 site:go.jp',
        ],
        "official_sources_found": ["https://example.com/ir"],
        "official_coverage_complete": True,
        "government_search_attempted": True,
        "external_queries_attempted": ["specialty material price supply"],
        "external_sources_found": ["https://industry.example/material"],
        "external_coverage_complete": True,
        "driver_groups": [{
            "driver": "specialty material price",
            "query_group": "pricing_cost_supply",
            "transmission_channel": "input price can affect manufacturing margin",
        }],
        "source_diversity": ["company_ir", "government", "industry_news"],
        "candidate_event_count": event_count,
        "final_event_count": event_count,
        "no_news_gate_pass": True,
        "window_review_complete": True,
        "stopped_after_first_event": False,
        "official_stage_elapsed_seconds": 12.0,
        "targeted_stage_elapsed_seconds": 8.0,
        "total_research_elapsed_seconds": 20.0,
        "candidate_provenance": provenance,
    }


def test_prepare_batch04v2_reuses_exact_fixed_cohort_and_is_inert(tmp_path):
    companies = _prepare_workspace(tmp_path)
    result = benchmark.prepare_run(
        tmp_path, BENCHMARK_ID, 4,
        run_name="batch04v2", strategy=benchmark.STRATEGY_V2_VERSION,
        now=FIXED_TIME,
    )
    run_directory = tmp_path / "data" / "news_work" / "batch_benchmark" / BENCHMARK_ID / "runs" / "batch04v2"
    ordered = benchmark._ordered_run_assignments(run_directory)

    assert result["strategy_version"] == benchmark.STRATEGY_V2_VERSION
    assert result["run_name"] == "batch04v2"
    assert result["batch_size"] == 4
    assert result["company_count"] == 192
    assert [item["ticker"] for item in ordered] == [item["ticker"] for item in companies]
    assert result["company_order_sha256"] == result["baseline_company_order_sha256"]
    assert result["search_from"] == "2026-08-25"
    assert result["search_to"] == "2026-08-31"
    assert result["production_write_forbidden"] is True
    assert result["gold_leakage_scan"] == {
        "status": "PASS", "hit_count": 0, "hits": [], "company_specific_branch_count": 0,
    }
    assert result["readiness_status"] == "READY_TO_ACTIVATE"
    assert not (tmp_path / "data" / "news_work" / "batch_benchmark" / "active.json").exists()
    assert Path(result["output_directory"]).is_relative_to(tmp_path / "data" / "news_benchmark_inbox")


def test_strategy_prepare_is_deterministic_across_synthetic_workspaces(tmp_path):
    results = []
    for name in ("one", "two"):
        root = tmp_path / name
        _prepare_workspace(root)
        results.append(benchmark.prepare_run(
            root, BENCHMARK_ID, 4, run_name="batch04v2",
            strategy=benchmark.STRATEGY_V2_VERSION, now=FIXED_TIME,
        ))
    stable_keys = (
        "benchmark_id", "run_name", "batch_size", "company_count", "search_from",
        "search_to", "company_order_sha256", "strategy_version", "strategy_contract",
        "gold_leakage_scan", "readiness_status",
    )
    assert {key: results[0][key] for key in stable_keys} == {
        key: results[1][key] for key in stable_keys
    }


def test_strategy_contract_reserves_company_budget_and_generic_government_search():
    contract = benchmark._strategy_v2_contract()
    assert contract["official_query_budget"] == {"reserved_minimum": 2, "maximum": 3}
    assert contract["external_query_budget"] == {"reserved_minimum": 1, "maximum": 3}
    assert contract["per_company_query_budget"] == {"reserved_minimum": 3, "maximum": 6}
    assert contract["driver_group_limit"] == 3
    assert contract["candidate_event_limit"] == 20
    assert contract["final_event_limit"] == 10
    assert any("site:go.jp" in query for query in contract["official_query_templates"])
    assert contract["no_news_requires_both_coverages"] is True
    assert contract["stop_after_first_event_forbidden"] is True


def test_strategy_runtime_prompt_is_generic_and_has_no_adjudication_leakage():
    prompt_path = Path(benchmark.__file__).parents[1] / "data" / "news_work" / "SCHEDULED_TASK_BATCH_BENCHMARK_PROMPT.txt"
    prompt = prompt_path.read_text(encoding="utf-8")
    result = benchmark._strategy_v2_firewall_scan(prompt, {})
    assert result["status"] == "PASS"
    assert result["hit_count"] == 0
    assert "site:go.jp" in prompt
    assert "eventを1件発見しても終了せず" in prompt
    assert "strategy_telemetry_temp_path" in prompt


def test_strategy_telemetry_accepts_complete_no_news_coverage():
    assignment = {"assignment_id": "assignment-1", "ticker": "X001", "company_name": "Example Manufacturing"}
    value = _valid_telemetry()
    assert benchmark.validate_strategy_v2_telemetry(value, assignment, 0, "no_news") == value


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(official_coverage_complete=False), "coverage"),
        (lambda value: value.update(external_coverage_complete=False), "coverage"),
        (lambda value: value.update(government_search_attempted=False), "government"),
        (lambda value: value.update(no_news_gate_pass=False), "gate"),
        (lambda value: value.update(stopped_after_first_event=True), "multi-event"),
        (lambda value: value["official_queries_attempted"].extend(["extra query", "another query"]), "budget"),
    ],
)
def test_strategy_telemetry_fails_closed(mutation, message):
    assignment = {"assignment_id": "assignment-1", "ticker": "X001", "company_name": "Example Manufacturing"}
    value = _valid_telemetry()
    mutation(value)
    with pytest.raises(benchmark.BenchmarkError, match=message):
        benchmark.validate_strategy_v2_telemetry(value, assignment, 0, "no_news")


def test_stage_provenance_covers_every_selected_event():
    assignment = {"assignment_id": "assignment-1", "ticker": "X001", "company_name": "Example Manufacturing"}
    value = _valid_telemetry(event_count=2)
    validated = benchmark.validate_strategy_v2_telemetry(value, assignment, 2, "news_present")
    assert [item["candidate_index"] for item in validated["candidate_provenance"]] == [0, 1]
    broken = copy.deepcopy(value)
    broken["candidate_provenance"].pop()
    with pytest.raises(benchmark.BenchmarkError, match="every candidate"):
        benchmark.validate_strategy_v2_telemetry(broken, assignment, 2, "news_present")


def test_candidate_only_events_remain_provisional_and_extension_is_blind():
    gold = {"events": [{
        "match_cluster_id": "known-1", "verdict": "VALID_MATERIAL",
        "ticker": "X001", "importance": "high", "earnings_relevance": "direct",
        "primary_source_type": "company_ir", "found_by": {"batch04": True},
    }]}
    novel = [{
        "ticker": "X002", "company": "Example Materials", "date": "2026-08-26",
        "headline": "New synthetic capacity plan", "category": "capacity",
        "source_url": "https://example.com/capacity", "source_type": "company_news",
        "importance": "medium", "earnings_relevance": "likely",
        "summary": "A new capacity plan was announced.",
        "why_it_matters": "Capacity may affect future volume.",
        "found_by": {"batch04v2": True}, "run_name": "batch04v2",
    }]
    metrics = benchmark.strategy_v2_known_gold_metrics(
        gold, {"known-1": "official_first"}, novel
    )
    packet1 = benchmark.build_gold_extension_packet_v2(BENCHMARK_ID, novel)
    packet2 = benchmark.build_gold_extension_packet_v2(BENCHMARK_ID, novel)
    encoded = json.dumps(packet1, ensure_ascii=False)

    assert metrics["known_gold_critical"]["recall"] == 1.0
    assert metrics["candidate_only_events_are_false_positives"] is False
    assert metrics["precision"] is None
    assert metrics["precision_status"] == "PROVISIONAL_PENDING_GOLD_EXTENSION"
    assert packet1 == packet2
    assert packet1["existing_gold_events_reincluded"] is False
    for forbidden in ("found_by", "run_name", "run_label", "members", "matcher_evidence", "verdict"):
        assert f'"{forbidden}"' not in encoded


def test_strategy_firewall_detects_provenance_and_company_branches():
    result = benchmark._strategy_v2_firewall_scan(
        "if ticker == special: use found_by", {}
    )
    assert result["status"] == "FAIL"
    assert result["hit_count"] == 2
    assert result["company_specific_branch_count"] == 1
