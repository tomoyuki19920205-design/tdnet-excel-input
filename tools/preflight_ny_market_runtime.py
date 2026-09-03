#!/usr/bin/env python3
"""Read-only Production preflight for the NY market runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.pipeline.db import get_supabase_read_config, supabase_select
from lib.production_environment import bootstrap_production_write_environment


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sqlite_snapshot(db_path: Path, stable_key: str) -> dict[str, object]:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = conn.execute(
            "SELECT report_markdown FROM canonical_ny_market_reports WHERE stable_key = ?",
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
    }


def preflight(production_root: Path, db_path: Path, stable_key: str) -> dict[str, object]:
    environment = bootstrap_production_write_environment(production_root)
    config = get_supabase_read_config()
    remote_report = supabase_select(
        "canonical_ny_market_reports",
        params={"stable_key": f"eq.{stable_key}", "select": "report_markdown"},
        config=config,
    )
    remote_runs = supabase_select(
        "canonical_ny_market_report_runs",
        params={"stable_key": f"eq.{stable_key}", "select": "status,attempt"},
        config=config,
    )
    return {
        "status": "ready",
        "environment": environment.safe_metadata(),
        "sqlite": _sqlite_snapshot(db_path, stable_key),
        "supabase": {
            "report_count": len(remote_report),
            "run_count": len(remote_runs),
            "markdown_sha256": (
                _sha256_text(remote_report[0]["report_markdown"])
                if len(remote_report) == 1 else None
            ),
            "run_status": remote_runs[0]["status"] if len(remote_runs) == 1 else None,
            "run_attempt": remote_runs[0]["attempt"] if len(remote_runs) == 1 else None,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-root", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--stable-key", required=True)
    args = parser.parse_args()
    print(json.dumps(
        preflight(args.production_root, args.db, args.stable_key),
        ensure_ascii=False,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
