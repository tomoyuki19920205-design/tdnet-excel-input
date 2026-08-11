#!/usr/bin/env python3
"""Restore eligible metric-level winners for 9249 FY2025 2Q.

Values are derived from the exact local J-Quants consolidated actual payload.
The tool only enriches provenance on matching legacy TDnet rows and inserts
canonical metrics that are genuinely absent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.pipeline.canonical_writer import expand_financials_rows
from lib.pipeline.db import (
    get_supabase_read_config,
    get_supabase_write_config,
    load_env,
    supabase_upsert,
)
from tools.backfill_structural_no_op_jquants_profit_before_tax import _manifest_hash


TICKER = "9249"
LOCAL_CODE = "92490"
PERIOD = "2025-09-30"
QUARTER = "2Q"
DISCLOSURE_DATE = "2025-05-15"
DISCLOSURE_NUMBER = "20250515552859"
DISCLOSURE_DATETIME = "2025-05-15T11:40:00+09:00"
DOCUMENT_TYPE = "2QFinancialStatements_Consolidated_JP"
PERIOD_START = "2024-10-01"
PERIOD_END = "2025-03-31"
LOCAL_DB = PROJECT_ROOT / "data" / "jquants.db"
APPLY_TOKEN = "I_UNDERSTAND_9249_FY2025_2Q_METRIC_SOURCE_REPAIR"

RAW_FIELDS = {
    "sales": "Sales",
    "gross_profit": "_gross_profit",
    "operating_profit": "OP",
    "ordinary_profit": "OdP",
    "net_income": "NP",
}
PROVENANCE_METRICS = ("sales", "operating_profit")
INSERT_METRICS = ("ordinary_profit", "net_income")


def load_source_payload(path: Path = LOCAL_DB) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT * FROM jquants_financials_normalized
               WHERE local_code=? AND disclosed_date=?
                 AND current_fiscal_year_end_date=?
                 AND type_of_current_period=? AND type_of_document=?""",
            (LOCAL_CODE, DISCLOSURE_DATE, PERIOD, QUARTER, DOCUMENT_TYPE),
        ).fetchall()
    finally:
        connection.close()
    if len(rows) != 1:
        raise RuntimeError(f"expected one exact local actual row, got {len(rows)}")
    raw = json.loads(rows[0]["raw_json"])
    validate_source_payload(raw)
    return raw


def validate_source_payload(raw: dict[str, Any]) -> None:
    expected = {
        "Code": LOCAL_CODE,
        "DiscDate": DISCLOSURE_DATE,
        "DiscNo": DISCLOSURE_NUMBER,
        "DocType": DOCUMENT_TYPE,
        "CurPerType": QUARTER,
        "CurPerSt": PERIOD_START,
        "CurPerEn": PERIOD_END,
        "CurFYEn": PERIOD,
    }
    for field, value in expected.items():
        if str(raw.get(field) or "") != value:
            raise RuntimeError(f"source metadata mismatch: {field}")
    if any(str(raw.get(field) or "") for field in ("NCSales", "NCOP", "NCOdP", "NCNP")):
        raise RuntimeError("nonconsolidated fields are populated")


def normalized_metrics(raw: dict[str, Any]) -> dict[str, int]:
    metrics: dict[str, int] = {}
    for metric, field in RAW_FIELDS.items():
        value = int(raw[field])
        metrics[metric] = value // 1_000_000
    return metrics


def read_canonical(config: dict[str, Any]) -> list[dict[str, Any]]:
    response = requests.get(
        f"{config['rest_url']}/canonical_financials",
        params={
            "select": "ticker,period,quarter,metric,value,unit,source,source_priority,filing_id,source_row_key,disclosure_datetime,recency_key",
            "ticker": f"eq.{TICKER}",
            "period": f"eq.{PERIOD}",
            "quarter": f"eq.{QUARTER}",
            "limit": "100",
        },
        headers=config["headers"],
        timeout=(3, 15),
    )
    response.raise_for_status()
    rows = response.json()
    if any(row.get("ticker") != TICKER for row in rows):
        raise RuntimeError("canonical read escaped exact ticker scope")
    return rows


