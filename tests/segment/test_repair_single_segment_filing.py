from __future__ import annotations

import hashlib
import json
import os
import zipfile
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from lib.pipeline.canonical_writer import expand_segments_rows
from src.segment.models import SegmentRawRow
from tools.repair_single_segment_filing import (
    RepairDeps,
    RepairStop,
    _default_load_metadata,
    _event_identity,
    main,
    run_repair,
)


REQUESTED = "20260713591788"
INTERNAL = "20260713340570"
CANONICAL = "4ee4e4cb3e3aaba10376497b5cd1f04ae66f4b51d64d3bde859f00fd46298483"


def _args(path: Path, **overrides) -> Namespace:
    values = dict(
        requested_id=REQUESTED, expected_ticker="4057", expected_internal_id=INTERNAL,
        expected_canonical_filing_id=CANONICAL, expected_period="2026-05-31",
        expected_quarter="FY", expected_zip_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        apply=False,
    )
    values.update(overrides)
    return Namespace(**values)


def _cli_argv(**overrides) -> list[str]:
    values = {
        "requested-id": REQUESTED,
        "expected-ticker": "4057",
        "expected-internal-id": INTERNAL,
        "expected-canonical-filing-id": CANONICAL,
        "expected-period": "2026-05-31",
        "expected-quarter": "FY",
        "expected-zip-sha256": "a" * 64,
    }
    values.update(overrides)
    return [item for key, value in values.items() for item in (f"--{key}", value)]


def _raw(name, period, sales, profit, role):
    start = "2025-06-01" if role == "current" else "2024-06-01"
    end = "2026-05-31" if role == "current" else "2025-05-31"
    return SegmentRawRow(
        source="xbrl", source_system="tdnet", raw_ticker="4057", normalized_ticker="4057",
        period=period, quarter="FY", raw_segment_name=name, normalized_segment_name=name,
        sales=sales, profit=profit, unit="million_yen",
        raw_json={"_context_evidence": {
            "current_or_previous": role, "context_start": start, "context_end": end,
        }},
    )


def _rows():
    names = ["Cloud Commerce Platform", "Ec Business Growth", "Datautillization"]
    current = [(2731, 840), (129, 4), (0, -59)]
    previous = [(2617, 867), (247, -13), (None, -29)]
    return [
        *[_raw(n, "2025-05-31", s, p, "previous") for n, (s, p) in zip(names, previous)],
        *[_raw(n, "2026-05-31", s, p, "current") for n, (s, p) in zip(names, current)],
    ]


def _logical():
    return [
        {"segment_name": "Cloud Commerce Platform", "sales": 2731, "profit": 840, "source_system": "tdnet"},
        {"segment_name": "Ec Business Growth", "sales": 129, "profit": 4, "source_system": "tdnet"},
        {"segment_name": "Datautillization", "sales": 0, "profit": -59, "source_system": "tdnet"},
    ]


def _event_row(**overrides):
    row = {
        "id": "event-id",
        "ticker": "4057",
        "company_name": "Ｇ－インタファクトリ",
        "headline": "2026年５月期 決算短信",
        "disclosed_at": "2026-07-13T11:30:00+09:00",
        "event_type": "earnings",
        "event_subtype": "FY",
        "source_url": f"https://example.invalid/1401{REQUESTED}.pdf",
        "pdf_url": f"https://example.invalid/1401{REQUESTED}.pdf",
        "raw_payload": {
            "raw": {
                "source_doc_id": CANONICAL,
                "xbrl_doc_id": INTERNAL,
                "requested_disclosure_no": REQUESTED,
            }
        },
    }
    row.update(overrides)
    return row


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = [] if payload is None else payload
        self.text = text

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


def _load_metadata(cache_zip, response=None, side_effect=None):
    config = {
        "rest_url": "https://example.invalid/rest/v1",
        "headers": {"apikey": "secret", "Authorization": "Bearer secret"},
    }
    with (
        patch("lib.pipeline.db.get_supabase_read_config", return_value=config),
        patch("requests.get", return_value=response, side_effect=side_effect) as get,
    ):
        result = _default_load_metadata(_args(cache_zip))
    return result, get


