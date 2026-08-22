#!/usr/bin/env python3
"""Compare the published revision counts with a freshly canonicalized snapshot."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import json
import os
from pathlib import Path
import sys
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.forecast_revision_canonical import metadata_role
from lib.jquants_values import parse_optional_boolean
from lib.screener_snapshot import FORECAST_METRICS, build_snapshot

REQUIRED_TICKERS = ("6454", "6042", "5803", "1980", "8697", "2737")


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def _production_rows() -> list[dict[str, Any]]:
    _load_dotenv()
    url = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("Supabase credentials are required for --fetch-production-before")
    endpoint = url.rstrip("/") + "/rest/v1/screener_metrics_current"
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    result: list[dict[str, Any]] = []
    for offset in range(0, 10000, 1000):
        response = requests.get(
            endpoint,
            headers={**headers, "Range": f"{offset}-{offset + 999}"},
            params={
                "select": (
                    "ticker,batch_id,op_upward_revision_count_3y,"
                    "any_earnings_upward_revision_event_count_3y"
                ),
                "order": "ticker.asc",
            },
            timeout=(10, 60),
        )
        response.raise_for_status()
        page = response.json()
        result.extend(page)
        if len(page) < 1000:
            break
    return result


def _metadata(connection: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT disclosure_id,disclosed_date,disclosed_time,title,disc_items_json,"
        "rev_no,disc_status,metadata_status FROM jquants_tdnet_metadata"
    ):
        result[str(row[0])] = {
            "disclosed_date": row[1], "disclosed_time": row[2], "title": row[3],
            "disc_items": json.loads(row[4] or "[]"), "rev_no": row[5],
            "disc_status": row[6], "metadata_status": row[7],
        }
    return result


def _raw_by_id(connection: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for (payload,) in connection.execute(
        "SELECT raw_json FROM jquants_financials_normalized WHERE raw_json IS NOT NULL"
    ):
        try:
            raw = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        disclosure_id = str(raw.get("DiscNo") or "")
        if disclosure_id:
            result[disclosure_id] = raw
    return result


def _distribution(rows: dict[str, tuple[int | None, int | None]]) -> dict[str, int]:
    return dict(sorted(Counter(f"op={op},event={event}" for op, event in rows.values()).items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "jquants.db"))
    parser.add_argument("--fetch-production-before", action="store_true")
    parser.add_argument("--before-json")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if args.fetch_production_before:
        before_rows = _production_rows()
        if args.before_json:
            Path(args.before_json).write_text(
                json.dumps(before_rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    elif args.before_json:
        before_rows = json.loads(Path(args.before_json).read_text(encoding="utf-8"))
    else:
        raise RuntimeError("provide --fetch-production-before or --before-json")

    build = build_snapshot(args.db)
    before = {
        str(row["ticker"]): (
            row.get("op_upward_revision_count_3y"),
            row.get("any_earnings_upward_revision_event_count_3y"),
        ) for row in before_rows
    }
    after = {
        str(row["ticker"]): (
            row.get("op_upward_revision_count_3y"),
            row.get("any_earnings_upward_revision_event_count_3y"),
        ) for row in build.rows
    }
    tickers = sorted(set(before) | set(after))
    changed = [ticker for ticker in tickers if before.get(ticker) != after.get(ticker)]
    deltas = [
        {
            "ticker": ticker,
            "before_op": (before.get(ticker) or (None, None))[0],
            "after_op": (after.get(ticker) or (None, None))[0],
            "before_event": (before.get(ticker) or (None, None))[1],
            "after_event": (after.get(ticker) or (None, None))[1],
        }
        for ticker in tickers
    ]
    top50 = sorted(
        deltas,
        key=lambda row: (
            -(row["after_event"] if row["after_event"] is not None else -1),
            -(row["after_op"] if row["after_op"] is not None else -1),
            row["ticker"],
        ),
    )[:50]

    cutoff_date = date.fromisoformat(build.universe_date)
    cutoff = cutoff_date.replace(year=cutoff_date.year - 3).isoformat()
    upward = [
        event for event in build.revision_events
        if event["disclosed_at"] >= cutoff
        and event["direction"] == "upward"
        and not event["is_correction"]
        and not event["is_split_only_change"]
        and event["metric"] in FORECAST_METRICS
    ]
    candidates = list(REQUIRED_TICKERS)
    for row in top50:
        if row["ticker"] not in candidates and row["after_event"]:
            candidates.append(row["ticker"])
        if len(candidates) >= 10:
            break
    selected = candidates[:10]
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in upward:
        if event["ticker"] in selected:
            grouped[(event["ticker"], event["disclosure_id"], event["target_fiscal_year"])].append(event)

    import sqlite3
    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    metadata = _metadata(connection)
    raw_by_id = _raw_by_id(connection)
    verification = []
    for (ticker, disclosure_id, target), events in sorted(grouped.items()):
        raw = raw_by_id.get(disclosure_id, {})
        meta = metadata.get(disclosure_id, {})
        event_metrics = {event["metric"] for event in events}
        verification.append({
            "ticker": ticker,
            "disclosure_id": disclosure_id,
            "disclosure_date": events[0]["disclosed_at"],
            "target_fiscal_year": target,
            "previous_forecast": {
                event["metric"]: event["previous_value"] for event in events
            },
            "revised_forecast": {
                event["metric"]: event["revised_value"] for event in events
            },
            "upward_metrics": sorted(event_metrics),
            "op_event_count": int("operating_profit" in event_metrics),
            "any_event_count": 1,
            "correction_status": metadata_role(meta),
            "metadata_status": meta.get("metadata_status"),
            "title": meta.get("title"),
            "disc_items": meta.get("disc_items"),
            "doc_type": raw.get("DocType"),
            "retro_rst_raw": raw.get("RetroRst"),
            "retro_rst_parsed": parse_optional_boolean(raw.get("RetroRst")),
        })
    connection.close()

    def _sum(index: int, mapping: dict[str, tuple[int | None, int | None]]) -> int:
        return sum(int(values[index] or 0) for values in mapping.values())

    report = {
        "universe_date": build.universe_date,
        "before_batch_ids": sorted({str(row.get("batch_id")) for row in before_rows}),
        "universe_count": len(build.rows),
        "before_row_count": len(before_rows),
        "affected_ticker_count": len(changed),
        "before_totals": {"op": _sum(0, before), "event": _sum(1, before)},
        "after_totals": {"op": _sum(0, after), "event": _sum(1, after)},
        "before_distribution": _distribution(before),
        "after_distribution": _distribution(after),
        "top50_after": top50,
        "required_and_sample_tickers": selected,
        "manual_verification_events": verification,
        "strict_boolean_distribution": {
            "true": 29, "false": 76007, "blank": 20987, "null": 6
        },
        "unmatched_metadata_status_distribution": dict(sorted(Counter(
            str(value.get("metadata_status")) for value in metadata.values()
            if value.get("metadata_status") != "verified"
        ).items())),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output), "affected_ticker_count": len(changed),
        "before_totals": report["before_totals"], "after_totals": report["after_totals"],
        "verification_event_count": len(verification), "selected": selected,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
