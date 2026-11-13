from datetime import datetime, time, timezone, timedelta
from types import SimpleNamespace
import json
import pytest
import requests
from tools.check_ny_market_delivery import assess, run
from tools.preflight_ny_market_runtime import _resilient_select, preflight

JST = timezone(timedelta(hours=9))

@pytest.mark.parametrize("hour,quality,expected", [(7,False,"pending"),(8,False,"failed"),(8,True,"healthy")])
def test_deadline_distinguishes_complete_from_provisional(hour, quality, expected):
    r=assess({"status":"ready","complete_readback_verified":quality}, {"ok":True},
             now=datetime(2026,11,13,hour,tzinfo=JST), deadline=time(7,45))
    assert r["status"] == expected

def test_disabled_automation_is_failure_even_with_complete_report():
    r=assess({"status":"ready","complete_readback_verified":True}, {"ok":False},
             now=datetime(2026,11,13,6,tzinfo=JST), deadline=time(7,45))
    assert r["reasons"] == ["automation_configuration_drift"]
    assert r["status"] == "failed"

@pytest.mark.parametrize("code,count", [(401,1),(403,1),(404,1),(429,4),(500,4),(503,4)])
def test_http_retry_limit_and_nonretryable_auth(monkeypatch,code,count):
    calls=[]
    def fail(*a,**kw):
        calls.append(1)
        response=requests.Response(); response.status_code=code
        raise requests.HTTPError(response=response)
    monkeypatch.setattr("tools.preflight_ny_market_runtime._strict_select",fail)
    monkeypatch.setattr("tools.preflight_ny_market_runtime.time.sleep",lambda _:None)
    with pytest.raises(requests.HTTPError):
        _resilient_select("reports",params={},config={})
    assert len(calls)==count

@pytest.mark.parametrize("mutation", ["none","empty_body","local_provisional","hash_mismatch","duplicate","unresolved"])
def test_readback_requires_real_complete_content(monkeypatch,mutation,tmp_path):
    import hashlib
    body="verified report" if mutation!="empty_body" else ""
    digest=hashlib.sha256(body.encode()).hexdigest()
    report={"report_markdown":body,"quality_status":"complete","unresolved_items":[]}
    if mutation=="unresolved": report["unresolved_items"]=["unfinished"]
    rows={"canonical_ny_market_reports":[report],"canonical_ny_market_report_runs":[{"status":"success","attempt":1}],
          "api_latest_news_stream":[{"report_markdown":"other" if mutation=="hash_mismatch" else body,"quality_status":"complete"}]}
    if mutation=="duplicate": rows["canonical_ny_market_reports"]*=2
    monkeypatch.setattr("tools.preflight_ny_market_runtime.bootstrap_production_write_environment",lambda _:SimpleNamespace(safe_metadata=lambda:{}))
    monkeypatch.setattr("tools.preflight_ny_market_runtime.get_supabase_read_config",lambda:{})
    monkeypatch.setattr("tools.preflight_ny_market_runtime._resilient_select",lambda t,**kw:rows[t])
    monkeypatch.setattr("tools.preflight_ny_market_runtime._sqlite_snapshot",lambda *a:{"report_count":1,"run_count":1,"markdown_sha256":digest,"run_status":"success","run_attempt":1,"quality_status":"provisional" if mutation=="local_provisional" else "complete"})
    r=preflight(tmp_path,tmp_path/'db','ny_market_daily:2026-11-13')
    assert r["complete_readback_verified"] == (mutation=="none")

def test_failure_is_recorded_without_exception_secret(monkeypatch,tmp_path):
    def fail(*a): raise requests.ConnectionError("secret-token-never-print")
    args=SimpleNamespace(automation_config=tmp_path/'absent.toml',runtime=tmp_path,
        production_root=tmp_path,db=tmp_path/'db',deadline="07:45",output_dir=tmp_path/'status')
    r=run(args,now=datetime(2026,11,13,8,tzinfo=JST),fetch=fail)
    assert r["status"]=="failed"
    raw=(args.output_dir/'latest.json').read_text(encoding='utf8')
    assert 'secret-token' not in raw
    assert len(list((args.output_dir/'history').glob('*.json')))==1
    assert '異常を検出' in (args.output_dir/'index.html').read_text(encoding='utf8')
