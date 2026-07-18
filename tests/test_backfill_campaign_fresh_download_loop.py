from __future__ import annotations

import json
import sqlite3
import subprocess
import uuid
from pathlib import Path

import pytest

from lib.backfill import campaign_fresh_download_loop as loop


CID = "v4-jquants-3y-20260710"


def _environment(tmp_path: Path, pending: int = 300, quarantined: int = 1):
    db = tmp_path / "campaign.db"
    cache = tmp_path / "cache"
    cache.mkdir()
    plan = tmp_path / "plan.jsonl"
    conn = sqlite3.connect(db)
    conn.executescript("""
      CREATE TABLE campaign_schema_metadata(schema_version TEXT NOT NULL,created_at TEXT,updated_at TEXT);
      INSERT INTO campaign_schema_metadata VALUES('2','x','x');
      CREATE TABLE campaign_filings(campaign_id TEXT,manifest_row_id TEXT,requested_disclosure_no TEXT,PRIMARY KEY(campaign_id,manifest_row_id));
      CREATE TABLE campaign_fresh_downloads(
        campaign_id TEXT,manifest_row_id TEXT,plan_classification TEXT,fresh_status TEXT,
        attempt_count INTEGER,auto_ready_allowed INTEGER,quarantine_release_required INTEGER,
        target_zip_path TEXT,target_provenance_path TEXT,artifact_zip_sha256 TEXT,
        artifact_internal_document_id TEXT,artifact_ticker TEXT,artifact_period TEXT,
        artifact_quarter TEXT,identity_verdict TEXT,last_run_id TEXT,last_journal_path TEXT,
        last_error_code TEXT,last_error_stage TEXT,last_error_message TEXT,completed_at TEXT,
        updated_at TEXT,PRIMARY KEY(campaign_id,manifest_row_id));
    """)
    lines = []
    for index in range(1, pending + quarantined + 1):
        row_id = f"{index:010d}"
        requested = f"20260000{index:06d}"
        status = "NOT_STARTED" if index <= pending else "QUARANTINED"
        classification = "STANDARD_FRESH_DOWNLOAD" if index <= pending else "QUARANTINE_FRESH_RECHECK"
        conn.execute("INSERT INTO campaign_filings VALUES(?,?,?)", (CID, row_id, requested))
        conn.execute("INSERT INTO campaign_fresh_downloads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            CID, row_id, classification, status, 0, int(status == "NOT_STARTED"), int(status == "QUARANTINED"),
            str(cache / row_id / "xbrl.zip"), str(cache / row_id / "provenance.json"),
            None, None, None, None, None, None, None, None, None, None, None, None, "x",
        ))
        lines.append(json.dumps({"campaign_id": CID, "manifest_row_id": row_id,
            "requested_disclosure_no": requested, "plan_classification": classification,
            "download_allowed": status == "NOT_STARTED"}, separators=(",", ":")))
    conn.commit(); conn.close()
    plan.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return db, cache, plan


def _fake_runner(db: Path, *, fail_call: int | None = None):
    calls = []
    def runner(command):
        calls.append(list(command))
        def value(flag): return command[command.index(flag) + 1]
        manifest = json.loads(Path(value("--manifest-list")).read_text(encoding="utf-8"))
        output = Path(value("--output-dir")); output.mkdir()
        if fail_call == len(calls):
            (output / "journal.json").write_text(json.dumps({"current_phase":"NETWORK_STARTED","failure_code":"MOCK_STAGE_A_37"}), encoding="utf-8")
            return subprocess.CompletedProcess(command, 2, "", "MOCK_STAGE_A_37")
        ids = [r["manifest_row_id"] for r in manifest["rows"]]
        conn = sqlite3.connect(db)
        for row_id in ids:
            conn.execute("UPDATE campaign_fresh_downloads SET fresh_status='COMPLETE',attempt_count=attempt_count+1 WHERE campaign_id=? AND manifest_row_id=?", (CID, row_id))
        conn.commit(); conn.close()
        journal = {"current_phase":"COMPLETE","run_id":f"mock-{len(calls)}","rows":{
            row_id:{"stage_a_state":"SUCCEEDED","stage_b_state":"SUCCEEDED"} for row_id in ids}}
        (output / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "ok", "")
    runner.calls = calls
    return runner


