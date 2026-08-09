"""Runtime wiring for post-EARNINGS financial reconciliation."""
from __future__ import annotations

import logging
import sqlite3

from .financial_reconciliation import (
    load_canonical_by_filing_id,
    load_summaries_by_source_url,
    reconcile_financial_results,
)

logger = logging.getLogger("pipeline.financial_reconciliation")


def reconcile_ingest_run(
    *,
    results: list[dict],
    target_items: list,
    summary: dict,
    state_db,
    run_id: str,
    decision_db_path: str,
    dry_run: bool,
) -> dict:
    """Reconcile one bounded ingest run and persist separate audit history."""
    error_ids = {
        str(row.get("disclosure_id") or "")
        for row in results
        if row.get("status") == "error"
    }
    error_items = [
        item for item in target_items if item.disclosure_id in error_ids
    ]

    conn = sqlite3.connect(decision_db_path)
    try:
        summaries = load_summaries_by_source_url(
            conn, [item.doc_url for item in error_items]
        )
    finally:
        conn.close()
    canonical = load_canonical_by_filing_id(
        [item.disclosure_id for item in error_items]
    )
    reconciliation = reconcile_financial_results(
        results,
        target_items,
        summaries_by_url=summaries,
        canonical_by_filing_id=canonical,
    )

    summary["old_parser_errors"] = reconciliation["old_parser_errors"]
    summary["recovered_by_earnings_v2"] = reconciliation["recovered_by_earnings_v2"]
    summary["supplemental_or_nonfinancial"] = reconciliation[
        "supplemental_or_nonfinancial"
    ]
    summary["unresolved_financial_errors"] = reconciliation["unresolved_financial"]
    summary["fatal_errors"] = reconciliation["unresolved_financial"]
    summary["financial_reconciliation"] = reconciliation

    if not dry_run:
        for row in reconciliation["rows"]:
            state_db.record_financial_reconciliation(
                run_id=run_id,
                disclosure_id=row["disclosure_id"],
                code=row["code"],
                old_parser_status=row["old_parser_status"],
                final_status=row["final_status"],
                reason=row["reason"],
            )

    logger.info(
        "[FINANCIAL_RECONCILE] old_success=%s old_errors=%s old_skipped=%s "
        "recovered=%s supplemental=%s unresolved=%s",
        reconciliation["old_parser_success"],
        reconciliation["old_parser_errors"],
        reconciliation["old_parser_skipped"],
        reconciliation["recovered_by_earnings_v2"],
        reconciliation["supplemental_or_nonfinancial"],
        reconciliation["unresolved_financial"],
    )
    return reconciliation
