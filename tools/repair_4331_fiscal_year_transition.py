#!/usr/bin/env python3
"""Audit and repair 4331 interim rows after its March-to-December FY change."""
from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any

import psycopg2
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.pipeline.db import get_supabase_read_config, load_env
from lib.pipeline.jquants_fiscal_period import resolved_fiscal_year_end_sql


TICKER = "4331"
LOCAL_CODE = "43310"
WRONG_PERIOD = "2026-03-31"
CORRECT_PERIOD = "2025-12-31"
QUARTERS = ("1Q", "2Q")
METRICS = ("sales", "gross_profit", "operating_profit")
EXPECTED_MOVE_COUNT = 6
APPLY_TOKEN = "I_UNDERSTAND_4331_FISCAL_YEAR_TRANSITION_REPAIR"


def _hash(payload: dict[str, Any]) -> str:
    clean = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    return hashlib.sha256(
        json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def local_transition_audit(db_path: Path) -> list[dict[str, Any]]:
    expression = resolved_fiscal_year_end_sql("source")
    query = f"""
    SELECT source.local_code, source.disclosed_date,
           source.type_of_current_period AS quarter,
           source.current_fiscal_year_end_date AS raw_fiscal_year_end,
           {expression} AS resolved_fiscal_year_end,
           json_extract(source.raw_json, '$.CurFYSt') AS fiscal_year_start,
           json_extract(source.raw_json, '$.CurPerSt') AS period_start,
           json_extract(source.raw_json, '$.CurPerEn') AS period_end,
           json_extract(source.raw_json, '$.DiscNo') AS disclosure_id,
           source.type_of_document, source.net_sales, source.gross_profit,
           source.operating_profit
    FROM jquants_financials_normalized AS source
    WHERE source.type_of_current_period IN ('1Q', '2Q', '3Q')
      AND ({expression}) <> source.current_fiscal_year_end_date
    ORDER BY source.local_code, source.disclosed_date
    """
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query)]


def _production_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    response = requests.get(
        f"{config['rest_url']}/canonical_financials",
        headers=config["headers"],
        params={
            "select": "id,ticker,period,quarter,metric,value,unit,source,source_priority,filing_id,source_row_key,disclosure_datetime,recency_key,updated_at",
            "ticker": f"eq.{TICKER}",
            "period": f"in.({CORRECT_PERIOD},{WRONG_PERIOD},2026-12-31)",
            "order": "period.asc,quarter.asc,metric.asc",
            "limit": "100",
        },
        timeout=(3, 20),
    )
    response.raise_for_status()
    rows = response.json()
    if any(row["ticker"] != TICKER for row in rows):
        raise RuntimeError("production read escaped exact ticker scope")
    return rows


