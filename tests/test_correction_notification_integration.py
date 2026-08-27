"""Isolated output integration for correction earnings disclosures.

The production pipeline must keep corrected financial facts while suppressing
only the Company Viewer notification row.  All external persistence is replaced
with in-memory fakes in this test.
"""
from types import SimpleNamespace

from lib.pipeline import canonical_writer
from src.events import tdnet_event_store
from src.events.earnings_production_pipeline import (
    _save_earnings_to_tdnet_events,
    _sync_canonical_financials,
)
from src.events.summary_financials import EarningsSummaryData


def test_correction_updates_canonical_without_inserting_tdnet_event(monkeypatch) -> None:
    tdnet_table_calls: list[str] = []
    canonical_writes: list[dict] = []

    class IsolatedSupabase:
        def table(self, name: str):
            tdnet_table_calls.append(name)
            raise AssertionError(f"unexpected Supabase table access: {name}")

    def capture_canonical_write(**kwargs):
        canonical_writes.append(kwargs)
        return {"written": len(kwargs["metrics_dict"]), "errors": 0}

    monkeypatch.setattr(tdnet_event_store, "_get_supabase", lambda: IsolatedSupabase())
    monkeypatch.setattr(canonical_writer, "write_financials_canonical", capture_canonical_write)
    monkeypatch.delenv("EARNINGS_CANONICAL_WRITE_REPLACE_APPLY", raising=False)
    monkeypatch.delenv("EARNINGS_CANONICAL_WRITE_REPLACE_DRYRUN", raising=False)

    source_doc_id = "de740f4923a289b826672d0a5aed94162505bca096f405d7d75f340944c707d8"
    doc = SimpleNamespace(
        ticker="3538",
        company_name="ウイルプラスＨＤ",
        title='(訂正・数値データ訂正)「2026年６月期決算短信〔日本基準〕(連結)」の一部訂正について',
        disclosure_id=source_doc_id,
        doc_id=source_doc_id,
        disclosure_datetime="2026-08-27T18:00:00+09:00",
        published_at="2026-08-27T18:00:00+09:00",
        doc_url="https://www.release.tdnet.info/inbs/140120260827527408.pdf",
    )
    earnings = EarningsSummaryData(
        ticker="3538",
        period="2026-06-30",
        quarter="FY",
        sales_current=96_839_000_000,
        sales_prior=88_614_000_000,
        gross_profit_current=13_135_727_000,
        selling_general_and_administrative_expenses_current=11_708_889_000,
        op_current=1_426_000_000,
        op_prior=1_849_000_000,
        source="xbrl",
    )

    event_result = _save_earnings_to_tdnet_events(
        doc=doc,
        earnings=earnings,
        company_name=doc.company_name,
        full_message="isolated correction integration",
        guidance=None,
        fiscal_year="2026",
        quarter="FY",
        xbrl_path="isolated/081220260827527408.zip",
        dry_run=False,
    )
    _sync_canonical_financials(
        ticker="3538",
        period="2026-06-30",
        quarter="FY",
        sales_value=earnings.sales_current,
        op_value=earnings.op_current,
        gross_value=earnings.gross_profit_current,
        sga_value=earnings.selling_general_and_administrative_expenses_current,
        guidance={},
        filing_id=source_doc_id,
        dry_run=False,
        route="isolated_integration_test",
    )

    assert event_result["action"] == "dedup_skipped"
    assert event_result["reason"] == "correction_disclosure"
    assert event_result["notification_suppressed"] is True
    assert tdnet_table_calls == []

    assert len(canonical_writes) == 1
    write = canonical_writes[0]
    assert write["ticker"] == "3538"
    assert write["period"] == "2026-06-30"
    assert write["quarter"] == "FY"
    assert write["filing_id"] == source_doc_id
    assert write["source"] == "jquants_earnings_summary"
    assert write["metrics_dict"] == {
        "sales": 96_839.0,
        "operating_profit": 1_426.0,
        "gross_profit": 13_135.727,
        "selling_general_and_administrative_expenses": 11_708.889,
    }
