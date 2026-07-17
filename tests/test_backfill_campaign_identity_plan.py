from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from lib.backfill.campaign_identity_plan import (
    CLASSIFICATIONS,
    IdentityPlanStop,
    build_plan,
    classify_row,
    inspect_zip,
    load_campaign,
    load_jquants,
    load_legacy_state,
    sha256_file,
    write_plan,
)
from lib.backfill.campaign_state import (
    create_campaign,
    create_campaign_filing,
    initialize_schema,
)
from tools.backfill_campaign_identity_plan import build_parser


REQUESTED = "20240101999999"
INTERNAL = "20240101123456"


def _row(**changes):
    row = {
        "campaign_id": "campaign",
        "manifest_row_id": "0000000001",
        "requested_disclosure_no": REQUESTED,
        "company_code": "7203",
        "normalized_company_code": "7203",
        "normalized_xbrl_url": f"https://www.release.tdnet.info/inbs/0812{REQUESTED}.zip",
        "expected_period": "2024-03-31",
        "document_type": "financial_statement",
    }
    row.update(changes)
    return row


def _jq(**changes):
    row = {
        "requested_disclosure_no": REQUESTED,
        "company_code": "72030",
        "normalized_company_code": "7203",
        "disclosed_date": "2024-01-01",
        "expected_period": "2024-03-31",
        "expected_quarter": "FY",
        "period_type": "FY",
        "document_type": "FYFinancialStatements_Consolidated_JP",
        "disclosed_time": "15:00:00",
        "match_status": "exact",
    }
    row.update(changes)
    return row


def _state(filing_id="legacy", **changes):
    row = {
        "filing_id": filing_id,
        "ticker": "7203",
        "period": "2024-03-31",
        "quarter": "FY",
        "xbrl_url": f"https://www.release.tdnet.info/inbs/0812{REQUESTED}.zip",
    }
    row.update(changes)
    return row