def build_manifest(db_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    audit = local_transition_audit(db_path)
    sources = [
        row for row in audit
        if row["local_code"] == LOCAL_CODE
        and row["raw_fiscal_year_end"] == WRONG_PERIOD
        and row["resolved_fiscal_year_end"] == CORRECT_PERIOD
        and row["quarter"] in QUARTERS
    ]
    if len(sources) != 2:
        raise RuntimeError(f"expected two exact 4331 source disclosures, got {len(sources)}")
    by_quarter = {row["quarter"]: row for row in sources}
    if set(by_quarter) != set(QUARTERS):
        raise RuntimeError("4331 transition source quarters mismatch")

    production = _production_rows(config)
    wrong = {
        (row["quarter"], row["metric"]): row
        for row in production
        if row["period"] == WRONG_PERIOD
        and row["quarter"] in QUARTERS
        and row["source"] == "jquants"
    }
    targets = {row["source_row_key"]: row for row in production if row["period"] == CORRECT_PERIOD}
    moves: list[dict[str, Any]] = []
    no_actions: list[dict[str, Any]] = []
    local_columns = {
        "sales": "net_sales",
        "gross_profit": "gross_profit",
        "operating_profit": "operating_profit",
    }
    for quarter in QUARTERS:
        source = by_quarter[quarter]
        for metric in METRICS:
            local_raw = source[local_columns[metric]]
            local_value = None if local_raw is None else int(local_raw) // 1_000_000
            if local_value is None:
                raise RuntimeError(f"local source value missing: {quarter} {metric}")
            new_key = f"cf|{TICKER}|{CORRECT_PERIOD}|{quarter}|{metric}|jquants|"
            current = wrong.get((quarter, metric))
            target = targets.get(new_key)
            if current is None:
                if not target or Decimal(str(target["value"])) != Decimal(local_value):
                    raise RuntimeError(f"neither wrong nor repaired row is valid: {quarter} {metric}")
                no_actions.append({
                    "ticker": TICKER,
                    "quarter": quarter,
                    "metric": metric,
                    "canonical_id": target["id"],
                    "value": target["value"],
                    "source_row_key": new_key,
                    "action": "NO_ACTION_ALREADY_REPAIRED",
                })
                continue
            if target:
                raise RuntimeError(f"old and target J-Quants rows both exist: {new_key}")
            if Decimal(str(current["value"])) != Decimal(local_value):
                raise RuntimeError(f"local/canonical value mismatch: {quarter} {metric}")
            moves.append({
                "ticker": TICKER,
                "quarter": quarter,
                "metric": metric,
                "canonical_id": current["id"],
                "value": current["value"],
                "unit": current["unit"],
                "source": current["source"],
                "disclosure_id": source["disclosure_id"],
                "disclosed_date": source["disclosed_date"],
                "fiscal_year_start": source["fiscal_year_start"],
                "period_start": source["period_start"],
                "period_end": source["period_end"],
                "raw_fiscal_year_end": source["raw_fiscal_year_end"],
                "resolved_fiscal_year_end": source["resolved_fiscal_year_end"],
                "old_source_row_key": current["source_row_key"],
                "new_source_row_key": new_key,
                "action": "UPDATE_PERIOD_AND_SOURCE_ROW_KEY",
            })
    controls = {
        "2025-12-FY": sorted(
            (row["metric"], row["value"], row["source"])
            for row in production
            if row["period"] == CORRECT_PERIOD and row["quarter"] == "FY"
            and row["source"] == "jquants"
        ),
        "2026-12-1Q": sorted(
            (row["metric"], row["value"], row["source"])
            for row in production
            if row["period"] == "2026-12-31" and row["quarter"] == "1Q"
            and row["source"] == "jquants"
        ),
    }
    manifest: dict[str, Any] = {
        "scope": {"ticker": TICKER, "old_period": WRONG_PERIOD, "new_period": CORRECT_PERIOD},
        "expected_period_updates": len(moves),
        "expected_value_updates": 0,
        "local_candidate_rows": len(audit),
        "local_candidate_companies": len({row["local_code"] for row in audit}),
        "moves": moves,
        "no_actions": no_actions,
        "untouched_controls": controls,
    }
    manifest["manifest_sha256"] = _hash(manifest)
    return manifest


def apply_manifest(manifest: dict[str, Any], expected_hash: str, token: str) -> dict[str, Any]:
    if token != APPLY_TOKEN:
        raise RuntimeError("invalid apply token")
    if manifest.get("manifest_sha256") != expected_hash or _hash(manifest) != expected_hash:
        raise RuntimeError("manifest hash mismatch")
    if manifest.get("expected_period_updates") != EXPECTED_MOVE_COUNT:
        raise RuntimeError("expected period update count mismatch")
    if manifest.get("expected_value_updates") != 0:
        raise RuntimeError("value updates are forbidden")

    config = get_supabase_read_config()
    fresh = build_manifest(PROJECT_ROOT / "data" / "jquants.db", config)
    if fresh["manifest_sha256"] != expected_hash:
        raise RuntimeError("production or local source state changed after manifest")
    db_url = os.environ.get("SUPABASE_POSTGRES_URL")
    if not db_url:
        raise RuntimeError("SUPABASE_POSTGRES_URL unavailable")

    safety: dict[str, Any] = {}
    moves = manifest["moves"]
    with psycopg2.connect(db_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '30s'")
            started = time.perf_counter()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            safety["supabase_latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            cursor.execute("""SELECT count(*) FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND state='active' AND query ~* '(insert[[:space:]]+into|update|delete[[:space:]]+from)[[:space:]]+(public[.])?canonical_financials'""")
            safety["active_canonical_writers"] = cursor.fetchone()[0]
            cursor.execute("""SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid=l.relation WHERE c.relname='canonical_financials' AND NOT l.granted""")
            safety["waiting_canonical_locks"] = cursor.fetchone()[0]
            viewer_started = time.perf_counter()
            viewer = requests.get("https://company-memo-app.vercel.app/tdnet-alerts", timeout=(3, 15))
            safety["viewer_status"] = viewer.status_code
            safety["viewer_latency_ms"] = round((time.perf_counter() - viewer_started) * 1000, 1)
            if (
                safety["active_canonical_writers"]
                or safety["waiting_canonical_locks"]
                or safety["supabase_latency_ms"] > 3000
                or viewer.status_code != 200
                or safety["viewer_latency_ms"] > 5000
            ):
                raise RuntimeError(f"production safety gate failed: {safety}")

            ids = [move["canonical_id"] for move in moves]
            cursor.execute(
                "SELECT id,ticker,period,quarter,metric,value,source,source_row_key "
                "FROM canonical_financials WHERE id=ANY(%s) FOR UPDATE",
                (ids,),
            )
            locked = {row[0]: row for row in cursor.fetchall()}
            if len(locked) != EXPECTED_MOVE_COUNT:
                raise RuntimeError("locked row count mismatch")
            cursor.execute(
                "SELECT source_row_key FROM canonical_financials WHERE source_row_key=ANY(%s)",
                ([move["new_source_row_key"] for move in moves],),
            )
            if cursor.fetchall():
                raise RuntimeError("target source-row-key collision")
            updated = 0
            for move in moves:
                row = locked[move["canonical_id"]]
                expected = (
                    move["canonical_id"], TICKER, WRONG_PERIOD, move["quarter"],
                    move["metric"], Decimal(str(move["value"])), "jquants",
                    move["old_source_row_key"],
                )
                actual = (*row[:5], Decimal(str(row[5])), *row[6:])
                if actual != expected:
                    raise RuntimeError(f"locked row changed: {move['canonical_id']}")
                cursor.execute(
                    "UPDATE canonical_financials SET period=%s, source_row_key=%s, updated_at=NOW() "
                    "WHERE id=%s AND ticker=%s AND period=%s AND quarter=%s "
                    "AND metric=%s AND source='jquants' AND source_row_key=%s",
                    (
                        CORRECT_PERIOD, move["new_source_row_key"], move["canonical_id"],
                        TICKER, WRONG_PERIOD, move["quarter"], move["metric"],
                        move["old_source_row_key"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("exact period update count mismatch")
                updated += 1
    return {"period_rows_updated": updated, "value_rows_updated": 0, "safety": safety}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "jquants.db"))
    parser.add_argument("--audit-output")
    parser.add_argument("--manifest-output")
    parser.add_argument("--manifest-input")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--manifest-sha256", default="")
    parser.add_argument("--apply-token", default="")
    args = parser.parse_args()
    load_env(PROJECT_ROOT)
    if args.apply:
        manifest = json.loads(Path(args.manifest_input).read_text(encoding="utf-8"))
        result = apply_manifest(manifest, args.manifest_sha256, args.apply_token)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    audit = local_transition_audit(Path(args.db))
    audit_payload = {
        "candidate_rows": len(audit),
        "candidate_companies": len({row["local_code"] for row in audit}),
        "rows": audit,
    }
    if args.audit_output:
        Path(args.audit_output).write_text(
            json.dumps(audit_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    manifest = build_manifest(Path(args.db), get_supabase_read_config())
    if args.manifest_output:
        Path(args.manifest_output).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
