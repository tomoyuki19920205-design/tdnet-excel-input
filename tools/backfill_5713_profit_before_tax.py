#!/usr/bin/env python3
"""Manifest and exact-ticker apply tool for official 5713 PBT facts.

Values are always parsed from official TDnet XBRL bytes. The tool never reads
legacy operating_profit and never writes operating_profit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

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
from src.events.summary_financials import _extract_multi_period_from_xbrl_bytes


TICKER = "5713"
COMPANY = "住友金属鉱山"
METRIC = "profit_before_tax"
SOURCE = "tdnet_xbrl"
APPLY_TOKEN = "I_UNDERSTAND_5713_PBT_WRITE"

DEFAULT_FY2026_SOURCE = (
    PROJECT_ROOT / "data" / "xbrl_archive" / "081220260511521788.zip"
)
DEFAULT_FY2027_Q1_URL = "https://www.release.tdnet.info/inbs/081220260810516462.zip"

DISCLOSURES = (
    {
        "name": "fy2026",
        "disclosure_id": "20260511521788",
        "disclosure_datetime": "2026-05-11T14:30:00+09:00",
        "default_source": str(DEFAULT_FY2026_SOURCE),
        "facts": (
            ("current_ytd", 2026, "2026-03-31", "FY"),
            ("prior_ytd", 2025, "2025-03-31", "FY"),
        ),
    },
    {
        "name": "fy2027_q1",
        "disclosure_id": "20260810516462",
        "disclosure_datetime": "2026-08-10T14:30:00+09:00",
        "default_source": DEFAULT_FY2027_Q1_URL,
        "facts": (
            ("current_ytd", 2027, "2027-03-31", "1Q"),
            ("prior_ytd", 2026, "2026-03-31", "1Q"),
        ),
    },
)


def _read_source(source: str) -> bytes:
    if source.startswith(("https://", "http://")):
        import requests

        response = requests.get(source, timeout=(5, 15))
        response.raise_for_status()
        raw = response.content
    else:
        raw = Path(source).read_bytes()
    if not raw.startswith(b"PK\x03\x04"):
        raise RuntimeError(f"not an XBRL ZIP package: {source}")
    return raw


def _select_evidence(period_data: Any) -> Any:
    matches = [
        item for item in period_data.evidences
        if item.metric == METRIC and item.value == period_data.profit_before_tax
    ]
    if not matches:
        raise RuntimeError("PBT value has no extraction evidence")
    # Summary is the preferred canonical disclosure fact. Detailed evidence is
    # retained as a generalized parser fallback.
    return sorted(
        matches,
        key=lambda item: (
            0 if item.qname == "tse-ed-t:ProfitBeforeTaxIFRS" else 1,
            item.qname or "",
            item.context_ref or "",
        ),
    )[0]


def discover_official_rows(sources: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for disclosure in DISCLOSURES:
        source_location = sources[disclosure["name"]]
        raw = _read_source(source_location)
        package_sha256 = hashlib.sha256(raw).hexdigest()
        periods = _extract_multi_period_from_xbrl_bytes(raw, include_evidence=True)
        for period_key, fiscal_year, period, quarter in disclosure["facts"]:
            period_data = periods.get(period_key)
            if period_data is None or period_data.profit_before_tax is None:
                continue
            raw_jpy = int(period_data.profit_before_tax)
            if raw_jpy % 1_000_000 != 0:
                raise RuntimeError(
                    f"PBT is not an exact millions_jpy value: {raw_jpy}"
                )
            evidence = _select_evidence(period_data)
            plan = CanonicalWritePlan(
                ticker=TICKER,
                period=period,
                quarter=quarter,
                metric=METRIC,
                value=raw_jpy // 1_000_000,
                unit="millions_jpy",
                source=SOURCE,
                filing_id=disclosure["disclosure_id"],
            )
            plan.validate_and_prepare()
            validate_canonical_write_plan(plan)
            if not plan.write_allowed:
                raise RuntimeError(f"canonical plan blocked: {plan.block_reason}")
            rows.append({
                "ticker": TICKER,
                "company": COMPANY,
                "fiscal_year": fiscal_year,
                "period": period,
                "period_end": period,
                "quarter": quarter,
                "value": plan.value,
                "unit": plan.unit,
                "metric": plan.metric,
                "source": plan.source,
                "disclosure_id": disclosure["disclosure_id"],
                "disclosure_datetime": disclosure["disclosure_datetime"],
                "document": source_location,
                "document_sha256": package_sha256,
                "qname": evidence.qname,
                "local_name": evidence.tag_name,
                "namespace": evidence.namespace,
                "context": evidence.context_ref,
                "source_row_key": plan.source_row_key,
            })
    rows.sort(key=lambda item: (item["period"], item["quarter"], item["disclosure_id"]))
    return rows


def _read_existing(config: dict[str, Any]) -> list[dict[str, Any]]:
    import requests

    response = requests.get(
        f"{config['rest_url']}/canonical_financials",
        params={
            "select": "ticker,period,quarter,metric,value,unit,source,filing_id,source_row_key,disclosure_datetime",
            "ticker": "eq.5713",
            "metric": "eq.profit_before_tax",
            "limit": "50",
        },
        headers=config["headers"],
        timeout=(3, 7),
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"canonical pre-read failed: HTTP {response.status_code} {response.text[:160]}"
        )
    return response.json()


def build_manifest(rows: list[dict[str, Any]], existing: list[dict[str, Any]]) -> dict[str, Any]:
    by_key = {item.get("source_row_key"): item for item in existing}
    manifest_rows = []
    for row in rows:
        current = by_key.get(row["source_row_key"])
        action = "NOOP" if current and float(current["value"]) == float(row["value"]) and current.get("unit") == row["unit"] else "UPSERT"
        manifest_rows.append({
            **row,
            "canonical_before": current,
            "intended_action": action,
        })
    expected = sum(item["intended_action"] == "UPSERT" for item in manifest_rows)
    manifest: dict[str, Any] = {
        "scope": {"ticker": TICKER, "metric": METRIC, "actual_only": True},
        "source_of_truth": "official_tdnet_xbrl",
        "operating_profit_write_count": 0,
        "discovered_period_count": len(manifest_rows),
        "expected_upsert_count": expected,
        "rows": manifest_rows,
    }
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    manifest["manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    return manifest


def _manifest_hash(manifest: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    payload = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


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
    if actual_hash != manifest.get("manifest_sha256") or actual_hash != expected_hash:
        raise RuntimeError("manifest hash mismatch")
    if manifest.get("scope") != {"ticker": TICKER, "metric": METRIC, "actual_only": True}:
        raise RuntimeError("manifest scope is not exact 5713 actual PBT")
    rows_to_write = [row for row in manifest["rows"] if row["intended_action"] == "UPSERT"]
    if len(rows_to_write) != expected_count or expected_count != manifest["expected_upsert_count"]:
        raise RuntimeError("expected UPSERT count mismatch")
    if any(row["ticker"] != TICKER or row["metric"] != METRIC for row in rows_to_write):
        raise RuntimeError("out-of-scope row in manifest")

    write_config = get_supabase_write_config()
    if not write_config:
        raise RuntimeError("service-role write credentials unavailable")
    payload: list[dict[str, Any]] = []
    for item in rows_to_write:
        expanded, skipped = expand_financials_rows(
            ticker=TICKER,
            period=item["period"],
            quarter=item["quarter"],
            metrics_dict={METRIC: item["value"]},
            source=SOURCE,
            filing_id=item["disclosure_id"],
            disclosure_datetime=item["disclosure_datetime"],
            correction_flag=False,
            unit="millions_jpy",
        )
        if skipped or len(expanded) != 1:
            raise RuntimeError("canonical expansion count mismatch")
        payload.extend(expanded)

    result = supabase_upsert(
        "canonical_financials",
        payload,
        config=write_config,
        on_conflict="source_row_key",
        timeout=(5, 15),
        batch_size=max(1, expected_count),
        # supabase_upsert counts the initial request in max_retries.
        # One attempt therefore means no retry after a failed request.
        max_retries=1,
    )
    if not result.get("ok") or result.get("count") != expected_count:
        raise RuntimeError(f"PBT upsert failed or count mismatched: {result}")

    verified = _read_existing(write_config)
    verified_by_key = {item.get("source_row_key"): item for item in verified}
    for item in rows_to_write:
        saved = verified_by_key.get(item["source_row_key"])
        if saved is None or float(saved["value"]) != float(item["value"]):
            raise RuntimeError(f"post-write verification failed: {item['source_row_key']}")
    return {"status": "applied", "written": expected_count, "verified": expected_count}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fy2026-source", default=str(DEFAULT_FY2026_SOURCE))
    parser.add_argument("--fy2027-q1-source", default=DEFAULT_FY2027_Q1_URL)
    parser.add_argument("--env-root", default=str(PROJECT_ROOT))
    parser.add_argument("--manifest-input")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--apply-token", default="")
    parser.add_argument("--expected-upsert-count", type=int)
    parser.add_argument("--manifest-sha256", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env(args.env_root)
    if args.apply:
        if not args.manifest_input or args.expected_upsert_count is None:
            raise RuntimeError("--apply requires manifest input and exact expected count")
        manifest = json.loads(Path(args.manifest_input).read_text(encoding="utf-8"))
        result = apply_manifest(
            manifest,
            expected_count=args.expected_upsert_count,
            expected_hash=args.manifest_sha256,
            apply_token=args.apply_token,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    read_config = get_supabase_read_config()
    if not read_config.get("url") or not read_config.get("key"):
        raise RuntimeError("Supabase read credentials unavailable")
    rows = discover_official_rows({
        "fy2026": args.fy2026_source,
        "fy2027_q1": args.fy2027_q1_source,
    })
    manifest = build_manifest(rows, _read_existing(read_config))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
