"""Offline Extended-Gold evaluator: frozen inputs, new analysis outputs only.

Run with ``python -m tools.company_news_extended_gold_v2 --benchmark-id ID``.
No research, adjudication, imports, database access, or scheduling operations.
"""
from __future__ import annotations

import argparse
import copy
import json
from collections import Counter, defaultdict
from pathlib import Path

from tools import company_news_batch_benchmark as b
from tools import company_news_post_run_v2 as p

SCORABLE = {"VALID_MATERIAL", "VALID_NON_MATERIAL", "INVALID_SOURCE", "OUT_OF_WINDOW"}
DUPLICATES = {"DUPLICATE", "MATCHER_FALSE_NEGATIVE"}
EXPECTED = {"VALID_MATERIAL": 67, "VALID_NON_MATERIAL": 96, "OUT_OF_WINDOW": 2}
RUNS = {**b.QUALITY_RUN_LABELS, "batch04v2": "batch04v2"}


def require(condition, message):
    if not condition:
        raise b.BenchmarkError(message)


def load_extension(directory, gold, union, *, expected=EXPECTED, packet_count=18):
    manifest = b._read_json(directory / "gold_extension_manifest.json")
    require(manifest["benchmark_id"] == gold["benchmark_id"] == union["benchmark_id"], "benchmark mismatch")
    require(manifest["source_gold_sha256"] == b._json_sha256(gold), "frozen Gold SHA mismatch")
    require(manifest["source_union_sha256"] == b._json_sha256(union), "frozen union SHA mismatch")
    records = manifest["packets"]
    require(len(records) == packet_count, "extension packet count mismatch")
    require(len({x["packet_id"] for x in records}) == packet_count, "duplicate packet")
    original_ids = {x["match_cluster_id"] for x in gold["events"]}
    all_ids = [i for record in records for i in record["cluster_ids"]]
    require(len(set(all_ids)) == len(all_ids) and not original_ids.intersection(all_ids), "cluster coverage overlap")
    allowed_ids = original_ids | set(all_ids)
    expected_paths = {r["packet_id"] + ".json" for r in records}
    require({x.name for x in (directory / "responses").glob("*.json")} == expected_paths, "missing/extra imported response")
    events, rows, packets = [], [], []
    for record in records:
        require(record["filename"] == record["packet_id"] + ".json" and
                Path(record["filename"]).name == record["filename"], "unsafe packet path")
        packet = b._read_json(directory / record["filename"])
        response = b._read_json(directory / "responses" / record["filename"])
        require(packet["packet_id"] == record["packet_id"] and packet["benchmark_id"] == manifest["benchmark_id"], "packet identity mismatch")
        require(packet["packet_sha256"] == record["packet_sha256"] == b._packet_sha256(packet), "packet SHA mismatch")
        require([x["cluster_id"] for x in packet["events"]] == record["cluster_ids"], "manifest coverage mismatch")
        b.validate_gold_adjudication_response_v2(response, packet, allowed_ids)
        require(all(x["verdict"] != "UNKNOWN" and x["evidence"] for x in response["items"]), "incomplete adjudication")
        events.extend(packet["events"])
        rows.extend(response["items"])
        packets.append({"packet_id": packet["packet_id"], "packet_sha256": packet["packet_sha256"],
                        "cluster_count": len(packet["events"]), "schema": "PASS", "imported": True})
    counts = Counter(x["verdict"] for x in rows)
    require(counts == Counter(expected) and len(rows) == sum(expected.values()), "extension verdict totals mismatch: STOP")
    return events, rows, {"packets": packets, "packet_count": len(packets), "cluster_count": len(rows),
                         "imported_response_count": len(packets), "verdict_counts": dict(sorted(counts.items())),
                         "material_rate": counts["VALID_MATERIAL"] / len(rows),
                         "nonmaterial_rate": counts["VALID_NON_MATERIAL"] / len(rows),
                         "import_evidence": "Importer persists the validated response itself; no separate import ledger exists."}


