"""Synthetic v3 contract tests; no real company/event or production fixture."""
import copy
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools import company_news_batch_benchmark as b
from tools import company_news_strategy_v3 as v

NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)
BID = "batchbench-strategy-v3-fixture"


def assignment():
    return {"assignment_id":"synthetic-a1","ticker":"X001","company_name":"Example Recycling",
            "search_from":"2026-08-25","search_to":"2026-08-31","strategy_version":v.VERSION}


def source(url, kind="company_ir"):
    return {"url":url,"source_type":kind,"content_verified":True,"evidence_excerpt":"Verified synthetic source content",
            "checked_at":"2026-09-04T00:00:00+00:00"}


def telemetry(documents=1, events=0):
    a=assignment(); index="https://example.test/ir/"; external="https://example.go.jp/notice/1"
    metadata={"sector":"environment","business_description":"recycling materials","source_url":index}
    themes=v.regulatory_themes(metadata)
    items=[{"date":"2026-08-25","title":f"Synthetic operating update {i}","href":f"https://example.test/docs/{i}.pdf",
            "document_type":"pdf","index_url":index,"requires_follow":True} for i in range(documents)]
    followed=[{"href":x["href"],"final_url":x["href"],"document_type":"pdf","content_verified":True,"evidence_excerpt":"Capacity update"} for x in items]
    sources=[source(index),source(external,"government")]+[source(x["href"]) for x in items]
    candidates=[{"candidate_index":i,"stage":"official_first","selected_for_output":True,"headline":x["title"],"source_url":x["href"],
                 "selection_reason":"New capacity affects volume","information_type":"event","economic_transmission":"Capacity supports sales"} for i,x in enumerate(items[:events])]
    value={"schema_version":v.SCHEMA,"strategy_version":v.VERSION,"assignment_id":a["assignment_id"],"ticker":a["ticker"],
        "official_index_found":True,"official_index_url":index,"official_indexes":[{"url":index,"enumeration_complete":True,"window_from":a["search_from"],"window_to":a["search_to"],"scan_evidence":"All pages checked"}],
        "official_items_in_window":items,"official_items_followed":followed,"official_items_skipped":[],"official_follow_ratio":1.0,
        "official_zero_items_confirmed":not items,"official_document_coverage_complete":True,"official_coverage_complete":True,"window_scan_complete":True,
        "official_queries_attempted":[a["company_name"]+" X001 IR",a["company_name"]+" X001 news"],
        "external_queries_attempted":[{"kind":"regulatory_pass","theme_index":0,"query":v.regulatory_query(a["company_name"],a["ticker"],themes[0]),"reason":"Business permission exposure"}],
        "external_coverage_complete":True,"external_sources_found":[external],"business_metadata":metadata,"regulatory_themes":themes,
        "regulatory_review_complete":True,"regulatory_candidates_reviewed":[{"url":external,"decision":"no_event","reason":"No new company-specific event"}],
        "driver_groups":[],"targeted_pass_triggers":[],"targeted_pass_skip_reason":"No uncovered economic driver",
        "source_provenance":sources,"source_diversity":["company_ir","government"],"candidate_event_count":len(candidates),"final_event_count":len(candidates),
        "candidate_provenance":candidates,"no_news_gate_pass":True,"stopped_after_first_event":False,
        "official_stage_elapsed_seconds":10.0,"targeted_stage_elapsed_seconds":0.0,"regulatory_stage_elapsed_seconds":5.0,"total_research_elapsed_seconds":15.0}
    payload={"sources_checked_count":len(sources),"items":[{"headline":x["headline"],"source_url":x["source_url"]} for x in candidates]}
    return value,payload


def test_v3_version_and_independent_v2_contract():
    assert v.VERSION != b.STRATEGY_V2_VERSION
    assert b._strategy_v2_contract()["strategy_version"] == b.STRATEGY_V2_VERSION
    assert v.contract()["document_coverage_required"] is True