class Repo:
    def __init__(self):
        self.rows = []
        self.writer_calls = 0

    def select(self, table, **kwargs):
        assert table == "canonical_segments"
        return list(self.rows)

    def writer(self, **kwargs):
        self.writer_calls += 1
        rows, _ = expand_segments_rows(
            ticker=kwargs["ticker"], period=kwargs["period"], quarter=kwargs["quarter"],
            segments=kwargs["segments"], source=kwargs["source"], filing_id=kwargs["filing_id"],
            disclosure_datetime=kwargs["disclosure_datetime"], correction_flag=False,
            unit=kwargs["unit"],
        )
        self.rows = rows
        return {"written": len(rows), "skipped": 0, "errors": 0}


def _deps(path: Path, repo=None, **changes):
    repo = repo or Repo()
    provenance = SimpleNamespace(
        internal_document_id=INTERNAL, period="2026-05-31", quarter="FY",
        requested_disclosure_no=REQUESTED,
    )
    values = dict(
        load_metadata=lambda args: {
            "requested_disclosure_no": REQUESTED, "ticker": "4057",
            "canonical_filing_id": CANONICAL, "internal_document_id": INTERNAL,
            "company_name": "Ｇ－インタファクトリ", "disclosed_at": "2026-07-13T11:30:00+09:00",
            "document_type": "earnings", "cache_path": str(path),
        },
        resolve=Mock(return_value=SimpleNamespace(
            zip_path=str(path), trusted_provenance=provenance, error_reason="",
        )),
        verify=Mock(return_value=SimpleNamespace(passed=True, verdict="official_linked_xbrl_match", rejection_reason="")),
        extract_detailed=Mock(return_value=SimpleNamespace(status="success_with_rows", segments=_rows())),
        filter_detailed=Mock(return_value=(_logical(), SimpleNamespace(status="success_with_rows"))),
        expand=expand_segments_rows,
        select=repo.select,
        writer=repo.writer,
        read_config=lambda: {"read": True},
        write_config=lambda: {"write": True},
    )
    values.update(changes)
    return RepairDeps(**values), repo


@pytest.fixture
def cache_zip(tmp_path):
    path = tmp_path / REQUESTED / "xbrl.zip"
    path.parent.mkdir()
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("dummy", b"fixture")
    return path


def test_event_identity_uses_production_raw_payload_contract():
    identity = _event_identity(_event_row(source_doc_id=None))
    assert identity == {
        "requested": {REQUESTED}, "canonical": {CANONICAL}, "internal": {INTERNAL},
    }


@pytest.mark.parametrize("container", ["payload", "raw", "extracted"])
def test_event_identity_reads_requested_id_from_formal_structure(container):
    payload = {"raw": {}, "extracted": {}}
    target = payload if container == "payload" else payload[container]
    target["requested_disclosure_no"] = REQUESTED
    row = _event_row(source_url="", pdf_url="", raw_payload=payload)
    assert _event_identity(row)["requested"] == {REQUESTED}


def test_metadata_load_succeeds_without_tdnet_events_source_doc_id(cache_zip):
    result, _ = _load_metadata(cache_zip, _Response(payload=[_event_row(source_doc_id=None)]))
    assert result["requested_disclosure_no"] == REQUESTED
    assert result["internal_document_id"] == INTERNAL
    assert result["canonical_filing_id"] == CANONICAL


def test_event_identity_uses_existing_url_disclosure_helper():
    row = _event_row(raw_payload={
        "raw": {"source_doc_id": CANONICAL, "xbrl_doc_id": INTERNAL},
    })
    assert _event_identity(row)["requested"] == {REQUESTED}


def test_metadata_query_is_read_only_and_uses_real_columns(cache_zip):
    with (
        patch("requests.post") as post,
        patch("requests.patch") as patch_request,
        patch("requests.delete") as delete,
    ):
        _, get = _load_metadata(cache_zip, _Response(payload=[_event_row()]))
    params = get.call_args.kwargs["params"]
    assert get.call_count == 1
    assert params["ticker"] == "eq.4057" and params["event_type"] == "eq.earnings"
    assert "source_doc_id" not in params and "source_doc_id" not in params["select"]
    post.assert_not_called(); patch_request.assert_not_called(); delete.assert_not_called()


