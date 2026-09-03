import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import tools.company_news_batch_benchmark as benchmark


BENCHMARK_ID = "batchbench-gold-miss-test"
FIXED_TIME = datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc)
RUN_LABELS = ("reference01", "reference02", "batch02", "batch04")


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _fixture(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    directory = (
        tmp_path / "data" / "news_work" / "batch_benchmark" / BENCHMARK_ID
        / "quality_v2" / "matcher_v2"
    )
    events = []
    union_events = []
    tickers = []
    source_types = (
        "company_news", "company_ir", "tdnet", "government",
        "industry_association", "reliable_news", "other",
    )
    for index in range(29):
        ticker = "1000" if index < 2 else f"{1000 + index:04d}"
        tickers.append(ticker)
        found_by = {
            "reference01": index < 25,
            "reference02": index < 22,
            "batch02": index < 19,
            "batch04": index < 17,
        }
        cluster_id = f"event-v2-{index:016x}"
        source_type = source_types[index % len(source_types)]
        event = {
            "match_cluster_id": cluster_id,
            "ticker": ticker,
            "company": f"Company {ticker}",
            "date": "2026-08-25",
            "headline": f"Material event {index}",
            "category": "demand",
            "source_url": f"https://example.com/{index}",
            "source_type": source_type,
            "importance": "high" if index < 3 else "medium",
            "earnings_relevance": "direct" if 3 <= index < 7 else "likely",
            "found_by": found_by,
            "verdict": "VALID_MATERIAL",
            "materiality_reason": "Material to investors.",
            "primary_source_url": f"https://example.com/{index}",
            "primary_source_type": source_type,
            "direction": benchmark.GOLD_DIRECTIONS[index % len(benchmark.GOLD_DIRECTIONS)],
            "confidence": "HIGH",
            "company_specific": True,
        }
        members = []
        for label, found in found_by.items():
            if found:
                members.append({
                    "run_name": "reference" if label == "reference01" else label,
                    "run_label": label,
                    "observation_id": f"{label}:{ticker}:{index}",
                    "headline": f"Observed event {index}",
                    "source_url": f"https://run.example/{label}/{index}",
                    "source_type": source_type,
                    "category": "demand",
                    "importance": event["importance"],
                    "earnings_relevance": event["earnings_relevance"],
                    "summary": "Observed summary.",
                })
        events.append(event)
        union_events.append({**event, "verdict": "UNKNOWN", "members": members, "matcher_evidence": []})

    duplicate = {
        **events[0],
        "match_cluster_id": "event-v2-duplicate000000",
        "headline": "Duplicate representation",
        "verdict": "DUPLICATE",
        "duplicate_of_cluster_id": events[0]["match_cluster_id"],
    }
    duplicate_union = {**duplicate, "verdict": "UNKNOWN", "members": [], "matcher_evidence": []}
    gold = {
        "schema_version": "company_news_gold_adjudication_v2",
        "benchmark_id": BENCHMARK_ID,
        "events": [*events, duplicate],
    }
    union = {
        "schema_version": benchmark.EVENT_UNION_V2_SCHEMA,
        "benchmark_id": BENCHMARK_ID,
        "events": [*union_events, duplicate_union],
    }
    audit = {
        "decision": "GOLD_COMPLETE",
        "verdict_counts": {"UNKNOWN": 0},
        "runs": {
            label: {"found_gold_material_event_count": count}
            for label, count in zip(RUN_LABELS, (25, 22, 19, 17))
        },
    }
    _write(directory / "gold_adjudication_v2.json", gold)
    _write(directory / "event_union_v2.json", union)
    _write(directory / "gold_audit_v2.json", audit)

    monkeypatch.setattr(benchmark, "_validate_gold_v2_against_union", lambda value, _: value)
    unique_tickers = sorted(set(tickers))
    monkeypatch.setattr(
        benchmark,
        "_load_cohort",
        lambda *_: {"reference_tickers": unique_tickers},
    )

    raw_by_label = {
        label: {
            ticker: [object() for event in events if event["ticker"] == ticker and event["found_by"][label]]
            for ticker in unique_tickers
        }
        for label in RUN_LABELS
    }
    telemetry_by_label = {
        label: [{
            "ticker": ticker,
            "research_elapsed_seconds": float(20 + position),
            "source_count": 4 + position % 3,
            "company_position": 1 if label.startswith("reference") else 1 + position % (2 if label == "batch02" else 4),
            "event_count": len(raw_by_label[label][ticker]),
            "run_id": f"{label}-{ticker}",
        } for position, ticker in enumerate(unique_tickers)]
        for label in RUN_LABELS
    }

    def completed(_root, _benchmark_id, run_name):
        label = benchmark.QUALITY_RUN_LABELS[run_name]
        return {}, raw_by_label[label], telemetry_by_label[label]

    monkeypatch.setattr(benchmark, "_completed_run_events", completed)
    return tmp_path, directory


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_gold_material_audit_counts_aggregation_and_determinism(tmp_path, monkeypatch):
    root, directory = _fixture(tmp_path, monkeypatch)
    raw_paths = [
        directory / "gold_adjudication_v2.json",
        directory / "event_union_v2.json",
        directory / "gold_audit_v2.json",
    ]
    hashes_before = {path: _sha(path) for path in raw_paths}

    first = benchmark.audit_gold_material_misses_v2(root, BENCHMARK_ID, now=FIXED_TIME)
    first_bytes = {
        name: (directory / name).read_bytes()
        for name in (
            "gold_material_discovery_matrix.json",
            "gold_material_discovery_matrix.md",
            "gold_material_miss_analysis.json",
            "GOLD_MATERIAL_MISS_ANALYSIS.md",
        )
    }
    second = benchmark.audit_gold_material_misses_v2(root, BENCHMARK_ID, now=FIXED_TIME)
    second_bytes = {name: (directory / name).read_bytes() for name in first_bytes}
    analysis = json.loads((directory / "gold_material_miss_analysis.json").read_text(encoding="utf-8"))
    matrix = json.loads((directory / "gold_material_discovery_matrix.json").read_text(encoding="utf-8"))

    assert first == second
    assert first_bytes == second_bytes
    assert {path: _sha(path) for path in raw_paths} == hashes_before
    assert matrix["gold_event_count"] == 29
    assert len(matrix["rows"]) == 29
    assert len({row["gold_event_id"] for row in matrix["rows"]}) == 29
    assert len(matrix["rows"][0]["normalized_cluster_ids"]) == 2
    assert analysis["summary"]["critical_event_count"] == 7
    assert analysis["summary"]["noncritical_event_count"] == 22
    assert [analysis["summary"]["run_results"][label]["found"] for label in RUN_LABELS] == [25, 22, 19, 17]
    assert all(analysis["summary"]["critical"][label]["found"] == 7 for label in RUN_LABELS)
    assert analysis["multi_event_analysis"]["company_count"] == 1
    assert sum(value["event_count"] for value in analysis["source_type_analysis"].values()) == 29
    assert sum(value["event_count"] for value in analysis["channel_analysis"].values()) == 29
    assert analysis["recommendation"]["decision"] == "DO_NOT_RUN_BATCH03_YET"


def test_gold_group_normalization_rejects_cycles():
    value = {"events": [
        {"match_cluster_id": "a", "verdict": "DUPLICATE", "duplicate_of_cluster_id": "b"},
        {"match_cluster_id": "b", "verdict": "MATCHER_FALSE_NEGATIVE", "duplicate_of_cluster_id": "a"},
    ]}
    try:
        benchmark._gold_normalized_groups_v2(value)
    except benchmark.BenchmarkError as exc:
        assert "cycle" in str(exc)
    else:
        raise AssertionError("duplicate cycle must fail closed")


def test_cli_exposes_gold_material_miss_audit():
    args = benchmark._build_parser().parse_args([
        "audit-gold-material-misses-v2", "--benchmark-id", BENCHMARK_ID,
    ])
    assert args.command == "audit-gold-material-misses-v2"
    assert args.benchmark_id == BENCHMARK_ID