def test_official_index_alone_cannot_complete():
    value,payload=telemetry();value["official_items_followed"]=[]
    with pytest.raises(b.BenchmarkError,match="coverage"):
        v.validate(value,assignment(),payload)


@pytest.mark.parametrize("field",["official_document_coverage_complete","external_coverage_complete","window_scan_complete","no_news_gate_pass"])
def test_no_news_requires_all_strong_gates(field):
    value,payload=telemetry();value[field]=False
    with pytest.raises(b.BenchmarkError): v.validate(value,assignment(),payload)


def test_multiple_documents_even_if_one_event():
    value,payload=telemetry(documents=3,events=1)
    assert v.validate(value,assignment(),payload)==value
    value["official_items_followed"].pop()
    with pytest.raises(b.BenchmarkError): v.validate(value,assignment(),payload)


def test_pdf_follow_cannot_be_skipped_or_redirect_to_index():
    value,payload=telemetry()
    value["official_items_followed"][0]["final_url"]=value["official_index_url"]
    with pytest.raises(b.BenchmarkError,match="index reach"): v.validate(value,assignment(),payload)
    value,payload=telemetry();value["official_items_in_window"][0]["requires_follow"]=False
    with pytest.raises(b.BenchmarkError,match="PDF"): v.validate(value,assignment(),payload)


def test_explicit_zero_items_and_window_enumeration():
    value,payload=telemetry(documents=0)
    assert v.validate(value,assignment(),payload)
    value["official_zero_items_confirmed"]=False
    with pytest.raises(b.BenchmarkError): v.validate(value,assignment(),payload)


@pytest.mark.parametrize("change",["date","enumeration","ratio","evidence","count"])
def test_document_evidence_consistency(change):
    value,payload=telemetry()
    if change=="date": value["official_items_in_window"][0]["date"]="2026-08-24"
    if change=="enumeration": value["official_indexes"][0]["enumeration_complete"]=False
    if change=="ratio": value["official_follow_ratio"]=.5
    if change=="evidence": value["source_provenance"][0]["evidence_excerpt"]=""
    if change=="count": payload["sources_checked_count"]=999
    with pytest.raises(b.BenchmarkError): v.validate(value,assignment(),payload)


def test_permitted_html_skip_has_explicit_reason_and_ratio():
    value,payload=telemetry(documents=2)
    item=value["official_items_in_window"][1]; item.update(document_type="html",requires_follow=False,href="https://example.test/notice.html")
    value["official_items_followed"].pop()
    value["official_items_skipped"]=[{"href":item["href"],"reason_code":"index_sufficient_nonmaterial","reason":"Full index text is a routine opening-hours notice"}]
    value["official_follow_ratio"]=.5
    assert v.validate(value,assignment(),payload)


def test_regulatory_theme_extraction_and_generic_queries():
    m={"sector":"transport","business_description":"logistics and energy"}
    themes=v.regulatory_themes(m)
    assert [x["theme"] for x in themes]==["transport","energy"]
    q=v.regulatory_query("ABC Transport","X002",themes[0])
    assert "ABC Transport" in q and "site:go.jp" in q and "site:lg.jp" in q
    assert v.regulatory_themes({"business_description":"synthetic software"})==[]
    assert len(v.regulatory_themes({"business_description":"transport finance energy food"}))==2


def test_missing_regulatory_pass_and_source_review_fail():
    value,payload=telemetry();value["regulatory_candidates_reviewed"]=[]
    with pytest.raises(b.BenchmarkError,match="regulator source"): v.validate(value,assignment(),payload)
    value,payload=telemetry();value["external_queries_attempted"][0]["query"]="all sectors news"
    with pytest.raises(b.BenchmarkError,match="generic template"): v.validate(value,assignment(),payload)


def targeted_fixture():
    value,payload=telemetry()
    value["driver_groups"]=[{"business_exposure":"recycled input price","transmission_channel":"Input cost changes operating margin",
                              "driver_reason":"Business consumes this input","exposure_source_url":value["official_index_url"]}]
    value["targeted_pass_triggers"]=["explicit_external_exposure"]
    value["targeted_pass_skip_reason"]=""
    value["external_queries_attempted"].append({"kind":"targeted_second_pass","driver_index":0,"query":"recycled input price specialist","reason":"Input cost exposure"})
    return value,payload


