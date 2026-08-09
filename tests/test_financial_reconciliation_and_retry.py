from __future__ import annotations

from unittest.mock import MagicMock, patch

from lib.pipeline.financial_reconciliation import (
    RECOVERED_BY_EARNINGS_V2,
    SUPPLEMENTAL_OR_NONFINANCIAL,
    UNRESOLVED_FINANCIAL,
    reconcile_financial_results,
)
from lib.pipeline.financial_recovery_enqueue import enqueue_unresolved_financials
from src.db import StateDB
from src.models import DisclosureItem, DisclosureType, Status
from src.pdf_financial_table import extract_actual_financial_table
from tools import financial_recovery_retry as retry
from tools import pipeline_run


def _item(index: int, title: str = "2026年12月期 第2四半期決算短信"):
    disclosure_id = f"{index:064x}"
    return DisclosureItem(
        disclosure_id=disclosure_id,
        ticker=f"{index % 9000 + 1000:04d}",
        company_name=f"会社{index}",
        title=title,
        doc_url=f"https://www.release.tdnet.info/inbs/140120260807{index:06d}.pdf",
        published_at="2026-08-07T15:30:00+09:00",
        xbrl_url=f"https://www.release.tdnet.info/inbs/140120260807{index:06d}.zip",
        disclosure_type=DisclosureType.FINANCIAL_STATEMENT,
        source_doc_id=f"20260807{index:06d}",
    )


def test_confirmed_137_aggregate_reconciles_to_128_5_4():
    items = [_item(i) for i in range(137)]
    results = [
        {"status": "error", "detail": "legacy parse failed", "code": item.ticker,
         "disclosure_id": item.disclosure_id}
        for item in items
    ]
    summaries = {}
    canonical = {}
    for item in items[:128]:
        summaries[item.doc_url] = {
            "ticker": item.ticker, "quarter": "2Q",
            "sales_value": 100_000_000, "op_value": 10_000_000,
        }
        canonical[item.disclosure_id] = [
            {"filing_id": item.disclosure_id, "ticker": item.ticker,
             "period": "2026-12-31", "quarter": "2Q", "metric": "sales"},
            {"filing_id": item.disclosure_id, "ticker": item.ticker,
             "period": "2026-12-31", "quarter": "2Q", "metric": "operating_profit"},
        ]
    for offset, item in enumerate(items[128:133]):
        item.title = f"2026年12月期 決算説明会資料 {offset}"

    outcome = reconcile_financial_results(
        results, items,
        summaries_by_url=summaries,
        canonical_by_filing_id=canonical,
    )

    assert outcome["old_parser_errors"] == 137
    assert outcome["recovered_by_earnings_v2"] == 128
    assert outcome["supplemental_or_nonfinancial"] == 5
    assert outcome["unresolved_financial"] == 4
    assert len(outcome["unresolved_items"]) == 4


def test_same_ticker_different_filing_lineage_is_not_recovered():
    item = _item(1)
    result = {"status": "error", "disclosure_id": item.disclosure_id}
    outcome = reconcile_financial_results(
        [result], [item],
        summaries_by_url={item.doc_url: {"quarter": "2Q", "sales_value": 1, "op_value": 1}},
        canonical_by_filing_id={"f" * 64: [
            {"ticker": item.ticker, "period": "2026-12-31", "quarter": "2Q", "metric": "sales"}
        ]},
    )
    assert outcome["unresolved_financial"] == 1
    assert result["financial_final_status"] == UNRESOLVED_FINANCIAL


def test_recovered_old_parser_errors_do_not_fail_ingest_step():
    mocked_result = {
        "total": 137,
        "failed": 137,
        "summary": {
            "success": 5, "errors": 137, "fatal_errors": 0,
            "unresolved_financial_errors": 0, "skipped": 155,
        },
    }
    with patch("tools.filings_ingest.run", return_value=mocked_result):
        step = pipeline_run._run_ingest(dry_run=True)
    assert step.status == "success"
    assert step.detail["old_parser_errors"] == 137
    assert step.detail["failed"] == 0