def _run(tmp_path, monkeypatch, *, pending=300, max_chunks=3, idle=None, fail_call=None):
    db, cache, plan = _environment(tmp_path, pending=pending)
    monkeypatch.setattr(loop, "_validate_parent_path", lambda path: None)
    monkeypatch.setattr(loop, "_verify_artifacts", lambda rows: {"accepted":len(rows),"production_ready":len(rows),"identity_verdicts":{"mock":len(rows)}})
    runner = _fake_runner(db, fail_call=fail_call)
    values = iter(idle or [60] * (max_chunks + 1))
    output = tmp_path / "v4-campaign-production-loop-20260718-010101"
    result = loop.run_loop(campaign_db=db, campaign_id=CID, download_plan=plan, cache_root=cache,
        parent_output_dir=output, chunk_size=100, max_chunks=max_chunks,
        min_idle_window_minutes=20, source_route="JQUANTS_TD_FILES",
        confirm_production_cache_root=str(cache), confirm_campaign_id=CID,
        confirm_max_chunks=max_chunks, apply=True, production_apply=True, repo_root=tmp_path,
        child_runner=runner, runtime_checker=lambda _: {"active_processes":[],"locks":[]},
        idle_minutes_provider=lambda: next(values),
        child_output_provider=lambda n: tmp_path / f"v4-campaign-production-download-20260718-0101{n:02d}")
    return result, db, cache, plan, output, runner


def _counts(db):
    conn=sqlite3.connect(db)
    result=dict(conn.execute("SELECT fresh_status,COUNT(*) FROM campaign_fresh_downloads GROUP BY fresh_status"))
    conn.close(); return result


def test_three_chunks_succeed(monkeypatch, tmp_path):
    result, db, _cache, _plan, output, runner = _run(tmp_path, monkeypatch)
    assert result["summary"]["chunks_completed"] == 3
    assert result["journal"]["phase"] == "COMPLETE"
    assert _counts(db) == {"COMPLETE":300,"QUARANTINED":1}
    assert len(runner.calls) == 3
    assert len(list((output / "manifests").glob("*.json"))) == 3


def test_child_failure_stops_without_retry(monkeypatch, tmp_path):
    with pytest.raises(loop.FreshDownloadLoopStop, match=loop.STOP_CHILD):
        _run(tmp_path, monkeypatch, fail_call=2)
    db = tmp_path / "campaign.db"
    assert _counts(db) == {"COMPLETE":100,"NOT_STARTED":200,"QUARANTINED":1}


def test_idle_window_stops_before_next_manifest(monkeypatch, tmp_path):
    result, db, _cache, _plan, output, runner = _run(tmp_path, monkeypatch, idle=[60,19])
    assert result["journal"]["phase"] == "STOPPED_IDLE_WINDOW"
    assert len(runner.calls) == 1
    assert len(list((output / "manifests").glob("*.json"))) == 1
    assert _counts(db)["COMPLETE"] == 100


def test_partial_final_chunk_uses_actual_count(monkeypatch, tmp_path):
    result, db, _cache, _plan, _output, runner = _run(tmp_path, monkeypatch, pending=79)
    command = runner.calls[0]
    assert command[command.index("--expected-count")+1] == "79"
    assert command[command.index("--max-items")+1] == "79"
    assert command[command.index("--confirm-production-item-count")+1] == "79"
    assert result["journal"]["campaign_completed"] is True
    assert _counts(db) == {"COMPLETE":79,"QUARANTINED":1}


@pytest.mark.parametrize("max_chunks,confirm", [(0,0),(501,501),(3,2)])
def test_max_chunk_guards(monkeypatch, tmp_path, max_chunks, confirm):
    db,cache,plan=_environment(tmp_path,1)
    monkeypatch.setattr(loop,"_validate_parent_path",lambda path:None)
    with pytest.raises(loop.FreshDownloadLoopStop,match=loop.STOP_GUARD):
        loop.run_loop(campaign_db=db,campaign_id=CID,download_plan=plan,cache_root=cache,
            parent_output_dir=tmp_path/"v4-campaign-production-loop-20260718-010101",chunk_size=100,
            max_chunks=max_chunks,min_idle_window_minutes=20,source_route="JQUANTS_TD_FILES",
            confirm_production_cache_root=str(cache),confirm_campaign_id=CID,
            confirm_max_chunks=confirm,apply=True,production_apply=True,repo_root=tmp_path)