def extension_member(event, source):
    return b._event_public_fields({"ticker": event["ticker"],
        "published_at": event["event_date"] + "T00:00:00+09:00", "headline": event["canonical_headline"],
        "category": "other", "source_url": source["url"], "source_type": source["source_type"],
        "summary": event["summary"], "why_it_matters": event["why_it_matters"]})


def normalize_truth(gold, union, extension, responses):
    """In-memory normalization only; never infer materiality or criticality."""
    known_ids = {x["match_cluster_id"] for x in gold["events"]}
    truth = copy.deepcopy(gold)
    clusters = copy.deepcopy(union["events"])
    rows = {x["cluster_id"]: x for x in responses}
    for event in extension:
        ident = event["cluster_id"]
        truth["events"].append({**copy.deepcopy(event), **copy.deepcopy(rows[ident]),
            "match_cluster_id": ident, "date": event["event_date"], "headline": event["canonical_headline"],
            "importance": None, "earnings_relevance": None})
        clusters.append({"match_cluster_id": ident, "ticker": event["ticker"],
                         "members": [extension_member(event, s) for s in event["source_urls"]]})
    by_id, groups = b._gold_normalized_groups_v2(truth)  # validates duplicate graph
    root_of = {x["match_cluster_id"]: root for root, members in groups.items() for x in members}
    merges, review = [], []
    # Cross-set HIGH/auto identity only, and never resolve a verdict conflict by guessing.
    for ext in clusters:
        if ext["match_cluster_id"] in known_ids or by_id[ext["match_cluster_id"]]["verdict"] in DUPLICATES:
            continue
        options = []
        for old in clusters:
            if old["match_cluster_id"] not in known_ids or old["ticker"] != ext["ticker"]:
                continue
            evidence = b._node_identity_evidence_v2(ext, old)
            if evidence["auto_merge"]:
                options.append({"root_id": root_of[old["match_cluster_id"]], "evidence": evidence})
        roots = {x["root_id"] for x in options}
        if len(roots) == 1 and by_id[next(iter(roots))]["verdict"] == by_id[ext["match_cluster_id"]]["verdict"]:
            target = next(iter(roots))
            by_id[ext["match_cluster_id"]].update(verdict="DUPLICATE", duplicate_of_cluster_id=target)
            merges.append({"extension_id": ext["match_cluster_id"], "canonical_id": target, "evidence": options})
        elif options:
            review.append({"extension_id": ext["match_cluster_id"], "reason": "ambiguous identity or conflicting frozen verdict", "options": options})
    by_id, groups = b._gold_normalized_groups_v2(truth)
    root_of = {x["match_cluster_id"]: root for root, group in groups.items() for x in group}
    require(all(len({x["ticker"] for x in group}) == 1 for group in groups.values()), "cross-company duplicate")
    return truth, {"events": clusters}, root_of, {"cross_set_merges": merges, "cross_set_review": review,
        "known_cluster_count": len(known_ids), "normalized_root_count": len(groups),
        "material_total": sum(by_id[k]["verdict"] == "VALID_MATERIAL" for k in groups),
        "truth_event_count": sum(by_id[k]["verdict"] in {"VALID_MATERIAL", "VALID_NON_MATERIAL"} for k in groups),
        "extension_criticality": "NOT_ADJUDICATED: critical metric uses frozen Known-Gold labels only; candidate importance is not truth."}


def classify_outputs(events, label, union, root_of, known_ids):
    """Preserve frozen original membership, then match remaining raw observations.

    Never dedupe different known events merely because they share an archive URL.
    Matched mirrors collapse by root; unmatched observations use strict v2 dedupe.
    """
    result = []
    for node in p.observation_nodes(events, label):
        m = node["members"][0]
        exact = []
        for cluster in union["events"]:
            if cluster["match_cluster_id"] not in known_ids:
                continue
            if any(x.get("observation_id") == m["observation_id"] and
                   x["headline"] == m["headline"] and x["normalized_source_url"] == m["normalized_source_url"]
                   for x in cluster["members"]):
                exact.append({"cluster_id": cluster["match_cluster_id"], "method": "frozen_observation_membership"})
        options = exact
        if not options:
            options = []
            for cluster in union["events"]:
                if cluster["ticker"] != node["ticker"]:
                    continue
                evidence = b._node_identity_evidence_v2(node, cluster)
                if evidence["auto_merge"]:
                    options.append({"cluster_id": cluster["match_cluster_id"], "evidence": evidence})
        roots = {root_of[x["cluster_id"]] for x in options}
        result.append({"observation_id": m["observation_id"], "ticker": m["ticker"],
            "member": m, "root_id": next(iter(roots)) if len(roots) == 1 else None,
            "status": "MATCHED" if len(roots) == 1 else "AMBIGUOUS" if roots else "UNADJUDICATED",
            "origin": "existing_gold" if any(x["cluster_id"] in known_ids for x in options) else "extension" if options else None,
            "options": options})
    return result


