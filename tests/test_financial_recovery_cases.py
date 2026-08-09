from __future__ import annotations

from unittest.mock import MagicMock, patch

from lib.pipeline.financial_reconciliation import (
    RECOVERED_BY_EARNINGS_V2,
    SUPPLEMENTAL_OR_NONFINANCIAL,
    UNRESOLVED_FINANCIAL,
    reconcile_financial_results,
)
from lib.pipeline.financial_recovery_enqueue import enqueue_unresolved_financials
from src.models import DisclosureItem, DisclosureType, ExtractedFinancials
from tools import financial_recovery_retry as retry


def _item(title: str = "2026年12月期 第2四半期決算短信") -> DisclosureItem:
    return DisclosureItem(
        disclosure_id="a" * 64,
        ticker="222A",
        company_name="テスト会社",
        title=title,
        doc_url="https://www.release.tdnet.info/inbs/140120260807514411.pdf",
        published_at="2026-08-07T15:30:00+09:00",
        xbrl_url=None,
        disclosure_type=DisclosureType.FINANCIAL_STATEMENT,
        source_doc_id="20260807514411",
    )


def _reconcile(item: DisclosureItem, summaries: dict, canonical: dict) -> dict:
    result = {
        "status": "error",
        "detail": "legacy parser failed",
        "disclosure_id": item.disclosure_id,
    }
    outcome = reconcile_financial_results(
        [result],
        [item],
        summaries_by_url=summaries,
        canonical_by_filing_id=canonical,
    )
    return {"result": result, "outcome": outcome}


def test_case_a_v2_recovered_is_nonfatal_and_queue_zero():
    item = _item()
    checked = _reconcile(
        item,
        {
            item.doc_url: {
                "ticker": item.ticker,
                "quarter": "2Q",
                "sales_value": 362_000_000,
                "op_value": 16_000_000,
            }
        },
        {
            item.disclosure_id: [
                {
                    "ticker": item.ticker,
                    "period": "2026-12-31",
                    "quarter": "2Q",
                    "metric": "sales",
                },
                {
                    "ticker": item.ticker,
                    "period": "2026-12-31",
                    "quarter": "2Q",
                    "metric": "operating_profit",
                },
            ]
        },
    )
    assert checked["result"]["financial_final_status"] == RECOVERED_BY_EARNINGS_V2
    assert checked["outcome"]["unresolved_financial"] == 0
    with patch("lib.pipeline.financial_recovery_enqueue.enqueue_job") as enqueue:
        queued = enqueue_unresolved_financials(
            {"summary": {"financial_reconciliation": checked["outcome"]}}
        )
    enqueue.assert_not_called()
    assert queued["enqueued"] == 0


def test_case_b_supplemental_is_nonfatal_and_queue_zero():
    checked = _reconcile(_item("2026年12月期 決算説明会資料"), {}, {})
    assert checked["result"]["financial_final_status"] == SUPPLEMENTAL_OR_NONFINANCIAL
    assert checked["outcome"]["unresolved_financial"] == 0
    with patch("lib.pipeline.financial_recovery_enqueue.enqueue_job") as enqueue:
        queued = enqueue_unresolved_financials(
            {"summary": {"financial_reconciliation": checked["outcome"]}}
        )
    enqueue.assert_not_called()
    assert queued["enqueued"] == 0


def test_case_c_formal_unresolved_is_fatal_and_queue_one():
    checked = _reconcile(_item(), {}, {})
    assert checked["result"]["financial_final_status"] == UNRESOLVED_FINANCIAL
    assert checked["outcome"]["unresolved_financial"] == 1
    with patch(
        "lib.pipeline.financial_recovery_enqueue.enqueue_job",
        return_value={"ok": True},
    ) as enqueue:
        queued = enqueue_unresolved_financials(
            {"summary": {"financial_reconciliation": checked["outcome"]}},
            canonical_exists_fn=lambda _row: False,
            terminal_exists_fn=lambda _row: False,
        )
    enqueue.assert_called_once()
    assert queued["enqueued"] == 1


def test_case_e_222a_official_pdf_writes_viewer_canonical_and_notifies_zero(tmp_path):
    payload = {
        "disclosure_id": "a" * 64,
        "code": "222A",
        "company_name": "テスト会社",
        "title": "2026年12月期 中間決算短信",
        "doc_url": "https://www.release.tdnet.info/inbs/140120260807514411.pdf",
        "published_at": "2026-08-07T15:30:00+09:00",
        "period": "2026-12-31",
        "quarter": "2Q",
    }
    financials = ExtractedFinancials(
        sales=362,
        operating_profit=16,
        ordinary_profit=17,
        net_income=10,
        fiscal_year="2026-12-31",
        quarter="2Q",
        source_unit="百万円",
        field_sources={
            "sales": "pdf_table",
            "operating_profit": "pdf_table",
            "ordinary_profit": "pdf_table",
            "net_income": "pdf_table",
        },
    )
    earnings = MagicMock()
    with patch.object(retry, "canonical_is_resolved", side_effect=[False, False, True]), \
         patch.object(retry, "_canonical_rows", return_value=[]), \
         patch.object(retry, "get_supabase_write_config", return_value={"key": "test"}), \
         patch.object(retry, "download_document", return_value=str(tmp_path / "222A.pdf")), \
         patch.object(retry, "extract_financials", return_value=(financials, "")), \
         patch.object(retry, "write_financials_canonical", return_value={"written": 4, "errors": 0}) as write, \
         patch("src.events.earnings_production_pipeline.run_earnings_production", earnings), \
         patch("src.events.earnings_production_pipeline.send_earnings_discord") as notify:
        outcome = retry.recover_one(
            payload, decision_db_path=str(tmp_path / "decision.db")
        )

    assert outcome == {"resolved": True, "route": "official_pdf"}
    assert write.call_args.kwargs["metrics_dict"] == {
        "sales": 362,
        "operating_profit": 16,
        "ordinary_profit": 17,
        "net_income": 10,
    }
    assert write.call_args.kwargs["source"] == "official_pdf"
    assert write.call_args.kwargs["filing_id"] == payload["disclosure_id"]
    assert earnings.call_args.kwargs["notify_enabled"] is False
    assert earnings.call_args.kwargs["webhook_url"] == ""
    notify.assert_not_called()


def test_recovery_summary_contract_always_reports_zero_notifications():
    with patch.object(retry, "load_env"), \
         patch.object(retry, "get_supabase_write_config", return_value={"key": "test"}), \
         patch.object(retry, "take_pending_jobs", return_value=[]):
        outcome = retry.run()
    assert outcome["notifications_sent"] == 0