def test_unresolved_financial_errors_remain_fatal():
    mocked_result = {
        "total": 137,
        "summary": {
            "success": 5, "errors": 137, "fatal_errors": 4,
            "unresolved_financial_errors": 4, "skipped": 155,
        },
    }
    with patch("tools.filings_ingest.run", return_value=mocked_result):
        step = pipeline_run._run_ingest(dry_run=True)
    assert step.status == "failed"
    assert step.detail["failed"] == 4


def test_only_unresolved_rows_enter_idempotent_queue():
    rows = [
        {"disclosure_id": "a" * 64, "final_status": UNRESOLVED_FINANCIAL},
        {"disclosure_id": "b" * 64, "final_status": UNRESOLVED_FINANCIAL},
    ]
    ingest = {"summary": {"financial_reconciliation": {"unresolved_items": rows}}}
    with patch(
        "lib.pipeline.financial_recovery_enqueue.enqueue_job",
        side_effect=[{"ok": True}, None],
    ) as enqueue:
        outcome = enqueue_unresolved_financials(
            ingest,
            canonical_exists_fn=lambda _row: False,
            terminal_exists_fn=lambda _row: False,
        )
    assert enqueue.call_count == 2
    assert outcome == {
        "unresolved": 2,
        "enqueued": 1,
        "duplicates": 1,
        "errors": 0,
        "already_resolved": 0,
        "terminal": 0,
    }
    assert all(call.kwargs["job_type"] == retry.JOB_TYPE for call in enqueue.call_args_list)


def test_unresolved_row_with_existing_canonical_is_not_enqueued():
    row = {
        "disclosure_id": "a" * 64,
        "code": "4263",
        "period": "2026-03-31",
        "quarter": "1Q",
    }
    ingest = {"summary": {"financial_reconciliation": {"unresolved_items": [row]}}}
    with patch("lib.pipeline.financial_recovery_enqueue.enqueue_job") as enqueue:
        outcome = enqueue_unresolved_financials(
            ingest, canonical_exists_fn=lambda _row: True
        )
    enqueue.assert_not_called()
    assert outcome["already_resolved"] == 1
    assert outcome["enqueued"] == 0


def test_canonical_precheck_error_fails_open_to_idempotent_queue():
    row = {"disclosure_id": "a" * 64, "code": "222A"}
    ingest = {"summary": {"financial_reconciliation": {"unresolved_items": [row]}}}
    with patch(
        "lib.pipeline.financial_recovery_enqueue.enqueue_job",
        return_value={"ok": True},
    ) as enqueue:
        outcome = enqueue_unresolved_financials(
            ingest,
            canonical_exists_fn=MagicMock(side_effect=TimeoutError("read timeout")),
            terminal_exists_fn=lambda _row: False,
        )
    enqueue.assert_called_once()
    assert outcome["enqueued"] == 1
    assert outcome["errors"] == 0


def test_terminal_retry_limit_prevents_future_reenqueue():
    row = {"disclosure_id": "a" * 64, "code": "222A"}
    ingest = {"summary": {"financial_reconciliation": {"unresolved_items": [row]}}}
    with patch("lib.pipeline.financial_recovery_enqueue.enqueue_job") as enqueue:
        outcome = enqueue_unresolved_financials(
            ingest,
            canonical_exists_fn=lambda _row: False,
            terminal_exists_fn=lambda _row: True,
        )
    enqueue.assert_not_called()
    assert outcome["terminal"] == 1
    assert outcome["enqueued"] == 0


def test_retry_is_pending_then_explicitly_failed_at_limit():
    config = {"rest_url": "x", "headers": {}, "key": "k"}
    base_job = {"id": 7, "attempts": 0, "payload_json": {"disclosure_id": "a" * 64}}
    with patch.object(retry, "get_supabase_write_config", return_value=config), \
         patch.object(retry, "load_env"), \
         patch.object(retry, "take_pending_jobs", return_value=[base_job]), \
         patch.object(retry, "recover_one", return_value={"resolved": False, "route": "all_routes_failed"}), \
         patch.object(retry, "supabase_update") as update, \
         patch.object(retry, "complete_job") as complete:
        first = retry.run()
    assert first["retried"] == 1
    assert update.call_args.args[1]["status"] == "pending"
    complete.assert_not_called()

    terminal_job = {**base_job, "attempts": 2}
    with patch.object(retry, "get_supabase_write_config", return_value=config), \
         patch.object(retry, "load_env"), \
         patch.object(retry, "take_pending_jobs", return_value=[terminal_job]), \
         patch.object(retry, "recover_one", return_value={"resolved": False, "route": "all_routes_failed"}), \
         patch.object(retry, "supabase_update"), \
         patch.object(retry, "complete_job") as complete:
        final = retry.run()
    assert final["failed"] == 1
    assert complete.call_args.kwargs["status"] == "failed"
    assert "retry limit 3" in complete.call_args.kwargs["error_message"]


