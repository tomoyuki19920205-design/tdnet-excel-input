"""Synthetic offline fixtures; never require or modify real benchmark artifacts."""
import copy

import pytest

from tools import company_news_batch_benchmark as b
from tools import company_news_post_run_v2 as p
from tools import company_news_extended_gold_v2 as e


def event(n=0, ticker="1000"):
    return {"ticker": ticker, "published_at": "2026-08-25T00:00:00+09:00",
            "headline": f"Project {n} new production capacity", "source_url": f"https://example.com/news/{n}.html",
            "category": "capacity", "summary": f"New production line at site {n}", "why_it_matters": "Sales capacity rises",
            "source_type": "company", "importance": "medium", "earnings_relevance": "likely"}


def gold_event(n=0, verdict="VALID_MATERIAL", ticker="1000", **kw):
    return {**b._event_public_fields(event(n, ticker)), "match_cluster_id": f"g{n}", "company": "Fixture",
            "verdict": verdict, "duplicate_of_cluster_id": None, "primary_source_type": "company_ir", **kw}


def fixture():
    g = {"benchmark_id": "fixture", "events": [gold_event()]}
    u = {"benchmark_id": "fixture", "events": [{"match_cluster_id": "g0", "ticker": "1000", "members": [b._event_public_fields(event())]}]}
    raw = event(1)
    ext = [{"cluster_id": "ext1", "ticker": "1000", "company": "Fixture", "event_date": "2026-08-25",
            "canonical_headline": raw["headline"], "summary": raw["summary"], "why_it_matters": raw["why_it_matters"],
            "source_urls": [{"url": raw["source_url"], "source_type": "company"}]}]
    rows = [{"cluster_id": "ext1", "verdict": "VALID_MATERIAL", "duplicate_of_cluster_id": None}]
    return g, u, ext, rows


def test_normalized_truth_is_in_memory_and_deterministic():
    args = fixture()
    before = copy.deepcopy(args)
    a = e.normalize_truth(*args)
    assert a == e.normalize_truth(*args) and args == before
    assert a[3]["material_total"] == 2
    assert a[0]["events"][-1]["importance"] is None


def test_cross_set_identity_merges_without_overwriting_verdicts():
    g,u,ext,rows = fixture()
    ext[0].update(canonical_headline=event()["headline"], summary=event()["summary"],
                  source_urls=[{"url": event()["source_url"], "source_type": "company"}])
    truth,_,roots,n = e.normalize_truth(g,u,ext,rows)
    assert roots["ext1"] == "g0" and n["material_total"] == 1
    assert len(n["cross_set_merges"]) == 1 and rows[0]["verdict"] == "VALID_MATERIAL"


def test_conflicting_verdict_not_re_adjudicated():
    g,u,ext,rows = fixture()
    ext[0].update(canonical_headline=event()["headline"], source_urls=[{"url":event()["source_url"],"source_type":"company"}])
    rows[0]["verdict"] = "VALID_NON_MATERIAL"
    a = e.normalize_truth(g,u,ext,rows)
    assert a[3]["cross_set_review"] and not a[3]["cross_set_merges"]


@pytest.mark.parametrize("verdict", ["DUPLICATE", "MATCHER_FALSE_NEGATIVE"])
def test_explicit_cross_set_duplicate_chain(verdict):
    g,u,ext,rows = fixture()
    rows[0].update(verdict=verdict, duplicate_of_cluster_id="g0")
    a = e.normalize_truth(g,u,ext,rows)
    assert a[2]["ext1"] == "g0" and a[3]["material_total"] == 1


def test_duplicate_cycle_rejected():
    g,u,ext,rows = fixture()
    rows[0].update(verdict="DUPLICATE", duplicate_of_cluster_id="ext1")
    with pytest.raises(b.BenchmarkError):
        e.normalize_truth(g,u,ext,rows)


def test_common48_uses_identity_not_order():
    tickers = [str(1000+i) for i in range(48)]
    c = {"reference_tickers": tickers}
    researched = {"reference01":set(tickers), "reference02":set(reversed(tickers)), "batch04v2":set(tickers)|{"2000"}}
    assert e.common_membership(c,researched) == set(tickers)
    researched["reference02"].remove(tickers[0])
    with pytest.raises(b.BenchmarkError):
        e.common_membership(c,researched)


def test_reference_outside_cohort_not_counted_missed():
    truth={"events":[gold_event(0),gold_event(1,ticker="2000")]}
    m=e.cohort_metrics(truth,[],{"1000"})
    assert m["material"]["target"] == 1 and m["company_level_material_FN_tickers"] == ["1000"]


