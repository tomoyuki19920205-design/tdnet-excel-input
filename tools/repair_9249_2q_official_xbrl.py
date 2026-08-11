#!/usr/bin/env python3
"""Exact 9249 FY2026 2Q official-XBRL repair with a hash-locked manifest."""
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
from src.events.canonical_write_gateway import validate_canonical_write_plan
from src.events.pipeline_context import CanonicalWritePlan
from src.tdnet_summary_actuals import SummaryActualFact, extract_summary_actuals_from_zip_bytes
from tools.backfill_structural_no_op_jquants_profit_before_tax import _manifest_hash


TICKER = "9249"
COMPANY = "日本エコシステム"
TARGET_PERIOD = "2026-09-30"
TARGET_QUARTER = "2Q"
CONTEXT_PERIOD_START = "2025-10-01"
CONTEXT_PERIOD_END = "2026-03-31"
PRIOR_PERIOD = "2025-09-30"
DISCLOSURE_ID = "20260513530259"
DISCLOSURE_DATETIME = "2026-05-15T16:20:00+09:00"
SOURCE = "tdnet_xbrl"
SOURCE_PACKAGE = PROJECT_ROOT / "data" / "xbrl_archive" / "081220260513530259.zip"
LOCAL_DB = PROJECT_ROOT / "data" / "jquants.db"
APPLY_TOKEN = "I_UNDERSTAND_9249_2Q_EXACT_REPAIR"
INSERT_METRICS = ("sales", "operating_profit", "ordinary_profit", "net_income")
DELETE_KEYS = {
    "sales": "cf|9249|2025-09-30|2Q|sales|jquants|",
    "operating_profit": "cf|9249|2025-09-30|2Q|operating_profit|jquants|",
}