def test_production_rejects_non_100_chunk(monkeypatch, tmp_path):
    db,cache,plan=_environment(tmp_path,1);monkeypatch.setattr(loop,"_validate_parent_path",lambda path:None)
    with pytest.raises(loop.FreshDownloadLoopStop,match=loop.STOP_GUARD):
        loop.run_loop(campaign_db=db,campaign_id=CID,download_plan=plan,cache_root=cache,
            parent_output_dir=tmp_path/"v4-campaign-production-loop-20260718-010101",chunk_size=99,max_chunks=1,
            min_idle_window_minutes=20,source_route="JQUANTS_TD_FILES",confirm_production_cache_root=str(cache),
            confirm_campaign_id=CID,confirm_max_chunks=1,apply=True,production_apply=True,repo_root=tmp_path)


def test_dynamic_selection_skips_quarantined(tmp_path):
    db,_cache,_plan=_environment(tmp_path,3,1)
    conn=sqlite3.connect(db);conn.execute("UPDATE campaign_fresh_downloads SET fresh_status='QUARANTINED',plan_classification='QUARANTINE_FRESH_RECHECK' WHERE manifest_row_id='0000000002'");conn.commit();conn.close()
    assert [r["manifest_row_id"] for r in loop.select_next_rows(db,CID,100)] == ["0000000001","0000000003"]


def test_atomic_journal_leaves_no_temp(tmp_path):
    path=tmp_path/"journal.json";loop.atomic_write_json(path,{"phase":"CREATED"});loop.atomic_write_json(path,{"phase":"COMPLETE"})
    assert json.loads(path.read_text())["phase"] == "COMPLETE"
    assert not list(tmp_path.glob("*.tmp"))


def test_parent_output_must_not_exist(tmp_path):
    path=tmp_path/"v4-campaign-production-loop-20260718-010101";path.mkdir()
    with pytest.raises(loop.FreshDownloadLoopStop,match=loop.STOP_PATH): loop._validate_parent_path(path)


def test_module_import_has_no_filesystem_side_effect(tmp_path):
    before=list(tmp_path.iterdir())
    __import__("tools.backfill_campaign_fresh_download_loop")
    assert list(tmp_path.iterdir()) == before


def test_parent_has_no_update_sql():
    source=Path(loop.__file__).read_text(encoding="utf-8")
    assert "UPDATE campaign_fresh_downloads" not in source
    assert "tools.backfill_campaign_fresh_download" in source


def test_failed_retryable_is_eligible(tmp_path):
    db,_cache,_plan=_environment(tmp_path,2,0)
    conn=sqlite3.connect(db);conn.execute("UPDATE campaign_fresh_downloads SET fresh_status='FAILED_RETRYABLE' WHERE manifest_row_id='0000000001'");conn.commit();conn.close()
    assert [r["manifest_row_id"] for r in loop.select_next_rows(db,CID,100)] == ["0000000001","0000000002"]


@pytest.mark.parametrize("field", ["apply","production_apply"])
def test_apply_flags_are_mandatory(monkeypatch,tmp_path,field):
    db,cache,plan=_environment(tmp_path,1,0);monkeypatch.setattr(loop,"_validate_parent_path",lambda path:None)
    kwargs=dict(campaign_db=db,campaign_id=CID,download_plan=plan,cache_root=cache,parent_output_dir=tmp_path/"v4-campaign-production-loop-20260718-010101",chunk_size=100,max_chunks=1,min_idle_window_minutes=20,source_route="JQUANTS_TD_FILES",confirm_production_cache_root=str(cache),confirm_campaign_id=CID,confirm_max_chunks=1,apply=True,production_apply=True,repo_root=tmp_path)
    kwargs[field]=False
    with pytest.raises(loop.FreshDownloadLoopStop,match=loop.STOP_GUARD):loop.run_loop(**kwargs)