def build_manifest(raw: dict[str, Any], canonical: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = normalized_metrics(raw)
    rows: list[dict[str, Any]] = []
    for metric in PROVENANCE_METRICS:
        candidates = [
            row for row in canonical
            if row.get("metric") == metric and row.get("source") == "tdnet"
        ]
        if len(candidates) != 1 or int(float(candidates[0]["value"])) != metrics[metric]:
            raise RuntimeError(f"verified legacy TDnet candidate mismatch: {metric}")
        current = candidates[0]
        already_eligible = bool(current.get("filing_id") or current.get("disclosure_datetime"))
        rows.append({
            "ticker": TICKER,
            "fiscal_year": 2025,
            "period": PERIOD,
            "quarter": QUARTER,
            "metric": metric,
            "source_value_jpy": int(raw[RAW_FIELDS[metric]]),
            "normalized_value_millions_jpy": metrics[metric],
            "source": "tdnet",
            "disclosure_id": DISCLOSURE_NUMBER,
            "disclosure_datetime": DISCLOSURE_DATETIME,
            "current_state": current,
            "source_row_key": current["source_row_key"],
            "intended_action": (
                "NO_ACTION_ELIGIBLE_TDNET" if already_eligible
                else "UPDATE_TDNET_PROVENANCE"
            ),
        })

    gross_profit = [
        row for row in canonical
        if row.get("metric") == "gross_profit" and row.get("source") == "jquants"
    ]
    if len(gross_profit) != 1 or int(float(gross_profit[0]["value"])) != metrics["gross_profit"]:
        raise RuntimeError("valid J-Quants gross profit is not preserved")
    rows.append({
        "ticker": TICKER,
        "fiscal_year": 2025,
        "period": PERIOD,
        "quarter": QUARTER,
        "metric": "gross_profit",
        "source_value_jpy": int(raw[RAW_FIELDS["gross_profit"]]),
        "normalized_value_millions_jpy": metrics["gross_profit"],
        "source": "jquants",
        "disclosure_id": DISCLOSURE_NUMBER,
        "current_state": gross_profit[0],
        "intended_action": "NO_ACTION_VALID_JQUANTS",
    })

    for metric in INSERT_METRICS:
        eligible = [row for row in canonical if row.get("metric") == metric]
        if eligible and any(int(float(row["value"])) != metrics[metric] for row in eligible):
            raise RuntimeError(f"conflicting canonical value: {metric}")
        expanded, skipped = expand_financials_rows(
            ticker=TICKER,
            period=PERIOD,
            quarter=QUARTER,
            metrics_dict={metric: metrics[metric]},
            source="jquants",
            filing_id=DISCLOSURE_NUMBER,
            disclosure_datetime=DISCLOSURE_DATETIME,
            correction_flag=False,
            unit="millions_jpy",
        )
        if skipped or len(expanded) != 1:
            raise RuntimeError(f"canonical expansion failed: {metric}")
        rows.append({
            "ticker": TICKER,
            "fiscal_year": 2025,
            "period": PERIOD,
            "quarter": QUARTER,
            "metric": metric,
            "source_value_jpy": int(raw[RAW_FIELDS[metric]]),
            "normalized_value_millions_jpy": metrics[metric],
            "source": "jquants",
            "disclosure_id": DISCLOSURE_NUMBER,
            "disclosure_datetime": DISCLOSURE_DATETIME,
            "current_state": eligible,
            "canonical_row": expanded[0],
            "intended_action": (
                "NO_ACTION_VALID_METRIC_EXISTS" if eligible
                else "INSERT_JQUANTS_ACTUAL"
            ),
        })

    manifest: dict[str, Any] = {
        "scope": {"ticker": TICKER, "period": PERIOD, "quarter": QUARTER},
        "source_identity": {
            "local_code": LOCAL_CODE,
            "document_type": DOCUMENT_TYPE,
            "disclosure_id": DISCLOSURE_NUMBER,
            "disclosure_datetime": DISCLOSURE_DATETIME,
            "period_start": PERIOD_START,
            "period_end": PERIOD_END,
            "raw_payload_sha256": hashlib.sha256(
                json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        },
        "expected_insert_count": sum(r["intended_action"] == "INSERT_JQUANTS_ACTUAL" for r in rows),
        "expected_update_count": sum(r["intended_action"] == "UPDATE_TDNET_PROVENANCE" for r in rows),
        "expected_delete_count": 0,
        "rows": rows,
    }
    manifest["manifest_sha256"] = _manifest_hash(manifest)
    return manifest


def apply_manifest(
    manifest: dict[str, Any], *, expected_insert: int, expected_update: int,
    expected_hash: str, apply_token: str,
) -> dict[str, Any]:
    if apply_token != APPLY_TOKEN:
        raise RuntimeError("invalid apply token")
    if manifest.get("manifest_sha256") != expected_hash or _manifest_hash(manifest) != expected_hash:
        raise RuntimeError("manifest hash mismatch")
    inserts = [r for r in manifest["rows"] if r["intended_action"] == "INSERT_JQUANTS_ACTUAL"]
    updates = [r for r in manifest["rows"] if r["intended_action"] == "UPDATE_TDNET_PROVENANCE"]
    if len(inserts) != expected_insert or len(updates) != expected_update:
        raise RuntimeError("write count mismatch")
    if expected_insert != manifest["expected_insert_count"] or expected_update != manifest["expected_update_count"]:
        raise RuntimeError("manifest expected counts changed")
    if manifest.get("scope") != {"ticker": TICKER, "period": PERIOD, "quarter": QUARTER}:
        raise RuntimeError("manifest scope mismatch")

    config = get_supabase_write_config()
    if not config:
        raise RuntimeError("Supabase write credentials unavailable")
    current = build_manifest(load_source_payload(), read_canonical(config))
    if [r["intended_action"] for r in current["rows"]] != [r["intended_action"] for r in manifest["rows"]]:
        raise RuntimeError("production state changed after manifest creation")

    updated_rows: list[dict[str, Any]] = []
    for row in updates:
        response = requests.patch(
            f"{config['rest_url']}/canonical_financials",
            params={"source_row_key": f"eq.{row['source_row_key']}"},
            headers={**config["headers"], "Prefer": "return=representation"},
            json={
                "filing_id": DISCLOSURE_NUMBER,
                "disclosure_datetime": DISCLOSURE_DATETIME,
            },
            timeout=(3, 15),
        )
        response.raise_for_status()
        updated_rows.extend(response.json())
    if len(updated_rows) != expected_update:
        raise RuntimeError(f"production update count mismatch: {len(updated_rows)}")

    result = supabase_upsert(
        "canonical_financials",
        [row["canonical_row"] for row in inserts],
        config=config,
        on_conflict="source_row_key",
        max_retries=1,
    )
    if not result.get("ok") or result.get("count") != expected_insert:
        raise RuntimeError(f"canonical insert failed: {result}")

    response = requests.get(
        f"{config['rest_url']}/api_latest_financials_canonical",
        params={
            "select": "ticker,period,quarter,sales,gross_profit,operating_profit,ordinary_profit,net_income",
            "ticker": f"eq.{TICKER}", "period": f"eq.{PERIOD}",
            "quarter": f"eq.{QUARTER}", "limit": "2",
        },
        headers=config["headers"], timeout=(3, 15),
    )
    response.raise_for_status()
    view = response.json()
    expected = normalized_metrics(load_source_payload())
    if len(view) != 1 or any(int(float(view[0].get(metric))) != expected[metric] for metric in RAW_FIELDS):
        raise RuntimeError(f"postwrite view verification failed: {view}")
    return {"inserted": expected_insert, "updated": expected_update, "deleted": 0, "verified": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest-output")
    parser.add_argument("--manifest-input")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--apply-token", default="")
    parser.add_argument("--expected-insert-count", type=int, default=-1)
    parser.add_argument("--expected-update-count", type=int, default=-1)
    parser.add_argument("--manifest-sha256", default="")
    args = parser.parse_args()
    load_env(PROJECT_ROOT)
    if args.apply:
        manifest = json.loads(Path(args.manifest_input).read_text(encoding="utf-8"))
        print(json.dumps(apply_manifest(
            manifest,
            expected_insert=args.expected_insert_count,
            expected_update=args.expected_update_count,
            expected_hash=args.manifest_sha256,
            apply_token=args.apply_token,
        ), ensure_ascii=False, indent=2))
        return 0
    config = get_supabase_read_config()
    manifest = build_manifest(load_source_payload(), read_canonical(config))
    rendered = json.dumps(manifest, ensure_ascii=False, indent=2)
    if args.manifest_output:
        Path(args.manifest_output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