def test_later_jquants_canonical_is_resolved_without_write():
    payload = {"code": "4263", "period": "2026-03-31", "quarter": "1Q"}
    rows = [
        {"metric": "sales", "source": "jquants"},
        {"metric": "net_income", "source": "jquants"},
    ]
    with patch.object(retry, "_canonical_rows", return_value=rows), \
         patch.object(retry, "download_document") as download, \
         patch.object(retry, "write_financials_canonical") as write:
        outcome = retry.recover_one(payload)
    assert outcome == {"resolved": True, "route": "existing_canonical"}
    download.assert_not_called()
    write.assert_not_called()


def test_222a_compact_official_pdf_table_extracts_four_pl_values():
    table = [
        ["", "売上高", None, "営業利益", None, "経常利益", None, "中間純利益", None],
        ["2026年12月期中間期\n2025年12月期中間期",
         "百万円\n362\n348", "％\n4.1\n△13.5",
         "百万円\n16\n△12", "％\n―\n―",
         "百万円\n17\n△5", "％\n―\n―",
         "百万円\n10\n△16", "％\n―\n―"],
    ]
    assert extract_actual_financial_table([table]) == {
        "sales": 362,
        "operating_profit": 16,
        "ordinary_profit": 17,
        "net_income": 10,
    }


def test_recovery_calls_earnings_v2_with_notifications_disabled(tmp_path):
    payload = {
        "disclosure_id": "a" * 64,
        "code": "222A",
        "company_name": "テスト",
        "title": "2026年12月期 中間決算短信",
        "doc_url": "https://www.release.tdnet.info/inbs/140120260807514411.pdf",
        "published_at": "2026-08-07T15:30:00+09:00",
        "period": "2026-12-31", "quarter": "2Q",
    }
    earnings = MagicMock()
    with patch.object(retry, "canonical_is_resolved", side_effect=[False, True]), \
         patch.object(retry, "get_supabase_write_config", return_value={"key": "x"}), \
         patch.object(retry, "download_document", return_value=None), \
         patch("src.events.earnings_production_pipeline.run_earnings_production", earnings):
        outcome = retry.recover_one(payload, decision_db_path=str(tmp_path / "d.db"))
    assert outcome["route"] == "earnings_v2"
    assert earnings.call_args.kwargs["notify_enabled"] is False
    assert earnings.call_args.kwargs["webhook_url"] == ""


def test_reconciliation_history_preserves_legacy_failure(tmp_path):
    db = StateDB(str(tmp_path / "state.db"))
    disclosure_id = "a" * 64
    try:
        db.record(
            disclosure_id=disclosure_id, code="222A", year="", quarter="",
            status=Status.PARSE_FAILED, error_detail="legacy parser",
        )
        db.record_financial_reconciliation(
            run_id="r1", disclosure_id=disclosure_id, code="222A",
            old_parser_status="error", final_status=RECOVERED_BY_EARNINGS_V2,
            reason="exact lineage",
        )
        assert db.get_log(disclosure_id)["status"] == Status.PARSE_FAILED
        row = db._conn.execute(
            "SELECT final_status FROM financial_reconciliation_history WHERE disclosure_id=?",
            (disclosure_id,),
        ).fetchone()
        assert row[0] == RECOVERED_BY_EARNINGS_V2
    finally:
        db.close()


def test_supplemental_rows_never_become_unresolved():
    item = _item(6098, "2026年3月期 決算補足資料")
    result = {"status": "error", "disclosure_id": item.disclosure_id}
    outcome = reconcile_financial_results(
        [result], [item], summaries_by_url={}, canonical_by_filing_id={}
    )
    assert result["financial_final_status"] == SUPPLEMENTAL_OR_NONFINANCIAL
    assert outcome["unresolved_items"] == []
