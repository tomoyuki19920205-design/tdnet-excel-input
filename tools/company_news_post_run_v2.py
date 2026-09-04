"""Offline post-run evaluation. No research, routing, or production writes.

Inputs are immutable; only post_run_<run> and gold_extension_<run> are writable.
The existing matcher and Gold response validator remain the canonical contracts.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from tools import company_news_batch_benchmark as b

MINUTES = (0, 8, 15, 23, 30, 38, 45, 53)
STAGES = ("official_first", "targeted_second_pass", "existing_general")


def hashes(paths):
    return {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(set(paths)) if p.is_file()}


def write_new(path, value, *, derived=False):
    """Deterministic, idempotent output; never overwrite differing artifacts."""
    body = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != body and not derived:
            raise b.BenchmarkError(f"refusing to replace different artifact: {path}")
        if path.read_text(encoding="utf-8") == body:
            return
    # Deep benchmark paths exceed MAX_PATH including the atomic sibling suffix.
    target = Path("\\\\?\\" + str(path.resolve())) if os.name == "nt" else path
    b.atomic_write_text(target, body)


def strict_nodes(events, run_name="candidate"):
    """Use canonical same-run v2 dedupe, without adding a research run label."""
    nodes, accepted, review = b._strict_dedupe_run_v2("batch04", events)
    # The helper requires a registered label. Relabel local copies so audit
    # provenance cannot be mistaken for the actual historical batch04 run.
    for node in nodes:
        node["run_name"] = node["run_label"] = run_name
        for member in node["members"]:
            member["run_name"] = member["run_label"] = run_name
            member["observation_id"] = b._observation_id(member)
    for item in accepted + review:
        for field in ("left_observation_id", "right_observation_id"):
            item[field] = item[field].replace("batch04:", f"{run_name}:", 1)
    return nodes, accepted, review


def observation_nodes(events, run_name):
    nodes = []
    for ticker, values in sorted(events.items()):
        for index, event in enumerate(values):
            member = {**b._event_public_fields(event), "run_name": run_name,
                      "run_label": run_name, "event_index": index}
            member["observation_id"] = b._observation_id(member)
            nodes.append({"node_id": member["observation_id"], "ticker": ticker,
                          "members": [member], "representative": member})
    return nodes


def match_nodes(nodes, union, gold):
    _, groups = b._gold_normalized_groups_v2(gold)
    root_of = {member["match_cluster_id"]: root for root, group in groups.items() for member in group}
    matches, novel, ambiguous = [], [], []
    for node in nodes:
        options = []
        for cluster in union["events"]:
            if cluster["ticker"] != node["ticker"]:
                continue
            evidence = b._node_identity_evidence_v2(node, cluster)
            if evidence["auto_merge"]:
                options.append({"root_id": root_of[cluster["match_cluster_id"]],
                                "cluster_id": cluster["match_cluster_id"], "evidence": evidence})
        roots = {option["root_id"] for option in options}
        if len(roots) == 1:
            matches.append({"node": node, "root_id": next(iter(roots)), "options": options})
        elif roots:
            # Do not inflate recall by crediting an ambiguous observation twice.
            ambiguous.append({"node": node, "options": options})
        else:
            novel.append(node)
    return matches, novel, ambiguous


def score(gold, discovered):
    by_id, groups = b._gold_normalized_groups_v2(gold)
    material = {k for k in groups if by_id[k]["verdict"] == "VALID_MATERIAL"}
    critical = {k for k in material if by_id[k].get("importance") == "high"
                or by_id[k].get("earnings_relevance") == "direct"}
    buckets = {k: b._gold_source_bucket(by_id[k].get("primary_source_type") or
                                       by_id[k].get("source_type") or "none") for k in material}
    targets = {"material": material, "critical": critical, "noncritical": material - critical,
               "official": {k for k in material if buckets[k] in {"official_company", "ir_tdnet", "government"}}}
    targets.update({bucket: {k for k in material if buckets[k] == bucket}
                    for bucket in ("ir_tdnet", "official_company", "government", "external_news", "industry_research", "other")})
    return {name: {"target": len(ids), "found": len(ids & discovered),
                   "missed": len(ids - discovered), "recall": len(ids & discovered) / len(ids) if ids else None}
            for name, ids in targets.items()}


def rescue_analysis(gold, matches, companies):
    by_id, groups = b._gold_normalized_groups_v2(gold)
    material = {k for k in groups if by_id[k]["verdict"] == "VALID_MATERIAL"}
    old = {k for k in material if any(x.get("found_by", {}).get("batch04") for x in groups[k])}
    found = {m["root_id"] for m in matches} & material
    provenance = {r["ticker"]: r.get("candidate_provenance", []) for r in companies}

    def row(k):
        e = by_id[k]
        stages = set()
        for m in matches:
            if m["root_id"] != k:
                continue
            for member in m["node"]["members"]:
                for p in provenance.get(member["ticker"], []):
                    if p.get("selected_for_output") and b._normalized_url(p.get("source_url", "")) == member["normalized_source_url"]:
                        stages.add(p["stage"])
        return {"cluster_id": k, "ticker": e["ticker"], "company": e.get("company"),
                "date": e["date"], "headline": e["headline"], "earnings_channel": e.get("earnings_channel"),
                "source_type": e.get("primary_source_type"),
                "critical": e.get("importance") == "high" or e.get("earnings_relevance") == "direct",
                "batch04": "FOUND" if k in old else "MISSED",
                "candidate": "FOUND" if k in found else "MISSED",
                "stage": next(iter(stages)) if len(stages) == 1 else "UNRESOLVED",
                "stages_observed": sorted(stages)}
    rescues = [row(k) for k in sorted(found - old)]
    regressions = [row(k) for k in sorted(old - found)]
    return {"rescues": rescues, "regressions": regressions,
            "net_rescue_count": len(rescues) - len(regressions),
            "rescue_stage_counts": dict(Counter(r["stage"] for r in rescues))}


def scheduler_coverage(manifest, runs, events, snapshots):
    start, end = (datetime.fromisoformat(manifest[k]) for k in ("activated_at", "completed_at"))
    opportunities = []
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour <= end:
        for index, minute in enumerate(MINUTES, 1):
            at = hour.replace(minute=minute)
            if start <= at <= end:
                opportunities.append({"task_id": f"task{index:02}", "scheduled_at": at.isoformat(),
                                      "classification": "NO_ROUTE_OBSERVED", "evidence": []})
        hour += timedelta(hours=1)
    evidence = []
    for r in runs:
        evidence.append({"task_id": r["task_id"], "at": r["task_started_at"],
                         "kind": "release", "run_id": r["run_id"],
                         "finished_at": r["task_finished_at"],
                         "company_count": r["snapshot_company_count"]})
    for s in snapshots:
        evidence.append({"task_id": s["task_id"], "at": s["task_started_at"],
                         "kind": "snapshot", "run_id": s["run_id"]})
    for e in events:
        evidence.append({"task_id": e["task_id"], "at": e["at"], "kind": e["event"],
                         "run_id": e.get("run_id")})
    unmatched = []
    for e in evidence:
        at = datetime.fromisoformat(e["at"])
        if not start <= at <= end:
            continue
        candidates = [o for o in opportunities if o["task_id"] == e["task_id"]
                      and 0 <= (at - datetime.fromisoformat(o["scheduled_at"])).total_seconds() < 3600]
        if not candidates:
            unmatched.append(e)
            continue
        o = candidates[-1]
        o["evidence"].append(e)
    for o in opportunities:
        ev = o["evidence"]
        if any(e["kind"] == "release" and e.get("company_count", 0) > 0 for e in ev):
            o["classification"] = "PRODUCTIVE"
        elif any(e["kind"] == "busy_skip" for e in ev):
            o["classification"] = "BUSY_SKIP"
        elif ev:
            o["classification"] = "OTHER"
    productive = sorted((r for r in runs if r["snapshot_company_count"] > 0), key=lambda r: r["task_started_at"])
    overlaps = 0
    for task_id in {r["task_id"] for r in productive}:
        task = [r for r in productive if r["task_id"] == task_id]
        for i, current in enumerate(task):
            overlaps += sum(datetime.fromisoformat(p["task_finished_at"]) > datetime.fromisoformat(current["task_started_at"]) for p in task[:i])
    counts = Counter(o["classification"] for o in opportunities)
    gaps, gap = [], None
    for o in opportunities:
        if o["classification"] == "NO_ROUTE_OBSERVED":
            if gap is None:
                gap = {"first_scheduled_at": o["scheduled_at"], "count": 0}
                gaps.append(gap)
            gap["last_scheduled_at"] = o["scheduled_at"]
            gap["count"] += 1
        else:
            gap = None
    observed = sum(bool(o["evidence"]) for o in opportunities)
    total = sum(r["success_count"] for r in productive)
    return {"activated_at": start.isoformat(), "completed_at": end.isoformat(),
            "expected_schedule_opportunities": len(opportunities), "classification_counts": dict(counts),
            "productive_run_count": len(productive), "expected_productive_runs": math.ceil(manifest["company_count"] / manifest["batch_size"]),
            "productive_runs": productive, "opportunities": opportunities, "unmatched_evidence": unmatched,
            "no_route_intervals": gaps,
            "empty_run_count": sum(r["snapshot_company_count"] == 0 for r in runs),
            "empty_after_complete": [r for r in runs if r["snapshot_company_count"] == 0 and datetime.fromisoformat(r["task_started_at"]) > end],
            "busy_skip": sum(e["event"] == "busy_skip" and start <= datetime.fromisoformat(e["at"]) <= end for e in events),
            "overlap": overlaps, "wall_clock_seconds": (end-start).total_seconds(),
            "wall_clock_companies_per_hour": total * 3600 / (end-start).total_seconds(),
            "observed_schedule_opportunities": observed,
            "scheduler_adjusted_companies_per_hour": total / (observed / 8) if observed else None,
            "scheduler_adjusted_basis": "Observed opportunities / 8 = cadence-equivalent hours; conditional estimate, not measured continuous capacity",
            "theoretical_companies_per_hour": 8 * manifest["batch_size"],
            "attribution_rule": "same task, preceding scheduled time within 60 min; inferred association, not scheduler execution proof",
            "route_attempts": None, "route_log_limitation": "route_task does not persist attempt telemetry; absent snapshot/release is NO_ROUTE_OBSERVED, not proof of missed execution",
            "gap_cause": "UNDETERMINED; no sleep/app/network attribution"}


def extension_packets(benchmark_id, nodes, names, window):
    events = []
    for node in nodes:
        members = sorted(node["members"], key=lambda m: (m["source_url"], m["headline"]))
        m = members[0]
        public = {"ticker": m["ticker"], "company": names.get(m["ticker"], ""),
                  "event_date": m["date"], "canonical_headline": m["headline"],
                  "summary": m["summary"], "why_it_matters": m["why_it_matters"],
                  "source_urls": sorted([{"url": u, "source_type": t} for u, t in
                                         {(x["source_url"], x["source_type"]) for x in members}], key=lambda x: x["url"])}
        events.append({"cluster_id": "ext-" + b._json_sha256(public)[:16], **public})
    events.sort(key=lambda e: (e["ticker"], e["event_date"], e["cluster_id"]))
    groups = []
    for e in events:
        if groups and groups[-1][0] == e["ticker"]:
            groups[-1][1].append(e)
        else:
            groups.append((e["ticker"], [e]))
    try:
        partitions = b._partition_gold_packet_groups(groups) if groups else []
    except b.BenchmarkError:
        # Small totals / oversized companies: preserve companies where possible,
        # never exceed ten. The final packet may contain fewer than eight.
        partitions, current, count = [], [], 0
        for ticker, group in groups:
            for offset in range(0, len(group), 10):
                piece = group[offset:offset+10]
                if count + len(piece) > 10:
                    partitions.append(current)
                    current, count = [], 0
                current.append((ticker, piece))
                count += len(piece)
        if current:
            partitions.append(current)
    packets = []
    for i, partition in enumerate(partitions, 1):
        packet = {"schema_version": b.GOLD_PACKET_SCHEMA, "benchmark_id": benchmark_id,
                  "packet_id": f"gold_extension_{i:03}", "search_from": window[0], "search_to": window[1],
                  "events": [e for _, group in partition for e in group],
                  "response_contract": b._gold_response_contract()}
        packet["packet_sha256"] = b._packet_sha256(packet)
        forbidden = ("found_by", "batch04v2", "official_first", "targeted_second_pass", "run_label", "matcher_evidence", "reference01", "reference02")
        if any(word in json.dumps(packet, ensure_ascii=False).lower() for word in forbidden):
            raise b.BenchmarkError("extension blindness check failed")
        packets.append(packet)
    return packets


def validate_evaluation_inputs(gold, union, status):
    b._validate_gold_v2_against_union(gold, union)
    metrics = b._gold_metrics_v2(gold)
    if metrics.get("decision") != "GOLD_COMPLETE" or metrics["gold_material_event_count"] != 29 or metrics["critical_gold_event_count"] != 7 or len(gold["events"]) != 82:
        raise b.BenchmarkError("expected complete Gold82 with material29 / critical7")
    if status["completed"] != 192 or status["total"] != 192 or status["failed"] or status["pending"]:
        raise b.BenchmarkError("candidate must complete 192/192, failed0, pending0")


def audit_strategy_v2_run(root, benchmark_id, run_name, adjudication=None, build_extension=False):
    directory = b._matcher_v2_directory(root, benchmark_id)
    benchmark_dir = directory.parent.parent
    run_dir = b._run_dir(root, benchmark_id, run_name)
    gold_path = adjudication or directory / "gold_adjudication_v2.json"
    # Inventory all immutable research runs, including baseline raw inboxes.
    protected = list((benchmark_dir / "runs").rglob("*"))
    protected += [benchmark_dir / "cohort.json", gold_path, directory / "event_union_v2.json"]
    protected += list((directory / "gold_responses").rglob("*"))
    protected += list((directory / "gold_packets").rglob("*"))
    for path in (benchmark_dir / "runs").glob("*/manifest.json"):
        output = Path(b._read_json(path)["output_directory"]).resolve()
        allowed = (root / "data/news_benchmark_inbox" / benchmark_id).resolve()
        if not output.is_relative_to(allowed):
            raise b.BenchmarkError("output directory escapes benchmark inbox")
        protected += list(output.rglob("*"))
    before = hashes(protected)
    union = b._read_json(directory / "event_union_v2.json")
    gold = b._validate_gold_v2_against_union(b._read_json(gold_path), union)
    status = b.benchmark_status(root, benchmark_id, run_name)
    validate_evaluation_inputs(gold, union, status)
    manifest, events, companies = b._completed_run_events(root, benchmark_id, run_name)
    if manifest.get("strategy_version") != b.STRATEGY_V2_VERSION or manifest["batch_size"] != 4:
        raise b.BenchmarkError("expected completed batch4 strategy v2")
    # Match observations first; dedupe ONLY the novel subset. A shared archive
    # URL must not merge distinct known events before their Gold comparison.
    matches, novel_raw, ambiguous = match_nodes(observation_nodes(events, run_name), union, gold)
    novel_events = defaultdict(list)
    for node in novel_raw:
        novel_events[node["ticker"]].extend(node["members"])
    novel, dedupe, review = strict_nodes(novel_events, run_name)
    by_id, groups = b._gold_normalized_groups_v2(gold)
    comparison = {label: score(gold, {k for k, group in groups.items() if any(x.get("found_by", {}).get(label) for x in group)})
                  for label in b.QUALITY_RUN_LABELS.values()}
    comparison[run_name] = score(gold, {m["root_id"] for m in matches})
    baseline = {"reference01": 25, "reference02": 22, "batch02": 19, "batch04": 17}
    if any(comparison[label]["material"]["found"] != count for label, count in baseline.items()):
        raise b.BenchmarkError("baseline Gold integrity mismatch")
    rescue = rescue_analysis(gold, matches, companies)
    cohort = b._load_cohort(root, benchmark_id)
    names = {c["ticker"]: c["company_name"] for c in cohort["companies"]}
    reference_tickers = set(cohort["reference_tickers"])
    runs = b._telemetry(run_dir, "runs.jsonl")
    snapshots = [b._read_json(p) for p in sorted((run_dir / "tasks").glob("task??/history/*.json"))
                 if b._read_json(p).get("schema_version") == b.SNAPSHOT_SCHEMA]
    coverage = scheduler_coverage(manifest, runs, b._task_events(run_dir), snapshots)
    runtime = {}
    for name, label in [*b.QUALITY_RUN_LABELS.items(), (run_name, run_name)]:
        d = b._run_dir(root, benchmark_id, name)
        c = b._telemetry(d, "companies.jsonl")
        t = [r for r in b._telemetry(d, "runs.jsonl") if r["snapshot_company_count"] > 0]
        runtime[label] = {"company_count": len(c), "source_count": b._summary([float(x["source_count"]) for x in c]),
                          "research_seconds": b._summary([float(x["research_elapsed_seconds"]) for x in c]),
                          "task_seconds": b._summary([float(x["task_elapsed_seconds"]) for x in t])}
        runtime[label]["all_task_seconds_including_empty"] = b._summary([float(x["task_elapsed_seconds"]) for x in b._telemetry(d, "runs.jsonl")])
        prior = d / "audit.json"
        runtime[label]["stored_legacy_task_seconds"] = b._read_json(prior).get("runtime", {}).get("task_elapsed_seconds") if prior.exists() else None
    for r in runtime.values():
        r["source_ratio_vs_reference"] = r["source_count"]["average"] / runtime["reference01"]["source_count"]["average"]
        r["research_ratio_vs_reference"] = r["research_seconds"]["average"] / runtime["reference01"]["research_seconds"]["average"]
    incidents = b._busy_skip_incidents(run_dir, runs)
    gates = {"p95_under_45min": runtime[run_name]["task_seconds"]["p95"] < 2700,
             "max_under_55min": runtime[run_name]["task_seconds"]["max"] < 3300,
             "capacity_busy_skip_zero": not any(x["classification"] == "capacity_busy_skip" for x in incidents),
             "overlap_zero": coverage["overlap"] == 0}
    packets = extension_packets(benchmark_id, novel, names, (manifest["search_from"], manifest["search_to"]))
    audit = {"schema_version": "company_news_post_run_gold_v2", "benchmark_id": benchmark_id,
             "run_name": run_name, "quality_basis": "KNOWN_GOLD_V2", "matcher": b.MATCHER_V2_SCHEMA,
             "status": status, "gold_verdict_counts": b._gold_verdict_counts(gold), "comparison": comparison,
             "precision": None, "precision_status": "PROVISIONAL_PENDING_GOLD_EXTENSION",
             "candidate_events_total_192": sum(map(len, events.values())),
             "candidate_events_reference48": sum(len(v) for k, v in events.items() if k in reference_tickers),
             "existing_gold_matched_raw": sum(len(m["node"]["members"]) for m in matches),
             "existing_gold_matched_unique": len({m["root_id"] for m in matches}),
             "existing_gold_ambiguous_raw": sum(len(m["node"]["members"]) for m in ambiguous),
             "novel_raw": sum(len(n["members"]) for n in novel), "novel_unique": len(novel),
             "novel_is_false_positive": False, "ambiguous_matches": ambiguous,
             "matches": matches, "dedupe_evidence": dedupe, "dedupe_review": review,
             "runtime": runtime, "runtime_gates": gates,
             "legacy_reference_metric": b._read_json(run_dir / "audit.json").get("quality"),
             "legacy_quality_basis": "LEGACY_REFERENCE_METRIC; not final Gold quality",
             "scope_note": "Gold82 drawn from reference48; novel extraction covers all192, so extension creates selection/coverage asymmetry for reference-only runs",
             "targets_pass": comparison[run_name]["material"]["found"] >= 24 and comparison[run_name]["critical"]["found"] == 7 and comparison[run_name]["ir_tdnet"]["found"] == 12,
             "web_research": 0, "production_writes": 0, "scheduled_task_changes": 0}
    extension = {"schema_version": "company_news_gold_extension_manifest_v2", "benchmark_id": benchmark_id,
                 "source_gold_sha256": b._json_sha256(gold), "source_union_sha256": b._json_sha256(union),
                 "status": "PENDING_BLIND_ADJUDICATION", "cluster_count": len(novel),
                 "packets": [{"packet_id": p["packet_id"], "packet_sha256": p["packet_sha256"],
                              "filename": f"{p['packet_id']}.json", "cluster_ids": [e["cluster_id"] for e in p["events"]]} for p in packets],
                 "excluded_existing_gold": True, "response_schema": b.GOLD_RESPONSE_SCHEMA,
                 "final_score_design": "Existing normalized Gold + extension VALID_MATERIAL; normalize duplicate chains across both without overwriting original. Score all five frozen outputs with matcher v2; report common48 and full192 separately, unresearched reference companies not as missed. Precision final only after all extension verdicts; no-news FN counts material tickers with no output events."}
    after = hashes(protected)
    if before != after:
        raise b.BenchmarkError("immutable artifact SHA changed during analysis")
    out = directory / f"post_run_{run_name}"
    write_new(out / f"{run_name}_gold_known_audit.json", audit, derived=True)
    write_new(out / f"{run_name}_rescue_analysis.json", rescue, derived=True)
    write_new(out / f"{run_name}_scheduler_coverage.json", coverage, derived=True)
    write_new(out / "input_sha256.json", {"before": before, "after": after, "raw_artifact_mutation": 0}, derived=True)
    if build_extension:
        ext_dir = directory / f"gold_extension_{run_name}"
        for packet in packets:
            write_new(ext_dir / f"{packet['packet_id']}.json", packet)
        write_new(ext_dir / "gold_extension_manifest.json", extension)
        write_new(ext_dir / "ADJUDICATION_INSTRUCTIONS.md", EXTENSION_INSTRUCTIONS)
    summary = {"audit": audit, "rescue": rescue, "scheduler": coverage, "extension": extension}
    write_new(out / "BATCH04V2_POST_RUN_AUDIT.md", report(summary), derived=True)
    if hashes(protected) != before:
        raise b.BenchmarkError("immutable artifact SHA changed during output")
    return {"output_directory": str(out), "known_gold": comparison[run_name],
            "novel_raw": audit["novel_raw"], "novel_unique": len(novel), "extension_packets": len(packets),
            "targets_pass": audit["targets_pass"], "runtime_gates": gates,
            "scheduler_counts": coverage["classification_counts"], "raw_artifact_mutation": 0}


def report(s):
    a, r, c, e = (s[k] for k in ("audit", "rescue", "scheduler", "extension"))
    name = a["run_name"]
    k = a["comparison"][name]
    lines = ["# batch04v2 Post-run Gold Audit", "", "## Executive Summary", "",
             f"- Completion: {a['status']['completed']}/192; failed={a['status']['failed']}",
             f"- Known Gold: {k['material']['found']}/29 ({k['material']['recall']:.2%}); critical {k['critical']['found']}/7; IR/TDNET {k['ir_tdnet']['found']}/12.",
             f"- Old batch04 17/29; rescues={len(r['rescues'])}; regressions={len(r['regressions'])}; net={r['net_rescue_count']}",
             f"- Source depth: {a['runtime']['batch04']['source_count']['average']:.4f} → {a['runtime'][name]['source_count']['average']:.4f}",
             f"- Runtime gates: {a['runtime_gates']}",
             f"- Wall clock {c['wall_clock_companies_per_hour']:.3f} companies/hour; gaps={c['classification_counts'].get('NO_ROUTE_OBSERVED', 0)}.",
             f"- Novel candidates {a['novel_raw']} raw / {a['novel_unique']} unique; {len(e['packets'])} blind extension packets.",
             "- Precision: PROVISIONAL_PENDING_GOLD_EXTENSION. Novel candidates are NOT false positives.",
             f"- Matcher ambiguity: {len(a['ambiguous_matches'])} node(s), {a['existing_gold_ambiguous_raw']} raw observations; excluded from unambiguous credit, not classified as novel/false positives.",
             f"- Targets pass: {a['targets_pass']}; batch03 remains ON HOLD.",
             "", "## Known Gold comparison", "",
             "| Run | Material | Critical | Noncritical | Official | IR/TDNET | Company official | Government | External | Industry |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for run, values in a["comparison"].items():
        cells = [f"{values[key]['found']}/{values[key]['target']}" for key in
                 ("material", "critical", "noncritical", "official", "ir_tdnet", "official_company", "government", "external_news", "industry_research")]
        lines.append("| " + " | ".join([run, *cells]) + " |")
    lines += ["", "## Rescue and regression", "", f"Stage counts: {r['rescue_stage_counts']}", ""]
    for kind in ("rescues", "regressions"):
        lines.append(f"### {kind}")
        lines.append("")
        for row in r[kind]:
            lines.append(f"- {row['ticker']} {row['company']} {row['date']}: {row['headline']} ({row['earnings_channel']}, {row['source_type']}); {row['batch04']} → {row['candidate']}; stage={row['stage']}")
        lines.append("")
    lines += ["## Runtime and compression", "",
              "| Run | Sources avg / median | Source ratio | Research sec avg / p50 / p95 / max | Time ratio | Task sec avg / p95 / max |",
              "|---|---|---|---|---|---|"]
    for run, v in a["runtime"].items():
        nums = lambda d, keys: " / ".join(f"{d[key]:.2f}" for key in keys)
        lines.append(f"| {run} | {nums(v['source_count'], ('average','p50'))} | {v['source_ratio_vs_reference']:.4f} | {nums(v['research_seconds'], ('average','p50','p95','max'))} | {v['research_ratio_vs_reference']:.4f} | {nums(v['task_seconds'], ('average','p95','max'))} |")
    lines += ["", "Source depth and elapsed research are separate telemetry measurements, not proof of equivalent quality.",
              "Task table uses productive runs only. Current all-run telemetry (including empty) and stored legacy audit may use different samples:", ""]
    for run in ("batch04", name):
        v = a["runtime"][run]
        lines.append(f"- {run}: productive avg={v['task_seconds']['average']:.3f}; all-run avg={v['all_task_seconds_including_empty']['average']:.3f}; stored legacy={v['stored_legacy_task_seconds']}")
    lines += ["",
              "Task duration is well below the one-hour per-task recurrence; scheduler availability must be assessed separately.",
              "", "## Scheduler coverage", "", f"- Expected opportunities: {c['expected_schedule_opportunities']}; {c['classification_counts']}",
              f"- Productive runs: {c['productive_run_count']} (expected {c['expected_productive_runs']}); busy={c['busy_skip']}; overlap={c['overlap']}",
              f"- Wall-clock: {c['wall_clock_companies_per_hour']:.4f} companies/hour over {c['wall_clock_seconds']:.3f} seconds.",
              f"- Conditional scheduler-adjusted: {c['scheduler_adjusted_companies_per_hour']:.4f} companies/hour. {c['scheduler_adjusted_basis']}",
              f"- Theoretical cadence: {c['theoretical_companies_per_hour']} companies/hour; not measured availability.",
              f"- {c['route_log_limitation']}", f"- {c['attribution_rule']}", f"- Gap cause: {c['gap_cause']}",
              f"- Consecutive no-route intervals: {c['no_route_intervals']}",
              "", "### All productive runs", "", "| Task | Started | Finished | Companies | Seconds |", "|---|---|---|---|---|"]
    lines += [f"| {x['task_id']} | {x['task_started_at']} | {x['task_finished_at']} | {x['success_count']} | {x['task_elapsed_seconds']:.2f} |" for x in c['productive_runs']]
    lines += ["", "## Extension / precision", "", a["scope_note"], "", e["final_score_design"],
              "", "No Web adjudication has been started. Human approval is required. Existing Gold82 is immutable.",
              "", "## Legacy audit", "", a["legacy_quality_basis"], "", "```json", json.dumps(a["legacy_reference_metric"], ensure_ascii=False, indent=2), "```",
              "", "## Safety", "", "Web research=0; Production writes=0; Scheduled Task changes=0; raw SHA mutation=0. No batch03 or rerun.",
              "All input SHA pairs and detailed matcher/scheduler evidence are in the adjacent JSON artifacts.", ""]
    lines += ["## Judgment", "", "WARN_BATCH04V2_POST_RUN_REQUIRES_REVIEW",
              "Known-Gold targets FAIL and critical regression requires review. Source recovery is promising only on depth, not validated quality. Proceed only to approved blind extension adjudication; do not rerun research or start batch03.", ""]
    return "\n".join(lines)


def import_extension(root, benchmark_id, run_name, response_path):
    """Import ONLY an extension sidecar. Never invoke the existing Gold importer."""
    d = b._matcher_v2_directory(root, benchmark_id)
    b._run_dir(root, benchmark_id, run_name)  # Validate path component, no writes.
    ext = d / f"gold_extension_{run_name}"
    manifest = b._read_json(ext / "gold_extension_manifest.json")
    if manifest.get("benchmark_id") != benchmark_id:
        raise b.BenchmarkError("extension benchmark mismatch")
    gold = b._read_json(d / "gold_adjudication_v2.json")
    union = b._read_json(d / "event_union_v2.json")
    if b._json_sha256(gold) != manifest["source_gold_sha256"] or b._json_sha256(union) != manifest["source_union_sha256"]:
        raise b.BenchmarkError("extension base SHA mismatch")
    response = b._read_json(response_path)
    entry = next((p for p in manifest["packets"] if p["packet_id"] == response.get("packet_id")), None)
    if entry is None:
        raise b.BenchmarkError("unknown extension packet")
    if Path(entry["filename"]).name != entry["filename"] or entry["filename"] != entry["packet_id"] + ".json":
        raise b.BenchmarkError("invalid extension packet filename")
    packets = {}
    original_ids = {x["match_cluster_id"] for x in gold["events"]}
    extension_ids = []
    for record in manifest["packets"]:
        if Path(record["filename"]).name != record["filename"] or record["filename"] != record["packet_id"] + ".json":
            raise b.BenchmarkError("invalid extension packet filename")
        packet = b._read_json(ext / record["filename"])
        if (packet["packet_id"] in packets or packet["packet_id"] != record["packet_id"]
                or packet["benchmark_id"] != benchmark_id
                or b._packet_sha256(packet) != record["packet_sha256"]
                or packet["packet_sha256"] != record["packet_sha256"]
                or [x["cluster_id"] for x in packet["events"]] != record["cluster_ids"]):
            raise b.BenchmarkError("extension manifest/packet integrity mismatch")
        b._assert_blind_packet(packet)
        packets[packet["packet_id"]] = packet
        extension_ids.extend(record["cluster_ids"])
    if len(extension_ids) != len(set(extension_ids)) or original_ids & set(extension_ids):
        raise b.BenchmarkError("extension coverage overlaps original Gold or another packet")
    packet = packets[entry["packet_id"]]
    ids = {x["match_cluster_id"] for x in gold["events"]} | {i for p in manifest["packets"] for i in p["cluster_ids"]}
    b.validate_gold_adjudication_response_v2(response, packet, ids)
    if any(x["verdict"] == "UNKNOWN" or not x["evidence"] for x in response["items"]):
        raise b.BenchmarkError("complete evidence-backed verdicts required")
    # Validate cross-packet duplicate graph before any directory/file creation.
    imported = [b._read_json(p) for p in sorted((ext / "responses").glob("*.json"))]
    for prior in imported:
        if prior.get("packet_id") not in packets:
            raise b.BenchmarkError("unknown saved extension response")
        b.validate_gold_adjudication_response_v2(prior, packets[prior["packet_id"]], ids)
    all_items = [x for r in imported if r["packet_id"] != response["packet_id"] for x in r["items"]] + response["items"]
    edges = {x["cluster_id"]: x["duplicate_of_cluster_id"] for x in all_items if x["duplicate_of_cluster_id"]}
    for node in edges:
        seen = set()
        while node in edges:
            if node in seen:
                raise b.BenchmarkError("extension duplicate cycle")
            seen.add(node)
            node = edges[node]
    write_new(ext / "responses" / f"{entry['packet_id']}.json", response)
    return {"status": "EXTENSION_RESPONSE_IMPORTED", "packet_id": entry["packet_id"], "existing_gold_modified": False}


EXTENSION_INSTRUCTIONS = """# Blind Gold Extension adjudication