def read_canonical(config: dict[str, Any]) -> list[dict[str, Any]]:
    response = requests.get(
        f"{config['rest_url']}/canonical_financials",
        params={
            "select": "ticker,period,quarter,metric,value,unit,source,source_priority,filing_id,source_row_key,disclosure_datetime",
            "ticker": f"eq.{TICKER}",
            "period": f"in.({PRIOR_PERIOD},{TARGET_PERIOD})",
            "quarter": f"eq.{TARGET_QUARTER}",
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


def load_official_facts(path: Path = SOURCE_PACKAGE) -> tuple[bytes, dict[str, SummaryActualFact]]:
    raw = path.read_bytes()
    facts = extract_summary_actuals_from_zip_bytes(
        raw,
        expected_quarter=TARGET_QUARTER,
        expected_period_start=CONTEXT_PERIOD_START,
        expected_period_end=CONTEXT_PERIOD_END,
    )
    missing = set(INSERT_METRICS) - set(facts)
    if missing:
        raise RuntimeError(f"official Summary facts missing: {sorted(missing)}")
    for metric in INSERT_METRICS:
        fact = facts[metric]
        if fact.period_start != CONTEXT_PERIOD_START or fact.period_end != CONTEXT_PERIOD_END:
            raise RuntimeError(f"unexpected official context dates for {metric}")
        if set(fact.members) != {"ConsolidatedMember", "ResultMember"}:
            raise RuntimeError(f"unexpected official context scope for {metric}")
        if fact.value_jpy % 1_000_000:
            raise RuntimeError(f"official value is not exact millions JPY: {metric}")
    return raw, facts


def _canonical_plan(metric: str, value: int) -> CanonicalWritePlan:
    plan = CanonicalWritePlan(
        ticker=TICKER,
        period=TARGET_PERIOD,
        quarter=TARGET_QUARTER,
        metric=metric,
        value=value,
        unit="millions_jpy",
        source=SOURCE,
        filing_id=DISCLOSURE_ID,
    )
    plan.validate_and_prepare()
    validate_canonical_write_plan(plan)
    if not plan.write_allowed:
        raise RuntimeError(f"canonical plan blocked: {metric} {plan.block_reason}")
    return plan


def build_manifest(
    facts: dict[str, SummaryActualFact],
    canonical: list[dict[str, Any]],
    *,
    package_sha256: str,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    current_by_metric = {
        metric: [
            row for row in canonical
            if row.get("period") == TARGET_PERIOD
            and row.get("quarter") == TARGET_QUARTER
            and row.get("metric") == metric
        ]
        for metric in INSERT_METRICS
    }
    for metric in INSERT_METRICS:
        fact = facts[metric]
        value = fact.value_jpy // 1_000_000
        plan = _canonical_plan(metric, value)
        official = [row for row in current_by_metric[metric] if row.get("source") == SOURCE]
        if official and any(float(row.get("value")) != float(value) for row in official):
            raise RuntimeError(f"conflicting existing official value: {metric}")
        expanded, skipped = expand_financials_rows(
            ticker=plan.ticker,
            period=plan.period,
            quarter=plan.quarter,
            metrics_dict={plan.metric: plan.value},
            source=plan.source,
            filing_id=plan.filing_id,
            disclosure_datetime=DISCLOSURE_DATETIME,
            correction_flag=False,
            unit=plan.unit,
        )
        if skipped or len(expanded) != 1:
            raise RuntimeError(f"canonical expansion count mismatch: {metric}")
        rows.append({
            "ticker": TICKER,
            "company": COMPANY,
            "fiscal_year": 2026,
            "period": TARGET_PERIOD,
            "quarter": TARGET_QUARTER,
            "metric": metric,
            "current_state": current_by_metric[metric],
            "source_value_jpy": fact.value_jpy,
            "normalized_value_millions_jpy": value,
            "source": SOURCE,
            "disclosure_id": DISCLOSURE_ID,
            "disclosure_datetime": DISCLOSURE_DATETIME,
            "canonical_row": expanded[0],
            "qname": fact.qname,
            "local_name": fact.local_name,
            "namespace": fact.namespace,
            "context": fact.context,
            "period_start": fact.period_start,
            "period_end": fact.period_end,
            "members": list(fact.members),
            "unit_ref": fact.unit_ref,
            "intended_action": (
                "NO_ACTION_OFFICIAL_XBRL_EXISTS" if official
                else "INSERT_OFFICIAL_XBRL_ACTUAL"
            ),
        })

    for metric, source_row_key in DELETE_KEYS.items():
        fact_value = facts[metric].value_jpy // 1_000_000
        exact = [
            row for row in canonical
            if row.get("source_row_key") == source_row_key
        ]
        prior_official = [
            row for row in canonical
            if row.get("period") == PRIOR_PERIOD
            and row.get("quarter") == TARGET_QUARTER
            and row.get("metric") == metric
            and row.get("source") == "tdnet"
        ]
        if len(exact) != 1 or float(exact[0].get("value")) != float(fact_value):
            raise RuntimeError(f"misperiodized J-Quants row mismatch: {metric}")
        if not prior_official or any(float(row.get("value")) == float(fact_value) for row in prior_official):
            raise RuntimeError(f"prior-period official conflict evidence missing: {metric}")
        rows.append({
            "ticker": TICKER,
            "company": COMPANY,
            "fiscal_year": 2025,
            "period": PRIOR_PERIOD,
            "quarter": TARGET_QUARTER,
            "metric": metric,
            "current_state": exact,
            "source_value_jpy": facts[metric].value_jpy,
            "source": "jquants",
            "disclosure_id": DISCLOSURE_ID,
            "canonical_row": exact[0],
            "source_row_key": source_row_key,
            "prior_period_official_rows_preserved": prior_official,
            "intended_action": "DELETE_MISPERIODIZED_JQUANTS",
        })

    manifest: dict[str, Any] = {
        "scope": {"ticker": TICKER, "periods": [PRIOR_PERIOD, TARGET_PERIOD], "quarter": TARGET_QUARTER},
        "official_package": str(SOURCE_PACKAGE),
        "official_package_sha256": package_sha256,
        "disclosure_id": DISCLOSURE_ID,
        "expected_insert_count": sum(row["intended_action"] == "INSERT_OFFICIAL_XBRL_ACTUAL" for row in rows),
        "expected_update_count": 0,
        "expected_delete_count": sum(row["intended_action"] == "DELETE_MISPERIODIZED_JQUANTS" for row in rows),
        "expected_local_delete_count": 1,
        "rows": rows,
    }
    manifest["manifest_sha256"] = _manifest_hash(manifest)
    return manifest


def _delete_local_stale_source() -> int:
    connection = sqlite3.connect(LOCAL_DB)
    try:
        cursor = connection.execute(
            """DELETE FROM jquants_financials_normalized
               WHERE local_code=? AND disclosed_date=?
                 AND current_fiscal_year_end_date=? AND type_of_current_period=?
                 AND type_of_document=?""",
            ("92490", "2026-05-15", PRIOR_PERIOD, TARGET_QUARTER,
             "2QFinancialStatements_Consolidated_JP"),
        )
        connection.commit()
        return cursor.rowcount
    finally:
        connection.close()


def apply_manifest(
    manifest: dict[str, Any], *, expected_insert: int, expected_delete: int,
    expected_hash: str, apply_token: str,
) -> dict[str, Any]:
    if apply_token != APPLY_TOKEN:
        raise RuntimeError("invalid apply token")
    if _manifest_hash(manifest) != expected_hash or manifest.get("manifest_sha256") != expected_hash:
        raise RuntimeError("manifest hash mismatch")
    inserts = [row for row in manifest["rows"] if row["intended_action"] == "INSERT_OFFICIAL_XBRL_ACTUAL"]
    deletes = [row for row in manifest["rows"] if row["intended_action"] == "DELETE_MISPERIODIZED_JQUANTS"]
    if len(inserts) != expected_insert or len(deletes) != expected_delete:
        raise RuntimeError("exact expected write counts changed")
    if expected_insert != manifest["expected_insert_count"] or expected_delete != manifest["expected_delete_count"]:
        raise RuntimeError("manifest expected counts changed")
    if any(row["ticker"] != TICKER for row in manifest["rows"]):
        raise RuntimeError("manifest escaped exact ticker scope")

    config = get_supabase_write_config()
    if not config:
        raise RuntimeError("Supabase write credentials unavailable")
    before = read_canonical(config)
    build_manifest(
        {metric: SummaryActualFact(**{
            "metric": metric,
            "value_jpy": next(row for row in inserts if row["metric"] == metric)["source_value_jpy"],
            "qname": next(row for row in inserts if row["metric"] == metric)["qname"],
            "local_name": next(row for row in inserts if row["metric"] == metric)["local_name"],
            "namespace": next(row for row in inserts if row["metric"] == metric)["namespace"],
            "context": next(row for row in inserts if row["metric"] == metric)["context"],
            "period_start": next(row for row in inserts if row["metric"] == metric)["period_start"],
            "period_end": next(row for row in inserts if row["metric"] == metric)["period_end"],
            "members": tuple(next(row for row in inserts if row["metric"] == metric)["members"]),
            "dimensions": (),
            "unit_ref": next(row for row in inserts if row["metric"] == metric)["unit_ref"],
            "scale": 6,
            "source_file": "manifest",
        }) for metric in INSERT_METRICS},
        before, package_sha256=manifest["official_package_sha256"],
    )

    local_deleted = _delete_local_stale_source()
    if local_deleted != manifest["expected_local_delete_count"]:
        raise RuntimeError(f"local stale source delete count mismatch: {local_deleted}")
    result = supabase_upsert(
        "canonical_financials", [row["canonical_row"] for row in inserts],
        config=config, on_conflict="source_row_key", max_retries=1,
    )
    if not result.get("ok") or result.get("count") != expected_insert:
        raise RuntimeError(f"official insert failed: {result}")
    deleted_rows: list[dict[str, Any]] = []
    for row in deletes:
        response = requests.delete(
            f"{config['rest_url']}/canonical_financials",
            params={"source_row_key": f"eq.{row['source_row_key']}"},
            headers={**config["headers"], "Prefer": "return=representation"},
            timeout=(3, 15),
        )
        if response.status_code not in (200, 204):
            raise RuntimeError(f"canonical delete failed: {response.status_code} {response.text[:200]}")
        if response.content:
            deleted_rows.extend(response.json())
    if len(deleted_rows) != expected_delete:
        raise RuntimeError(f"production delete count mismatch: {len(deleted_rows)}")

    after = read_canonical(config)
    for row in inserts:
        if not any(
            item.get("source_row_key") == row["canonical_row"]["source_row_key"]
            and float(item.get("value")) == float(row["normalized_value_millions_jpy"])
            for item in after
        ):
            raise RuntimeError(f"official postwrite verification failed: {row['metric']}")
    if any(item.get("source_row_key") in DELETE_KEYS.values() for item in after):
        raise RuntimeError("misperiodized J-Quants row remains after delete")
    return {
        "inserted": expected_insert,
        "updated": 0,
        "deleted": expected_delete,
        "local_source_deleted": local_deleted,
        "verified": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-root", default=str(PROJECT_ROOT))
    parser.add_argument("--manifest-output")
    parser.add_argument("--manifest-input")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--apply-token", default="")
    parser.add_argument("--expected-insert-count", type=int)
    parser.add_argument("--expected-delete-count", type=int)
    parser.add_argument("--manifest-sha256", default="")
    args = parser.parse_args()
    load_env(args.env_root)
    if args.apply:
        manifest = json.loads(Path(args.manifest_input).read_text(encoding="utf-8"))
        print(json.dumps(apply_manifest(
            manifest,
            expected_insert=args.expected_insert_count,
            expected_delete=args.expected_delete_count,
            expected_hash=args.manifest_sha256,
            apply_token=args.apply_token,
        ), ensure_ascii=False, indent=2))
        return 0
    config = get_supabase_read_config()
    raw, facts = load_official_facts()
    manifest = build_manifest(
        facts, read_canonical(config), package_sha256=hashlib.sha256(raw).hexdigest()
    )
    rendered = json.dumps(manifest, ensure_ascii=False, indent=2)
    if args.manifest_output:
        Path(args.manifest_output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