def cohort_metrics(truth, classified, membership):
    by_id, groups = b._gold_normalized_groups_v2(truth)
    scoped = {k for k in groups if by_id[k]["ticker"] in membership}
    rows = [x for x in classified if x["ticker"] in membership]
    found = {x["root_id"] for x in rows if x["root_id"]} & scoped
    material = {k for k in scoped if by_id[k]["verdict"] == "VALID_MATERIAL"}
    critical = {k for k in material if by_id[k].get("importance") == "high" or by_id[k].get("earnings_relevance") == "direct"}
    # Missing critical labels are not evidence of noncriticality.
    noncritical = {k for k in material - critical if by_id[k].get("importance") is not None}
    unclassified_criticality = material - critical - noncritical
    denominator = {k for k in found if by_id[k]["verdict"] in SCORABLE}
    remaining = defaultdict(list)
    for row in rows:
        if row["root_id"] is None:
            remaining[row["ticker"]].append(row["member"])
    unmatched, _, _ = p.strict_nodes(remaining)
    mt = {by_id[k]["ticker"] for k in material}
    ft = {by_id[k]["ticker"] for k in material & found}
    at = {x["ticker"] for x in rows}
    def metric(ids):
        return {"target": len(ids), "found": len(ids & found), "recall": len(ids & found)/len(ids) if ids else None}
    verdicts = Counter(by_id[k]["verdict"] for k in found)
    return {"company_count": len(membership), "material": metric(material), "critical": metric(critical),
        "noncritical_known_labels": metric(noncritical), "extension_criticality_unclassified": len(unclassified_criticality),
        "material_excluding_known_critical": metric(material-critical),
        "material_precision": len(material & found)/len(denominator) if denominator else None,
        "precision_numerator": len(material & found), "precision_denominator": len(denominator),
        "precision_status": "COMPLETE_FOR_OBSERVED_OUTPUTS" if not unmatched else "PARTIAL_ADJUDICATED_OUTPUTS_ONLY",
        "precision_lower_bound_including_unresolved": len(material & found)/(len(denominator)+len(unmatched)) if denominator or unmatched else None,
        "raw_output_count": len(rows), "normalized_output_count": len(found)+len(unmatched),
        "matched_verdict_counts": dict(sorted(verdicts.items())), "unresolved_unique_count": len(unmatched),
        "company_level_material_FN": len(mt-ft), "company_level_material_FN_tickers": sorted(mt-ft),
        "no_news_FN": len(mt-at), "no_news_FN_tickers": sorted(mt-at),
        "material_missed_ids": sorted(material-found), "critical_missed_ids": sorted(critical-found),
        "found_ids": sorted(found)}


def common_membership(cohort, researched):
    common = set(cohort["reference_tickers"])
    require(len(common) == 48, "COMMON48 count mismatch")
    require(common == researched["reference01"] == researched["reference02"], "reference research membership mismatch")
    require(all(common <= tickers for tickers in researched.values()), "common company not researched")
    return common


def coverage_inventory(classified):
    counts = Counter(x["origin"] for x in classified if x["root_id"])
    unresolved = [x for x in classified if x["root_id"] is None]
    values = defaultdict(list)
    for row in unresolved:
        values[row["ticker"]].append(row["member"])
    nodes, _, _ = p.strict_nodes(values)
    return {"total_candidate_events": len(classified), "existing_gold_matched_raw": counts["existing_gold"],
        "extension_matched_raw": counts["extension"], "not_yet_adjudicated_candidate_only_raw": sum(x["status"] == "UNADJUDICATED" for x in unresolved),
        "ambiguous_raw": sum(x["status"] == "AMBIGUOUS" for x in unresolved),
        "unresolved_unique": len(nodes), "unresolved_observations": unresolved}