def test_minimum_idle_contract_guard(monkeypatch,tmp_path):
    db,cache,plan=_environment(tmp_path,1,0);monkeypatch.setattr(loop,"_validate_parent_path",lambda path:None)
    with pytest.raises(loop.FreshDownloadLoopStop,match=loop.STOP_GUARD):
        loop.run_loop(campaign_db=db,campaign_id=CID,download_plan=plan,cache_root=cache,parent_output_dir=tmp_path/"v4-campaign-production-loop-20260718-010101",chunk_size=100,max_chunks=1,min_idle_window_minutes=19,source_route="JQUANTS_TD_FILES",confirm_production_cache_root=str(cache),confirm_campaign_id=CID,confirm_max_chunks=1,apply=True,production_apply=True,repo_root=tmp_path)


@pytest.mark.parametrize("bad_cache,bad_campaign", [(True,False),(False,True)])
def test_confirmation_guards(monkeypatch,tmp_path,bad_cache,bad_campaign):
    db,cache,plan=_environment(tmp_path,1,0);monkeypatch.setattr(loop,"_validate_parent_path",lambda path:None)
    with pytest.raises(loop.FreshDownloadLoopStop,match=loop.STOP_GUARD):
        loop.run_loop(campaign_db=db,campaign_id=CID,download_plan=plan,cache_root=cache,parent_output_dir=tmp_path/"v4-campaign-production-loop-20260718-010101",chunk_size=100,max_chunks=1,min_idle_window_minutes=20,source_route="JQUANTS_TD_FILES",confirm_production_cache_root="bad" if bad_cache else str(cache),confirm_campaign_id="bad" if bad_campaign else CID,confirm_max_chunks=1,apply=True,production_apply=True,repo_root=tmp_path)


def test_child_output_collision_fails(monkeypatch,tmp_path):
    db,cache,plan=_environment(tmp_path,1,0);monkeypatch.setattr(loop,"_validate_parent_path",lambda path:None)
    child=tmp_path/"v4-campaign-production-download-20260718-010101";child.mkdir()
    with pytest.raises(loop.FreshDownloadLoopStop,match=loop.STOP_PATH):
        loop.run_loop(campaign_db=db,campaign_id=CID,download_plan=plan,cache_root=cache,parent_output_dir=tmp_path/"v4-campaign-production-loop-20260718-010101",chunk_size=100,max_chunks=1,min_idle_window_minutes=20,source_route="JQUANTS_TD_FILES",confirm_production_cache_root=str(cache),confirm_campaign_id=CID,confirm_max_chunks=1,apply=True,production_apply=True,repo_root=tmp_path,runtime_checker=lambda _:{},idle_minutes_provider=lambda:60,child_output_provider=lambda _:child)


def test_runtime_active_stops_before_manifest(monkeypatch,tmp_path):
    db,cache,plan=_environment(tmp_path,1,0);monkeypatch.setattr(loop,"_validate_parent_path",lambda path:None)
    def active(_): raise loop.FreshDownloadLoopStop("ACTIVE")
    with pytest.raises(loop.FreshDownloadLoopStop,match="ACTIVE"):
        loop.run_loop(campaign_db=db,campaign_id=CID,download_plan=plan,cache_root=cache,parent_output_dir=tmp_path/"v4-campaign-production-loop-20260718-010101",chunk_size=100,max_chunks=1,min_idle_window_minutes=20,source_route="JQUANTS_TD_FILES",confirm_production_cache_root=str(cache),confirm_campaign_id=CID,confirm_max_chunks=1,apply=True,production_apply=True,repo_root=tmp_path,runtime_checker=active,idle_minutes_provider=lambda:60)