def test_targeted_requires_trigger_and_economic_link():
    value,payload=targeted_fixture();assert v.validate(value,assignment(),payload)
    value["driver_groups"][0]["driver_reason"]=""
    with pytest.raises(b.BenchmarkError,match="economic-link"): v.validate(value,assignment(),payload)


def test_generic_sector_targeted_query_prohibited():
    value,payload=targeted_fixture();value["external_queries_attempted"][-1]["query"]="sector daily news"
    with pytest.raises(b.BenchmarkError,match="sector"): v.validate(value,assignment(),payload)


def test_generic_profile_suppressed_and_final_source_linked():
    value,payload=telemetry(events=1);value["candidate_provenance"][0]["information_type"]="generic_profile"
    with pytest.raises(b.BenchmarkError,match="profile"): v.validate(value,assignment(),payload)
    value,payload=telemetry(events=1);payload["items"][0]["headline"]="Unrelated result"
    with pytest.raises(b.BenchmarkError,match="selected evidence"): v.validate(value,assignment(),payload)


def test_per_company_budget_not_transferable():
    for _ in range(4):
        value,payload=telemetry();assert v.validate(value,assignment(),payload)
    value["external_queries_attempted"]*=4
    with pytest.raises(b.BenchmarkError,match="budget"): v.validate(value,assignment(),payload)
    assert v.contract()["budget_transfer_between_companies"] is False


def test_postrun_hard_targets_and_fresh_unseen_required():
    policy=v.evaluation_policy()
    assert policy["critical_required"]==7 and policy["ir_tdnet_required"]==12
    assert policy["material_minimum"]==25 and policy["precision_minimum"]==.45
    assert policy["fresh_unseen_gate"]["different_week"] and policy["fresh_unseen_gate"]["different_company_sample"]
    assert not policy["same_cohort_pass_authorizes_production"]
    assert "critical_target" not in v.contract()


def workspace(root):
    companies=[{"ticker":f"X{i:03d}","company_name":f"XYZ Materials {i}","sector":"materials"} for i in range(192)]
    cohort={"schema_version":b.BENCHMARK_SCHEMA,"benchmark_mode":True,"benchmark_id":BID,"search_from":"2026-08-25","search_to":"2026-08-31",
            "reference_tickers":[x["ticker"] for x in companies[:48]],"companies":companies}
    b.atomic_write_json(b._benchmark_dir(root,BID)/"cohort.json",cohort)
    b._prepare_named_run(root,BID,cohort,companies,4,"batch04",reference=False,now=NOW)
    b.atomic_write_text(root/"config"/v.PROMPT_NAME, (Path(b.__file__).parents[1]/"config"/v.PROMPT_NAME).read_text(encoding="utf-8"))
    return companies


def test_v3_prepare_same_192_order_window_isolated_and_inert(tmp_path):
    companies=workspace(tmp_path)
    old=b._benchmark_dir(tmp_path,BID)/"runs/batch04/manifest.json"
    before=old.read_bytes()
    m=b.prepare_run(tmp_path,BID,4,run_name=v.RUN_NAME,strategy=v.VERSION,now=NOW)
    order=b._ordered_run_assignments(b._run_dir(tmp_path,BID,v.RUN_NAME))
    assert len(order)==m["company_count"]==192 and m["batch_size"]==4
    assert [x["ticker"] for x in order]==[x["ticker"] for x in companies]
    assert m["company_order_sha256"]==hashlib.sha256("\n".join(x["ticker"] for x in companies).encode()).hexdigest()
    assert m["company_order_sha256"]==m["baseline_company_order_sha256"]
    assert (m["search_from"],m["search_to"])==("2026-08-25","2026-08-31")
    assert m["status"]=="prepared" and m["readiness_status"]=="READY_TO_ACTIVATE"
    assert old.read_bytes()==before and not b._paths(tmp_path)["active"].exists()
    assert m["gold_leakage_scan"]["hit_count"]==0
    assert not (tmp_path/"data/news_inbox").exists()


