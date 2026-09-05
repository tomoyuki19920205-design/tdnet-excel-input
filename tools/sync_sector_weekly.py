#!/usr/bin/env python3
"""Sync additive sector-weekly canonical tables from SQLite to Supabase."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline.db import get_supabase_write_config, load_env, supabase_upsert
from lib.sector_weekly import connect_sector_db, rows_for_sync, rows_for_sync_all


TABLES = ("canonical_sector_reports", "canonical_sector_report_runs")
CONFLICTS = {"canonical_sector_reports": "dedupe_key", "canonical_sector_report_runs": "run_id"}


def _validate_one(
    batches: dict[str, list[dict]],
    *,
    dedupe_key: str,
    run_id: str,
) -> None:
    reports = batches["canonical_sector_reports"]
    runs = batches["canonical_sector_report_runs"]
    if len(reports) != 1:
        raise RuntimeError(f"expected exactly one sector report for dedupe_key; found {len(reports)}")
    if len(runs) != 1:
        raise RuntimeError(f"expected exactly one sector run for run_id; found {len(runs)}")
    report, run = reports[0], runs[0]
    if report.get("dedupe_key") != dedupe_key or report.get("run_id") != run_id:
        raise RuntimeError("sector report keys do not match requested sync keys")
    if run.get("run_id") != run_id or run.get("dedupe_key") != dedupe_key:
        raise RuntimeError("sector run keys do not match requested sync keys")
    identity = ("sector_code", "sector_name")
    if any(report.get(field) != run.get(field) for field in identity):
        raise RuntimeError("sector report and run identity do not match")
    for field in ("period_start", "period_end"):
        try:
            report_time = datetime.fromisoformat(str(report.get(field)).replace("Z", "+00:00"))
            run_time = datetime.fromisoformat(str(run.get(field)).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("sector report and run period is invalid") from exc
        if report_time.astimezone(timezone.utc) != run_time.astimezone(timezone.utc):
            raise RuntimeError("sector report and run identity do not match")


def _send_batches(batches: dict[str, list[dict]], *, dry_run: bool) -> dict[str, int]:
    if dry_run:
        return {table: len(rows) for table, rows in batches.items()}
    load_env()
    config = get_supabase_write_config()
    if not config:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY is not configured")
    result: dict[str, int] = {}
    for table, rows in batches.items():
        if not rows:
            result[table] = 0
            continue
        response = supabase_upsert(table, rows, on_conflict=CONFLICTS[table], config=config)
        if not response.get("ok"):
            raise RuntimeError(f"{table} sync failed: {response.get('error', 'unknown')}")
        synced = int(response.get("count", 0))
        if synced != len(rows):
            raise RuntimeError(f"{table} sync count mismatch: expected {len(rows)}, got {synced}")
        result[table] = synced
    return result


def sync_one(
    db_path: Path,
    dedupe_key: str,
    run_id: str,
    dry_run: bool = False,
) -> dict[str, int]:
    if not isinstance(dedupe_key, str) or not dedupe_key.strip():
        raise ValueError("dedupe_key is required")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id is required")
    conn = connect_sector_db(db_path)
    try:
        batches = {
            "canonical_sector_reports": rows_for_sync(
                conn, "canonical_sector_reports", key=dedupe_key,
            ),
            "canonical_sector_report_runs": rows_for_sync(
                conn, "canonical_sector_report_runs", key=run_id,
            ),
        }
    finally:
        conn.close()
    _validate_one(batches, dedupe_key=dedupe_key, run_id=run_id)
    return _send_batches(batches, dry_run=dry_run)


def sync_all(db_path: Path, dry_run: bool = False) -> dict[str, int]:
    """Explicit maintenance-only full synchronization."""
    conn = connect_sector_db(db_path)
    try:
        batches = {table: rows_for_sync_all(conn, table) for table in TABLES}
    finally:
        conn.close()
    return _send_batches(batches, dry_run=dry_run)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "decision_db.db")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dedupe-key")
    parser.add_argument("--run-id")
    parser.add_argument("--all", action="store_true", help="explicitly sync every canonical sector row")
    args = parser.parse_args()
    if args.all:
        if args.dedupe_key or args.run_id:
            parser.error("--all cannot be combined with row keys")
        result = sync_all(args.db, args.dry_run)
    else:
        if not args.dedupe_key or not args.run_id:
            parser.error("--dedupe-key and --run-id are required unless --all is explicit")
        result = sync_one(args.db, args.dedupe_key, args.run_id, args.dry_run)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
