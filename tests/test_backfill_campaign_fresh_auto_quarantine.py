from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from lib.backfill import campaign_fresh_download_loop as loop
from lib.backfill.campaign_fresh_auto_quarantine import (
    FreshAutoQuarantineStop,
    canonical_failure_stage,
    failure_contract,
    prospective_limit_reason,
    safe_failure_telemetry,
    verify_evidence_tree,
    write_known_failure_evidence,
)
from tests.test_backfill_campaign_fresh_download_loop import CID, _environment


def _failure(code="TD_FILES_DISCNO_NOT_FOUND", stage="STAGE_A", status=404):
    return {
        "failure_code": code, "failure_stage": stage, "retryable": False,
        "source_route": "JQUANTS_TD_FILES",
        "http_status": status, "td_files_http_status": status,
        "download_attempts": [{"stage": "TD_FILES", "http_status": status,
            "reason_phrase": "Not Found", "result_code": code}],
    }


@pytest.mark.parametrize("failure,expected", [
    (_failure(), ("TD_FILES_DISCNO_NOT_FOUND", "STAGE_A", "JQUANTS_TD_FILES", 404)),
    (_failure("ZIP_INTERNAL_IDENTITY_CONFLICT", "ZIP_IDENTITY", 200),
     ("ZIP_INTERNAL_IDENTITY_CONFLICT", "ZIP_IDENTITY", "JQUANTS_TD_FILES", 200)),
    (_failure("HTTP_FAILED", "STAGE_A", 500), None),
])
def test_exact_known_contracts_only(failure, expected):
    assert failure_contract(failure) == expected


def test_td_files_raw_stage_is_normalized_only_for_exact_stage_a_404():
    failure = _failure(stage="TD_FILES")
    assert canonical_failure_stage(failure) == "STAGE_A"
    assert failure_contract(failure) == (
        "TD_FILES_DISCNO_NOT_FOUND", "STAGE_A", "JQUANTS_TD_FILES", 404,
    )


@pytest.mark.parametrize("changes", [
    {"failure_code": "HTTP_FAILED"},
    {"td_files_http_status": 401, "http_status": 401},
    {"td_files_http_status": 403, "http_status": 403},
    {"td_files_http_status": 500, "http_status": 500},
    {"td_files_http_status": 429, "http_status": 429},
    {"td_files_http_status": None, "http_status": None},
    {"failure_code": "HTTP_TIMEOUT", "td_files_http_status": None, "http_status": None},
    {"failure_code": "HTTP_CONNECTION_ERROR", "td_files_http_status": None, "http_status": None},
    {"failure_stage": "SIGNED_URL"},
    {"failure_stage": None},
    {"source_route": "STATIC_TDNET"},
])
def test_td_files_stage_normalization_is_fail_closed(changes):
    failure = _failure(stage="TD_FILES")
    failure.update(changes)
    assert canonical_failure_stage(failure) == str(failure.get("failure_stage") or "")
    assert failure_contract(failure) is None


def test_safe_failure_telemetry_keeps_stage_a_diagnostics_without_secrets():
    failure = _failure(stage="TD_FILES")
    failure["download_attempts"][0].update({
        "endpoint": "https://api.jquants.com/v2/td/files/20260000000001",
        "elapsed_seconds": 0.157,
        "retry_after": "12",
        "signed_url_received": False,
        "exception_type": "HTTPError",
        "exception_message": "Not Found",
    })
    telemetry = safe_failure_telemetry(
        failure,
        {"manifest_row_id": "0000001116", "requested_disclosure_no": "20260000000001"},
    )
    assert telemetry == {
        "manifest_row_id": "0000001116",
        "requested_disclosure_no": "20260000000001",
        "source_route": "JQUANTS_TD_FILES",
        "adapter_result_code": "TD_FILES_DISCNO_NOT_FOUND",
        "raw_failure_stage": "TD_FILES",
        "canonical_failure_stage": "STAGE_A",
        "http_status": 404,
        "endpoint_host": "api.jquants.com",
        "elapsed_milliseconds": 157.0,
        "retry_after_present": True,
        "retry_after": "12",
        "attempt_number": 1,
        "signed_url_received": False,
        "stage_b_started": False,
        "exception_class": "HTTPError",
        "exception_message_summary": "Not Found",
    }
    assert "https://" not in json.dumps(telemetry)


