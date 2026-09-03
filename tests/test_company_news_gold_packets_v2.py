import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import tools.company_news_batch_benchmark as benchmark
from tools.company_news_batch_benchmark import (
    BenchmarkError,
    GOLD_PACKET_SCHEMA,
    GOLD_RESPONSE_SCHEMA,
    MATCHER_V2_SCHEMA,
    _gold_metrics_v2,
    _gold_template,
    _json_sha256,
    _packet_sha256,
    build_gold_packets_v2,
    gold_status_v2,
    import_gold_adjudication_v2,
    validate_gold_adjudication_response_v2,
)


BENCHMARK_ID = "batchbench-gold-packet-test"
FIXED_TIME = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def cluster(index: int, *, ticker: str, found_by: dict[str, bool] | None = None) -> dict:
    cluster_id = f"event-v2-{index:016x}"
    url = f"https://example.com/news/{index}"
    return {
        "match_cluster_id": cluster_id,
        "ticker": ticker,
        "company": f"Company {ticker}",
        "date": "2026-08-25",
        "headline": f"Event {index}",
        "category": "demand",
        "source_url": url,
        "source_domain": "example.com",
        "source_type": "company",
        "importance": "high" if index == 0 else "medium",
        "earnings_relevance": "direct" if index == 0 else "likely",
        "found_by": found_by or {
            "reference01": True,
            "reference02": True,
            "batch02": True,
            "batch04": True,
        },
        "members": [{
            "observation_id": f"reference01:{ticker}:{index:04d}",
            "source_url": url,
            "source_type": "company",
            "summary": f"Summary {index}",
            "why_it_matters": f"Why {index}",
        }],
    }


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def workspace(tmp_path: Path, *, count: int = 82) -> tuple[Path, dict, dict]:
    events = [cluster(index, ticker=f"{1000 + index // 2:04d}") for index in range(count)]
    union = {
        "schema_version": "company_news_quality_event_union_v2",
        "matcher_schema_version": MATCHER_V2_SCHEMA,
        "benchmark_id": BENCHMARK_ID,
        "search_from": "2026-08-25",
        "search_to": "2026-08-31",
        "events": events,
    }
    directory = (
        tmp_path / "data" / "news_work" / "batch_benchmark" / BENCHMARK_ID
        / "quality_v2" / "matcher_v2"
    )
    write_json(directory / "event_union_v2.json", union)
    gold = _gold_template(BENCHMARK_ID, union, now=FIXED_TIME)
    gold["matcher_schema_version"] = MATCHER_V2_SCHEMA
    gold["source_union_path"] = str((directory / "event_union_v2.json").resolve())
    write_json(directory / "gold_adjudication_v2.json", gold)
    return tmp_path, union, gold


def response(packet: dict, *, verdict: str = "VALID_MATERIAL") -> dict:
    return {
        "schema_version": GOLD_RESPONSE_SCHEMA,
        "benchmark_id": packet["benchmark_id"],
        "packet_id": packet["packet_id"],
        "packet_sha256": packet["packet_sha256"],
        "adjudicated_at": "2026-09-03T12:00:00+09:00",
        "items": [{
            "cluster_id": event["cluster_id"],
            "verdict": verdict,
            "materiality_reason": "Verified material event.",
            "event_exists": True,
            "event_within_window": True,
            "company_specific": True,
            "duplicate_of_cluster_id": None,
            "primary_source_url": event["source_urls"][0]["url"],
            "primary_source_type": "company_ir",
            "primary_source_verified": True,
            "original_source_verified": True,
            "earnings_channel": "revenue",
            "direction": "positive",
            "confidence": "HIGH",
            "notes": "",
            "evidence": [{
                "url": event["source_urls"][0]["url"],
                "source_type": "company_ir",
                "supports": "Confirms the disclosure and date.",
            }],
        } for event in packet["events"]],
    }


