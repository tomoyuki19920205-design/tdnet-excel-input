#!/usr/bin/env python3
"""Archive false-positive review-completion Viewer cards safely.

Dry-run is the default.  ``--apply`` additionally requires the exact dry-run
candidate count, which prevents a changed production result set from being
silently applied.  This tool only sets ``tdnet_events.archived_at`` on the
targeted notification artifacts; disclosures and canonical financial rows are
never updated or deleted.  Viewer listing semantics require both
``status='archived'`` and an ``archived_at`` audit timestamp.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.review_completion import (  # noqa: E402
    has_ambiguous_change_marker,
    has_review_completion_marker,
    should_suppress_earnings_notification,
)

JST = timezone(timedelta(hours=9))


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _request(method: str, path: str, payload: dict | None = None):
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        raise RuntimeError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY is required")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{base}/rest/v1/{path}",
        data=body,
        method=method,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else []
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase {method} failed: HTTP {exc.code}: {detail}") from exc


def fetch_events(since: str) -> list[dict]:
    rows: list[dict] = []
    select = (
        "id,disclosed_at,ticker,company_name,event_type,event_subtype,headline,"
        "source_title,source_url,pdf_url,status,archived_at,raw_payload"
    )
    for offset in range(0, 20_000, 1_000):
        query = urlencode(
            {
                "select": select,
                "event_type": "eq.earnings",
                "disclosed_at": f"gte.{since}",
                "order": "disclosed_at.desc",
                "limit": "1000",
                "offset": str(offset),
            }
        )
        page = _request("GET", f"tdnet_events?{query}")
        rows.extend(page)
        if len(page) < 1_000:
            break
    return rows


def build_cleanup_plan(rows: list[dict], ambiguous_comparison: dict[str, str] | None = None) -> dict:
    # Offset pagination can repeat a row when multiple events share the same
    # disclosure timestamp.  Plan cardinality and the apply confirmation gate
    # must therefore be based on immutable event IDs.
    unique_rows = list({row["id"]: row for row in rows if row.get("id")}.values())
    review_rows = [row for row in unique_rows if has_review_completion_marker(row.get("headline") or row.get("source_title") or "")]
    ambiguous_comparison = ambiguous_comparison or {}
    procedural_rows = [
        row for row in review_rows
        if (
            should_suppress_earnings_notification(row.get("headline") or row.get("source_title") or "")
            or (
                has_ambiguous_change_marker(row.get("headline") or row.get("source_title") or "")
                and ambiguous_comparison.get(row["id"]) == "financials_unchanged"
            )
        )
    ]
    candidates = [row for row in procedural_rows if row.get("archived_at") is None and row.get("status") != "archived"]
    already_archived = [row for row in procedural_rows if row.get("archived_at") is not None or row.get("status") == "archived"]
    retained_material = [
        row for row in review_rows
        if row not in procedural_rows
    ]
    unique_periods = {
        (row.get("ticker"), row.get("event_subtype"), _fiscal_period_label(row.get("headline") or ""))
        for row in review_rows
    }
    return {
        "review_completion_disclosures": len(review_rows),
        "notification_artifacts": len(review_rows),
        "false_positive_candidates": len(candidates),
        "retained_material_change_candidates": len(retained_material),
        "already_archived_false_positives": len(already_archived),
        "unique_tickers": len({row.get("ticker") for row in review_rows}),
        "unique_fiscal_periods": len(unique_periods),
        "candidate_ids": [row["id"] for row in candidates],
        "candidates": candidates,
        "retained_material": retained_material,
        "already_archived_ids": [row["id"] for row in already_archived],
        "ambiguous_financial_comparison": ambiguous_comparison,
    }


def _fiscal_period_label(title: str) -> str:
    import re
    import unicodedata

    normalized = unicodedata.normalize("NFKC", title or "")
    match = re.search(r"(\d{4})年(\d{1,2})月期", normalized)
    return f"{match.group(1)}-{int(match.group(2)):02d}" if match else "unknown"


def _financial_signature(row: sqlite3.Row) -> dict[str, str]:
    signature = {
        key: str(row[key])
        for key in ("net_sales", "gross_profit", "operating_profit", "profit_before_tax")
        if row[key] is not None
    }
    try:
        raw = json.loads(row["raw_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = {}
    for key, value in raw.items():
        lowered = key.lower()
        if value not in (None, "") and any(
            token in lowered for token in ("sales", "profit", "income", "eps", "dividend")
        ):
            signature[f"raw.{key}"] = str(value)
    return signature


def compare_ambiguous_events(rows: list[dict], db_path: Path) -> dict[str, str]:
    """Compare generic-change revisions with the preceding J-Quants period row."""
    result: dict[str, str] = {}
    if not db_path.exists():
        return result
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for event in rows:
            title = event.get("headline") or event.get("source_title") or ""
            if not (has_review_completion_marker(title) and has_ambiguous_change_marker(title)):
                continue
            period_label = _fiscal_period_label(title)
            if period_label == "unknown":
                result[event["id"]] = "comparison_unavailable"
                continue
            year, month = map(int, period_label.split("-"))
            import calendar

            period = f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
            ticker = str(event.get("ticker") or "")
            local_code = ticker if len(ticker) >= 5 else f"{ticker}0"
            disclosed_date = str(event.get("disclosed_at") or "")[:10]
            quarter = str(event.get("event_subtype") or "")
            current = conn.execute(
                """
                SELECT * FROM jquants_financials_normalized
                WHERE local_code=? AND current_fiscal_year_end_date=?
                  AND type_of_current_period=? AND disclosed_date=?
                ORDER BY fetched_at DESC LIMIT 1
                """,
                (local_code, period, quarter, disclosed_date),
            ).fetchone()
            previous = conn.execute(
                """
                SELECT * FROM jquants_financials_normalized
                WHERE local_code=? AND current_fiscal_year_end_date=?
                  AND type_of_current_period=? AND disclosed_date < ?
                ORDER BY disclosed_date DESC, fetched_at DESC LIMIT 1
                """,
                (local_code, period, quarter, disclosed_date),
            ).fetchone()
            if current is None or previous is None:
                result[event["id"]] = "comparison_unavailable"
                continue
            before = _financial_signature(previous)
            after = _financial_signature(current)
            comparable = set(before) & set(after)
            if not comparable:
                result[event["id"]] = "comparison_unavailable"
            elif any(before[key] != after[key] for key in comparable):
                result[event["id"]] = "financials_changed"
            else:
                result[event["id"]] = "financials_unchanged"
    finally:
        conn.close()
    return result


def archive_candidates(candidate_ids: list[str], archived_at: str) -> int:
    updated = 0
    for start in range(0, len(candidate_ids), 25):
        chunk = candidate_ids[start : start + 25]
        id_filter = f"in.({','.join(chunk)})"
        path = f"tdnet_events?{urlencode({'id': id_filter})}"
        result = _request("PATCH", path, {"status": "archived", "archived_at": archived_at})
        updated += len(result)
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="2025-08-21T00:00:00+09:00")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-count", type=int)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    _load_env(PROJECT_ROOT / ".env")
    rows = fetch_events(args.since)
    ambiguous_comparison = compare_ambiguous_events(rows, PROJECT_ROOT / "data" / "jquants.db")
    plan = build_cleanup_plan(rows, ambiguous_comparison=ambiguous_comparison)
    plan["mode"] = "apply" if args.apply else "dry_run"
    plan["since"] = args.since
    plan["generated_at"] = datetime.now(JST).isoformat()
    plan["cleanup_scope"] = "tdnet_events notification artifact status/archived_at only"
    plan["updated"] = 0

    if args.apply:
        expected = plan["false_positive_candidates"]
        if args.confirm_count != expected:
            raise SystemExit(
                f"refusing apply: --confirm-count={args.confirm_count!r}, current candidates={expected}"
            )
        archived_at = datetime.now(JST).isoformat()
        plan["updated"] = archive_candidates(plan["candidate_ids"], archived_at)
        plan["archived_at"] = archived_at
        if plan["updated"] != expected:
            raise RuntimeError(f"cleanup cardinality mismatch: expected={expected} updated={plan['updated']}")

    output = json.dumps(plan, ensure_ascii=False, indent=2, default=str)
    print(output)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
