"""Disclosure-level reconciliation for the legacy and EARNINGS_V2 paths.

The legacy parser result is audit evidence, not the final financial outcome.
This module keeps that evidence intact and derives a final status only after
checking the exact EARNINGS_V2 summary URL and exact canonical filing lineage.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import asdict, is_dataclass
from typing import Callable, Iterable

from lib.pipeline.db import supabase_select
from src.models import DisclosureType
from src.year_parser import extract_fiscal_info


OLD_PARSER_SUCCESS = "old_parser_success"
OLD_PARSER_SKIPPED = "old_parser_skipped"
RECOVERED_BY_EARNINGS_V2 = "recovered_by_earnings_v2"
SUPPLEMENTAL_OR_NONFINANCIAL = "supplemental_or_nonfinancial"
UNRESOLVED_FINANCIAL = "unresolved_financial"

_SUPPLEMENTAL_RE = re.compile(
    r"(?:決算説明(?:会)?資料|決算補足資料|決算概要|決算ハイライト|"
    r"決算データシート|データシート|決算短信の開示[^。]*お知らせ|"
    r"決算短信[^。]*に関するお知らせ|プレゼンテーション資料|参考資料)"
)


def _item_dict(item) -> dict:
    if is_dataclass(item):
        return asdict(item)
    if isinstance(item, dict):
        return dict(item)
    return {
        name: getattr(item, name, None)
        for name in (
            "disclosure_id", "ticker", "company_name", "title", "doc_url",
            "published_at", "xbrl_url", "disclosure_type", "source_doc_id",
        )
    }


def is_supplemental_or_nonfinancial(item) -> bool:
    """Return True only for strong non-primary-document evidence.

    This is intentionally used *after* exact summary/canonical checks.  A title
    match alone never overrides successfully stored financial data.
    """
    data = _item_dict(item)
    if data.get("disclosure_type") != DisclosureType.FINANCIAL_STATEMENT:
        return True
    return bool(_SUPPLEMENTAL_RE.search(str(data.get("title") or "")))


def load_summaries_by_source_url(
    conn: sqlite3.Connection,
    urls: Iterable[str],
) -> dict[str, dict]:
    """Load EARNINGS_V2 summaries by their exact official PDF URL."""
    unique = sorted({str(url) for url in urls if url})
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    sql = (
        "SELECT ticker, fiscal_year, quarter, title, source_url, "
        "sales_value, op_value FROM earnings_summaries "
        f"WHERE source_url IN ({placeholders})"
    )
    cursor = conn.execute(sql, unique)
    columns = [description[0] for description in cursor.description]
    rows = cursor.fetchall()
    return {str(row[4]): dict(zip(columns, row)) for row in rows}


def load_canonical_by_filing_id(
    disclosure_ids: Iterable[str],
    *,
    select_fn: Callable = supabase_select,
) -> dict[str, list[dict]]:
    """Batch-load exact canonical rows for the supplied SHA-256 filing IDs."""
    ids = sorted({str(value) for value in disclosure_ids if value})
    grouped: dict[str, list[dict]] = {}
    for offset in range(0, len(ids), 50):
        chunk = ids[offset:offset + 50]
        rows = select_fn(
            "canonical_financials",
            params={
                "filing_id": f"in.({','.join(chunk)})",
                "select": "filing_id,ticker,period,quarter,metric,value,source",
            },
        )
        for row in rows or []:
            filing_id = str(row.get("filing_id") or "")
            if filing_id:
                grouped.setdefault(filing_id, []).append(row)
    return grouped


def canonical_matches_summary(item, summary: dict, rows: list[dict]) -> bool:
    """Validate exact filing lineage plus ticker/period/quarter/metric identity."""
    if not summary or not rows:
        return False
    data = _item_dict(item)
    ticker = str(data.get("ticker") or "")
    period, title_quarter = extract_fiscal_info(
        str(data.get("title") or ""),
        published_at=str(data.get("published_at") or ""),
    )
    quarter = str(summary.get("quarter") or title_quarter or "")
    expected = set()
    if summary.get("sales_value") is not None:
        expected.add("sales")
    if summary.get("op_value") is not None:
        expected.add("operating_profit")
    if not expected:
        return False

    matching = [
        row for row in rows
        if str(row.get("ticker") or "") == ticker
        and (not period or str(row.get("period") or "") == period)
        and (not quarter or str(row.get("quarter") or "") == quarter)
    ]
    found = {str(row.get("metric") or "") for row in matching}
    return expected.issubset(found)


def reconcile_financial_results(
    results: list[dict],
    items: list,
    *,
    summaries_by_url: dict[str, dict],
    canonical_by_filing_id: dict[str, list[dict]],
) -> dict:
    """Classify every target result and return fatal/unresolved aggregates."""
    item_by_id = {
        str(_item_dict(item).get("disclosure_id") or ""): item for item in items
    }
    rows: list[dict] = []
    counts: Counter = Counter()

    for result in results:
        disclosure_id = str(result.get("disclosure_id") or "")
        item = item_by_id.get(disclosure_id)
        if item is None:
            # Missing identity must fail closed; it cannot be safely reconciled.
            if result.get("status") == "error":
                final_status = UNRESOLVED_FINANCIAL
            elif result.get("status") == "skipped":
                final_status = OLD_PARSER_SKIPPED
            else:
                final_status = OLD_PARSER_SUCCESS
            reason = "missing_disclosure_identity"
            payload = {}
        else:
            payload = _item_dict(item)
            if result.get("status") == "skipped":
                final_status = OLD_PARSER_SKIPPED
                reason = "legacy_parser_skipped"
            elif result.get("status") != "error":
                final_status = OLD_PARSER_SUCCESS
                reason = "legacy_parser_success"
            else:
                summary = summaries_by_url.get(str(payload.get("doc_url") or ""))
                canonical = canonical_by_filing_id.get(disclosure_id, [])
                if summary and canonical_matches_summary(item, summary, canonical):
                    final_status = RECOVERED_BY_EARNINGS_V2
                    reason = "exact_summary_url_and_canonical_filing_lineage"
                elif is_supplemental_or_nonfinancial(item):
                    final_status = SUPPLEMENTAL_OR_NONFINANCIAL
                    reason = "strong_supplemental_metadata_without_financial_lineage"
                else:
                    final_status = UNRESOLVED_FINANCIAL
                    reason = "no_complete_canonical_for_financial_disclosure"

        result["financial_final_status"] = final_status
        result["financial_final_reason"] = reason
        counts[final_status] += 1

        period, quarter = extract_fiscal_info(
            str(payload.get("title") or ""),
            published_at=str(payload.get("published_at") or ""),
        ) if payload else (None, None)
        rows.append({
            "disclosure_id": disclosure_id,
            "code": str(payload.get("ticker") or result.get("code") or ""),
            "title": str(payload.get("title") or ""),
            "doc_url": str(payload.get("doc_url") or ""),
            "xbrl_url": payload.get("xbrl_url"),
            "source_doc_id": payload.get("source_doc_id"),
            "company_name": str(payload.get("company_name") or ""),
            "published_at": str(payload.get("published_at") or ""),
            "disclosure_type": str(payload.get("disclosure_type") or ""),
            "period": period or "",
            "quarter": quarter or "",
            "old_parser_status": str(result.get("status") or ""),
            "old_parser_error": str(result.get("detail") or "") if result.get("status") == "error" else "",
            "final_status": final_status,
            "reason": reason,
        })

    old_errors = sum(1 for row in results if row.get("status") == "error")
    return {
        "old_parser_errors": old_errors,
        "recovered_by_earnings_v2": counts[RECOVERED_BY_EARNINGS_V2],
        "supplemental_or_nonfinancial": counts[SUPPLEMENTAL_OR_NONFINANCIAL],
        "unresolved_financial": counts[UNRESOLVED_FINANCIAL],
        "old_parser_success": counts[OLD_PARSER_SUCCESS],
        "old_parser_skipped": counts[OLD_PARSER_SKIPPED],
        "rows": rows,
        "unresolved_items": [
            row for row in rows if row["final_status"] == UNRESOLVED_FINANCIAL
        ],
    }