def test_missing_child_journal_is_postflight_failure(monkeypatch,tmp_path):
    db,cache,plan=_environment(tmp_path,1,0);monkeypatch.setattr(loop,"_validate_parent_path",lambda path:None)
    monkeypatch.setattr(loop,"_verify_artifacts",lambda rows:{"accepted":len(rows)})
    def runner(command):
        Path(command[command.index("--output-dir")+1]).mkdir()
        return subprocess.CompletedProcess(command,0,"","")
    parent=tmp_path/"v4-campaign-production-loop-20260718-010101"
    with pytest.raises(loop.FreshDownloadLoopStop,match=loop.STOP_POSTFLIGHT):
        loop.run_loop(campaign_db=db,campaign_id=CID,download_plan=plan,cache_root=cache,parent_output_dir=parent,chunk_size=100,max_chunks=1,min_idle_window_minutes=20,source_route="JQUANTS_TD_FILES",confirm_production_cache_root=str(cache),confirm_campaign_id=CID,confirm_max_chunks=1,apply=True,production_apply=True,repo_root=tmp_path,child_runner=runner,runtime_checker=lambda _:{},idle_minutes_provider=lambda:60,child_output_provider=lambda _:tmp_path/"v4-campaign-production-download-20260718-010101")
    assert json.loads((parent/"journal.json").read_text())["phase"] == "STOPPED_CHILD_FAILURE"


def test_resume_reselects_current_state(monkeypatch,tmp_path):
    with pytest.raises(loop.FreshDownloadLoopStop,match=loop.STOP_CHILD):
        _run(tmp_path,monkeypatch,pending=250,fail_call=2)
    db=tmp_path/"campaign.db";cache=tmp_path/"cache";plan=tmp_path/"plan.jsonl"
    monkeypatch.setattr(loop,"_validate_parent_path",lambda path:None);monkeypatch.setattr(loop,"_verify_artifacts",lambda rows:{"accepted":len(rows),"production_ready":len(rows)})
    runner=_fake_runner(db)
    loop.run_loop(campaign_db=db,campaign_id=CID,download_plan=plan,cache_root=cache,parent_output_dir=tmp_path/"v4-campaign-production-loop-20260718-020202",chunk_size=100,max_chunks=1,min_idle_window_minutes=20,source_route="JQUANTS_TD_FILES",confirm_production_cache_root=str(cache),confirm_campaign_id=CID,confirm_max_chunks=1,apply=True,production_apply=True,repo_root=tmp_path,child_runner=runner,runtime_checker=lambda _:{},idle_minutes_provider=lambda:60,child_output_provider=lambda _:tmp_path/"v4-campaign-production-download-20260718-020202")
    manifest=json.loads((tmp_path/"v4-campaign-production-loop-20260718-020202/manifests/chunk-0001.json").read_text())
    assert manifest["rows"][0]["manifest_row_id"] == "0000000101"
    assert manifest["rows"][-1]["manifest_row_id"] == "0000000200"
    assert _counts(db)["COMPLETE"] == 200


def test_child_command_uses_formal_module(monkeypatch,tmp_path):
    result,_db,_cache,_plan,_output,runner=_run(tmp_path,monkeypatch,pending=1,max_chunks=1)
    command=runner.calls[0]
    assert command[command.index("-m")+1] == "tools.backfill_campaign_fresh_download"
    assert result["summary"]["chunks_completed"] == 1


@pytest.mark.parametrize("limit", [0, 101])
def test_dynamic_selection_limit_guard(tmp_path, limit):
    db,_cache,_plan=_environment(tmp_path,1,0)
    with pytest.raises(loop.FreshDownloadLoopStop,match=loop.STOP_GUARD):
        loop.select_next_rows(db,CID,limit)


def test_source_route_guard(monkeypatch,tmp_path):
    db,cache,plan=_environment(tmp_path,1,0);monkeypatch.setattr(loop,"_validate_parent_path",lambda path:None)
    with pytest.raises(loop.FreshDownloadLoopStop,match=loop.STOP_GUARD):
        loop.run_loop(campaign_db=db,campaign_id=CID,download_plan=plan,cache_root=cache,parent_output_dir=tmp_path/"v4-campaign-production-loop-20260718-010101",chunk_size=100,max_chunks=1,min_idle_window_minutes=20,source_route="OTHER",confirm_production_cache_root=str(cache),confirm_campaign_id=CID,confirm_max_chunks=1,apply=True,production_apply=True,repo_root=tmp_path)