def test_metadata_candidate_zero_is_filing_not_found(cache_zip):
    with pytest.raises(RepairStop) as exc_info:
        _load_metadata(cache_zip, _Response(payload=[]))
    assert exc_info.value.judgment == "STOP_SINGLE_SEGMENT_REPAIR_FILING_NOT_FOUND"


@pytest.mark.parametrize("field", ["requested", "ticker", "internal", "canonical"])
def test_metadata_candidate_identity_mismatch_is_not_found(cache_zip, field):
    row = _event_row()
    if field == "requested":
        other = "20260713590000"
        row["source_url"] = f"https://example.invalid/1401{other}.pdf"
        row["pdf_url"] = row["source_url"]
        row["raw_payload"]["raw"]["requested_disclosure_no"] = other
    elif field == "ticker":
        row["ticker"] = "9999"
    elif field == "internal":
        row["raw_payload"]["raw"]["xbrl_doc_id"] = "20260713000000"
    else:
        row["raw_payload"]["raw"]["source_doc_id"] = "f" * 64
    with pytest.raises(RepairStop) as exc_info:
        _load_metadata(cache_zip, _Response(payload=[row]))
    assert exc_info.value.judgment == "STOP_SINGLE_SEGMENT_REPAIR_FILING_NOT_FOUND"


def test_metadata_two_exact_matches_are_not_unique(cache_zip):
    with pytest.raises(RepairStop) as exc_info:
        _load_metadata(cache_zip, _Response(payload=[_event_row(), _event_row(id="event-2")]))
    assert exc_info.value.judgment == "STOP_SINGLE_SEGMENT_REPAIR_FILING_NOT_UNIQUE"
    assert exc_info.value.detail == "matches=2"


@pytest.mark.parametrize(
    ("status", "code", "classification"),
    [(400, "PGRST100", "http_error"), (400, "42703", "schema_error")],
)
def test_metadata_http_and_postgres_errors_are_query_failed(cache_zip, status, code, classification):
    response = _Response(status_code=status, payload={"code": code, "message": "unsafe detail"})
    with pytest.raises(RepairStop) as exc_info:
        _load_metadata(cache_zip, response)
    assert exc_info.value.judgment == "STOP_SINGLE_SEGMENT_REPAIR_METADATA_QUERY_FAILED"
    assert f"http_status={status}" in exc_info.value.detail
    assert f"postgres_code={code}" in exc_info.value.detail
    assert f"classification={classification}" in exc_info.value.detail
    assert "unsafe detail" not in exc_info.value.detail


def test_metadata_connection_error_is_query_failed(cache_zip):
    with pytest.raises(RepairStop) as exc_info:
        _load_metadata(cache_zip, side_effect=ConnectionError("secret endpoint"))
    assert exc_info.value.judgment == "STOP_SINGLE_SEGMENT_REPAIR_METADATA_QUERY_FAILED"
    assert exc_info.value.detail == (
        "operation=select_tdnet_events http_status=none "
        "postgres_code=none classification=connection_error"
    )


def test_metadata_invalid_response_is_query_failed(cache_zip):
    with pytest.raises(RepairStop) as exc_info:
        _load_metadata(cache_zip, _Response(payload=ValueError("bad json")))
    assert exc_info.value.judgment == "STOP_SINGLE_SEGMENT_REPAIR_METADATA_QUERY_FAILED"
    assert "classification=invalid_response" in exc_info.value.detail


def test_malformed_raw_payload_is_safe_and_not_found(cache_zip):
    row = _event_row(raw_payload="{malformed", source_url="", pdf_url="")
    with pytest.raises(RepairStop) as exc_info:
        _load_metadata(cache_zip, _Response(payload=[row]))
    assert exc_info.value.judgment == "STOP_SINGLE_SEGMENT_REPAIR_FILING_NOT_FOUND"


def test_url_partial_numeric_match_is_not_accepted(cache_zip):
    row = _event_row(
        source_url=f"https://example.invalid/{REQUESTED}0.pdf",
        pdf_url="",
        raw_payload={"raw": {"source_doc_id": CANONICAL, "xbrl_doc_id": INTERNAL}},
    )
    with pytest.raises(RepairStop) as exc_info:
        _load_metadata(cache_zip, _Response(payload=[row]))
    assert exc_info.value.judgment == "STOP_SINGLE_SEGMENT_REPAIR_FILING_NOT_FOUND"