Do not begin Web adjudication until the human explicitly authorizes a packet.
Only read the authorized packet and its response contract, not provenance or other results.
Window: 2026-08-25 through 2026-08-31 (publication/disclosure date, not historical dates in text).
Treat headline, summary and URLs only as search starting points; independently verify each event.
Prioritize company IR/news, TDNET, EDINET, government, counterparty official,
industry associations, reliable reporting, then secondary sources.

VALID_MATERIAL: a reasonable investor could update cash-flow or risk estimates via
sales, volume, price, margin, costs, orders, customers, suppliers, capacity, utilization,
CAPEX, M&A, capital allocation, balance sheet, regulation, management or competitiveness.
Specific industry transmission requires documented company exposure, not merely a sector match.
VALID_NON_MATERIAL: real but ordinary profile/rating, minor campaign, routine exhibition,
CSR/blog/recruiting/product publicity or recycled information without material business impact.
Do not mechanically reject a product event; consider company size and concrete impact.
DUPLICATE / MATCHER_FALSE_NEGATIVE require verified identical events and a valid target cluster ID.
INVALID_SOURCE requires fabricated, unrelated or mismatched source; a broken original URL alone
does not invalidate an event verified elsewhere. Record original and event validity separately.
OUT_OF_WINDOW uses publication date. UNKNOWN is forbidden; unresolvable evidence must use
LOW confidence, the best defensible permitted verdict, and explicit limitations in notes.

Copy packet_id and packet_sha256 verbatim. Follow response_contract exactly, all clusters
exactly once. Each item requires nonempty evidence. Primary evidence is preferred.
No extra fields, invalid enums, missing clusters or unsupported claims.
Save a response artifact only. Never modify original Gold or invoke production writes.
Import uses import-gold-extension-v2, not the original Gold importer; it saves separate responses.
Existing-Gold duplicate targets may be supplied by a human without exposing discovery provenance.
"""
