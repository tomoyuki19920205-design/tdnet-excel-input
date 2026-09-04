"""Offline synthetic fixtures only; no production access or research execution."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from tools import company_news_batch_benchmark as b
from tools import company_news_post_run_v2 as p

BID = "batchbench-post-test"


def event(i=0):
    return {"ticker": str(1000+i), "published_at": "2026-08-25T00:00:00+09:00",
            "headline": f"Specific project {i} capacity expansion", "category": "capacity",
            "source_url": f"https://example.com/news/{i}", "source_type": "company",
            "importance": "high" if i < 7 else "medium", "earnings_relevance": "likely",
            "summary": f"Plant {i} added a new production line", "why_it_matters": "Higher sales capacity"}


def truth():
    clusters = []
    for i in range(82):
        e = b._event_public_fields(event(i))
        clusters.append({**e, "match_cluster_id": f"gold-{i}", "company": f"Company{i}",
                         "members": [e], "found_by": {label: i < limit for label, limit in
                         {"reference01": 25, "reference02": 22, "batch02": 19, "batch04": 17}.items()}})
    union = {"benchmark_id": BID, "events": clusters}
    gold = b._gold_template(BID, union)
    gold["matcher_schema_version"] = b.MATCHER_V2_SCHEMA
    for i, e in enumerate(gold["events"]):
        e.update(verdict="VALID_MATERIAL" if i < 29 else "VALID_NON_MATERIAL",
                 rationale="Verified", adjudicator="fixture", adjudicated_at="2026-09-04T00:00:00+09:00",
                 primary_source_type="company_ir" if i < 12 else "company_news" if i < 17 else "government" if i < 21 else "secondary")
    return gold, union


def status():
    return {"completed": 192, "total": 192, "failed": 0, "pending": 0}


def test_completed_192_gold29_critical7():
    gold, union = truth()
    p.validate_evaluation_inputs(gold, union, status())
    s = p.score(gold, {"gold-0", "gold-7"})
    assert s["material"] == {"target": 29, "found": 2, "missed": 27, "recall": 2/29}
    assert s["critical"]["target"] == 7
    assert s["noncritical"]["target"] == 22
    assert s["official"]["target"] == 21


@pytest.mark.parametrize("key,value", [("completed", 191), ("failed", 1), ("pending", 1), ("total", 191)])
def test_incomplete_or_failed_run_rejected(key, value):
    gold, union = truth()
    state = status()
    state[key] = value
    with pytest.raises(b.BenchmarkError):
        p.validate_evaluation_inputs(gold, union, state)


@pytest.mark.parametrize("change", ["unknown", "material", "critical", "sha"])
def test_gold_integrity_fail_closed(change):
    gold, union = truth()
    if change == "unknown":
        gold["events"][0]["verdict"] = "UNKNOWN"
    elif change == "material":
        gold["events"][0]["verdict"] = "VALID_NON_MATERIAL"
    elif change == "critical":
        gold["events"][0]["importance"] = "medium"
    else:
        gold["source_union_sha256"] = "wrong"
    with pytest.raises(b.BenchmarkError):
        p.validate_evaluation_inputs(gold, union, status())


def test_known_gold_uses_v2_and_novel_not_false_positive(monkeypatch):
    gold, union = truth()
    calls = []
    original = b._node_identity_evidence_v2
    def spy(left, right):
        calls.append((left, right))
        return original(left, right)
    monkeypatch.setattr(b, "_node_identity_evidence_v2", spy)
    nodes = p.observation_nodes({"1000": [event(0)], "1099": [event(99)]}, "batch04v2")
    matches, novel, ambiguous = p.match_nodes(nodes, union, gold)
    assert calls and len(matches) == len(novel) == 1 and not ambiguous
    result = b.strategy_v2_known_gold_metrics(gold, {"gold-0": "official_first"}, [event(99)])
    assert result["precision"] is None
    assert result["candidate_only_events_are_false_positives"] is False


def packets():
    nodes, _, _ = p.strict_nodes({str(1100+i): [event(100+i)] for i in range(18)}, "batch04v2")
    return p.extension_packets(BID, nodes, {}, ("2026-08-25", "2026-08-31"))


def test_extension_blind_excludes_gold_deterministic_sha():
    result = packets()
    assert result == packets()
    assert [len(x["events"]) for x in result] == [9, 9]
    for packet in result:
        assert b._packet_sha256(packet) == packet["packet_sha256"]
        assert all(e["cluster_id"].startswith("ext-") for e in packet["events"])
    text = json.dumps(result)
    assert not any(key in text for key in ("batch04", "found_by", "strategy", "reference01", "matcher_evidence"))


def test_same_day_is_not_dedupe_identity():
    left, right = event(99), event(100)
    right["ticker"] = left["ticker"]
    right["headline"] = "Dividend payment update"
    right["summary"] = "Dividend per share announced"
    nodes, _, _ = p.strict_nodes({left["ticker"]: [left, right]})
    assert len(nodes) == 2


def test_mirror_dedupe_and_observation_provenance():
    left = event(99)
    right = {**left, "source_url": left["source_url"] + "?utm_source=mirror"}
    nodes, _, _ = p.strict_nodes({left["ticker"]: [left, right]}, "batch04v2")
    assert len(nodes) == 1
    assert all(m["run_label"] == "batch04v2" for m in nodes[0]["members"])


def test_rescue_and_regression_attribution():
    gold, union = truth()
    values = {str(1000+i): [event(i)] for i in [*range(1, 17), 18]}
    matches, _, _ = p.match_nodes(p.observation_nodes(values, "batch04v2"), union, gold)
    companies = [{"ticker": "1018", "candidate_provenance": [{"source_url": event(18)["source_url"],
                  "stage": "targeted_second_pass", "selected_for_output": True}]}]
    result = p.rescue_analysis(gold, matches, companies)
    assert result["rescues"][0]["cluster_id"] == "gold-18"
    assert result["regressions"][0]["cluster_id"] == "gold-0"
    assert result["regressions"][0]["critical"]
    assert result["rescue_stage_counts"] == {"targeted_second_pass": 1}


def test_schedule_reconstruction_48_and_gap_throughput():
    start = datetime(2026, 9, 3, 0, 0, tzinfo=timezone(timedelta(hours=9)))
    manifest = {"activated_at": start.isoformat(), "completed_at": (start+timedelta(hours=7)).isoformat(),
                "company_count": 192, "batch_size": 4}
    runs = []
    for h in range(6):
        for i, minute in enumerate(p.MINUTES, 1):
            at = start + timedelta(hours=h, minutes=minute, seconds=60)
            runs.append({"task_id": f"task{i:02}", "run_id": f"r{h}-{i}", "task_started_at": at.isoformat(),
                         "task_finished_at": (at+timedelta(minutes=8)).isoformat(), "success_count": 4,
                         "snapshot_company_count": 4, "task_elapsed_seconds": 480})
    result = p.scheduler_coverage(manifest, runs, [], [])
    assert result["productive_run_count"] == result["expected_productive_runs"] == 48
    assert result["expected_schedule_opportunities"] == 57
    assert result["classification_counts"]["NO_ROUTE_OBSERVED"] == 9
    assert result["overlap"] == result["busy_skip"] == 0
    assert result["wall_clock_companies_per_hour"] == 192/7
    assert result["scheduler_adjusted_companies_per_hour"] == result["theoretical_companies_per_hour"] == 32
    assert result["route_attempts"] is None


def test_busy_skip_classification():
    manifest = {"activated_at": "2026-09-03T00:00:00+09:00", "completed_at": "2026-09-03T00:05:00+09:00",
                "company_count": 192, "batch_size": 4}
    result = p.scheduler_coverage(manifest, [], [{"task_id": "task01", "at": "2026-09-03T00:01:00+09:00", "event": "busy_skip"}], [])
    assert result["classification_counts"] == {"BUSY_SKIP": 1}


def response(packet):
    return {"schema_version": b.GOLD_RESPONSE_SCHEMA, "benchmark_id": BID,
            "packet_id": packet["packet_id"], "packet_sha256": packet["packet_sha256"],
            "adjudicated_at": "2026-09-04T00:00:00+09:00", "items": [{
                "cluster_id": e["cluster_id"], "verdict": "VALID_MATERIAL", "materiality_reason": "Revenue impact",
                "event_exists": True, "event_within_window": True, "company_specific": True,
                "duplicate_of_cluster_id": None, "primary_source_url": e["source_urls"][0]["url"],
                "primary_source_type": "company_ir", "primary_source_verified": True,
                "original_source_verified": True, "earnings_channel": "revenue", "direction": "positive",
                "confidence": "HIGH", "notes": "Fixture", "evidence": [{"url": e["source_urls"][0]["url"],
                "source_type": "company_ir", "supports": "Dated disclosure"}]} for e in packet["events"]]}


def extension_fixture(root):
    gold, union = truth()
    d = b._matcher_v2_directory(root, BID)
    ext = d / "gold_extension_batch04v2"
    ps = packets()
    p.write_new(d / "gold_adjudication_v2.json", gold)
    p.write_new(d / "event_union_v2.json", union)
    for packet in ps:
        p.write_new(ext / f"{packet['packet_id']}.json", packet)
    p.write_new(ext / "gold_extension_manifest.json", {
        "benchmark_id": BID, "source_gold_sha256": b._json_sha256(gold), "source_union_sha256": b._json_sha256(union),
        "packets": [{"packet_id": x["packet_id"], "packet_sha256": x["packet_sha256"],
                     "filename": x["packet_id"] + ".json", "cluster_ids": [e["cluster_id"] for e in x["events"]]} for x in ps]})
    return d, ext, ps


def test_extension_import_idempotent_original_gold_unchanged(tmp_path):
    d, ext, ps = extension_fixture(tmp_path)
    before = p.hashes([d/"gold_adjudication_v2.json", d/"event_union_v2.json"])
    path = tmp_path / "response.json"
    p.write_new(path, response(ps[0]))
    for _ in range(2):
        result = p.import_extension(tmp_path, BID, "batch04v2", path)
        assert result["existing_gold_modified"] is False
    assert len(list((ext / "responses").glob("*.json"))) == 1
    assert before == p.hashes([d/"gold_adjudication_v2.json", d/"event_union_v2.json"])


@pytest.mark.parametrize("mutation", ["sha", "partial", "unknown", "evidence", "cycle"])
def test_extension_import_fail_closed(tmp_path, mutation):
    d, ext, ps = extension_fixture(tmp_path)
    before = p.hashes(d.rglob("*"))
    value = response(ps[0])
    if mutation == "sha":
        value["packet_sha256"] = "wrong"
    elif mutation == "partial":
        value["items"].pop()
    elif mutation == "unknown":
        value["items"][0]["verdict"] = "UNKNOWN"
    elif mutation == "evidence":
        value["items"][0]["evidence"] = []
    else:
        for i, target in ((0, 1), (1, 0)):
            value["items"][i]["verdict"] = "DUPLICATE"
            value["items"][i]["duplicate_of_cluster_id"] = value["items"][target]["cluster_id"]
    path = tmp_path / "response.json"
    p.write_new(path, value)
    with pytest.raises(b.BenchmarkError):
        p.import_extension(tmp_path, BID, "batch04v2", path)
    assert not (ext / "responses").exists()
    assert before == p.hashes(d.rglob("*"))


def test_immutable_writer_rejects_differing_packet(tmp_path):
    path = tmp_path / "packet.json"
    p.write_new(path, {"a": 1})
    with pytest.raises(b.BenchmarkError):
        p.write_new(path, {"a": 2})
    assert json.loads(path.read_text()) == {"a": 1}