def test_metadata_query_error_stops_before_resolver_and_writer(cache_zip):
    deps, repo = _deps(cache_zip)
    deps.load_metadata = _default_load_metadata
    config = {
        "rest_url": "https://example.invalid/rest/v1",
        "headers": {"apikey": "secret", "Authorization": "Bearer secret"},
    }
    with (
        patch("lib.pipeline.db.get_supabase_read_config", return_value=config),
        patch("requests.get", return_value=_Response(
            status_code=400, payload={"code": "42703"},
        )),
        pytest.raises(RepairStop) as exc_info,
    ):
        run_repair(_args(cache_zip), deps)
    assert exc_info.value.judgment == "STOP_SINGLE_SEGMENT_REPAIR_METADATA_QUERY_FAILED"
    deps.resolve.assert_not_called()
    assert repo.writer_calls == 0


def test_dry_run_writer_zero_and_payload_six(cache_zip):
    deps, repo = _deps(cache_zip)
    result = run_repair(_args(cache_zip), deps)
    assert result["final_judgment"] == "PASS_SINGLE_SEGMENT_REPAIR_DRY_RUN_READY"
    assert result["current_target_rows"] == 3
    assert result["excluded_previous_rows"] == 3
    assert len(result["eav_rows"]) == 6
    assert repo.writer_calls == 0


def test_apply_writer_once_and_readback(cache_zip):
    deps, repo = _deps(cache_zip)
    result = run_repair(_args(cache_zip, apply=True), deps)
    assert result["final_judgment"] == "PASS_SINGLE_SEGMENT_REPAIR_APPLIED_AND_VERIFIED"
    assert result["readback_count"] == 6
    assert repo.writer_calls == 1


def test_same_apply_twice_is_idempotent(cache_zip):
    deps, repo = _deps(cache_zip)
    run_repair(_args(cache_zip, apply=True), deps)
    result = run_repair(_args(cache_zip, apply=True), deps)
    assert result["final_judgment"] == "PASS_SINGLE_SEGMENT_REPAIR_ALREADY_PRESENT"
    assert repo.writer_calls == 1
    assert len(repo.rows) == 6


@pytest.mark.parametrize("field,value", [
    ("requested_disclosure_no", "20260713590000"),
    ("ticker", "9999"),
    ("canonical_filing_id", "f" * 64),
])
def test_metadata_identity_mismatch_stops(cache_zip, field, value):
    def metadata(args):
        row = {"requested_disclosure_no": REQUESTED, "ticker": "4057", "canonical_filing_id": CANONICAL,
               "cache_path": str(cache_zip), "disclosed_at": "2026-07-13T11:30:00+09:00"}
        row[field] = value
        return row
    deps, _ = _deps(cache_zip, load_metadata=metadata)
    with pytest.raises(RepairStop) as exc_info:
        run_repair(_args(cache_zip), deps)
    assert exc_info.value.judgment == "STOP_SINGLE_SEGMENT_REPAIR_IDENTITY_ARGUMENT_MISMATCH"
    assert field in exc_info.value.detail


def test_internal_id_mismatch_stops(cache_zip):
    prov = SimpleNamespace(internal_document_id="20260713000000")
    deps, _ = _deps(cache_zip, resolve=Mock(return_value=SimpleNamespace(
        zip_path=str(cache_zip), trusted_provenance=prov, error_reason="")))
    with pytest.raises(RepairStop) as exc_info:
        run_repair(_args(cache_zip), deps)
    assert exc_info.value.judgment == "STOP_SINGLE_SEGMENT_REPAIR_IDENTITY_ARGUMENT_MISMATCH"
    assert exc_info.value.detail == "internal document ID mismatch"


def test_zip_sha_mismatch_stops_before_resolver(cache_zip):
    deps, _ = _deps(cache_zip)
    with pytest.raises(RepairStop, match="ZIP_SHA_MISMATCH"):
        run_repair(_args(cache_zip, expected_zip_sha256="0" * 64), deps)
    deps.resolve.assert_not_called()


