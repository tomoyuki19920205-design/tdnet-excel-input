#!/usr/bin/env python3
"""Bounded, idempotent recovery for unresolved financial disclosures only."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.pipeline.canonical_writer import write_financials_canonical
from lib.pipeline.db import (
    get_supabase_write_config,
    load_env,
    supabase_select,
    supabase_update,
)
from lib.pipeline.queue import complete_job, take_pending_jobs
from src.config import Config
from src.downloader import download_document
from src.extractor import extract_financials
from src.models import DisclosureItem, DisclosureType
from src.utils import convert_to_excel_unit, excel_unit_multiplier, parse_scale_unit

logger = logging.getLogger("pipeline.financial_recovery")

JOB_TYPE = "tdnet_financial_recovery"
MAX_ATTEMPTS = 3
_CORE_METRICS = {"sales", "operating_profit", "ordinary_profit", "net_income"}
_XBRL_SOURCES = {"summary_xbrl", "attachment_xbrl", "xbrl"}


def _official_url(url: str, suffix: str) -> bool:
    return (
        isinstance(url, str)
        and url.startswith("https://www.release.tdnet.info/")
        and url.lower().split("?", 1)[0].endswith(suffix)
    )


def _payload(job: dict) -> dict:
    value = job.get("payload_json") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _canonical_rows(payload: dict) -> list[dict]:
    params = {
        "ticker": f"eq.{payload.get('code', '')}",
        "select": "filing_id,ticker,period,quarter,metric,value,source",
    }
    if payload.get("period"):
        params["period"] = f"eq.{payload['period']}"
    if payload.get("quarter"):
        params["quarter"] = f"eq.{payload['quarter']}"
    return supabase_select("canonical_financials", params=params) or []


def canonical_is_resolved(payload: dict) -> bool:
    """A canonical key is valid when it contains sales plus a profit metric.

    Ordinary profit is legitimately absent under IFRS/US GAAP, so four fixed
    metrics cannot be required for the retry no-op guard.
    """
    rows = _canonical_rows(payload)
    metrics = {str(row.get("metric") or "") for row in rows}
    profit_metrics = metrics & {
        "operating_profit", "ordinary_profit", "net_income"
    }
    return "sales" in metrics and bool(profit_metrics)


def _to_item(payload: dict) -> DisclosureItem:
    return DisclosureItem(
        disclosure_id=str(payload.get("disclosure_id") or ""),
        ticker=str(payload.get("code") or ""),
        company_name=str(payload.get("company_name") or ""),
        title=str(payload.get("title") or ""),
        doc_url=str(payload.get("doc_url") or ""),
        published_at=str(payload.get("published_at") or ""),
        xbrl_url=payload.get("xbrl_url"),
        disclosure_type=DisclosureType.FINANCIAL_STATEMENT,
        source_doc_id=payload.get("source_doc_id"),
    )


def _write_extracted(item: DisclosureItem, financials, config: dict) -> bool:
    period = financials.fiscal_year
    quarter = financials.quarter
    if not period or not quarter:
        return False

    source_multiplier = parse_scale_unit(financials.source_unit or "円")
    million = excel_unit_multiplier("million_yen")
    grouped: dict[str, dict] = {}
    for field in _CORE_METRICS | {"gross_profit"}:
        value = getattr(financials, field, None)
        if value is None:
            continue
        source = (financials.field_sources or {}).get(field, "")
        canonical_source = "summary_xbrl" if source in _XBRL_SOURCES else "official_pdf"
        grouped.setdefault(canonical_source, {})[field] = convert_to_excel_unit(
            value, source_multiplier, million
        )

    wrote = 0
    errors = 0
    for source, metrics in grouped.items():
        outcome = write_financials_canonical(
            ticker=item.ticker,
            period=period,
            quarter=quarter,
            metrics_dict=metrics,
            source=source,
            filing_id=item.disclosure_id,
            disclosure_datetime=item.published_at,
            correction_flag="訂正" in item.title,
            unit="millions_jpy",
            config=config,
        )
        wrote += int(outcome.get("written", 0) or 0)
        errors += int(outcome.get("errors", 0) or 0)
    return wrote > 0 and errors == 0


def recover_one(payload: dict, *, decision_db_path: str | None = None) -> dict:
    """Try official XBRL, EARNINGS_V2, J-Quants no-op, then official PDF."""
    if canonical_is_resolved(payload):
        return {"resolved": True, "route": "existing_canonical"}

    item = _to_item(payload)
    if not item.disclosure_id or not _official_url(item.doc_url, ".pdf"):
        return {"resolved": False, "route": "invalid_identity_or_source"}

    config = get_supabase_write_config()
    if not config:
        return {"resolved": False, "route": "no_write_config"}

    docs_dir = str(Path(_PROJECT_ROOT) / "data" / "docs")
    pdf_path = download_document(item.doc_url, docs_dir)
    xbrl_path = None
    if item.xbrl_url and _official_url(str(item.xbrl_url), ".zip"):
        xbrl_path = download_document(str(item.xbrl_url), docs_dir)

    # 1. Official XBRL/raw route. PDF is supplied only for safe per-field gaps.
    if pdf_path and xbrl_path:
        financials, _error = extract_financials(
            doc_path=pdf_path, title=item.title, xbrl_path=xbrl_path
        )
        if financials and _write_extracted(item, financials, config):
            if canonical_is_resolved(payload):
                return {"resolved": True, "route": "official_xbrl"}

    # 2. EARNINGS_V2 may resolve/cache the official ZIP through its resolver.
    db_path = decision_db_path or os.getenv(
        "DECISION_DB_PATH", str(Path(_PROJECT_ROOT) / "decision_db.db")
    )
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        from src.events.earnings_production_pipeline import run_earnings_production
        run_earnings_production(
            docs=[item],
            conn=conn,
            webhook_url="",
            dry_run=False,
            state_db=None,
            notify_enabled=False,
        )
    except Exception as exc:
        logger.warning(
            "[financial-recovery] EARNINGS_V2 route failed; continuing "
            "disclosure_id=%s error=%s", item.disclosure_id, exc,
        )
    finally:
        if conn is not None:
            conn.close()
    if canonical_is_resolved(payload):
        return {"resolved": True, "route": "earnings_v2"}

    # 3. Nightly J-Quants sync runs before this command.  Rechecking the period
    # key here makes a later J-Quants recovery an idempotent no-op.
    if any(row.get("source") == "jquants" for row in _canonical_rows(payload)):
        if canonical_is_resolved(payload):
            return {"resolved": True, "route": "jquants"}

    # 4. Existing conservative official-PDF fallback (222A regression case).
    if pdf_path:
        financials, _error = extract_financials(
            doc_path=pdf_path, title=item.title, xbrl_path=None
        )
        if financials and _write_extracted(item, financials, config):
            if canonical_is_resolved(payload):
                return {"resolved": True, "route": "official_pdf"}

    return {"resolved": False, "route": "all_routes_failed"}


def _pending_jobs(*, limit: int) -> list[dict]:
    return supabase_select(
        "job_queue",
        params={
            "job_type": f"eq.{JOB_TYPE}",
            "status": "eq.pending",
            "select": "*",
            "order": "priority.asc,created_at.asc",
            "limit": str(limit),
        },
    ) or []


def run(*, dry_run: bool = False, max_jobs: int = 50) -> dict:
    """Consume a bounded queue; failed jobs remain pending until attempt 3."""
    load_env(_PROJECT_ROOT)
    config = get_supabase_write_config()
    if not config and not dry_run:
        raise RuntimeError("financial recovery requires Supabase service-role config")

    jobs = _pending_jobs(limit=max_jobs) if dry_run else take_pending_jobs(
        JOB_TYPE, limit=max_jobs, config=config
    )
    result = {
        "taken": len(jobs), "resolved": 0, "retried": 0,
        "failed": 0, "dry_run": dry_run, "notifications_sent": 0,
        "routes": {},
    }
    for job in jobs:
        payload = _payload(job)
        if dry_run:
            route = "would_noop" if canonical_is_resolved(payload) else "would_retry"
            result["routes"][route] = result["routes"].get(route, 0) + 1
            continue

        attempt = int(job.get("attempts", 0) or 0) + 1
        try:
            outcome = recover_one(payload)
        except Exception as exc:
            logger.exception(
                "[financial-recovery] disclosure_id=%s attempt=%s error=%s",
                payload.get("disclosure_id"), attempt, exc,
            )
            outcome = {"resolved": False, "route": f"exception:{type(exc).__name__}"}

        route = str(outcome.get("route") or "unknown")
        result["routes"][route] = result["routes"].get(route, 0) + 1
        if outcome.get("resolved"):
            complete_job(job["id"], status="done", config=config)
            result["resolved"] += 1
        elif attempt < MAX_ATTEMPTS:
            supabase_update(
                "job_queue",
                {
                    "status": "pending",
                    "error_message": f"attempt {attempt}/{MAX_ATTEMPTS}: {route}",
                },
                params={"id": f"eq.{job['id']}"},
                config=config,
            )
            result["retried"] += 1
        else:
            complete_job(
                job["id"], status="failed",
                error_message=f"retry limit {MAX_ATTEMPTS}: {route}",
                config=config,
            )
            result["failed"] += 1

    logger.info("[financial-recovery] summary=%s", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-jobs", type=int, default=50)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    outcome = run(dry_run=args.dry_run, max_jobs=max(1, args.max_jobs))
    print(json.dumps(outcome, ensure_ascii=False))
    return 1 if outcome["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
