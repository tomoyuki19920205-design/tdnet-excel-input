#!/usr/bin/env python3
"""Sync daily NY market canonical tables from SQLite to Supabase."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.ny_market import connect_db, rows_for_sync
from lib.pipeline.db import (
    get_supabase_read_config,
    get_supabase_write_config,
    supabase_select,
    supabase_upsert,
)
from lib.production_environment import bootstrap_production_write_environment


def _filter_rows(
    rows: list[dict], stable_keys: set[str] | None
) -> list[dict]:
    if stable_keys is None:
        return rows
    return [row for row in rows if row.get("stable_key") in stable_keys]


def _remote_snapshot(
    tables: Iterable[str], stable_keys: set[str], config: dict
) -> dict[str, dict[str, list[dict]]]:
    snapshots: dict[str, dict[str, list[dict]]] = {}
    for table in tables:
        table_snapshot: dict[str, list[dict]] = {}
        for stable_key in sorted(stable_keys):
            rows = supabase_select(
                table,
                params={"stable_key": f"eq.{stable_key}", "select": "*"},
                config=config,
            )
            if len(rows) > 1:
                raise RuntimeError(
                    f"{table} preflight found duplicate stable_key: {stable_key}"
                )
            table_snapshot[stable_key] = rows
        snapshots[table] = table_snapshot
    return snapshots


def _delete_remote_rows(table: str, stable_key: str, config: dict) -> bool:
    import requests

    response = requests.delete(
        f"{config['rest_url']}/{table}",
        params={"stable_key": f"eq.{stable_key}"},
        headers={**config["headers"], "Prefer": "return=minimal"},
        timeout=(10, 60),
    )
    return response.status_code in (200, 204)


def _restore_remote_snapshot(
    table: str,
    table_snapshot: dict[str, list[dict]],
    *,
    conflict: str,
    config: dict,
) -> list[str]:
    errors: list[str] = []
    for stable_key, prior_rows in table_snapshot.items():
        if prior_rows:
            restored = supabase_upsert(
                table, prior_rows, on_conflict=conflict, config=config
            )
            if not restored.get("ok"):
                errors.append(f"{table}/{stable_key}: restore failed")
        elif not _delete_remote_rows(table, stable_key, config):
            errors.append(f"{table}/{stable_key}: delete compensation failed")
    return errors


def sync(
    db_path: Path,
    dry_run: bool = False,
    *,
    stable_keys: Iterable[str] | None = None,
    production_root: Path | None = None,
) -> dict[str, int]:
    selected_keys = set(stable_keys) if stable_keys is not None else None
    if not dry_run:
        if production_root is None:
            raise RuntimeError("production_root is required for NY market writes")
        bootstrap_production_write_environment(production_root)
    conn = connect_db(db_path)
    try:
        tables = ("canonical_ny_market_reports", "canonical_ny_market_report_runs")
        batches = {
            table: _filter_rows(rows_for_sync(conn, table), selected_keys)
            for table in tables
        }
    finally:
        conn.close()
    if dry_run:
        return {table: len(rows) for table, rows in batches.items()}
    config = get_supabase_write_config()
    if not config:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY is not configured")
    conflicts = {"canonical_ny_market_reports": "stable_key", "canonical_ny_market_report_runs": "run_id"}
    snapshots = (
        _remote_snapshot(tables, selected_keys, get_supabase_read_config())
        if selected_keys is not None else {}
    )
    result: dict[str, int] = {}
    attempted: list[str] = []
    try:
        for table, rows in batches.items():
            if not rows:
                result[table] = 0
                continue
            attempted.append(table)
            response = supabase_upsert(table, rows, on_conflict=conflicts[table], config=config)
            if not response.get("ok"):
                raise RuntimeError(f"{table} sync failed: {response.get('error', 'unknown')}")
            result[table] = int(response.get("count", 0))
    except Exception as exc:
        rollback_errors: list[str] = []
        for table in reversed(attempted):
            table_snapshot = snapshots.get(table)
            if table_snapshot is None:
                rollback_errors.append(f"{table}: no preflight snapshot")
                continue
            rollback_errors.extend(_restore_remote_snapshot(
                table,
                table_snapshot,
                conflict=conflicts[table],
                config=config,
            ))
        if rollback_errors:
            raise RuntimeError(
                f"NY market sync failed and compensation was incomplete: {'; '.join(rollback_errors)}"
            ) from exc
        raise RuntimeError("NY market sync failed; remote snapshot restored") from exc
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "decision_db.db")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stable-key", action="append")
    parser.add_argument("--production-root", type=Path)
    args = parser.parse_args()
    print(sync(
        args.db,
        args.dry_run,
        stable_keys=args.stable_key,
        production_root=args.production_root,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