def test_cache_missing_stops(tmp_path):
    missing = tmp_path / "missing.zip"
    deps, _ = _deps(missing)
    with pytest.raises(RepairStop, match="CACHE_MISSING"):
        run_repair(Namespace(**{**vars(_args.__wrapped__)}) if False else Namespace(
            requested_id=REQUESTED, expected_ticker="4057", expected_internal_id=INTERNAL,
            expected_canonical_filing_id=CANONICAL, expected_period="2026-05-31",
            expected_quarter="FY", expected_zip_sha256="0" * 64, apply=False), deps)


def test_resolver_is_cache_only(cache_zip):
    deps, _ = _deps(cache_zip)
    run_repair(_args(cache_zip), deps)
    kwargs = deps.resolve.call_args.kwargs
    assert kwargs["allow_jquants_fetch"] is False
    assert kwargs["persist_provenance"] is False
    assert kwargs["doc_id"] == REQUESTED


def test_identity_rejection_stops(cache_zip):
    deps, repo = _deps(cache_zip, verify=Mock(return_value=SimpleNamespace(
        passed=False, verdict="rejected", rejection_reason="period_mismatch")))
    with pytest.raises(RepairStop) as exc_info:
        run_repair(_args(cache_zip, apply=True), deps)
    assert exc_info.value.judgment == "STOP_SINGLE_SEGMENT_REPAIR_IDENTITY_REJECTED"
    assert exc_info.value.detail == "period_mismatch"
    assert repo.writer_calls == 0


def test_no_current_segments_stops(cache_zip):
    deps, _ = _deps(cache_zip, filter_detailed=Mock(return_value=([], None)))
    with pytest.raises(RepairStop, match="NO_CURRENT_SEGMENTS"):
        run_repair(_args(cache_zip), deps)


def test_duplicate_segment_keys_stop(cache_zip):
    duplicate = [_logical()[0], dict(_logical()[0])]
    deps, _ = _deps(cache_zip, filter_detailed=Mock(return_value=(duplicate, None)))
    with pytest.raises(RepairStop, match="DUPLICATE_SEGMENT_KEYS"):
        run_repair(_args(cache_zip), deps)


def _expected_payload(cache_zip):
    return expand_segments_rows(
        ticker="4057", period="2026-05-31", quarter="FY", segments=_logical(), source="xbrl",
        filing_id=CANONICAL, disclosure_datetime="2026-07-13T11:30:00+09:00",
        correction_flag=False, unit="millions_jpy",
    )[0]


def test_partial_existing_rows_stop(cache_zip):
    repo = Repo(); repo.rows = _expected_payload(cache_zip)[:1]
    deps, _ = _deps(cache_zip, repo)
    with pytest.raises(RepairStop, match="PARTIAL_EXISTING_ROWS"):
        run_repair(_args(cache_zip), deps)


def test_conflicting_existing_rows_stop(cache_zip):
    repo = Repo(); repo.rows = _expected_payload(cache_zip); repo.rows[0] = dict(repo.rows[0], value=-1)
    deps, _ = _deps(cache_zip, repo)
    with pytest.raises(RepairStop, match="CONFLICTING_EXISTING_ROWS"):
        run_repair(_args(cache_zip), deps)


def test_duplicate_existing_rows_stop(cache_zip):
    repo = Repo(); repo.rows = _expected_payload(cache_zip); repo.rows.append(dict(repo.rows[0]))
    deps, _ = _deps(cache_zip, repo)
    with pytest.raises(RepairStop, match="DUPLICATE_EXISTING_ROWS"):
        run_repair(_args(cache_zip), deps)


def test_readback_mismatch_stops(cache_zip):
    repo = Repo()
    def writer_without_readback(**kwargs):
        repo.writer_calls += 1
        return {"written": 6, "skipped": 0, "errors": 0}
    deps, _ = _deps(cache_zip, repo, writer=writer_without_readback)
    with pytest.raises(RepairStop, match="READBACK_MISMATCH"):
        run_repair(_args(cache_zip, apply=True), deps)


