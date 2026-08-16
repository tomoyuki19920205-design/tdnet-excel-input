"""Targeted, auditable rebuild for the five PL canaries.

This is an operator script, not producer logic.  Values are the verified
official disclosure results captured in artifacts/pl_5_disclosures_before_20260816.json.
Producer fixes live in the extractor, period resolver, and J-Quants merger.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline.canonical_writer import write_financials_canonical
from lib.pipeline.db import load_env, get_supabase_write_config, supabase_select


CANARY_WRITES = [
    dict(ticker="5125", period="2026-06-30", quarter="3Q",
         metrics_dict={"gross_profit": 1486.077}, source="attachment_xbrl",
         filing_id="20260630584241", disclosure_datetime="2026-06-30T15:30:00+09:00",
         correction_flag=True),
    dict(ticker="7350", period="2026-03-31", quarter="FY",
         metrics_dict={"operating_profit": 15799}, source="jquants_bank_proxy",
         filing_id="20260426510953", disclosure_datetime="2026-05-15T12:00:00+09:00",
         correction_flag=False),
    dict(ticker="319A", period="2026-12-31", quarter="1Q",
         metrics_dict={"sales": 6275, "gross_profit": 1928, "operating_profit": 906,
                       "ordinary_profit": 887, "net_income": 1081},
         source="summary_xbrl", filing_id="20260515536073",
         disclosure_datetime="2026-05-15T16:00:00+09:00", correction_flag=False),
    dict(ticker="3925", period="2026-03-31", quarter="1Q",
         metrics_dict={"sales": 1408, "gross_profit": 626, "operating_profit": 327,
                       "profit_before_tax": 307, "ordinary_profit": 327, "net_income": 211},
         source="summary_xbrl", filing_id="20250819544106",
         disclosure_datetime="2025-08-19T00:00:00+09:00", correction_flag=True),
]


def select_rows(config, table: str, params: dict) -> list[dict]:
    return supabase_select(table, params=params, config=config) or []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    load_env()
    config = get_supabase_write_config()
    if not config:
        raise SystemExit("Supabase write config missing")

    phantom_canonical = select_rows(config, "canonical_financials", {
        "select": "*", "ticker": "eq.1967", "period": "eq.2027-03-31",
    })
    phantom_financials = select_rows(config, "financials", {
        "select": "*", "ticker": "eq.1967", "period": "eq.2027-03-31",
    })
    correct_1967 = select_rows(config, "canonical_financials", {
        "select": "metric,value,source", "ticker": "eq.1967", "period": "eq.2027-03-20",
    })
    if not correct_1967:
        raise SystemExit("Refusing cleanup: 1967/2027-03-20 canonical rows are absent")

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "writes": CANARY_WRITES,
        "1967_correct_rows_count": len(correct_1967),
        "1967_phantom_canonical_backup": phantom_canonical,
        "1967_phantom_financials_backup": phantom_financials,
        "results": [],
    }
    if args.apply:
        for write in CANARY_WRITES:
            result = write_financials_canonical(
                **write, unit="millions_jpy", config=config,
            )
            manifest["results"].append({"write": write, "result": result})
            if result["errors"]:
                raise SystemExit(f"Canonical write failed: {write}")

        session = requests.Session()
        headers = {**config["headers"], "Prefer": "return=representation"}
        for table in ("canonical_financials", "financials"):
            response = session.delete(
                f"{config['rest_url']}/{table}",
                headers=headers,
                params={"ticker": "eq.1967", "period": "eq.2027-03-31"},
                timeout=30,
            )
            response.raise_for_status()
            manifest["results"].append({
                "delete_table": table, "deleted": len(response.json() or []),
            })

    output = ROOT / "artifacts" / "pl_5_canary_rebuild_20260816.json"
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    print(json.dumps({
        "mode": manifest["mode"],
        "writes": len(CANARY_WRITES),
        "phantom_canonical": len(phantom_canonical),
        "phantom_financials": len(phantom_financials),
        "results": manifest["results"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
