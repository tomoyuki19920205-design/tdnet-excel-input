"""Capture a deterministic, read-only Production Viewer PL API snapshot."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline.db import get_supabase_write_config, load_env
from src.common_ticker import normalize_ticker


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    load_env()
    config = get_supabase_write_config()
    if not config:
        raise SystemExit("Supabase config missing")

    data: dict[str, dict[str, list[dict]]] = {}
    for raw_ticker in args.tickers:
        ticker = normalize_ticker(raw_ticker)
        data[ticker] = {}
        for table in (
            "api_latest_financials_canonical",
            "api_latest_financials_canonical_forecast",
        ):
            response = requests.get(
                f"{config['rest_url']}/{table}",
                headers=config["headers"],
                params={
                    "select": "*",
                    "ticker": f"eq.{ticker}",
                    "order": "period.asc,quarter.asc",
                    "limit": "1000",
                },
                timeout=30,
            )
            response.raise_for_status()
            data[ticker][table] = response.json() or []

    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "data": data,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "sha256": report["sha256"],
        "row_counts": {
            ticker: {table: len(rows) for table, rows in tables.items()}
            for ticker, tables in data.items()
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