def stage_attribution(extension, responses, classified, companies):
    rows = {x["cluster_id"]: x for x in responses}
    telemetry = {x["ticker"]: x for x in companies}
    details, counts, buckets = [], defaultdict(Counter), defaultdict(lambda: defaultdict(Counter))
    for event in extension:
        ident = event["cluster_id"]
        members = [x["member"] for x in classified if any(o["cluster_id"] == ident for o in x["options"])]
        stages = set()
        for member in members:
            for candidate in telemetry[event["ticker"]].get("candidate_provenance", []):
                # Require both headline and URL, not just a reused IR listing URL.
                if candidate.get("selected_for_output") and candidate["headline"] == member["headline"] and b._normalized_url(candidate["source_url"]) == member["normalized_source_url"]:
                    stages.add(candidate["stage"])
        stage = next(iter(stages)) if len(stages) == 1 else "MULTI_STAGE" if stages else "UNRESOLVED"
        verdict = rows[ident]["verdict"]
        bucket = b._gold_source_bucket(rows[ident]["primary_source_type"])
        counts[stage][verdict] += 1
        buckets[stage][bucket][verdict] += 1
        details.append({"cluster_id": ident, "stage": stage, "stages_observed": sorted(stages), "verdict": verdict, "source_bucket": bucket,
                        "unresolved_reason": "No exact selected headline/URL join; a source substitution must not be assigned a stage by assumption." if not stages else None})
    summary = {}
    for stage in [*p.STAGES, "MULTI_STAGE", "UNRESOLVED"]:
        c = counts[stage]
        summary[stage] = {"verdict_counts": dict(sorted(c.items())), "total": sum(c.values()),
                          "material_rate": c["VALID_MATERIAL"]/sum(c.values()) if c else None,
                          "source_buckets": {k: dict(sorted(v.items())) for k, v in sorted(buckets[stage].items())}}
    return {"summary": summary, "events": details, "basis": "Frozen selected candidate provenance joined on ticker + headline + normalized URL; no inferred stage."}


def critical_detail(truth, union, missed, baseline, candidate, companies):
    by_id, groups = b._gold_normalized_groups_v2(truth)
    telemetry = {x["ticker"]: x for x in companies}
    result = []
    for ident in missed:
        g = by_id[ident]
        t = telemetry[g["ticker"]]
        sources = {b._normalized_url(x) for x in t.get("official_sources_found", []) + t.get("external_sources_found", [])}
        target = b._normalized_url(g.get("primary_source_url") or g.get("source_url", ""))
        selected = [x for x in t.get("candidate_provenance", []) if b._normalized_url(x["source_url"]) == target]
        # Absence from a self-reported source list is not proof the source was never opened.
        if selected:
            classification = "CATEGORY/MATCHER_ISSUE" if any(x["selected_for_output"] for x in selected) else "SOURCE_REACHED_EVENT_NOT_SELECTED"
            confidence = "MEDIUM"
        elif target in sources:
            classification, confidence = "SOURCE_REACHED_EVENT_NOT_SELECTED", "MEDIUM"
        else:
            classification, confidence = "SOURCE_NOT_REACHED", "MEDIUM"
        result.append({"cluster_id": ident, "ticker": g["ticker"], "company": g["company"],
            "date": g["date"], "headline": g["headline"], "gold_source_type": g.get("primary_source_type"),
            "gold_source_url": g.get("primary_source_url"), "gold_channel": g.get("earnings_channel"),
            "gold_rationale": g.get("materiality_reason", g.get("rationale")),
            "baseline_result": [x for x in baseline if x["root_id"] == ident],
            "candidate_result": "MISSED_UNDER_FROZEN_MATCHER", "candidate_outputs": [x for x in candidate if x["ticker"] == g["ticker"]],
            "telemetry": t, "classification": classification, "confidence": confidence,
            "document_url_in_source_telemetry": target in sources,
            "document_url_in_candidate_provenance": bool(selected),
            "limit": "Document-level reach/selection is inferred from persisted telemetry, not browser traces. Related IR listings or publication notices do not prove extraction of the Gold event's substantive content. Compression/query causality cannot be established."})
    return result


