#!/usr/bin/env python3
"""Exact-ticker J-Quants actual PBT manifest and production apply tool."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
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
from lib.pipeline.unit_convert import to_millions
from src.events.canonical_write_gateway import validate_canonical_write_plan
from src.events.pipeline_context import CanonicalWritePlan
from src.jquants.financial_details import select_latest_effective_pbt
from tools.jquants_auth import get_auth_headers


TICKER = "5713"
JQUANTS_CODE = "57130"
METRIC = "profit_before_tax"
SOURCE = "jquants"
ENDPOINT = "https://api.jquants.com/v2/fins/details"
SUMMARY_ENDPOINT = "https://api.jquants.com/v2/fins/summary"
APPLY_TOKEN = "I_UNDERSTAND_5713_JQUANTS_PBT_WRITE"
OFFICIAL_SOURCES = {"tdnet_xbrl", "summary_xbrl", "xbrl"}
VALID_QUARTERS = {"1Q", "2Q", "3Q", "FY"}


def fetch_raw_details(session: requests.Session | None = None) -> tuple[list[dict[str, Any]], str]:
    own_session = session is None
    session = session or requests.Session()
    try:
        response = session.get(
            ENDPOINT,
            params={"code": JQUANTS_CODE},
            headers=get_auth_headers(),
            timeout=(5, 30),
        )
        response.raise_for_status()
        payload = response.content
        data = response.json()
    finally:
        if own_session:
            session.close()
    items = data.get("data")
    if not isinstance(items, list):
        raise RuntimeError("unexpected /v2/fins/details payload schema")
    if any(item.get("Code") != JQUANTS_CODE for item in items):
        raise RuntimeError("J-Quants response contains an out-of-scope code")
    return items, hashlib.sha256(payload).hexdigest()


def fetch_raw_summaries(session: requests.Session | None = None) -> tuple[dict[str, dict[str, Any]], str]:
    own_session = session is None
    session = session or requests.Session()
    try:
        response = session.get(
            SUMMARY_ENDPOINT,
            params={"code": JQUANTS_CODE},
            headers=get_auth_headers(),
            timeout=(5, 30),
        )
        response.raise_for_status()
        payload = response.content
        data = response.json()
    finally:
        if own_session:
            session.close()
    items = data.get("data")
    if not isinstance(items, list):
        raise RuntimeError("unexpected /v2/fins/summary payload schema")
    if any(item.get("Code") != JQUANTS_CODE for item in items):
        raise RuntimeError("J-Quants summary contains an out-of-scope code")
    summaries = {
        str(item.get("DiscNo")): item
        for item in items
        if item.get("DiscNo")
    }
    return summaries, hashlib.sha256(payload).hexdigest()


def _read_canonical(config: dict[str, Any]) -> list[dict[str, Any]]:
    response = requests.get(
        f"{config['rest_url']}/canonical_financials",
        params={
            "select": "ticker,period,quarter,metric,value,unit,source,source_priority,filing_id,source_row_key,disclosure_datetime",
            "ticker": f"eq.{TICKER}",
            "limit": "2000",
        },
        headers=config["headers"],
        timeout=(3, 10),
    )
    if response.status_code != 200:
        raise RuntimeError(f"canonical read failed: HTTP {response.status_code} {response.text[:200]}")
    rows = response.json()
    if any(item.get("ticker") != TICKER for item in rows):
        raise RuntimeError("canonical response contains an out-of-scope ticker")
    return rows


def _manifest_hash(manifest: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    raw = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def build_manifest(
    details: list[dict[str, Any]],
    details_sha256: str,
    summaries: dict[str, dict[str, Any]],
    canonical: list[dict[str, Any]],
    summary_sha256: str | None = None,
) -> dict[str, Any]:
    records, extraction_audit = select_latest_effective_pbt(
        details, summaries, expected_code=JQUANTS_CODE
    )
    valid_by_period = {(r.fiscal_year_end, r.quarter): r for r in records}
    canonical_by_period: dict[tuple[str, str], list[dict[str, Any]]] = {}
    viewer_periods: set[tuple[str, str]] = set()
    for item in canonical:
        key = (str(item.get("period") or ""), str(item.get("quarter") or ""))
        if key[1] not in VALID_QUARTERS:
            continue
        if item.get("metric") == METRIC:
            canonical_by_period.setdefault(key, []).append(item)
        if item.get("metric") in {"sales", METRIC}:
            viewer_periods.add(key)

    rows: list[dict[str, Any]] = []
    for period, quarter in sorted(viewer_periods):
        existing = canonical_by_period.get((period, quarter), [])
        official = [row for row in existing if row.get("source") in OFFICIAL_SOURCES]
        record = valid_by_period.get((period, quarter))
        if official:
            action = "NO_ACTION_OFFICIAL_PBT_EXISTS"
        elif existing:
            action = "NO_ACTION_NO_VALID_PBT"
        elif record is not None:
            action = "INSERT_JQUANTS_PBT"
        else:
            action = "NO_ACTION_NO_VALID_PBT"

        row: dict[str, Any] = {
            "ticker": TICKER,
            "fiscal_year": int(period[:4]) if period[:4].isdigit() else None,
            "quarter": quarter,
            "period_end": period,
            "raw_jquants_pbt_jpy": record.raw_value_jpy if record else None,
            "canonical_normalized_value_millions_jpy": to_millions(record.raw_value_jpy) if record else None,
            "existing_canonical_pbt": existing,
            "source": SOURCE if record else None,
            "disclosure_number": record.disclosure_number if record else None,
            "disclosure_datetime": record.disclosure_datetime if record else None,
            "document_type": record.document_type if record else None,
            "accounting_standard": record.accounting_standard if record else None,
            "consolidation_scope": record.actual_scope if record else None,
            "pbt_field": record.pbt_field if record else None,
            "intended_action": action,
        }
        if record:
            plan = CanonicalWritePlan(
                ticker=TICKER,
                period=period,
                quarter=quarter,
                metric=METRIC,
                value=row["canonical_normalized_value_millions_jpy"],
                unit="millions_jpy",
                source=SOURCE,
                filing_id=record.disclosure_number,
            )
            plan.validate_and_prepare()
            validate_canonical_write_plan(plan)
            if not plan.write_allowed:
                raise RuntimeError(f"canonical plan blocked: {plan.block_reason}")
            row["source_row_key"] = plan.source_row_key
        rows.append(row)

    inserts = [row for row in rows if row["intended_action"] == "INSERT_JQUANTS_PBT"]
    manifest: dict[str, Any] = {
        "scope": {
            "ticker": TICKER,
            "jquants_code": JQUANTS_CODE,
            "metric": METRIC,
            "actual_only": True,
            "consolidated_only": True,
        },
        "endpoint": ENDPOINT,
        "raw_response_sha256": details_sha256,
        "raw_summary_response_sha256": summary_sha256,
        "source_priority": ["tdnet_xbrl", "jquants", None],
        "operating_profit_write_count": 0,
        "viewer_period_count": len(viewer_periods),
        "valid_jquants_period_count": len(records),
        "expected_insert_count": len(inserts),
        "official_rows_preserved_count": sum(
            row["intended_action"] == "NO_ACTION_OFFICIAL_PBT_EXISTS" for row in rows
        ),
        "rows": rows,
        "extraction_audit": extraction_audit,
    }
    manifest["manifest_sha256"] = _manifest_hash(manifest)
    return manifest


def apply_manifest(
    manifest: dict[str, Any],
    *,
    expected_count: int,
    expected_hash: str,
    apply_token: str,
) -> dict[str, Any]:
    if apply_token != APPLY_TOKEN:
        raise RuntimeError("invalid --apply-token")
    actual_hash = _manifest_hash(manifest)
    if actual_hash != expected_hash or actual_hash != manifest.get("manifest_sha256"):
        raise RuntimeError("manifest hash mismatch")
    expected_scope = {
        "ticker": TICKER,
        "jquants_code": JQUANTS_CODE,
        "metric": METRIC,
        "actual_only": True,
        "consolidated_only": True,
    }
    if manifest.get("scope") != expected_scope or manifest.get("operating_profit_write_count") != 0:
        raise RuntimeError("manifest scope/invariant mismatch")
    targets = [row for row in manifest["rows"] if row["intended_action"] == "INSERT_JQUANTS_PBT"]
    if len(targets) != expected_count or expected_count != manifest.get("expected_insert_count"):
        raise RuntimeError("expected insert count mismatch")
    if any(row["ticker"] != TICKER or row.get("source") != SOURCE for row in targets):
        raise RuntimeError("out-of-scope manifest row")

    config = get_supabase_write_config()
    if not config:
        raise RuntimeError("service-role write credentials unavailable")
    before = _read_canonical(config)
    current_pbt_keys = {
        (row["period"], row["quarter"])
        for row in before
        if row.get("metric") == METRIC
    }
    if any((row["period_end"], row["quarter"]) in current_pbt_keys for row in targets):
        raise RuntimeError("prewrite state changed: a target PBT is no longer missing")

    payload: list[dict[str, Any]] = []
    for row in targets:
        expanded, skipped = expand_financials_rows(
            ticker=TICKER,
            period=row["period_end"],
            quarter=row["quarter"],
            metrics_dict={METRIC: row["canonical_normalized_value_millions_jpy"]},
            source=SOURCE,
            filing_id=row["disclosure_number"],
            disclosure_datetime=row["disclosure_datetime"],
            correction_flag=False,
            unit="millions_jpy",
        )
        if skipped or len(expanded) != 1 or expanded[0]["source_row_key"] != row["source_row_key"]:
            raise RuntimeError("canonical expansion mismatch")
        payload.extend(expanded)

    result = supabase_upsert(
        "canonical_financials",
        payload,
        config=config,
        on_conflict="source_row_key",
        timeout=(5, 15),
        batch_size=max(1, expected_count),
        max_retries=1,
    )
    if not result.get("ok") or result.get("count") != expected_count:
        raise RuntimeError(f"J-Quants PBT insert failed: {result}")
    after = _read_canonical(config)
    after_by_key = {row.get("source_row_key"): row for row in after}
    for row in targets:
        saved = after_by_key.get(row["source_row_key"])
        if saved is None or float(saved["value"]) != float(row["canonical_normalized_value_millions_jpy"]):
            raise RuntimeError(f"postwrite verification failed: {row['source_row_key']}")
    return {"status": "applied", "written": expected_count, "verified": expected_count}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-root", default=str(PROJECT_ROOT))
    parser.add_argument("--manifest-output")
    parser.add_argument("--manifest-input")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--apply-token", default="")
    parser.add_argument("--expected-insert-count", type=int)
    parser.add_argument("--manifest-sha256", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env(args.env_root)
    if args.apply:
        if not args.manifest_input or args.expected_insert_count is None:
            raise RuntimeError("--apply requires a manifest and exact expected count")
        manifest = json.loads(Path(args.manifest_input).read_text(encoding="utf-8"))
        print(json.dumps(apply_manifest(
            manifest,
            expected_count=args.expected_insert_count,
            expected_hash=args.manifest_sha256,
            apply_token=args.apply_token,
        ), ensure_ascii=False, indent=2))
        return 0

    config = get_supabase_read_config()
    if not config.get("url") or not config.get("key"):
        raise RuntimeError("Supabase read credentials unavailable")
    details, details_hash = fetch_raw_details()
    summaries, summary_hash = fetch_raw_summaries()
    manifest = build_manifest(
        details,
        details_hash,
        summaries,
        _read_canonical(config),
        summary_hash,
    )
    rendered = json.dumps(manifest, ensure_ascii=False, indent=2)
    if args.manifest_output:
        Path(args.manifest_output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
