#!/usr/bin/env python3
"""Build, validate, and atomically publish the Company Viewer screener snapshot."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.screener_snapshot import SnapshotBuild, build_snapshot

BATCH_SIZE = 250
COVERAGE_GATES = {
    "forward_per_per_forecast_sales_growth": 70.0,
    "forward_peg": 47.0,
    "return_5d_pct": 94.0,
    "sector17_code": 99.9,
    "sector33_code": 99.9,
    "market_code": 99.9,
    "op_upward_revision_count_3y": 90.0,
}


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _chunks(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for offset in range(0, len(rows), size):
        yield rows[offset:offset + size]


def validate(build: SnapshotBuild) -> None:
    if len(build.rows) != 3889:
        raise RuntimeError(f"ordinary-stock universe must be 3889, got {len(build.rows)}")
    failures = []
    for metric, minimum in COVERAGE_GATES.items():
        actual = float(build.coverage[metric]["coverage_pct"])
        if actual < minimum:
            failures.append(f"{metric}={actual:.2f}% < {minimum:.2f}%")
    if failures:
        raise RuntimeError("coverage gate failed: " + "; ".join(failures))


class SupabaseWriter:
    def __init__(self, url: str, key: str) -> None:
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal,resolution=merge-duplicates",
        }

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        response = requests.request(
            method, self.base + path, headers=self.headers, timeout=(10, 90), **kwargs
        )
        if not response.ok:
            raise RuntimeError(
                f"Supabase {method} {path} failed: HTTP {response.status_code} {response.text[:1000]}"
            )
        return response

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str) -> None:
        for chunk in _chunks(rows, BATCH_SIZE):
            self.request("POST", f"/{table}", params={"on_conflict": on_conflict}, json=chunk)


def batch_payload(build: SnapshotBuild) -> dict[str, Any]:
    return {
        "batch_id": build.batch_id,
        "universe_date": build.universe_date,
        "status": "building",
        "expected_row_count": len(build.rows),
        "revision_event_count": len(build.revision_events),
        "coverage": {"metrics": build.coverage, "null_reasons": build.null_reasons},
        "calculated_at": build.rows[0]["calculated_at"],
    }


def publish(build: SnapshotBuild, writer: SupabaseWriter) -> None:
    batch = batch_payload(build)
    writer.upsert("screener_batches", [batch], "batch_id")
    try:
        events = [
            {key: value for key, value in event.items() if key != "batch_id"}
            for event in build.revision_events
        ]
        writer.upsert(
            "forecast_revision_events", events,
            "ticker,disclosure_id,target_fiscal_year,metric,source",
        )
        writer.upsert("screener_metrics", build.rows, "batch_id,ticker")
        writer.request(
            "POST", "/rpc/publish_screener_batch",
            json={"p_batch_id": build.batch_id, "p_expected_rows": len(build.rows)},
        )
    except Exception as exc:
        try:
            writer.request(
                "PATCH", "/screener_batches",
                params={"batch_id": f"eq.{build.batch_id}"},
                json={"status": "failed", "failure_reason": str(exc)[:2000]},
            )
        finally:
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "jquants.db"))
    parser.add_argument("--as-of")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-gates", action="store_true")
    args = parser.parse_args()

    build = build_snapshot(args.db, as_of=args.as_of)
    if not args.skip_gates:
        validate(build)
    summary = {
        "mode": "apply" if args.apply else "dry-run",
        "batch_id": build.batch_id,
        "universe_date": build.universe_date,
        "row_count": len(build.rows),
        "revision_event_count": len(build.revision_events),
        "coverage": build.coverage,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if not args.apply:
        return 0

    _load_dotenv()
    url = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
    publish(build, SupabaseWriter(url, key))
    print(f"published batch={build.batch_id} rows={len(build.rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