def decision(common, gates, coverage, cross_review):
    candidate = common["batch04v2"]
    critical_pass = candidate["critical"]["found"] == candidate["critical"]["target"]
    recall_pass = (candidate["material"]["recall"] or 0) >= .95
    warnings = []
    if not critical_pass:
        warnings.append("WARN_BATCH04V2_CRITICAL_REGRESSION")
    if any(x["unresolved_unique"] for x in coverage.values()):
        warnings.append("WARN_BATCH04V2_FULL192_GOLD_INCOMPLETE")
    if cross_review:
        warnings.append("WARN_CROSS_SET_NORMALIZATION_REVIEW")
    quality = "FAIL_BATCH04V2_QUALITY" if not critical_pass or not recall_pass else "WARN_BATCH04V2_PRECISION_GATE_UNSPECIFIED"
    return {"quality_verdict": quality,
        "warnings": warnings, "production_candidate": False, "batch03": "ON_HOLD",
        "gates": {"common48_recall_at_least_95pct": recall_pass, "known_critical_100pct": critical_pass,
                  "precision": {"value": candidate["material_precision"], "threshold": None, "status": "REQUIRES_EXPLICIT_ACCEPTANCE_THRESHOLD"},
                  "runtime_capacity": "PASS" if all(gates.values()) else "FAIL",
                  "scheduler_reliability": "SEPARATE_SCHEDULER_RELIABILITY_ISSUE"},
        "next_action": "Review critical document-level extraction and matcher evidence without re-adjudicating Gold. Define a precision acceptance threshold. Do not deploy; investigate scheduler separately after quality architecture decision.",
        "final_judgment": "WARN_BATCH04V2_EXTENDED_GOLD_REQUIRES_REVIEW"}