def test_v3_prepare_deterministic(tmp_path):
    values=[]
    for sub in ("one","two"):
        root=tmp_path/sub;workspace(root)
        m=b.prepare_run(root,BID,4,strategy=v.VERSION,now=NOW)
        values.append({k:m[k] for k in ("company_order_sha256","strategy_contract","evaluation_policy","gold_leakage_scan","readiness_status")})
    assert values[0]==values[1]


def test_frozen_prompt_route_and_snapshot(tmp_path):
    workspace(tmp_path)
    m=b.prepare_run(tmp_path,BID,4,strategy=v.VERSION,now=NOW)
    # Activation occurs only inside this synthetic tmp_path fixture, never real state.
    b.activate_run(tmp_path,BID,v.RUN_NAME,now=NOW)
    route=b.route_task(tmp_path,"task01")
    assert route["prompt_file"]==m["runtime_prompt_path"]
    snap=b.snapshot_task(tmp_path,"task01",now=NOW)
    assert len(snap["assignments"])==4 and snap["strategy_version"]==v.VERSION
    assert all(x["strategy_version"]==v.VERSION and "strategy_telemetry_temp_path" in x for x in snap["assignments"])
    assert "evaluation_policy" not in snap
    b.atomic_write_text(Path(m["runtime_prompt_path"]),"changed")
    with pytest.raises(b.BenchmarkError,match="frozen runtime prompt"): b.route_task(tmp_path,"task01")


def test_finalize_rejects_document_gap_before_any_write(monkeypatch):
    root=Path("synthetic-never-written").resolve()
    rd=b._run_dir(root,BID,v.RUN_NAME)
    a=assignment()
    out=root/"data/news_benchmark_inbox"/BID/v.RUN_NAME
    a.update(temp_path=str(out/"x.tmp"),output_path=str(out/"x.json"),
             strategy_telemetry_temp_path=str(rd/"x.tmp"),strategy_telemetry_path=str(rd/"x.json"))
    snapshot={"assignments":[a],"company_state":{a["assignment_id"]:{"research_finished_at":NOW.isoformat()}}}
    value,payload=telemetry();value["official_items_followed"]=[]
    monkeypatch.setattr(b,"_owned_guard",lambda *args:(rd/"active.json",snapshot))
    monkeypatch.setattr(b,"_active_run",lambda *args:({},rd,{"strategy_version":v.VERSION}))
    monkeypatch.setattr(b,"_read_json",lambda path:value if path==rd/"x.tmp" else payload)
    monkeypatch.setattr(b,"_validate_success",lambda *args:(3,0,"no_news"))
    writes=[]
    monkeypatch.setattr(b,"replace_with_retry",lambda *args:writes.append(args))
    monkeypatch.setattr(b,"atomic_write_json",lambda *args:writes.append(args))
    monkeypatch.setattr(b,"_append_jsonl",lambda *args:writes.append(args))
    with pytest.raises(b.BenchmarkError,match="coverage"):
        b.finalize_company(root,"task01","token",a["assignment_id"],now=NOW)
    assert writes==[] and not root.exists()


def test_prompt_leakage_structure_company_and_agency_protection():
    root=Path(b.__file__).parents[1]
    prompt=v.prompt_text(root)
    assert v.leakage_scan(prompt,{},[{"ticker":"X001","company_name":"Example Recycling"}])["hit_count"]==0
    assert v.leakage_scan("Specific X001 query",{},[{"ticker":"X001","company_name":"Example Recycling"}])["status"]=="FAIL"
    assert v.leakage_scan(prompt,{"task01":[{"assignment_id":"x","nested":{"found_by":True}}]},[])["status"]=="FAIL"
    with pytest.raises(b.BenchmarkError): v.leakage_scan("https://named-agency.go.jp/docs",{},[])
