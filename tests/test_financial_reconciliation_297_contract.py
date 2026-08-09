from __future__ import annotations

from lib.pipeline.financial_reconciliation import reconcile_financial_results
from src.models import DisclosureItem, DisclosureType


def _item(index: int, title: str = "2026年12月期 第2四半期決算短信"):
    return DisclosureItem(
        disclosure_id=f"{index:064x}",
        ticker=f"{index % 9000 + 1000:04d}",
        company_name=f"会社{index}",
        title=title,
        doc_url=f"https://www.release.tdnet.info/inbs/140120260807{index:06d}.pdf",
        published_at="2026-08-07T15:30:00+09:00",
        disclosure_type=DisclosureType.FINANCIAL_STATEMENT,
    )


def test_297_result_contract_separates_success_errors_and_skips():
    error_items = [_item(index) for index in range(137)]
    success_items = [_item(1000 + index) for index in range(5)]
    skipped_items = [_item(2000 + index) for index in range(155)]
    items = error_items + success_items + skipped_items
    results = [
        {"status": "error", "disclosure_id": item.disclosure_id}
        for item in error_items
    ] + [
        {"status": "inserted", "disclosure_id": item.disclosure_id}
        for item in success_items
    ] + [
        {"status": "skipped", "disclosure_id": item.disclosure_id}
        for item in skipped_items
    ]

    summaries = {}
    canonical = {}
    for item in error_items[:128]:
        summaries[item.doc_url] = {
            "ticker": item.ticker,
            "quarter": "2Q",
            "sales_value": 1,
            "op_value": 1,
        }
        canonical[item.disclosure_id] = [
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
    for item in error_items[128:133]:
        item.title = "2026年12月期 決算説明会資料"

    outcome = reconcile_financial_results(
        results,
        items,
        summaries_by_url=summaries,
        canonical_by_filing_id=canonical,
    )

    assert outcome["old_parser_success"] == 5
    assert outcome["old_parser_errors"] == 137
    assert outcome["old_parser_skipped"] == 155
    assert outcome["recovered_by_earnings_v2"] == 128
    assert outcome["supplemental_or_nonfinancial"] == 5
    assert outcome["unresolved_financial"] == 4
    assert len(outcome["rows"]) == 297