def _zip(path: Path, *, ticker="7203", period="2024-03-31", quarter="FY", marker=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    entry_ticker = ticker + "0" if len(ticker) == 4 else ticker
    number = {"1Q": "1", "2Q": "2", "3Q": "3", "FY": "4"}[quarter]
    name = f"XBRLData/Summary/tse-qcedjpsm-{entry_ticker}-{INTERNAL}{marker}.htm"
    content = (
        f'<xbrli:identifier scheme="http://example/sicc">{entry_ticker}</xbrli:identifier>'
        f"<xbrli:endDate>{period}</xbrli:endDate>"
        f'<tse-ed-t:QuarterlyPeriod name="tse-ed-t:QuarterlyPeriod">{number}</tse-ed-t:QuarterlyPeriod>'
        "<tse-ed-t:AnnualMember/>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, content)
    return path


def _sidecar(zip_path: Path, *, requested=REQUESTED, ticker="7203", period="2024-03-31", quarter="FY"):
    meta = inspect_zip(zip_path, period, quarter)
    payload = {
        "schema_version": "1",
        "source": "jquants",
        "requested_disclosure_no": requested,
        "requested_file_type": "x",
        "internal_document_id": meta["internal_document_id"],
        "zip_sha256": sha256_file(zip_path),
        "downloaded_size": zip_path.stat().st_size,
        "ticker": ticker,
        "period": period,
        "quarter": quarter,
        "document_type": "attachment_xbrl",
        "fetched_at": "2024-01-01T00:00:00Z",
        "resolved_by_function": "test",
    }
    sidecar = Path(str(zip_path) + ".provenance.json")
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    return sidecar


def _class(cache: Path, *, row=None, jq=None, state=None):
    return classify_row(row or _row(), jq if jq is not None else [_jq()], state or [], cache)


def test_target_zip_and_sidecar_is_ready(tmp_path):
    target = _zip(tmp_path / REQUESTED / "xbrl.zip")
    _sidecar(target)
    assert _class(tmp_path)["classification"] == "READY_IDENTITY_VERIFIED"


def test_target_zip_without_sidecar_needs_sidecar(tmp_path):
    _zip(tmp_path / REQUESTED / "xbrl.zip")
    assert _class(tmp_path)["classification"] == "TARGET_ZIP_NEEDS_SIDECAR"


def test_unique_legacy_zip_is_copy_candidate(tmp_path):
    _zip(tmp_path / "legacy" / "xbrl.zip")
    assert _class(tmp_path, state=[_state()])["classification"] == "LEGACY_CACHE_COPY_CANDIDATE"


def test_jquants_only_is_metadata_resolved(tmp_path):
    assert _class(tmp_path)["classification"] == "METADATA_RESOLVED_CACHE_MISSING"


def test_missing_quarter_without_zip_is_incomplete(tmp_path):
    incomplete = _jq(expected_quarter="", match_status="incomplete")
    result = _class(tmp_path, jq=[incomplete])
    assert result["classification"] == "METADATA_INCOMPLETE_CACHE_MISSING"
    assert result["expected_identity"]["quarter_status"] == "UNRESOLVED"


def test_zip_period_mismatch_is_rejected(tmp_path):
    _zip(tmp_path / REQUESTED / "xbrl.zip", period="2023-03-31")
    assert _class(tmp_path)["classification"] == "CACHE_IDENTITY_MISMATCH"


def test_target_and_legacy_conflict(tmp_path):
    _zip(tmp_path / REQUESTED / "xbrl.zip")
    _zip(tmp_path / "legacy" / "xbrl.zip", marker="-different")
    assert _class(tmp_path, state=[_state()])["classification"] == "TARGET_CACHE_CONFLICT"


def test_duplicate_legacy_state_is_ambiguous(tmp_path):
    result = _class(tmp_path, state=[_state("one"), _state("two")])
    assert result["classification"] == "LEGACY_STATE_AMBIGUOUS"


def test_distinct_jquants_metadata_is_ambiguous(tmp_path):
    result = _class(tmp_path, jq=[_jq(), _jq(expected_quarter="3Q")])
    assert result["classification"] == "JQUANTS_METADATA_AMBIGUOUS"


def test_invalid_url_is_rejected(tmp_path):
    result = _class(tmp_path, row=_row(normalized_xbrl_url="http://example.invalid/a.zip"))
    assert result["classification"] == "INVALID_OR_UNSUPPORTED_URL"


def test_formal_non_xbrl_document_is_not_applicable(tmp_path):
    result = _class(tmp_path, jq=[_jq(document_type="DividendForecastRevision")])
    assert result["classification"] == "NOT_APPLICABLE"


def test_corrupt_zip_is_fail_closed(tmp_path):
    target = tmp_path / REQUESTED / "xbrl.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"not-a-zip")
    result = _class(tmp_path)
    assert result["classification"] == "CACHE_IDENTITY_MISMATCH"
    assert "ZIP_CORRUPT" in result["flags"]


def test_invalid_sidecar_is_conflict(tmp_path):
    target = _zip(tmp_path / REQUESTED / "xbrl.zip")
    Path(str(target) + ".provenance.json").write_text("{}", encoding="utf-8")
    result = _class(tmp_path)
    assert result["classification"] == "TARGET_CACHE_CONFLICT"
    assert "SIDECAR_INVALID" in result["flags"]


def test_alphanumeric_ticker_is_preserved(tmp_path):
    requested = "20240101888888"
    target = _zip(tmp_path / requested / "xbrl.zip", ticker="581A")
    row = _row(requested_disclosure_no=requested, company_code="581A", normalized_company_code="581A")
    jq = [_jq(requested_disclosure_no=requested, company_code="581A0", normalized_company_code="581A")]
    result = classify_row(row, jq, [], tmp_path)
    assert result["actual_identity"]["ticker"] == "581A"
    assert "ALPHANUMERIC_TICKER" in result["flags"]


def test_quarter_is_not_inferred_from_date(tmp_path):
    incomplete = _jq(expected_quarter="", period_type="", match_status="incomplete")
    result = _class(tmp_path, jq=[incomplete])
    assert result["expected_identity"]["expected_quarter"] == ""
    assert result["classification"] == "METADATA_INCOMPLETE_CACHE_MISSING"


def test_classification_never_calls_network(tmp_path, monkeypatch):
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: pytest.fail("network called"))
    assert _class(tmp_path)["classification"] == "METADATA_RESOLVED_CACHE_MISSING"


def _campaign_db(path: Path):
    conn = sqlite3.connect(path)
    initialize_schema(conn)
    create_campaign(conn, {
        "campaign_id": "campaign", "campaign_name": "name", "manifest_path": "m",
        "manifest_sha256": "a" * 64, "manifest_record_count": 1,
        "code_sha": "b" * 40, "worker_version": "v4", "status": "READY",
    })
    create_campaign_filing(conn, {
        "campaign_id": "campaign", "manifest_row_id": "0001",
        "requested_disclosure_no": REQUESTED, "company_code": "7203",
        "normalized_company_code": "7203",
        "normalized_xbrl_url": f"https://www.release.tdnet.info/inbs/0812{REQUESTED}.zip",
        "expected_period": "2024-03-31", "registration_status": "REGISTERED",
        "identity_status": "UNVERIFIED", "cache_status": "UNKNOWN",
        "extraction_status": "NOT_STARTED", "sqlite_save_status": "NOT_STARTED",
        "canonical_save_status": "NOT_STARTED", "supabase_save_status": "NOT_STARTED",
        "overall_status": "REGISTERED", "retryable": 1,
    })
    conn.commit(); conn.close()


def _jquants_db(path: Path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE jquants_financials_normalized(local_code TEXT,disclosed_date TEXT,current_fiscal_year_end_date TEXT,type_of_current_period TEXT,type_of_document TEXT,raw_json TEXT)")
    raw = json.dumps({"DiscNo": REQUESTED, "DiscTime": "15:00:00"})
    conn.execute("INSERT INTO jquants_financials_normalized VALUES(?,?,?,?,?,?)", ("72030","2024-01-01","2024-03-31","FY","FYFinancialStatements",raw))
    conn.commit(); conn.close()


def _state_db(path: Path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE filing_state(filing_id TEXT,ticker TEXT,disclosure_date TEXT,doc_type TEXT,period TEXT,quarter TEXT,source_url TEXT,xbrl_url TEXT,xbrl_path TEXT,cache_dir TEXT)")
    conn.execute("INSERT INTO filing_state VALUES(?,?,?,?,?,?,?,?,?,?)", ("legacy","7203","2024-01-01","financial","2024-03-31","FY","",f"https://www.release.tdnet.info/inbs/0812{REQUESTED}.zip","",""))
    conn.commit(); conn.close()


def test_source_databases_are_not_written(tmp_path):
    campaign, jq, state = (tmp_path / name for name in ("campaign.db", "jq.db", "state.db"))
    _campaign_db(campaign); _jquants_db(jq); _state_db(state)
    before = [sha256_file(path) for path in (campaign, jq, state)]
    load_campaign(campaign, "campaign", expected_count=1, expected_sha256=before[0])
    load_jquants(jq); load_legacy_state(state)
    after = [sha256_file(path) for path in (campaign, jq, state)]
    assert after == before
    assert not any(tmp_path.glob("*.db-wal"))


def test_build_plan_classification_total_matches(tmp_path):
    campaign, jq, state, cache = (tmp_path / name for name in ("campaign.db", "jq.db", "state.db", "cache"))
    _campaign_db(campaign); _jquants_db(jq); _state_db(state); cache.mkdir()
    rows, _schema = build_plan(campaign_db=campaign, campaign_id="campaign", jquants_db=jq, legacy_state_db=state, cache_root=cache, expected_count=1, campaign_db_sha256=sha256_file(campaign))
    assert len(rows) == 1
    assert rows[0]["classification"] in CLASSIFICATIONS


def test_output_digests_are_deterministic(tmp_path):
    rows = [_class(tmp_path / "cache")]
    execution = {"network_calls": 0, "db_writes": 0, "cache_writes": 0}
    first = write_plan(output_dir=tmp_path / "one", rows=rows, source_schema={}, execution=execution, repo_root=tmp_path / "repo")
    second = write_plan(output_dir=tmp_path / "two", rows=rows, source_schema={}, execution=execution, repo_root=tmp_path / "repo")
    assert first["digests"] == second["digests"]


def test_output_inside_repository_is_rejected(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    with pytest.raises(IdentityPlanStop, match="OUTPUT_UNSAFE"):
        write_plan(output_dir=repo / "out", rows=[], source_schema={}, execution={}, repo_root=repo)


def test_cli_has_no_write_option():
    actions = {action.dest for action in build_parser()._actions}
    assert "apply" not in actions
    assert {"campaign_db", "campaign_id", "jquants_db", "legacy_state_db", "cache_root", "output_dir"}.issubset(actions)


def test_cache_files_are_not_modified(tmp_path):
    target = _zip(tmp_path / REQUESTED / "xbrl.zip")
    before = (target.stat().st_mtime_ns, sha256_file(target))
    _class(tmp_path)
    after = (target.stat().st_mtime_ns, sha256_file(target))
    assert after == before
