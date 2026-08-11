#!/usr/bin/env python3
"""Delete only 417A rows proven to originate from numeric ticker 4170.

The repair deliberately preserves Blue Zones Holdings' official March-period
rows.  It never inserts or changes financial values.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

import psycopg2
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.pipeline.db import load_env

TARGET = "417A"
TRUE_SOURCE_TICKER = "4170"
TRUE_SOURCE_COMPANY = "Kaizen Platform, Inc."
EXPECTED_CANONICAL_DELETE = 158
EXPECTED_FINANCIALS_DELETE = 38
APPLY_TOKEN = "I_UNDERSTAND_417A_CROSS_COMPANY_DELETE"
PRESERVE_PERIODS = {
    ("2026-03-31", "3Q"),
    ("2026-03-31", "FY"),
    ("2027-03-31", "1Q"),
    ("2027-03-31", "FY"),
}
CONTROL_TICKERS = ("4170", "418A", "4180", "472A", "4720")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _rows(cursor: Any, table: str, ticker: str) -> list[dict[str, Any]]:
    if table not in {"canonical_financials", "financials"}:
        raise ValueError(table)
    order = "id" if table == "canonical_financials" else "period,quarter"
    cursor.execute(f"SELECT row_to_json(t) FROM {table} t WHERE ticker=%s ORDER BY {order}", (ticker,))
    return [row[0] for row in cursor.fetchall()]


def _period_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row["period"]), str(row["quarter"])


def _control_snapshot(cursor: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for ticker in CONTROL_TICKERS:
        canonical = _rows(cursor, "canonical_financials", ticker)
        financials = _rows(cursor, "financials", ticker)
        result[ticker] = {
            "canonical_count": len(canonical), "canonical_sha256": _hash(canonical),
            "financials_count": len(financials), "financials_sha256": _hash(financials),
        }
    return result


def build_manifest(cursor: Any) -> dict[str, Any]:
    canonical = _rows(cursor, "canonical_financials", TARGET)
    financials = _rows(cursor, "financials", TARGET)
    contaminated_canonical = [row for row in canonical if _period_key(row) not in PRESERVE_PERIODS]
    contaminated_financials = [row for row in financials if _period_key(row) not in PRESERVE_PERIODS]
    preserved_canonical = [row for row in canonical if _period_key(row) in PRESERVE_PERIODS]
    preserved_financials = [row for row in financials if _period_key(row) in PRESERVE_PERIODS]
    actions = [{
        "canonical_id": row["id"], "period": str(row["period"]), "quarter": row["quarter"],
        "metric": row["metric"], "contaminated_value": row["value"], "source": row["source"],
        "filing_id": row.get("filing_id"), "source_row_key": row.get("source_row_key"),
        "true_source_ticker": TRUE_SOURCE_TICKER, "true_source_company": TRUE_SOURCE_COMPANY,
        "root_cause": "retired JQUANTS_ALPHA_MAP numeric 41700 -> alpha 417A",
        "intended_action": "DELETE_CROSS_COMPANY_CONTAMINATION",
    } for row in contaminated_canonical]
    summary = {
        "target": TARGET,
        "canonical_before": len(canonical), "financials_before": len(financials),
        "canonical_delete": len(contaminated_canonical),
        "financials_delete": len(contaminated_financials),
        "canonical_preserve": len(preserved_canonical),
        "financials_preserve": len(preserved_financials),
        "insert": 0, "update": 0, "rebuild": 0,
    }
    if summary["canonical_delete"] not in (0, EXPECTED_CANONICAL_DELETE):
        raise RuntimeError(f"unexpected canonical delete count: {summary}")
    if summary["financials_delete"] not in (0, EXPECTED_FINANCIALS_DELETE):
        raise RuntimeError(f"unexpected financials delete count: {summary}")
    manifest: dict[str, Any] = {
        "scope": "exact ticker 417A only",
        "identity_finding": {
            "417A": "Blue Zones Holdings; raw J-Quants code 417A0; March fiscal year",
            "4170": "Kaizen Platform; raw J-Quants code 41700; December fiscal year",
            "first_broken_layer": "legacy ticker normalization before canonical expansion",
        },
        "preserved_official_periods": sorted([list(key) for key in PRESERVE_PERIODS]),
        "summary": summary,
        "canonical_actions": actions,
        "financials_delete_rows": contaminated_financials,
        "preserved_canonical_rows": preserved_canonical,
        "preserved_financials_rows": preserved_financials,
        "controls_before": _control_snapshot(cursor),
    }
    manifest["manifest_sha256"] = _hash(manifest)
    return manifest


def safety_gate(connection: Any, viewer_url: str, schedulers_confirmed_none: bool) -> dict[str, Any]:
    if not schedulers_confirmed_none:
        raise RuntimeError("scheduler NONE confirmation flag is required")
    started = time.perf_counter()
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        cursor.execute("""SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid=l.relation
                          WHERE c.relname IN ('canonical_financials','financials') AND NOT l.granted""")
        locks = int(cursor.fetchone()[0])
        cursor.execute("""SELECT count(*) FROM pg_stat_activity
                          WHERE pid <> pg_backend_pid() AND state <> 'idle'
                            AND query ~* '(insert[[:space:]]+into|update|delete[[:space:]]+from)[[:space:]]+(public\\.)?(canonical_financials|financials)'""")
        active_writers = int(cursor.fetchone()[0])
    response = requests.get(viewer_url, timeout=(5, 20))
    result = {
        "realtime": "NONE", "nightly": "NONE", "backfill": "NONE",
        "locks": locks, "active_canonical_or_financials_sessions": active_writers,
        "supabase_latency_ms": latency_ms, "viewer_status": response.status_code,
    }
    if locks or active_writers or response.status_code != 200:
        raise RuntimeError(f"safety gate failed: {result}")
    return result


def apply_manifest(connection: Any, manifest: dict[str, Any]) -> dict[str, Any]:
    canonical_ids = [int(row["canonical_id"]) for row in manifest["canonical_actions"]]
    financial_keys = [(str(row["period"]), str(row["quarter"])) for row in manifest["financials_delete_rows"]]
    if len(canonical_ids) != EXPECTED_CANONICAL_DELETE or len(financial_keys) != EXPECTED_FINANCIALS_DELETE:
        raise RuntimeError("apply requires the fixed first-run delete counts")
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout='5s'")
        cursor.execute("SET LOCAL statement_timeout='30s'")
        cursor.execute(
            "DELETE FROM canonical_financials WHERE ticker=%s AND id=ANY(%s) RETURNING id",
            (TARGET, canonical_ids),
        )
        deleted_canonical = [int(row[0]) for row in cursor.fetchall()]
        deleted_financials = 0
        for period, quarter in financial_keys:
            cursor.execute(
                "DELETE FROM financials WHERE ticker=%s AND period=%s AND quarter=%s RETURNING ticker",
                (TARGET, period, quarter),
            )
            deleted_financials += len(cursor.fetchall())
        if len(deleted_canonical) != EXPECTED_CANONICAL_DELETE or deleted_financials != EXPECTED_FINANCIALS_DELETE:
            raise RuntimeError(f"delete mismatch: {len(deleted_canonical)}, {deleted_financials}")
    connection.commit()
    return {"canonical_deleted": len(deleted_canonical), "financials_deleted": deleted_financials}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--token")
    parser.add_argument("--manifest-hash")
    parser.add_argument("--schedulers-confirmed-none", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "417a_cross_company_prewrite_manifest.json")
    parser.add_argument("--viewer-url", default="https://company-memo-app.vercel.app/tdnet-alerts")
    args = parser.parse_args()
    load_env()
    connection = psycopg2.connect(os.environ["SUPABASE_POSTGRES_URL"])
    try:
        manifest = build_manifest(connection.cursor())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        result: dict[str, Any] = {"manifest": str(args.output), "summary": manifest["summary"], "manifest_sha256": manifest["manifest_sha256"]}
        if args.apply:
            if args.token != APPLY_TOKEN or args.manifest_hash != manifest["manifest_sha256"]:
                raise RuntimeError("apply token or freshly regenerated manifest hash mismatch")
            result["safety"] = safety_gate(connection, args.viewer_url, args.schedulers_confirmed_none)
            result["write"] = apply_manifest(connection, manifest)
            with connection.cursor() as cursor:
                post = build_manifest(cursor)
                controls_after = _control_snapshot(cursor)
            if post["summary"]["canonical_delete"] or post["summary"]["financials_delete"]:
                raise RuntimeError(f"idempotency failed: {post['summary']}")
            if controls_after != manifest["controls_before"]:
                raise RuntimeError("control ticker invariant failed")
            result["postwrite"] = post["summary"]
            result["controls_after"] = controls_after
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