def evaluate(root, benchmark_id):
    directory = b._matcher_v2_directory(root, benchmark_id)
    benchmark = directory.parent.parent
    ext = directory / "gold_extension_batch04v2"
    protected = list((benchmark / "runs").rglob("*")) + [benchmark / "cohort.json"]
    protected += list(ext.rglob("*")) + [directory / "gold_adjudication_v2.json", directory / "event_union_v2.json"]
    protected += list((directory / "gold_packets").rglob("*")) + list((directory / "gold_responses").rglob("*"))
    for run in RUNS:
        m = b._read_json(b._run_dir(root, benchmark_id, run) / "manifest.json")
        output = Path(m["output_directory"]).resolve()
        require(output.is_relative_to((root / "data/news_benchmark_inbox" / benchmark_id).resolve()), "non-benchmark input path")
        protected += list(output.rglob("*"))
    before = p.hashes(protected)
    gold = b._read_json(directory / "gold_adjudication_v2.json")
    union = b._read_json(directory / "event_union_v2.json")
    p.validate_evaluation_inputs(gold, union, b.benchmark_status(root, benchmark_id, "batch04v2"))
    extension, responses, integrity = load_extension(ext, gold, union)
    truth, extended_union, root_of, normalization = normalize_truth(gold, union, extension, responses)
    known_ids = {x["match_cluster_id"] for x in gold["events"]}
    manifests, events, companies, classified, runtime, researched = {}, {}, {}, {}, {}, {}
    cohort = b._load_cohort(root, benchmark_id)
    for run, label in RUNS.items():
        m, ev, cs = b._completed_run_events(root, benchmark_id, run)
        require((m["search_from"], m["search_to"]) == (cohort["search_from"], cohort["search_to"]), "window mismatch")
        require(m.get("production_write_forbidden") is True and m["status"] == "completed", "unsafe/incomplete run")
        order = {x["ticker"] for x in b._ordered_run_assignments(b._run_dir(root, benchmark_id, run))}
        require(order == {x["ticker"] for x in cs} and len(cs) == len(order) == m["company_count"], "research coverage mismatch")
        manifests[label], events[label], companies[label], researched[label] = m, ev, cs, order
        classified[label] = classify_outputs(ev, label, extended_union, root_of, known_ids)
        task = [x for x in b._telemetry(b._run_dir(root, benchmark_id, run), "runs.jsonl") if x["snapshot_company_count"] > 0]
        runtime[label] = {"all_researched": {"companies": len(cs), "source_count": b._summary([x["source_count"] for x in cs]),
                          "research_seconds": b._summary([x["research_elapsed_seconds"] for x in cs])},
                          "productive_task_seconds": b._summary([x["task_elapsed_seconds"] for x in task])}
    common = common_membership(cohort, researched)
    full = {x["ticker"] for x in cohort["companies"]}
    require(len(full) == 192 and all(researched[x] == full for x in ("batch02", "batch04", "batch04v2")), "FULL192 membership mismatch")
    common_scores = {label: cohort_metrics(truth, rows, common) for label, rows in classified.items()}
    known_scores = {}
    for label, ev in events.items():
        matches, _, _ = p.match_nodes(p.observation_nodes(ev, label), union, gold)
        if label != "batch04v2":
            # Original run membership is frozen in the adjudicated union.
            _, groups = b._gold_normalized_groups_v2(gold)
            discovered = {k for k, group in groups.items() if any(x.get("found_by", {}).get(label) for x in group)}
        else:
            discovered = {x["root_id"] for x in matches}
        known_scores[label] = p.score(gold, discovered)
    require({k:v["material"]["found"] for k,v in known_scores.items()} ==
            {"reference01":25, "reference02":22, "batch02":19, "batch04":17, "batch04v2":16}, "known Gold regression mismatch")
    inventory = {label: coverage_inventory(classified[label]) for label in ("batch02", "batch04", "batch04v2")}
    for label, cs in companies.items():
        selected = [x for x in cs if x["ticker"] in common]
        runtime[label]["common48"] = {"source_count": b._summary([x["source_count"] for x in selected]),
                                       "research_seconds": b._summary([x["research_elapsed_seconds"] for x in selected])}
    run_dir = b._run_dir(root, benchmark_id, "batch04v2")
    snapshots = [b._read_json(x) for x in sorted((run_dir / "tasks").glob("task??/history/*.json"))]
    snapshots = [x for x in snapshots if x.get("schema_version") == b.SNAPSHOT_SCHEMA]
    scheduler = p.scheduler_coverage(manifests["batch04v2"], b._telemetry(run_dir, "runs.jsonl"), b._task_events(run_dir), snapshots)
    t = runtime["batch04v2"]["productive_task_seconds"]
    gates = {"p95_under_45min": t["p95"] < 2700, "max_under_55min": t["max"] < 3300,
             "busy_skip_zero": scheduler["busy_skip"] == 0, "overlap_zero": scheduler["overlap"] == 0}
    full_score = cohort_metrics(truth, classified["batch04v2"], full)
    details = critical_detail(truth, extended_union, full_score["critical_missed_ids"], classified["batch04"], classified["batch04v2"], companies["batch04v2"])
    result = {"schema_version": "company_news_extended_gold_final_audit_v1", "benchmark_id": benchmark_id,
        "extension": integrity, "normalization": normalization, "known_gold_scores": known_scores,
        "common48": {"tickers": sorted(common), "membership_basis": "Persisted reference_tickers == both reference assignment/telemetry sets",
                     "scores": common_scores, "caveat": "Shared cohort and adjudicated observed union, not exhaustive web truth; extension sampling is candidate-originated."},
        "full192": {"status": "PARTIAL_GOLD_COVERAGE" if any(x["unresolved_unique"] for x in inventory.values()) else "FAIR_FULL192_COMPARISON",
                    "self_score_label": "batch04v2_FULL192_SELF_SCORE", "batch04v2": full_score, "inventory": inventory,
                    "additional_adjudication_proposal": "For fair Full192 comparison, blind-adjudicate the unresolved union across baseline candidates; do not start research in this audit."},
        "precision_contract": "Canonical _gold_metrics_v2: unique discovered roots with verdict VALID_MATERIAL / VALID_NON_MATERIAL / INVALID_SOURCE / OUT_OF_WINDOW. Mirrors and duplicate chains count once. OUT_OF_WINDOW excluded from truth targets but INCLUDED in precision denominator. Unresolved output is not silently false positive; show adjudicated-only precision and conservative bound.",
        "critical_regressions": details, "stage_attribution": stage_attribution(extension, responses, classified["batch04v2"], companies["batch04v2"]),
        "sanity_check": {"unmerged_material_sum": sum(x["verdict"] == "VALID_MATERIAL" for x in gold["events"]) + integrity["verdict_counts"]["VALID_MATERIAL"],
                         "raw_precision": full_score["precision_numerator"] / full_score["raw_output_count"],
                         "removed_output_mirrors": full_score["raw_output_count"] - full_score["normalized_output_count"],
                         "explanation": "Recall credits are rematched, not presumed from candidate origin. Formal precision collapses repeated root matches; the raw denominator retains mirrors."},
        "runtime": runtime, "runtime_gates": gates, "scheduler": scheduler,
        "decision": decision(common_scores, gates, inventory, normalization["cross_set_review"]),
        "normalized_truth_view": truth, "classification": classified,
        "safety": {key: 0 for key in ("web_research", "production_write", "supabase_write", "canonical_db_write", "scheduled_task_changes", "benchmark_rerun", "batch03_run", "gold_mutation", "extension_response_mutation", "raw_artifact_mutation")}}
    require(before == p.hashes(protected), "immutable inputs changed during evaluation")
    result["input_sha256"] = before
    return result, protected