def test_only_metadata_loader_receives_single_requested_id(cache_zip):
    loader = Mock(return_value={
        "requested_disclosure_no": REQUESTED, "ticker": "4057", "canonical_filing_id": CANONICAL,
        "internal_document_id": INTERNAL,
        "cache_path": str(cache_zip), "disclosed_at": "2026-07-13T11:30:00+09:00",
    })
    deps, _ = _deps(cache_zip, load_metadata=loader)
    run_repair(_args(cache_zip), deps)
    assert loader.call_count == 1
    assert loader.call_args.args[0].requested_id == REQUESTED


def test_no_noncanonical_writers_or_production_log(cache_zip, tmp_path):
    log = tmp_path / "realtime.log"
    deps, _ = _deps(cache_zip)
    run_repair(_args(cache_zip), deps)
    assert not log.exists()


def _make_real_4057_zip(path: Path):
    names = ["CloudCommercePlatform", "EcBusinessGrowth", "Datautillization"]
    display = ["Cloud Commerce Platform", "Ec Business Growth", "Datautillization"]
    periods = [("Prior1YearDuration", "2024-06-01", "2025-05-31", [(2617,867),(247,-13),(None,-29)]),
               ("CurrentYearDuration", "2025-06-01", "2026-05-31", [(2731,840),(129,4),(0,-59)])]
    contexts=[]; facts=[]
    for prefix,start,end,vals in periods:
        for token,label,(sales,profit) in zip(names,display,vals):
            cid=f"{prefix}_NonConsolidatedMember_tse-anedjpfr-40570{token}ReportableSegmentsMember"
            contexts.append(f'<xbrli:context id="{cid}"><xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>')
            if sales is not None: facts.append(f'<ix:nonfraction name="jppfs_cor:netsales" contextref="{cid}" unitref="JPY" scale="6">{sales}</ix:nonfraction>')
            sign=' sign="-"' if profit < 0 else ''
            facts.append(f'<ix:nonfraction name="jppfs_cor:operatingincome" contextref="{cid}" unitref="JPY" scale="6"{sign}>{abs(profit)}</ix:nonfraction>')
    summary_html=f'<html><body><ix:nonNumeric name="jpcrp_cor:DocumentTitle">2026年５月期 決算短信〔日本基準〕（非連結）</ix:nonNumeric><xbrli:identifier scheme="sicc">40570</xbrli:identifier><xbrli:endDate>2026-05-31</xbrli:endDate><xbrli:endDate>2027-05-31</xbrli:endDate><ix:nonFraction name="tse-ed-t:QuarterlyPeriod">4</ix:nonFraction><span>AnnualMember</span></body></html>'
    segment_html=f'<html><body>{"".join(contexts)}{"".join(facts)}</body></html>'
    with zipfile.ZipFile(path,'w') as zf:
        zf.writestr(f'XBRLData/Summary/tse-anedjpsm-40570-{INTERNAL}.xsd',b'')
        zf.writestr(f'XBRLData/Summary/tse-anedjpsm-40570-{INTERNAL}-ixbrl.htm',summary_html)
        zf.writestr('XBRLData/Attachment/0105010-ansg02-tse-anedjpfr-40570-2026-05-31-01-2026-07-13-ixbrl.htm',segment_html)


def test_real_resolver_stale_sidecar_integration(tmp_path):
    from src.events.earnings_production_pipeline import _extract_and_filter_segments_detailed
    from src.segment.segment_zip_resolver import resolve_xbrl_zip
    from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed
    from src.segment.zip_identity_verifier import verify_zip_identity
    cache = tmp_path / REQUESTED / 'xbrl.zip'; cache.parent.mkdir(); _make_real_4057_zip(cache)
    sha=hashlib.sha256(cache.read_bytes()).hexdigest()
    sidecar={"schema_version":"1","source":"jquants","requested_disclosure_no":REQUESTED,"requested_file_type":"x","internal_document_id":INTERNAL,"zip_sha256":sha,"downloaded_size":cache.stat().st_size,"ticker":"4057","period":"2027-05-31","quarter":"FY","document_type":"attachment_xbrl","fetched_at":"2026-07-13T00:00:00+00:00","resolved_by_function":"get_file_url"}
    Path(str(cache)+'.provenance.json').write_text(json.dumps(sidecar),encoding='utf-8')
    repo=Repo()
    deps, _ = _deps(cache, repo, resolve=lambda **kw: resolve_xbrl_zip(**kw), verify=verify_zip_identity,
                    extract_detailed=extract_segments_from_xbrl_zip_detailed,
                    filter_detailed=_extract_and_filter_segments_detailed)
    with patch('src.segment.segment_zip_resolver.get_file_url') as network:
        result=run_repair(_args(cache),deps)
    assert result['identity_verdict']=='official_linked_xbrl_match'
    assert result['extractor_total_rows']==6 and result['current_target_rows']==3
    assert len(result['eav_rows'])==6 and repo.writer_calls==0
    assert json.loads(Path(str(cache)+'.provenance.json').read_text())['period']=='2027-05-31'
    network.assert_not_called()