def test_five_run_common_scoring_and_mirrors():
    g,u,ext,rows=fixture()
    truth,union,roots,_=e.normalize_truth(g,u,ext,rows)
    for label in e.RUNS.values():
        classified=e.classify_outputs({"1000":[event(),event(),event(1)]},label,union,roots,{"g0"})
        m=e.cohort_metrics(truth,classified,{"1000"})
        assert m["material"]["found"] == m["material"]["target"] == 2
        assert m["precision_denominator"] == 2 and m["raw_output_count"] == 3


def test_out_of_window_excluded_truth_included_precision():
    g,u,ext,rows=fixture()
    rows[0]["verdict"]="OUT_OF_WINDOW"
    truth,union,roots,_=e.normalize_truth(g,u,ext,rows)
    c=e.classify_outputs({"1000":[event(),event(1)]},"test",union,roots,{"g0"})
    m=e.cohort_metrics(truth,c,{"1000"})
    assert m["material"]["target"] == 1 and m["material_precision"] == .5
    assert m["matched_verdict_counts"]["OUT_OF_WINDOW"] == 1


def test_unadjudicated_is_not_silently_false_positive():
    g,u,ext,rows=fixture()
    truth,union,roots,_=e.normalize_truth(g,u,ext,rows)
    c=e.classify_outputs({"1000":[event(),event(5)]},"test",union,roots,{"g0"})
    m=e.cohort_metrics(truth,c,{"1000"})
    assert m["material_precision"] == 1 and m["precision_lower_bound_including_unresolved"] == .5
    assert m["precision_status"] == "PARTIAL_ADJUDICATED_OUTPUTS_ONLY"
    assert e.coverage_inventory(c)["unresolved_unique"] == 1


def test_critical_extension_not_inferred_from_candidate():
    g,u,ext,rows=fixture()
    g["events"][0]["importance"]="high"
    truth,union,roots,_=e.normalize_truth(g,u,ext,rows)
    raw=event(1); raw["importance"]="high"
    m=e.cohort_metrics(truth,e.classify_outputs({"1000":[raw]},"test",union,roots,{"g0"}),{"1000"})
    assert m["critical"] == {"target":1,"found":0,"recall":0}
    assert m["extension_criticality_unclassified"] == 1


@pytest.mark.parametrize("stage,verdict", [(s,v) for s in p.STAGES for v in ("VALID_MATERIAL","VALID_NON_MATERIAL","OUT_OF_WINDOW")])
def test_stage_material_and_noise_attribution(stage,verdict):
    g,u,ext,rows=fixture(); rows[0]["verdict"]=verdict; rows[0]["primary_source_type"]="government"
    truth,union,roots,_=e.normalize_truth(g,u,ext,rows)
    c=e.classify_outputs({"1000":[event(1)]},"test",union,roots,{"g0"})
    cs=[{"ticker":"1000","candidate_provenance":[{"headline":event(1)["headline"],"source_url":event(1)["source_url"],"selected_for_output":True,"stage":stage}]}]
    a=e.stage_attribution(ext,rows,c,cs)
    assert a["summary"][stage]["verdict_counts"] == {verdict:1}
    assert a["summary"][stage]["source_buckets"]["government"] == {verdict:1}


def test_shared_archive_different_title_not_stage_evidence():
    g,u,ext,rows=fixture(); rows[0]["primary_source_type"]="company_news"
    _,union,roots,_=e.normalize_truth(g,u,ext,rows)
    c=e.classify_outputs({"1000":[event(1)]},"test",union,roots,{"g0"})
    cs=[{"ticker":"1000","candidate_provenance":[{"headline":"Other disclosure", "source_url":event(1)["source_url"],"selected_for_output":True,"stage":"official_first"}]}]
    assert e.stage_attribution(ext,rows,c,cs)["summary"]["UNRESOLVED"]["total"] == 1


def test_critical_miss_details_do_not_assert_browser_trace():
    g,u,_,_=fixture(); g["events"][0]["primary_source_url"]=event()["source_url"]
    cs=[{"ticker":"1000","official_sources_found":["https://example.com/ir/"],"external_sources_found":[],"candidate_provenance":[]}]
    d=e.critical_detail(g,u,["g0"],[],[],cs)[0]
    assert d["classification"] == "SOURCE_NOT_REACHED" and d["confidence"] == "MEDIUM"
    assert not d["document_url_in_source_telemetry"] and "not browser traces" in d["limit"]