def test_canonical_evidence_round_trip_and_secret_redaction(tmp_path):
    output = tmp_path / "child"; output.mkdir()
    manifest = tmp_path / "manifest.json"; manifest.write_text("{}\n", encoding="utf-8")
    journal = output / "journal.json"
    result = write_known_failure_evidence(
        output_dir=output, run_id="run-1", campaign_id=CID,
        row={"manifest_row_id":"0000000001","requested_disclosure_no":"20260000000001",
             "fresh_status":"NOT_STARTED","attempt_count":0,"artifact_zip_sha256":None,
             "artifact_internal_document_id":None,"completed_at":None},
        failure=_failure(), journal_path=journal, manifest_path=manifest,
        manifest_sha256="a"*64, campaign_db_start_sha256="b"*64,
    )
    payload = verify_evidence_tree(Path(result["path"]), result["tree_sha256"])
    raw = Path(result["file"]).read_bytes()
    assert raw.endswith(b"\n") and b"http://" not in raw and b"token" not in raw.lower()
    assert payload["failure"]["failure_code"] == "TD_FILES_DISCNO_NOT_FOUND"
    assert payload["failure"]["raw_failure_stage"] == "STAGE_A"
    assert payload["failure"]["canonical_failure_stage"] == "STAGE_A"


def test_evidence_sha_mismatch_is_rejected(tmp_path):
    output=tmp_path/"child";output.mkdir();manifest=tmp_path/"m";manifest.write_text("x")
    result=write_known_failure_evidence(output_dir=output,run_id="r",campaign_id=CID,
        row={"manifest_row_id":"1","requested_disclosure_no":"2","fresh_status":"NOT_STARTED","attempt_count":0},
        failure=_failure(),journal_path=output/"journal.json",manifest_path=manifest,
        manifest_sha256="a"*64,campaign_db_start_sha256="b"*64)
    Path(result["file"]).write_text("{}\n",encoding="utf-8")
    with pytest.raises(FreshAutoQuarantineStop):
        verify_evidence_tree(Path(result["path"]),result["tree_sha256"])


@pytest.mark.parametrize("failure", [
    _failure("TD_FILES_DISCNO_NOT_FOUND", "ZIP_IDENTITY", 404),
    _failure("ZIP_INTERNAL_IDENTITY_CONFLICT", "STAGE_A", 200),
    _failure("TD_FILES_DISCNO_NOT_FOUND", "STAGE_A", 200),
    _failure("ZIP_INTERNAL_IDENTITY_CONFLICT", "ZIP_IDENTITY", 404),
])
def test_allowlisted_contract_cross_combinations_are_rejected(failure):
    assert failure_contract(failure) is None


