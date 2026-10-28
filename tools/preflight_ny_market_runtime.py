#!/usr/bin/env python3
"""Read-only Production preflight for the NY market runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.pipeline.db import get_supabase_read_config
from lib.production_environment import bootstrap_production_write_environment


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _strict_select(table: str, *, params: dict[str, str], config: dict[str, str]) -> list[dict]:
    """Keep HTTP failures distinct from a valid empty Production result."""
    import requests

    response = requests.get(
        f"{config['rest_url']}/{table}",
        params=params,
        headers=config["headers"],
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError(f"Production {table} response is not a row list")
    return rows


def _resilient_select(table: str, *, params: dict[str, str], config: dict[str, str]) -> list[dict]:
    """Retry transient connection failures, never convert them to empty rows."""
    import requests

    for attempt, delay in enumerate((0, 2, 5, 10)):
        if delay:
            time.sleep(delay)
        try:
            return _strict_select(table, params=params, config=config)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt == 3:
                raise
    raise AssertionError("unreachable")


def _sqlite_snapshot(db_path: Path, stable_key: str) -> dict[str, object]:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = conn.execute(
            "SELECT report_markdown, quality_status FROM canonical_ny_market_reports WHERE stable_key = ?",
            (stable_key,),
        ).fetchall()
        runs = conn.execute(
            "SELECT status, attempt FROM canonical_ny_market_report_runs WHERE stable_key = ?",
            (stable_key,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "report_count": len(report),
        "run_count": len(runs),
        "markdown_sha256": _sha256_text(report[0]["report_markdown"]) if len(report) == 1 else None,
        "run_status": runs[0]["status"] if len(runs) == 1 else None,
        "run_attempt": runs[0]["attempt"] if len(runs) == 1 else None,
        "quality_status": report[0]["quality_status"] if len(report) == 1 else None,
    }


def preflight(production_root: Path, db_path: Path, stable_key: str) -> dict[str, object]:
    environment = bootstrap_production_write_environment(production_root)
    config = get_supabase_read_config()
    remote_report = _resilient_select(
        "canonical_ny_market_reports",
        params={"stable_key": f"eq.{stable_key}", "select": "report_markdown,quality_status,unresolved_items"},
        config=config,
    )
    remote_runs = _resilient_select(
        "canonical_ny_market_report_runs",
        params={"stable_key": f"eq.{stable_key}", "select": "status,attempt"},
        config=config,
    )
    report_date = stable_key.removeprefix("ny_market_daily:")
    remote_viewer = _resilient_select(
        "api_latest_news_stream",
        params={"report_type": "eq.ny_market_daily", "report_date_jst": f"eq.{report_date}", "select": "*"},
        config=config,
    )
    sqlite = _sqlite_snapshot(db_path, stable_key)
    remote_hash = _sha256_text(remote_report[0]["report_markdown"]) if len(remote_report) == 1 else None
    viewer_hash = _sha256_text(remote_viewer[0]["report_markdown"]) if len(remote_viewer) == 1 else None
    complete_readback_verified = (
        len(remote_report) == len(remote_runs) == len(remote_viewer) == 1
        and sqlite["report_count"] == sqlite["run_count"] == 1
        and remote_report[0].get("quality_status") == "complete"
        and remote_runs[0].get("status") == sqlite["run_status"] == "success"
        and remote_viewer[0].get("quality_status") == "complete"
        and remote_hash == viewer_hash == sqlite["markdown_sha256"]
    )
    return {
        "status": "ready",
        "complete_readback_verified": complete_readback_verified,
        "environment": environment.safe_metadata(),
        "sqlite": sqlite,
        "supabase": {
            "report_count": len(remote_report),
            "run_count": len(remote_runs),
            "markdown_sha256": remote_hash,
            "quality_status": remote_report[0].get("quality_status") if len(remote_report) == 1 else None,
            "unresolved_items": remote_report[0].get("unresolved_items") if len(remote_report) == 1 else None,
            "run_status": remote_runs[0]["status"] if len(remote_runs) == 1 else None,
            "run_attempt": remote_runs[0]["attempt"] if len(remote_runs) == 1 else None,
            "viewer_count": len(remote_viewer),
            "viewer_markdown_sha256": viewer_hash,
            "viewer_quality_status": remote_viewer[0].get("quality_status") if len(remote_viewer) == 1 else None,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-root", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--stable-key", required=True)
    args = parser.parse_args()
    try:
        result = preflight(args.production_root, args.db, args.stable_key)
    except Exception as exc:
        print(json.dumps({
            "status": "blocked", "stage": "production_preflight",
            "error_type": type(exc).__name__,
        }, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