def test_cli_valid_arguments_call_run_repair_once(capsys):
    argv = _cli_argv(
        **{
            "expected-canonical-filing-id": CANONICAL.upper(),
            "expected-zip-sha256": "A" * 64,
        }
    )
    with patch("tools.repair_single_segment_filing.run_repair", return_value={"final_judgment": "PASS"}) as repair:
        assert main(argv) == 0
    parsed = repair.call_args.args[0]
    assert parsed.expected_canonical_filing_id == CANONICAL
    assert parsed.expected_zip_sha256 == "a" * 64
    assert json.loads(capsys.readouterr().out)["final_judgment"] == "PASS"
    repair.assert_called_once()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested-id", "2026071359178"),
        ("requested-id", "2026071359178A"),
        ("expected-internal-id", "202607133405700"),
        ("expected-internal-id", "20260713-40570"),
        ("expected-ticker", "405"),
        ("expected-ticker", "a057"),
        ("expected-ticker", "４０５７"),
        ("expected-canonical-filing-id", "a" * 63),
        ("expected-canonical-filing-id", "g" * 64),
        ("expected-zip-sha256", "a" * 65),
        ("expected-zip-sha256", "z" * 64),
        ("expected-period", "2026/05/31"),
        ("expected-period", "2026-5-31"),
        ("expected-period", "2026-02-30"),
        ("expected-quarter", "4Q"),
        ("expected-quarter", "fy"),
    ],
)
def test_cli_invalid_argument_stops_before_run_repair(field, value, capsys):
    with (
        patch("tools.repair_single_segment_filing.run_repair") as repair,
        patch("tools.repair_single_segment_filing.default_deps") as dependencies,
    ):
        assert main(_cli_argv(**{field: value})) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["final_judgment"] == "STOP_SINGLE_SEGMENT_REPAIR_INVALID_ARGUMENT"
    assert f"field={field}" in payload["detail"]
    repair.assert_not_called()
    dependencies.assert_not_called()


def test_cli_missing_required_argument_has_final_judgment(capsys):
    argv = _cli_argv()
    position = argv.index("--expected-quarter")
    del argv[position:position + 2]
    with (
        patch("tools.repair_single_segment_filing.run_repair") as repair,
        patch("tools.repair_single_segment_filing.default_deps") as dependencies,
    ):
        assert main(argv) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["final_judgment"] == "STOP_SINGLE_SEGMENT_REPAIR_INVALID_ARGUMENT"
    assert "field=expected-quarter" in payload["detail"]
    repair.assert_not_called()
    dependencies.assert_not_called()


def test_cli_unknown_argument_has_final_judgment(capsys):
    with (
        patch("tools.repair_single_segment_filing.run_repair") as repair,
        patch("tools.repair_single_segment_filing.default_deps") as dependencies,
    ):
        assert main([*_cli_argv(), "--unknown-option", "value"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["final_judgment"] == "STOP_SINGLE_SEGMENT_REPAIR_INVALID_ARGUMENT"
    assert "field=unknown-option" in payload["detail"]
    repair.assert_not_called()
    dependencies.assert_not_called()


def test_cli_help_exits_zero_without_run_repair(capsys):
    with (
        patch("tools.repair_single_segment_filing.run_repair") as repair,
        patch("tools.repair_single_segment_filing.default_deps") as dependencies,
        pytest.raises(SystemExit) as exc_info,
    ):
        main(["--help"])
    assert exc_info.value.code == 0
    assert "--requested-id" in capsys.readouterr().out
    repair.assert_not_called()
    dependencies.assert_not_called()