def test_secret_material_is_rejected(tmp_path):
    failure = _failure(); failure["download_attempts"][0]["signed_url"] = "https://example.test/?token=x"
    # Non-allowlisted diagnostic keys are discarded rather than serialized.
    output=tmp_path/"child"; output.mkdir(); manifest=tmp_path/"m"; manifest.write_text("x")
    result=write_known_failure_evidence(output_dir=output,run_id="r",campaign_id=CID,
        row={"manifest_row_id":"1","requested_disclosure_no":"2","fresh_status":"NOT_STARTED","attempt_count":0},
        failure=failure,journal_path=output/"journal.json",manifest_path=manifest,
        manifest_sha256="a"*64,campaign_db_start_sha256="b"*64)
    assert "example.test" not in Path(result["file"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("kwargs,reason", [
    ({"auto_quarantines":1,"consecutive":0,"completed_in_run":0,"max_auto_quarantines":1,
      "max_consecutive_quarantines":2,"max_quarantine_rate_percent":50}, "MAX_AUTO_QUARANTINES"),
    ({"auto_quarantines":0,"consecutive":2,"completed_in_run":0,"max_auto_quarantines":5,
      "max_consecutive_quarantines":2,"max_quarantine_rate_percent":50}, "MAX_CONSECUTIVE_QUARANTINES"),
    ({"auto_quarantines":49,"consecutive":0,"completed_in_run":50,"max_auto_quarantines":100,
      "max_consecutive_quarantines":2,"max_quarantine_rate_percent":49}, "MAX_QUARANTINE_RATE"),
])
def test_prospective_limits(kwargs, reason):
    assert prospective_limit_reason(**kwargs) == reason


def test_six_hundred_row_policy_allows_500_complete_and_100_known_defects():
    completed = quarantined = consecutive = 0
    for _group in range(100):
        completed += 5
        consecutive = 0
        assert prospective_limit_reason(
            auto_quarantines=quarantined, consecutive=consecutive,
            completed_in_run=completed, max_auto_quarantines=100,
            max_consecutive_quarantines=2, max_quarantine_rate_percent=50,
        ) is None
        quarantined += 1; consecutive += 1
    assert (completed, quarantined) == (500, 100)


def test_cli_exposes_explicit_opt_in_and_limit_confirmations():
    from tools.backfill_campaign_fresh_download_loop import build_parser
    help_text = build_parser().format_help()
    for flag in (
        "--auto-quarantine-known-defects", "--confirm-auto-quarantine-known-defects",
        "--max-auto-quarantines", "--confirm-max-auto-quarantines",
        "--max-consecutive-auto-quarantines", "--confirm-max-consecutive-auto-quarantines",
        "--max-auto-quarantine-rate-percent", "--confirm-max-auto-quarantine-rate-percent",
    ):
        assert flag in help_text


def test_auto_mode_unknown_failure_stops_without_quarantine(monkeypatch, tmp_path):
    db, cache, plan = _environment(tmp_path, pending=1, quarantined=0)
    monkeypatch.setattr(loop, "_validate_parent_path", lambda path: None)
    def runner(command):
        output=Path(command[command.index("--output-dir")+1]);output.mkdir()
        (output/"journal.json").write_text(json.dumps({"current_phase":"FAILED","failure_code":"HTTP_500","rows":{}}),encoding="utf-8")
        return subprocess.CompletedProcess(command,2,"","HTTP_500")
    with pytest.raises(loop.FreshDownloadLoopStop, match=loop.STOP_UNKNOWN):
        loop.run_loop(campaign_db=db,campaign_id=CID,download_plan=plan,cache_root=cache,
            parent_output_dir=tmp_path/"v4-campaign-production-loop-20260719-030303",chunk_size=100,max_chunks=1,
            min_idle_window_minutes=20,source_route="JQUANTS_TD_FILES",confirm_production_cache_root=str(cache),
            confirm_campaign_id=CID,confirm_max_chunks=1,apply=True,production_apply=True,repo_root=tmp_path,
            child_runner=runner,runtime_checker=lambda _: {},idle_minutes_provider=lambda:60,
            child_output_provider=lambda n:tmp_path/"v4-campaign-production-download-20260719-030303",
            auto_quarantine_known_defects=True,confirm_auto_quarantine_known_defects=True,
            max_auto_quarantines=5,confirm_max_auto_quarantines=5,
            max_consecutive_auto_quarantines=2,confirm_max_consecutive_auto_quarantines=2,
            max_auto_quarantine_rate_percent=50,confirm_max_auto_quarantine_rate_percent=50)


def test_known_failure_is_formally_quarantined_then_reselected(monkeypatch, tmp_path):
    db, cache, plan = _environment(tmp_path, pending=100, quarantined=0)
    monkeypatch.setattr(loop, "_validate_parent_path", lambda path: None)
    monkeypatch.setattr(loop, "_verify_artifacts", lambda rows: {"accepted":len(rows),"production_ready":len(rows)})
    download_calls=[]; quarantine_calls=[]
    def value(command, flag): return command[command.index(flag)+1]
    def download_runner(command):
        download_calls.append(list(command)); output=Path(value(command,"--output-dir")); output.mkdir()
        manifest_path=Path(value(command,"--manifest-list")); manifest=json.loads(manifest_path.read_text())
        if len(download_calls)==1:
            row_id=manifest["rows"][0]["manifest_row_id"]
            conn=sqlite3.connect(db); row=dict(zip([x[1] for x in conn.execute("PRAGMA table_info(campaign_fresh_downloads)")],
                conn.execute("SELECT * FROM campaign_fresh_downloads WHERE manifest_row_id=?",(row_id,)).fetchone())); conn.close()
            row["requested_disclosure_no"]=manifest["rows"][0]["requested_disclosure_no"]
            evidence=write_known_failure_evidence(output_dir=output,run_id="child-1",campaign_id=CID,row=row,
                failure=_failure(),journal_path=output/"journal.json",manifest_path=manifest_path,
                manifest_sha256=loop.sha256_file(manifest_path),campaign_db_start_sha256=value(command,"--campaign-db-sha256"))
            journal={"current_phase":"FAILED","failure_code":"TD_FILES_DISCNO_NOT_FOUND","rows":{
                row_id:{"stage_a_state":"FAILED","failure_code":"TD_FILES_DISCNO_NOT_FOUND","failure_stage":"STAGE_A",
                        "source_route":"JQUANTS_TD_FILES","http_status":404,"failure_evidence":evidence}}}
            (output/"journal.json").write_text(json.dumps(journal),encoding="utf-8")
            return subprocess.CompletedProcess(command,2,"","known")
        ids=[x["manifest_row_id"] for x in manifest["rows"]]
        conn=sqlite3.connect(db)
        for row_id in ids: conn.execute("UPDATE campaign_fresh_downloads SET fresh_status='COMPLETE',attempt_count=1 WHERE manifest_row_id=?",(row_id,))
        conn.commit();conn.close()
        (output/"journal.json").write_text(json.dumps({"current_phase":"COMPLETE","run_id":"child-2","rows":{x:{} for x in ids}}),encoding="utf-8")
        return subprocess.CompletedProcess(command,0,"ok","")
    def quarantine_runner(command):
        quarantine_calls.append(list(command)); row_id=value(command,"--manifest-row-id")
        conn=sqlite3.connect(db);conn.execute("UPDATE campaign_fresh_downloads SET fresh_status='QUARANTINED',plan_classification='QUARANTINE_FRESH_RECHECK',last_error_code='TD_FILES_DISCNO_NOT_FOUND',last_error_stage='STAGE_A' WHERE manifest_row_id=?",(row_id,));conn.commit();conn.close()
        output=Path(value(command,"--output-dir"));output.mkdir();(output/"journal.json").write_text(json.dumps({"current_phase":"COMPLETE"}),encoding="utf-8")
        return subprocess.CompletedProcess(command,0,"ok","")
    result=loop.run_loop(campaign_db=db,campaign_id=CID,download_plan=plan,cache_root=cache,
        parent_output_dir=tmp_path/"v4-campaign-production-loop-20260719-010101",chunk_size=100,max_chunks=1,
        min_idle_window_minutes=20,source_route="JQUANTS_TD_FILES",confirm_production_cache_root=str(cache),
        confirm_campaign_id=CID,confirm_max_chunks=1,apply=True,production_apply=True,repo_root=tmp_path,
        child_runner=download_runner,quarantine_runner=quarantine_runner,runtime_checker=lambda _: {},
        idle_minutes_provider=lambda:60,child_output_provider=lambda n:tmp_path/f"v4-campaign-production-download-20260719-01010{n}",
        quarantine_output_provider=lambda n:tmp_path/f"v4-fresh-quarantine-20260719-02020{n}",
        auto_quarantine_known_defects=True,confirm_auto_quarantine_known_defects=True,
        max_auto_quarantines=5,confirm_max_auto_quarantines=5,
        max_consecutive_auto_quarantines=2,confirm_max_consecutive_auto_quarantines=2,
        max_auto_quarantine_rate_percent=50,confirm_max_auto_quarantine_rate_percent=50)
    assert result["summary"]["auto_quarantines"]==1
    assert result["summary"]["successful_chunks"]==1
    assert len(download_calls)==2 and len(quarantine_calls)==1
    assert "tools.backfill_campaign_fresh_quarantine" in quarantine_calls[0]


def test_row_1116_raw_td_files_404_is_quarantined_without_retry(monkeypatch, tmp_path):
    db, cache, plan = _environment(tmp_path, pending=101, quarantined=0)
    monkeypatch.setattr(loop, "_validate_parent_path", lambda path: None)
    monkeypatch.setattr(loop, "_verify_artifacts", lambda rows: {"accepted":len(rows),"production_ready":len(rows)})
    launches = quarantines = 0
    row_id = "0000000016"

    def value(command, flag): return command[command.index(flag)+1]
    def download(command):
        nonlocal launches
        launches += 1
        output=Path(value(command,"--output-dir"));output.mkdir()
        manifest_path=Path(value(command,"--manifest-list"));manifest=json.loads(manifest_path.read_text())
        ids=[item["manifest_row_id"] for item in manifest["rows"]]
        if row_id in ids:
            item=next(x for x in manifest["rows"] if x["manifest_row_id"]==row_id)
            conn=sqlite3.connect(db);columns=[x[1] for x in conn.execute("PRAGMA table_info(campaign_fresh_downloads)")]
            row=dict(zip(columns,conn.execute("SELECT * FROM campaign_fresh_downloads WHERE manifest_row_id=?",(row_id,)).fetchone()));conn.close()
            row["requested_disclosure_no"]=item["requested_disclosure_no"]
            failure=_failure(stage="TD_FILES")
            evidence=write_known_failure_evidence(
                output_dir=output,run_id="row-1116",campaign_id=CID,row=row,failure=failure,
                journal_path=output/"journal.json",manifest_path=manifest_path,
                manifest_sha256=loop.sha256_file(manifest_path),campaign_db_start_sha256=value(command,"--campaign-db-sha256"),
            )
            telemetry=safe_failure_telemetry(failure,row)
            (output/"journal.json").write_text(json.dumps({"current_phase":"FAILED","failure_code":failure["failure_code"],"rows":{row_id:{
                "failure_code":failure["failure_code"],"failure_stage":"STAGE_A","raw_failure_stage":"TD_FILES",
                "canonical_failure_stage":"STAGE_A","source_route":"JQUANTS_TD_FILES","http_status":404,
                "failure_telemetry":telemetry,"failure_evidence":evidence}}}),encoding="utf-8")
            return subprocess.CompletedProcess(command,2,"","known")
        conn=sqlite3.connect(db)
        for target in ids:conn.execute("UPDATE campaign_fresh_downloads SET fresh_status='COMPLETE',attempt_count=1 WHERE manifest_row_id=?",(target,))
        conn.commit();conn.close();(output/"journal.json").write_text(json.dumps({"current_phase":"COMPLETE","run_id":"recovery","rows":{x:{} for x in ids}}),encoding="utf-8")
        return subprocess.CompletedProcess(command,0,"ok","")
    def quarantine(command):
        nonlocal quarantines
        quarantines += 1
        assert value(command,"--manifest-row-id") == row_id
        assert value(command,"--failure-stage") == "STAGE_A"
        conn=sqlite3.connect(db);conn.execute("UPDATE campaign_fresh_downloads SET fresh_status='QUARANTINED',plan_classification='QUARANTINE_FRESH_RECHECK',last_error_code='TD_FILES_DISCNO_NOT_FOUND',last_error_stage='STAGE_A' WHERE manifest_row_id=?",(row_id,));conn.commit();conn.close()
        output=Path(value(command,"--output-dir"));output.mkdir();(output/"journal.json").write_text('{"current_phase":"COMPLETE"}',encoding="utf-8")
        return subprocess.CompletedProcess(command,0,"ok","")
    result=loop.run_loop(campaign_db=db,campaign_id=CID,download_plan=plan,cache_root=cache,
        parent_output_dir=tmp_path/"v4-campaign-production-loop-20260719-060606",chunk_size=100,max_chunks=1,
        min_idle_window_minutes=20,source_route="JQUANTS_TD_FILES",confirm_production_cache_root=str(cache),
        confirm_campaign_id=CID,confirm_max_chunks=1,apply=True,production_apply=True,repo_root=tmp_path,
        child_runner=download,quarantine_runner=quarantine,runtime_checker=lambda _: {},idle_minutes_provider=lambda:60,
        child_output_provider=lambda n:tmp_path/f"v4-campaign-production-download-20260719-{500000+n:06d}",
        quarantine_output_provider=lambda n:tmp_path/f"v4-fresh-quarantine-20260719-{600000+n:06d}",
        auto_quarantine_known_defects=True,confirm_auto_quarantine_known_defects=True,
        max_auto_quarantines=5,confirm_max_auto_quarantines=5,max_consecutive_auto_quarantines=2,
        confirm_max_consecutive_auto_quarantines=2,max_auto_quarantine_rate_percent=50,confirm_max_auto_quarantine_rate_percent=50)
    assert quarantines == 1 and launches == 2
    assert result["summary"]["auto_quarantines"] == 1
    conn=sqlite3.connect(db)
    row=conn.execute("SELECT fresh_status,attempt_count,last_error_code,last_error_stage FROM campaign_fresh_downloads WHERE manifest_row_id=?",(row_id,)).fetchone()
    conn.close()
    assert row == ("QUARANTINED",0,"TD_FILES_DISCNO_NOT_FOUND","STAGE_A")


def test_six_hundred_rows_one_in_six_known_defects_complete(monkeypatch, tmp_path):
    db, cache, plan = _environment(tmp_path, pending=600, quarantined=0)
    monkeypatch.setattr(loop, "_validate_parent_path", lambda path: None)
    monkeypatch.setattr(loop, "_verify_artifacts", lambda rows: {"accepted":len(rows),"production_ready":len(rows)})
    download_calls=quarantine_calls=0
    def value(command, flag): return command[command.index(flag)+1]
    def runner(command):
        nonlocal download_calls
        download_calls += 1; output=Path(value(command,"--output-dir"));output.mkdir()
        manifest_path=Path(value(command,"--manifest-list")); manifest=json.loads(manifest_path.read_text())
        defect=next((item for item in manifest["rows"] if (int(item["manifest_row_id"])-1)%6==0),None)
        if defect:
            row_id=defect["manifest_row_id"]
            conn=sqlite3.connect(db); columns=[x[1] for x in conn.execute("PRAGMA table_info(campaign_fresh_downloads)")]
            row=dict(zip(columns,conn.execute("SELECT * FROM campaign_fresh_downloads WHERE manifest_row_id=?",(row_id,)).fetchone()));conn.close()
            row["requested_disclosure_no"]=defect["requested_disclosure_no"]
            identity=((int(row_id)-1)//6)%2==0
            failure=_failure("ZIP_INTERNAL_IDENTITY_CONFLICT","ZIP_IDENTITY",200) if identity else _failure()
            evidence=write_known_failure_evidence(output_dir=output,run_id=f"child-{download_calls}",campaign_id=CID,row=row,
                failure=failure,journal_path=output/"journal.json",manifest_path=manifest_path,
                manifest_sha256=loop.sha256_file(manifest_path),campaign_db_start_sha256=value(command,"--campaign-db-sha256"))
            contract=evidence["contract"]
            (output/"journal.json").write_text(json.dumps({"current_phase":"FAILED","failure_code":contract[0],"rows":{
                row_id:{"failure_code":contract[0],"failure_stage":contract[1],"source_route":contract[2],
                        "http_status":contract[3],"failure_evidence":evidence}}}),encoding="utf-8")
            return subprocess.CompletedProcess(command,2,"","known")
        ids=[item["manifest_row_id"] for item in manifest["rows"]]
        conn=sqlite3.connect(db)
        for row_id in ids: conn.execute("UPDATE campaign_fresh_downloads SET fresh_status='COMPLETE',attempt_count=1 WHERE manifest_row_id=?",(row_id,))
        conn.commit();conn.close()
        (output/"journal.json").write_text(json.dumps({"current_phase":"COMPLETE","run_id":f"child-{download_calls}","rows":{x:{} for x in ids}}),encoding="utf-8")
        return subprocess.CompletedProcess(command,0,"ok","")
    def quarantine(command):
        nonlocal quarantine_calls
        quarantine_calls += 1; row_id=value(command,"--manifest-row-id"); code=value(command,"--reason-code"); stage=value(command,"--failure-stage")
        conn=sqlite3.connect(db);conn.execute("UPDATE campaign_fresh_downloads SET fresh_status='QUARANTINED',plan_classification='QUARANTINE_FRESH_RECHECK',last_error_code=?,last_error_stage=? WHERE manifest_row_id=?",(code,stage,row_id));conn.commit();conn.close()
        output=Path(value(command,"--output-dir"));output.mkdir();(output/"journal.json").write_text('{"current_phase":"COMPLETE"}',encoding="utf-8")
        return subprocess.CompletedProcess(command,0,"ok","")
    result=loop.run_loop(campaign_db=db,campaign_id=CID,download_plan=plan,cache_root=cache,
        parent_output_dir=tmp_path/"v4-campaign-production-loop-20260719-040404",chunk_size=100,max_chunks=5,
        min_idle_window_minutes=20,source_route="JQUANTS_TD_FILES",confirm_production_cache_root=str(cache),
        confirm_campaign_id=CID,confirm_max_chunks=5,apply=True,production_apply=True,repo_root=tmp_path,
        child_runner=runner,quarantine_runner=quarantine,runtime_checker=lambda _: {},idle_minutes_provider=lambda:60,
        child_output_provider=lambda n:tmp_path/f"v4-campaign-production-download-20260719-{100000+n:06d}",
        quarantine_output_provider=lambda n:tmp_path/f"v4-fresh-quarantine-20260719-{200000+n:06d}",
        auto_quarantine_known_defects=True,confirm_auto_quarantine_known_defects=True,
        max_auto_quarantines=150,confirm_max_auto_quarantines=150,
        max_consecutive_auto_quarantines=20,confirm_max_consecutive_auto_quarantines=20,
        max_auto_quarantine_rate_percent=30,confirm_max_auto_quarantine_rate_percent=30)
    assert result["summary"]["counts"]=={"COMPLETE":500,"QUARANTINED":100}
    assert result["summary"]["remaining"]==0 and result["journal"]["campaign_completed"] is True
    assert result["summary"]["auto_quarantines"]==100 and quarantine_calls==100
    assert download_calls==105


def test_row_803_conflict_is_quarantined_and_replenished_to_one_hundred(monkeypatch, tmp_path):
    db, cache, plan = _environment(tmp_path, pending=812, quarantined=0)
    conn=sqlite3.connect(db)
    conn.execute("UPDATE campaign_fresh_downloads SET fresh_status='COMPLETE' WHERE CAST(manifest_row_id AS INTEGER)<=710")
    conn.execute("UPDATE campaign_fresh_downloads SET fresh_status='QUARANTINED',plan_classification='QUARANTINE_FRESH_RECHECK' WHERE manifest_row_id='0000000719'")
    conn.commit();conn.close()
    monkeypatch.setattr(loop,"_validate_parent_path",lambda path:None)
    monkeypatch.setattr(loop,"_verify_artifacts",lambda rows:{"accepted":len(rows),"production_ready":len(rows)})
    launches=0
    def value(command,flag):return command[command.index(flag)+1]
    def runner(command):
        nonlocal launches
        launches+=1;output=Path(value(command,"--output-dir"));output.mkdir();manifest_path=Path(value(command,"--manifest-list"));manifest=json.loads(manifest_path.read_text())
        ids=[item["manifest_row_id"] for item in manifest["rows"]]
        if "0000000803" in ids:
            item=next(x for x in manifest["rows"] if x["manifest_row_id"]=="0000000803")
            conn=sqlite3.connect(db);columns=[x[1] for x in conn.execute("PRAGMA table_info(campaign_fresh_downloads)")];row=dict(zip(columns,conn.execute("SELECT * FROM campaign_fresh_downloads WHERE manifest_row_id='0000000803'").fetchone()));conn.close();row["requested_disclosure_no"]=item["requested_disclosure_no"]
            evidence=write_known_failure_evidence(output_dir=output,run_id="row-803",campaign_id=CID,row=row,
                failure=_failure("ZIP_INTERNAL_IDENTITY_CONFLICT","ZIP_IDENTITY",200),journal_path=output/"journal.json",
                manifest_path=manifest_path,manifest_sha256=loop.sha256_file(manifest_path),campaign_db_start_sha256=value(command,"--campaign-db-sha256"))
            (output/"journal.json").write_text(json.dumps({"current_phase":"FAILED","failure_code":"ZIP_INTERNAL_IDENTITY_CONFLICT","rows":{"0000000803":{
                "failure_code":"ZIP_INTERNAL_IDENTITY_CONFLICT","failure_stage":"ZIP_IDENTITY","source_route":"JQUANTS_TD_FILES","http_status":200,"failure_evidence":evidence}}}),encoding="utf-8")
            return subprocess.CompletedProcess(command,2,"","conflict")
        conn=sqlite3.connect(db)
        for row_id in ids:conn.execute("UPDATE campaign_fresh_downloads SET fresh_status='COMPLETE',attempt_count=1 WHERE manifest_row_id=?",(row_id,))
        conn.commit();conn.close();(output/"journal.json").write_text(json.dumps({"current_phase":"COMPLETE","run_id":"replenished","rows":{x:{} for x in ids}}),encoding="utf-8")
        return subprocess.CompletedProcess(command,0,"ok","")
    def quarantine(command):
        row_id=value(command,"--manifest-row-id");conn=sqlite3.connect(db);conn.execute("UPDATE campaign_fresh_downloads SET fresh_status='QUARANTINED',plan_classification='QUARANTINE_FRESH_RECHECK',last_error_code='ZIP_INTERNAL_IDENTITY_CONFLICT',last_error_stage='ZIP_IDENTITY' WHERE manifest_row_id=?",(row_id,));conn.commit();conn.close();output=Path(value(command,"--output-dir"));output.mkdir();(output/"journal.json").write_text('{"current_phase":"COMPLETE"}',encoding="utf-8");return subprocess.CompletedProcess(command,0,"ok","")
    result=loop.run_loop(campaign_db=db,campaign_id=CID,download_plan=plan,cache_root=cache,
        parent_output_dir=tmp_path/"v4-campaign-production-loop-20260719-050505",chunk_size=100,max_chunks=1,min_idle_window_minutes=20,
        source_route="JQUANTS_TD_FILES",confirm_production_cache_root=str(cache),confirm_campaign_id=CID,confirm_max_chunks=1,
        apply=True,production_apply=True,repo_root=tmp_path,child_runner=runner,quarantine_runner=quarantine,
        runtime_checker=lambda _: {},idle_minutes_provider=lambda:60,
        child_output_provider=lambda n:tmp_path/f"v4-campaign-production-download-20260719-{300000+n:06d}",
        quarantine_output_provider=lambda n:tmp_path/f"v4-fresh-quarantine-20260719-{400000+n:06d}",
        auto_quarantine_known_defects=True,confirm_auto_quarantine_known_defects=True,
        max_auto_quarantines=5,confirm_max_auto_quarantines=5,max_consecutive_auto_quarantines=2,
        confirm_max_consecutive_auto_quarantines=2,max_auto_quarantine_rate_percent=50,confirm_max_auto_quarantine_rate_percent=50)
    assert result["summary"]["counts"]=={"COMPLETE":810,"QUARANTINED":2}
    assert result["summary"]["auto_quarantines"]==1 and launches==2
    assert result["summary"]["quarantined_requested_disclosure_nos"]