def build_fixture_packets(tmp_path: Path) -> tuple[Path, dict, list[dict]]:
    root, union, _ = workspace(tmp_path)
    result = build_gold_packets_v2(root, BENCHMARK_ID)
    packet_dir = Path(result["packet_directory"])
    packets = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(packet_dir.glob("gold_packet_*.json"))]
    return root, union, packets


def test_all_82_clusters_are_packeted_exactly_once_and_company_groups_are_whole(tmp_path):
    _, union, packets = build_fixture_packets(tmp_path)

    ids = [event["cluster_id"] for packet in packets for event in packet["events"]]
    expected = {event["match_cluster_id"] for event in union["events"]}
    ticker_packets: dict[str, set[str]] = {}
    for packet in packets:
        for event in packet["events"]:
            ticker_packets.setdefault(event["ticker"], set()).add(packet["packet_id"])

    assert len(ids) == len(set(ids)) == 82
    assert set(ids) == expected
    assert all(len(packet_ids) == 1 for packet_ids in ticker_packets.values())
    assert all(8 <= packet["event_count"] <= 10 for packet in packets)


def test_packet_order_and_sha_are_deterministic(tmp_path):
    root, _, packets = build_fixture_packets(tmp_path)
    first = [(packet["packet_id"], packet["packet_sha256"], packet["events"]) for packet in packets]

    second_result = build_gold_packets_v2(root, BENCHMARK_ID)
    second_packets = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(Path(second_result["packet_directory"]).glob("gold_packet_*.json"))
    ]

    assert second_result["idempotent"] is True
    assert first == [(packet["packet_id"], packet["packet_sha256"], packet["events"]) for packet in second_packets]
    assert all(_packet_sha256(packet) == packet["packet_sha256"] for packet in packets)


def test_packets_are_blind_to_found_by_and_all_batch_labels(tmp_path):
    _, _, packets = build_fixture_packets(tmp_path)
    text = json.dumps(packets, ensure_ascii=False)

    assert "found_by" not in text
    assert "run_label" not in text
    assert "reference01" not in text
    assert "reference02" not in text
    assert "batch02" not in text
    assert "batch04" not in text


def test_blind_manifest_does_not_contain_provenance(tmp_path):
    root, _, _ = build_fixture_packets(tmp_path)
    path = Path(root) / "data" / "news_work" / "batch_benchmark" / BENCHMARK_ID / "quality_v2" / "matcher_v2" / "gold_packets" / "manifest.json"
    text = path.read_text(encoding="utf-8")

    assert "found_by" not in text
    assert "reference01" not in text
    assert "batch02" not in text


def test_response_schema_accepts_one_complete_valid_packet(tmp_path):
    _, union, packets = build_fixture_packets(tmp_path)
    value = response(packets[0])

    assert validate_gold_adjudication_response_v2(
        value, packets[0], {event["match_cluster_id"] for event in union["events"]}
    ) == value


def test_event_and_original_source_validity_are_independent(tmp_path):
    _, union, packets = build_fixture_packets(tmp_path)
    value = response(packets[0])
    value["items"][0]["original_source_verified"] = False

    validate_gold_adjudication_response_v2(
        value, packets[0], {event["match_cluster_id"] for event in union["events"]}
    )

    assert value["items"][0]["event_exists"] is True
    assert value["items"][0]["primary_source_verified"] is True


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(schema_version="wrong"), "schema_version"),
        (lambda value: value.update(packet_sha256="0" * 64), "SHA"),
        (lambda value: value["items"][0].update(cluster_id="unknown"), "unknown cluster"),
        (lambda value: value["items"].pop(), "every packet cluster"),
        (lambda value: value["items"].append(copy.deepcopy(value["items"][0])), "duplicate cluster"),
        (lambda value: value["items"][0].update(verdict="MATERIAL"), "invalid verdict"),
    ],
)
def test_invalid_response_is_rejected(tmp_path, mutation, message):
    _, union, packets = build_fixture_packets(tmp_path)
    value = response(packets[0])
    mutation(value)

    with pytest.raises(BenchmarkError, match=message):
        validate_gold_adjudication_response_v2(
            value, packets[0], {event["match_cluster_id"] for event in union["events"]}
        )


