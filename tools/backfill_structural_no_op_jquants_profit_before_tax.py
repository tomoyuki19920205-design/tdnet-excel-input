#!/usr/bin/env python3
"""Exact-four-ticker J-Quants actual PBT manifest and production apply tool."""
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


COMPANY_CONFIG: dict[str, dict[str, str]] = {
    "2282": {"name": "日本ハム", "jquants_code": "22820"},
    "8031": {"name": "三井物産", "jquants_code": "80310"},
    "8058": {"name": "三菱商事", "jquants_code": "80580"},
    "4819": {"name": "デジタルガレージ", "jquants_code": "48190"},
}
TICKERS = tuple(COMPANY_CONFIG)
METRIC = "profit_before_tax"
SOURCE = "jquants"
ENDPOINT = "https://api.jquants.com/v2/fins/details"
SUMMARY_ENDPOINT = "https://api.jquants.com/v2/fins/summary"
APPLY_TOKEN = "I_UNDERSTAND_STRUCTURAL_NO_OP_FOUR_JQUANTS_PBT_WRITE"
OFFICIAL_SOURCES = {"tdnet_xbrl", "summary_xbrl", "xbrl"}
VALID_QUARTERS = {"1Q", "2Q", "3Q", "FY"}


def _fetch_json(
    endpoint: str,
    jquants_code: str,
    session: requests.Session,
) -> tuple[list[dict[str, Any]], str]:
    response = session.get(
        endpoint,
        params={"code": jquants_code},
        headers=get_auth_headers(),
        timeout=(5, 30),
    )
    response.raise_for_status()
    payload = response.content
    data = response.json()
    items = data.get("data")
    if not isinstance(items, list):
        raise RuntimeError(f"unexpected J-Quants payload schema: {endpoint}")
    if any(item.get("Code") != jquants_code for item in items):
        raise RuntimeError(f"J-Quants response contains an out-of-scope code: {jquants_code}")
    return items, hashlib.sha256(payload).hexdigest()