def report(a):
    lines = ["# batch04v2 Extended-Gold Final Audit", "", a["decision"]["final_judgment"], "",
        "## Extension / normalization", "", f"18 packets; {a['extension']['cluster_count']} clusters; {a['extension']['verdict_counts']}",
        f"Material rate {a['extension']['material_rate']:.2%}; nonmaterial {a['extension']['nonmaterial_rate']:.2%}.",
        f"Normalized Material={a['normalization']['material_total']}; cross-set merges={len(a['normalization']['cross_set_merges'])}; review={len(a['normalization']['cross_set_review'])}.",
        a["normalization"]["extension_criticality"], "", "## Common48: formal shared-cohort comparison", "",
        a["common48"]["caveat"], "", "| Run | Material | Recall | Critical | Precision | Known noncritical | Company FN | No-news FN | Unresolved |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for label, v in a["common48"]["scores"].items():
        lines.append(f"| {label} | {v['material']['found']}/{v['material']['target']} | {v['material']['recall']:.2%} | {v['critical']['found']}/{v['critical']['target']} | {v['material_precision']:.2%} ({v['precision_numerator']}/{v['precision_denominator']}) | {v['noncritical_known_labels']['found']}/{v['noncritical_known_labels']['target']} | {v['company_level_material_FN']} | {v['no_news_FN']} | {v['unresolved_unique_count']} |")
    lines += ["", "## Full192", "", a["full192"]["status"], "", a["full192"]["self_score_label"], "", "```json", json.dumps(a["full192"]["batch04v2"], ensure_ascii=False, indent=2), "```", "",
              "| Run | Raw outputs | Known matched | Extension matched | Unadjudicated raw | Ambiguous raw | Unresolved unique |", "|---|---:|---:|---:|---:|---:|---:|"]
    for label, x in a["full192"]["inventory"].items():
        lines.append(f"| {label} | {x['total_candidate_events']} | {x['existing_gold_matched_raw']} | {x['extension_matched_raw']} | {x['not_yet_adjudicated_candidate_only_raw']} | {x['ambiguous_raw']} | {x['unresolved_unique']} |")
    lines += ["", a["full192"]["additional_adjudication_proposal"], "", "## Precision definition", "", a["precision_contract"], "",
              "Sanity check:", "", "```json", json.dumps(a["sanity_check"], indent=2), "```", "",
              "Noncritical recall above uses the 22 explicitly labelled Known-Gold noncritical events. The 7 Common48 / 67 Full192 extension material events lack independent criticality labels. They are not silently assigned noncritical status. JSON also exposes material_excluding_known_critical as a separate operational metric.", "",
              "## Critical regression", ""]
    for row in a["critical_regressions"]:
        lines += [f"### {row['ticker']} {row['company']} — {row['date']}", "", row["headline"], "",
                  f"{row['gold_source_type']} / {row['gold_channel']}; baseline FOUND → candidate MISSED.",
                  f"Classification: {row['classification']} / {row['confidence']}. {row['limit']}", "",
                  "```json", json.dumps({k: v for k, v in row.items() if k not in {"baseline_result", "candidate_outputs"}}, ensure_ascii=False, indent=2), "```", "",
                  "Baseline source/result and all candidate outputs/matcher evidence are included in the machine-readable audit.", ""]
    lines += ["## Stage value and noise", "", "| Stage | Material | Nonmaterial | Out of window | Material rate |", "|---|---:|---:|---:|---:|"]
    for stage, x in a["stage_attribution"]["summary"].items():
        c = x["verdict_counts"]
        rate = f"{x['material_rate']:.2%}" if x["material_rate"] is not None else "N/A"
        lines.append(f"| {stage} | {c.get('VALID_MATERIAL',0)} | {c.get('VALID_NON_MATERIAL',0)} | {c.get('OUT_OF_WINDOW',0)} | {rate} |")
    lines += ["", "Source-bucket breakdown:", "", "```json", json.dumps({k:v["source_buckets"] for k,v in a["stage_attribution"]["summary"].items()}, ensure_ascii=False, indent=2), "```", "",
              "Stage is a discovery route, not the final evidence source type. A targeted search can find an official disclosure. UNRESOLVED records are retained when the final URL/headline differs from the selected provenance; no stage is guessed.", "",
              "## Known-Gold source coverage (unchanged labels)", "", "```json", json.dumps(a["known_gold_scores"]["batch04v2"], indent=2), "```", "",
              "## Source depth and research time", "", "| Run | All sources avg | Common48 sources avg | All research sec avg | Common48 research sec avg | Productive task avg / p95 / max |", "|---|---:|---:|---:|---:|---|"]
    for label, x in a["runtime"].items():
        t = x["productive_task_seconds"]
        lines.append(f"| {label} | {x['all_researched']['source_count']['average']:.4f} | {x['common48']['source_count']['average']:.4f} | {x['all_researched']['research_seconds']['average']:.3f} | {x['common48']['research_seconds']['average']:.3f} | {t['average']:.2f} / {t['p95']:.2f} / {t['max']:.2f} |")
    s = a["scheduler"]
    lines += ["", "Source recovery generated material novelty as well as substantial noise. Depth alone is not equivalent to critical-event extraction quality.", "",
        "## Scheduler (separate from quality)", "", f"{s['expected_schedule_opportunities']} slots: {s['classification_counts']}; busy={s['busy_skip']}; overlap={s['overlap']}.",
        f"Wall clock {s['wall_clock_companies_per_hour']:.3f} companies/h; observed-slot conditional {s['scheduler_adjusted_companies_per_hour']:.3f}; theoretical {s['theoretical_companies_per_hour']}.",
        s["scheduler_adjusted_basis"], s["route_log_limitation"], "SEPARATE_SCHEDULER_RELIABILITY_ISSUE; cause not determined.", "",
        "## Decision / safety", "", "```json", json.dumps(a["decision"], ensure_ascii=False, indent=2), "```", "", "```json", json.dumps(a["safety"], indent=2), "```", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-id", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    audit, protected = evaluate(root, args.benchmark_id)
    output = b._matcher_v2_directory(root, args.benchmark_id) / "post_run_batch04v2"
    # Only new, deterministic analysis artifacts. Refuse differing existing outputs.
    p.write_new(output / "batch04v2_extended_gold_final_audit.json", audit)
    p.write_new(output / "BATCH04V2_EXTENDED_GOLD_FINAL_AUDIT.md", report(audit))
    require(p.hashes(protected) == audit["input_sha256"], "immutable inputs changed during output")
    print(json.dumps({"output_directory": str(output), "decision": audit["decision"],
                      "material": audit["full192"]["batch04v2"]["material"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
