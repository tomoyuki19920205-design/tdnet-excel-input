from pathlib import Path
from types import SimpleNamespace
import copy
from src.analysis.segment_detection_v4 import run_segment_detection_v4
from src.segment.pdf_segment_evidence import verified_pdf_records
from lib.backfill.segment_partial_check import allow_verified_pdf_fallback


def test_real_two_business_pdf_is_adopted_with_bound_cells():
    pdf = Path(__file__).parent / "fixtures/segment_partials/6993_correction_202312.pdf"
    result = run_segment_detection_v4(str(pdf), ticker="6993")
    assert not result.success and result.quarantine_reason == "low_segment_count"
    rows = verified_pdf_records(result, "2023-03-31", "FY", current_only=True)
    assert rows and all(row["period"] == "2023-03-31" for row in rows)
    assert allow_verified_pdf_fallback(result, rows)
    prior = verified_pdf_records(result, "2023-03-31", "FY")
    pawn = next(r for r in prior if r["period"] == "2022-03-31" and "古物" in r["segment_name"])
    assert pawn["segment_sales"] == 16918.411 and pawn["segment_profit"] == 166.872
    other = next(r for r in prior if r["period"] == "2022-03-31" and "その他" in r["segment_name"])
    assert other["segment_sales"] is None and other["segment_profit"] == -42.707
    assert not allow_verified_pdf_fallback(result, [])
    broken = copy.deepcopy(rows);broken[0]["segment_metadata"].pop("original_metric_cells")
    assert not allow_verified_pdf_fallback(result, broken)
    broken = copy.deepcopy(rows);broken[0]["segment_metadata"]["normalization_count"] = 2
    assert not allow_verified_pdf_fallback(result, broken)
    assert not allow_verified_pdf_fallback(SimpleNamespace(success=False, quarantine_reason="validation_failed"), rows)


def test_normal_ingest_saves_verified_two_business_pdf(monkeypatch,tmp_path):
    from unittest.mock import Mock
    from tools import tdnet_ingest
    from src.models import DisclosureType
    pdf = Path(__file__).parent / "fixtures/segment_partials/6993_correction_202312.pdf"
    item = SimpleNamespace(disclosure_id="20231214503051",ticker="6993",title="訂正 2023年3月期 決算短信",disclosure_type=None,doc_url="https://www.release.tdnet.info/inbs/140120231214503051.pdf",xbrl_url=None)
    monkeypatch.setattr(tdnet_ingest,"classify_disclosure_security",lambda item:SimpleNamespace(is_etf_like=False))
    financials=SimpleNamespace(fiscal_year="2023-03-31",quarter="FY",source_unit="百万円",field_sources={})
    state=Mock();state.is_processed.return_value=False
    database=Mock();database.upsert_quarterly_result.return_value="inserted";database.upsert_segment.return_value="inserted"
    result=tdnet_ingest._process_single(item,SimpleNamespace(state_db_path=str(tmp_path/"state.db"),excel_unit="百万円"),state,database,"fixture",pre_fetched={"doc_path":str(pdf),"xbrl_path":None,"financials":financials,"extract_error":None})
    assert result["status"]=="inserted" and result["seg_metrics"]["v4_success"]
    saved=[call.kwargs for call in database.upsert_segment.call_args_list]
    assert len(saved)==3 and all(row["fiscal_year_end"]=="2023-03-31" for row in saved)
    assert all(row["segment_metadata"]["normalization_count"]==1 for row in saved)
    database.quarantine_record.assert_not_called()