def fetch_company_source(
    ticker: str,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    if ticker not in COMPANY_CONFIG:
        raise RuntimeError(f"ticker is outside approved scope: {ticker}")
    own_session = session is None
    session = session or requests.Session()
    try:
        code = COMPANY_CONFIG[ticker]["jquants_code"]
        details, details_hash = _fetch_json(ENDPOINT, code, session)
        summaries, summary_hash = _fetch_json(SUMMARY_ENDPOINT, code, session)
    finally:
        if own_session:
            session.close()
    return {
        "details": details,
        "details_sha256": details_hash,
        "summaries": {
            str(item.get("DiscNo")): item for item in summaries if item.get("DiscNo")
        },
        "summary_sha256": summary_hash,
    }


def _read_canonical(config: dict[str, Any], ticker: str) -> list[dict[str, Any]]:
    if ticker not in COMPANY_CONFIG:
        raise RuntimeError(f"canonical read outside approved scope: {ticker}")
    response = requests.get(
        f"{config['rest_url']}/canonical_financials",
        params={
            "select": "ticker,period,quarter,metric,value,unit,source,source_priority,filing_id,source_row_key,disclosure_datetime",
            "ticker": f"eq.{ticker}",
            "limit": "2000",
        },
        headers=config["headers"],
        timeout=(3, 10),
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"canonical read failed for {ticker}: HTTP {response.status_code} {response.text[:200]}"
        )
    rows = response.json()
    if any(item.get("ticker") != ticker for item in rows):
        raise RuntimeError(f"canonical response contains an out-of-scope ticker: {ticker}")
    return rows


def _manifest_hash(manifest: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    raw = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _company_manifest_rows(
    ticker: str,
    source_data: dict[str, Any],
    canonical: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    code = COMPANY_CONFIG[ticker]["jquants_code"]
    records, extraction_audit = select_latest_effective_pbt(
        source_data["details"], source_data["summaries"], expected_code=code
    )
    valid_by_period = {(record.fiscal_year_end, record.quarter): record for record in records}
    canonical_by_period: dict[tuple[str, str], list[dict[str, Any]]] = {}
    viewer_periods: set[tuple[str, str]] = set()
    for item in canonical:
        if item.get("ticker") != ticker:
            raise RuntimeError(f"canonical input outside exact ticker scope: {ticker}")
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
            action = "NO_ACTION_CANONICAL_PBT_EXISTS"
        elif record is not None:
            action = "INSERT_JQUANTS_PBT"
        else:
            action = "NO_ACTION_NO_VALID_PBT"

        row: dict[str, Any] = {
            "ticker": ticker,
            "company_name": COMPANY_CONFIG[ticker]["name"],
            "fiscal_year": int(period[:4]) if period[:4].isdigit() else None,
            "quarter": quarter,
            "period_end": period,
            "raw_jquants_pbt_jpy": record.raw_value_jpy if record else None,
            "canonical_normalized_value_millions_jpy": (
                to_millions(record.raw_value_jpy) if record else None
            ),
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
                ticker=ticker,
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
    return rows, extraction_audit, len(records)


def build_manifest(
    sources_by_ticker: dict[str, dict[str, Any]],
    canonical_by_ticker: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    if tuple(sources_by_ticker) != TICKERS or tuple(canonical_by_ticker) != TICKERS:
        raise RuntimeError("manifest inputs must contain the exact approved ticker order")
    rows: list[dict[str, Any]] = []
    extraction_audit: dict[str, list[dict[str, Any]]] = {}
    valid_counts: dict[str, int] = {}
    raw_hashes: dict[str, dict[str, str]] = {}
    for ticker in TICKERS:
        source_data = sources_by_ticker[ticker]
        ticker_rows, audit, valid_count = _company_manifest_rows(
            ticker, source_data, canonical_by_ticker[ticker]
        )
        rows.extend(ticker_rows)
        extraction_audit[ticker] = audit
        valid_counts[ticker] = valid_count
        raw_hashes[ticker] = {
            "details_sha256": source_data["details_sha256"],
            "summary_sha256": source_data["summary_sha256"],
        }

    inserts = [row for row in rows if row["intended_action"] == "INSERT_JQUANTS_PBT"]
    manifest: dict[str, Any] = {
        "scope": {
            "tickers": list(TICKERS),
            "jquants_codes": {
                ticker: COMPANY_CONFIG[ticker]["jquants_code"] for ticker in TICKERS
            },
            "metric": METRIC,
            "actual_only": True,
            "consolidated_only": True,
        },
        "endpoints": {"details": ENDPOINT, "summary": SUMMARY_ENDPOINT},
        "raw_response_sha256": raw_hashes,
        "source_priority": ["tdnet_xbrl", "jquants", None],
        "operating_profit_write_count": 0,
        "viewer_period_count_by_ticker": {
            ticker: sum(row["ticker"] == ticker for row in rows) for ticker in TICKERS
        },
        "valid_jquants_period_count_by_ticker": valid_counts,
        "expected_insert_count_by_ticker": {
            ticker: sum(row["ticker"] == ticker for row in inserts) for ticker in TICKERS
        },
        "expected_insert_count": len(inserts),
        "official_rows_preserved_count": sum(
            row["intended_action"] == "NO_ACTION_OFFICIAL_PBT_EXISTS" for row in rows
        ),
        "rows": rows,
        "extraction_audit": extraction_audit,
    }
    manifest["manifest_sha256"] = _manifest_hash(manifest)
    return manifest


def _expected_scope() -> dict[str, Any]:
    return {
        "tickers": list(TICKERS),
        "jquants_codes": {
            ticker: COMPANY_CONFIG[ticker]["jquants_code"] for ticker in TICKERS
        },
        "metric": METRIC,
        "actual_only": True,
        "consolidated_only": True,
    }


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
    if manifest.get("scope") != _expected_scope():
        raise RuntimeError("manifest scope mismatch")
    if manifest.get("operating_profit_write_count") != 0:
        raise RuntimeError("operating_profit write invariant mismatch")
    targets = [row for row in manifest["rows"] if row["intended_action"] == "INSERT_JQUANTS_PBT"]
    if len(targets) != expected_count or expected_count != manifest.get("expected_insert_count"):
        raise RuntimeError("expected insert count mismatch")
    if any(row.get("ticker") not in TICKERS or row.get("source") != SOURCE for row in targets):
        raise RuntimeError("out-of-scope manifest row")

    config = get_supabase_write_config()
    if not config:
        raise RuntimeError("service-role write credentials unavailable")
    before = {ticker: _read_canonical(config, ticker) for ticker in TICKERS}
    current_pbt_keys = {
        (ticker, row["period"], row["quarter"])
        for ticker, ticker_rows in before.items()
        for row in ticker_rows
        if row.get("metric") == METRIC
    }
    if any((row["ticker"], row["period_end"], row["quarter"]) in current_pbt_keys for row in targets):
        raise RuntimeError("prewrite state changed: a target PBT is no longer missing")

    payload: list[dict[str, Any]] = []
    for row in targets:
        expanded, skipped = expand_financials_rows(
            ticker=row["ticker"],
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
        if expanded[0].get("metric") != METRIC:
            raise RuntimeError("operating_profit write invariant violated")
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
    after = {
        row.get("source_row_key"): row
        for ticker in TICKERS
        for row in _read_canonical(config, ticker)
    }
    for row in targets:
        saved = after.get(row["source_row_key"])
        if saved is None or float(saved["value"]) != float(row["canonical_normalized_value_millions_jpy"]):
            raise RuntimeError(f"postwrite verification failed: {row['source_row_key']}")
    return {
        "status": "applied",
        "written": expected_count,
        "verified": expected_count,
        "written_by_ticker": {
            ticker: sum(row["ticker"] == ticker for row in targets) for ticker in TICKERS
        },
        "operating_profit_written": 0,
    }


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
    sources: dict[str, dict[str, Any]] = {}
    canonical: dict[str, list[dict[str, Any]]] = {}
    with requests.Session() as session:
        for ticker in TICKERS:
            sources[ticker] = fetch_company_source(ticker, session)
            canonical[ticker] = _read_canonical(config, ticker)
    manifest = build_manifest(sources, canonical)
    rendered = json.dumps(manifest, ensure_ascii=False, indent=2)
    if args.manifest_output:
        Path(args.manifest_output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
