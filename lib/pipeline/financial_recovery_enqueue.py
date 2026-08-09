"""Queue only disclosure-level unresolved financial outcomes."""
from __future__ import annotations

import logging
from typing import Callable

from .db import supabase_select
from .queue import enqueue_job

logger = logging.getLogger("pipeline.financial_recovery_enqueue")


def _canonical_already_present(row: dict) -> bool:
    """Fail open to retry unless the exact financial period is populated."""
    ticker = str(row.get("code") or "")
    period = str(row.get("period") or "")
    quarter = str(row.get("quarter") or "")
    if not ticker or not period or not quarter:
        return False
    rows = supabase_select(
        "canonical_financials",
        params={
            "ticker": f"eq.{ticker}",
            "period": f"eq.{period}",
            "quarter": f"eq.{quarter}",
            "select": "metric,value,source,filing_id",
        },
    ) or []
    metrics = {str(value.get("metric") or "") for value in rows}
    return "sales" in metrics and bool(
        metrics & {"operating_profit", "ordinary_profit", "net_income"}
    )


def _terminal_retry_exists(row: dict) -> bool:
    disclosure_id = str(row.get("disclosure_id") or "")
    if not disclosure_id:
        return False
    rows = supabase_select(
        "job_queue",
        params={
            "job_type": "eq.tdnet_financial_recovery",
            "target_type": "eq.disclosure",
            "target_id": f"eq.{disclosure_id}",
            "status": "eq.failed",
            "select": "id,attempts,status,error_message",
            "order": "finished_at.desc",
            "limit": "1",
        },
    ) or []
    return bool(rows)


def enqueue_unresolved_financials(
    ingest_result: dict,
    *,
    canonical_exists_fn: Callable[[dict], bool] | None = None,
    terminal_exists_fn: Callable[[dict], bool] | None = None,
) -> dict:
    reconciliation = (ingest_result.get("summary") or {}).get(
        "financial_reconciliation"
    ) or {}
    unresolved = reconciliation.get("unresolved_items") or []
    canonical_exists = canonical_exists_fn or _canonical_already_present
    terminal_exists = terminal_exists_fn or _terminal_retry_exists
    enqueued = 0
    duplicates = 0
    already_resolved = 0
    terminal = 0
    errors = 0

    for row in unresolved:
        disclosure_id = str(row.get("disclosure_id") or "")
        if not disclosure_id:
            errors += 1
            continue
        try:
            resolved = canonical_exists(row)
        except Exception as exc:
            # A read-side outage must not silently drop a genuine financial
            # gap.  The idempotent queue check remains the final guard.
            logger.warning(
                "[financial-recovery] canonical precheck failed; enqueueing "
                "disclosure_id=%s error=%s", disclosure_id, exc,
            )
            resolved = False
        if resolved:
            already_resolved += 1
            continue
        try:
            exhausted = terminal_exists(row)
        except Exception as exc:
            logger.warning(
                "[financial-recovery] terminal precheck failed; enqueueing "
                "disclosure_id=%s error=%s", disclosure_id, exc,
            )
            exhausted = False
        if exhausted:
            terminal += 1
            logger.warning(
                "[financial-recovery] retry limit already exhausted "
                "disclosure_id=%s", disclosure_id,
            )
            continue
        try:
            result = enqueue_job(
                job_type="tdnet_financial_recovery",
                target_type="disclosure",
                target_id=disclosure_id,
                payload=row,
                priority=1,
            )
            if result is None:
                duplicates += 1
            elif result.get("ok"):
                enqueued += 1
            else:
                errors += 1
        except Exception as exc:
            errors += 1
            logger.warning(
                "[financial-recovery] enqueue failed disclosure_id=%s error=%s",
                disclosure_id,
                exc,
            )

    outcome = {
        "unresolved": len(unresolved),
        "enqueued": enqueued,
        "duplicates": duplicates,
        "errors": errors,
        "already_resolved": already_resolved,
        "terminal": terminal,
    }
    logger.info("[financial-recovery] enqueue summary=%s", outcome)
    return outcome