def test_duplicate_of_is_required_and_validated(tmp_path):
    _, union, packets = build_fixture_packets(tmp_path)
    value = response(packets[0])
    value["items"][0]["verdict"] = "DUPLICATE"

    with pytest.raises(BenchmarkError, match="duplicate target"):
        validate_gold_adjudication_response_v2(
            value, packets[0], {event["match_cluster_id"] for event in union["events"]}
        )

    value["items"][0]["duplicate_of_cluster_id"] = packets[1]["events"][0]["cluster_id"]
    validate_gold_adjudication_response_v2(
        value, packets[0], {event["match_cluster_id"] for event in union["events"]}
    )


def test_matcher_false_negative_also_requires_same_event_target(tmp_path):
    _, union, packets = build_fixture_packets(tmp_path)
    value = response(packets[0])
    value["items"][0]["verdict"] = "MATCHER_FALSE_NEGATIVE"

    with pytest.raises(BenchmarkError, match="duplicate target"):
        validate_gold_adjudication_response_v2(
            value, packets[0], {event["match_cluster_id"] for event in union["events"]}
        )


def test_partial_import_fails_closed_without_touching_gold(tmp_path):
    root, _, packets = build_fixture_packets(tmp_path)
    directory = Path(root) / "data" / "news_work" / "batch_benchmark" / BENCHMARK_ID / "quality_v2" / "matcher_v2"
    gold_path = directory / "gold_adjudication_v2.json"
    before = gold_path.read_bytes()
    invalid_responses = []
    missing = response(packets[0])
    missing["items"].pop()
    invalid_responses.append((missing, "every packet cluster"))
    sha_mismatch = response(packets[0])
    sha_mismatch["packet_sha256"] = "0" * 64
    invalid_responses.append((sha_mismatch, "packet SHA mismatch"))

    for index, (value, message) in enumerate(invalid_responses):
        response_path = tmp_path / f"invalid-{index}.json"
        write_json(response_path, value)
        with pytest.raises(BenchmarkError, match=message):
            import_gold_adjudication_v2(root, BENCHMARK_ID, response_path, now=FIXED_TIME)
        assert gold_path.read_bytes() == before
        assert not (directory / "gold_backups").exists()


def test_import_is_atomic_backed_up_and_idempotent(tmp_path, monkeypatch):
    root, _, packets = build_fixture_packets(tmp_path)
    response_path = tmp_path / "response.json"
    write_json(response_path, response(packets[0]))
    gold_path = Path(root) / "data" / "news_work" / "batch_benchmark" / BENCHMARK_ID / "quality_v2" / "matcher_v2" / "gold_adjudication_v2.json"
    calls: list[Path] = []
    original = benchmark.atomic_write_json

    def recording_write(path, value):
        calls.append(Path(path))
        original(path, value)

    monkeypatch.setattr(benchmark, "atomic_write_json", recording_write)
    backup_directory = gold_path.parent / "gold_backups"
    assert not backup_directory.exists()
    first = import_gold_adjudication_v2(root, BENCHMARK_ID, response_path, now=FIXED_TIME)
    after_first = gold_path.read_bytes()
    second = import_gold_adjudication_v2(root, BENCHMARK_ID, response_path, now=FIXED_TIME)

    assert first["gold_updated"] is True
    assert Path(first["backup_path"]).exists()
    assert calls[-1] == gold_path
    assert second["idempotent"] is True
    assert second["gold_updated"] is False
    assert gold_path.read_bytes() == after_first


def test_gold_status_is_incomplete_until_every_cluster_is_non_unknown(tmp_path):
    root, _, _ = workspace(tmp_path)
    assert gold_status_v2(root, BENCHMARK_ID)["status"] == "GOLD_INCOMPLETE"

    path = Path(root) / "data" / "news_work" / "batch_benchmark" / BENCHMARK_ID / "quality_v2" / "matcher_v2" / "gold_adjudication_v2.json"
    gold = json.loads(path.read_text(encoding="utf-8"))
    for event in gold["events"]:
        event.update({
            "verdict": "VALID_NON_MATERIAL",
            "rationale": "Verified but not material.",
            "adjudicator": "human",
            "adjudicated_at": "2026-09-03T12:00:00+09:00",
        })
    write_json(path, gold)

    status = gold_status_v2(root, BENCHMARK_ID)
    assert status["status"] == "GOLD_COMPLETE"
    assert status["batch03_allowed"] == "REQUIRES_GOLD_METRIC_REVIEW"