def test_scheduler_and_quality_separation_and_full192_gate():
    m={"material":{"recall":.8},"critical":{"found":6,"target":7},"precision_status":"COMPLETE_FOR_OBSERVED_OUTPUTS","material_precision":.5}
    d=e.decision({"batch04v2":m},{"fast":True},{"batch02":{"unresolved_unique":1}},[])
    assert d["gates"]["runtime_capacity"] == "PASS" and not d["production_candidate"]
    assert d["quality_verdict"] == "FAIL_BATCH04V2_QUALITY"
    assert "WARN_BATCH04V2_FULL192_GOLD_INCOMPLETE" in d["warnings"]
    assert d["gates"]["scheduler_reliability"] == "SEPARATE_SCHEDULER_RELIABILITY_ISSUE"


def extension_files(tmp_path):
    """165 synthetic events partitioned into the expected 18 packets."""
    g,u,_,_=fixture()
    records=[]; offset=0
    verdicts=["VALID_MATERIAL"]*67+["VALID_NON_MATERIAL"]*96+["OUT_OF_WINDOW"]*2
    for number in range(1,19):
        # Split 165 across 18 packets without depending on live inputs.
        count=10 if number<=3 else 9
        ev=[]; items=[]
        for index in range(offset,offset+count):
            raw=event(index+100,ticker=str(2000+index))
            cid=f"ext{index}"
            ev.append({"cluster_id":cid,"ticker":raw["ticker"],"company":"Fixture","event_date":"2026-08-25",
                       "canonical_headline":raw["headline"],"summary":raw["summary"],"why_it_matters":raw["why_it_matters"],
                       "source_urls":[{"url":raw["source_url"],"source_type":"company"}]})
            items.append({"cluster_id":cid,"verdict":verdicts[index],"materiality_reason":"Fixture verified",
                "event_exists":True,"event_within_window":verdicts[index]!="OUT_OF_WINDOW","company_specific":True,
                "duplicate_of_cluster_id":None,"primary_source_url":raw["source_url"],"primary_source_type":"company_news",
                "primary_source_verified":True,"original_source_verified":True,"earnings_channel":"capacity",
                "direction":"neutral","confidence":"HIGH","notes":"Synthetic", "evidence":[{"url":raw["source_url"],"source_type":"company_news","supports":"Fixture"}]})
        ident=f"gold_extension_{number:03d}"
        packet={"schema_version":b.GOLD_PACKET_SCHEMA,"benchmark_id":"fixture","packet_id":ident,"search_from":"2026-08-25","search_to":"2026-08-31","events":ev,"response_contract":b._gold_response_contract()}
        packet["packet_sha256"]=b._packet_sha256(packet)
        response={"schema_version":b.GOLD_RESPONSE_SCHEMA,"benchmark_id":"fixture","packet_id":ident,"packet_sha256":packet["packet_sha256"],"adjudicated_at":"2026-09-04T00:00:00+09:00","items":items}
        p.write_new(tmp_path/(ident+".json"),packet)
        p.write_new(tmp_path/"responses"/(ident+".json"),response)
        records.append({"packet_id":ident,"filename":ident+".json","packet_sha256":packet["packet_sha256"],"cluster_ids":[x["cluster_id"] for x in ev]})
        offset+=count
    manifest={"benchmark_id":"fixture","source_gold_sha256":b._json_sha256(g),"source_union_sha256":b._json_sha256(u),"packets":records}
    p.write_new(tmp_path/"gold_extension_manifest.json",manifest)
    return g,u


def test_extension_18_165_67_96_2_unknown0_read_only(tmp_path):
    g,u=extension_files(tmp_path)
    before=p.hashes(tmp_path.rglob("*"))
    a=e.load_extension(tmp_path,g,u)
    assert a == e.load_extension(tmp_path,g,u)
    facts=a[2]
    assert facts["packet_count"] == facts["imported_response_count"] == 18
    assert facts["cluster_count"] == 165 and facts["verdict_counts"] == e.EXPECTED
    assert p.hashes(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("failure",["missing","sha","coverage","unknown","totals","gold"])
def test_extension_integrity_fail_closed(tmp_path,failure):
    g,u=extension_files(tmp_path)
    target=tmp_path/"responses/gold_extension_001.json"
    r=b._read_json(target)
    if failure=="missing":
        target.unlink()
    elif failure=="gold":
        g["events"][0]["headline"]="changed"
    else:
        if failure=="sha": r["packet_sha256"]="bad"
        if failure=="coverage": r["items"].pop()
        if failure=="unknown": r["items"][0]["verdict"]="UNKNOWN"
        if failure=="totals": r["items"][0]["verdict"]="VALID_NON_MATERIAL"
        p.write_new(target,r,derived=True)
    before=p.hashes(tmp_path.rglob("*"))
    with pytest.raises(b.BenchmarkError):
        e.load_extension(tmp_path,g,u)
    assert p.hashes(tmp_path.rglob("*")) == before
