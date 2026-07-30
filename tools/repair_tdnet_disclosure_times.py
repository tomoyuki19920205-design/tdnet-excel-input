#!/usr/bin/env python3
"""Repair date-only TDNET event timestamps from the official TDNET listing."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.pipeline.db import load_env, supabase_select, supabase_update
from src.tdnet_disclosure_time import canonicalize_tdnet_url, fetch_official_listing_times

logger = logging.getLogger("repair_tdnet_disclosure_times")


def load_candidates(date_iso: str) -> list[dict]:
    """Load only rows whose stored timestamp is UTC midnight on the JST date."""
    midnight = f"{date_iso}T00:00:00+00:00"
    return supabase_select(
        "tdnet_events",
        params={
            "select": "id,ticker,event_type,event_subtype,headline,source_url,disclosed_at,detected_at",
            "disclosed_at": f"eq.{midnight}",
            "order": "id.asc",
            "limit": "1000",
        },
    )


def build_repair_plan(rows: list[dict], date_iso: str) -> tuple[list[dict], list[dict]]:
    """Map corrupt events to official timestamps without changing persistence."""
    official_times = fetch_official_listing_times(date_iso)
    updates: list[dict] = []
    unresolved: list[dict] = []
    for row in rows:
        url = str(row.get("source_url") or "")
        official = official_times.get(canonicalize_tdnet_url(url))
        if not official:
            unresolved.append({"id": row.get("id"), "ticker": row.get("ticker"), "source_url": url})
            continue
        updates.append({"id": row["id"], "disclosed_at": official, "detected_at": official})
    return updates, unresolved


def apply_plan(updates: list[dict]) -> None:
    for update in updates:
        result = supabase_update(
            "tdnet_events",
            {"disclosed_at": update["disclosed_at"], "detected_at": update["detected_at"]},
            params={"id": f"eq.{update['id']}"},
        )
        # Legacy helper returns bool; newer implementations may return a
        # result dictionary.  Treat both forms explicitly so a completed row
        # never aborts the resumable repair loop.
        ok = result if isinstance(result, bool) else result.get("ok")
        if not ok:
            detail = None if isinstance(result, bool) else result.get("error")
            raise RuntimeError(f"update failed for {update['id']}: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="JST calendar date, YYYY-MM-DD")
    parser.add_argument("--apply", action="store_true", help="perform the verified updates")
    args = parser.parse_args()
    load_env(str(PROJECT_ROOT))

    rows = load_candidates(args.date)
    updates, unresolved = build_repair_plan(rows, args.date)
    report = {
        "date_jst": args.date,
        "candidate_count": len(rows),
        "resolved_count": len(updates),
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
        "apply": args.apply,
    }
    if unresolved:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    if args.apply:
        apply_plan(updates)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