def scored_gold() -> dict:
    flags = {
        "reference01": False,
        "reference02": False,
        "batch02": True,
        "batch04": False,
    }
    union = {
        "benchmark_id": BENCHMARK_ID,
        "events": [cluster(0, ticker="1000", found_by=flags)],
    }
    gold = _gold_template(BENCHMARK_ID, union, now=FIXED_TIME)
    gold["events"][0].update({
        "verdict": "VALID_MATERIAL",
        "rationale": "Material customer event.",
        "adjudicator": "human",
        "adjudicated_at": "2026-09-03T12:00:00+09:00",
    })
    return gold


@pytest.mark.parametrize(
    ("run", "recall"),
    [("reference01", 0.0), ("reference02", 0.0), ("batch02", 1.0), ("batch04", 0.0)],
)
def test_each_run_is_scored_against_gold_and_candidate_only_material_is_valid(run, recall):
    metrics = _gold_metrics_v2(scored_gold())

    assert metrics["decision"] == "GOLD_COMPLETE"
    assert metrics["runs"][run]["gold_event_recall"] == recall
    assert metrics["runs"][run]["critical_gold_event_recall"] == recall


def test_duplicate_clusters_are_deduplicated_for_recall_and_precision():
    first = cluster(0, ticker="1000")
    second = cluster(1, ticker="1000", found_by={
        "reference01": False, "reference02": False, "batch02": True, "batch04": False,
    })
    union = {"benchmark_id": BENCHMARK_ID, "events": [first, second]}
    gold = _gold_template(BENCHMARK_ID, union, now=FIXED_TIME)
    gold["events"][0].update({
        "verdict": "VALID_MATERIAL", "rationale": "Material.", "adjudicator": "human",
        "adjudicated_at": "2026-09-03T12:00:00+09:00",
    })
    gold["events"][1].update({
        "verdict": "MATCHER_FALSE_NEGATIVE", "rationale": "Same event.",
        "adjudicator": "human", "adjudicated_at": "2026-09-03T12:00:00+09:00",
        "duplicate_of_cluster_id": first["match_cluster_id"],
    })

    metrics = _gold_metrics_v2(gold)

    assert metrics["gold_material_event_count"] == 1
    assert metrics["deduplicated_gold_event_count"] == 1
    assert metrics["runs"]["batch02"]["gold_event_recall"] == 1.0
    assert metrics["runs"]["batch02"]["gold_material_precision"] == 1.0


def test_non_material_discovery_is_in_precision_denominator():
    gold = scored_gold()
    second = copy.deepcopy(gold["events"][0])
    second.update({
        "match_cluster_id": "event-v2-nonmaterial",
        "ticker": "1001",
        "verdict": "VALID_NON_MATERIAL",
        "rationale": "Verified but not investor-material.",
        "importance": "low",
        "earnings_relevance": "general",
    })
    gold["events"].append(second)

    metrics = _gold_metrics_v2(gold)

    assert metrics["runs"]["batch02"]["precision_denominator_count"] == 2
    assert metrics["runs"]["batch02"]["gold_material_precision"] == 0.5


def test_packet_files_use_declared_schema_and_hash_strategy(tmp_path):
    _, _, packets = build_fixture_packets(tmp_path)

    assert all(packet["schema_version"] == GOLD_PACKET_SCHEMA for packet in packets)
    assert all(packet["response_contract"]["schema_version"] == GOLD_RESPONSE_SCHEMA for packet in packets)
    assert all(packet["packet_sha256"] == _packet_sha256(packet) for packet in packets)
