#!/usr/bin/env python3
"""CLI entry point for the nightly company IR monitor."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.company_ir_monitor import import_sources_csv, init_db, notifications_enabled, run_monitor
from src.events.env_loader import load_project_env


def publisher_configuration_available() -> bool:
    """Whether the existing Supabase publisher has its required credentials."""
    return bool(
        os.environ.get("SUPABASE_URL", "").strip()
        and os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    )


def publisher_preflight_failure(
    *, gate: bool, dry_run: bool, pending: int, configured: bool
) -> bool:
    """Fail only when a production run already has durable publish work."""
    return gate and not dry_run and pending > 0 and not configured


def main() -> int:
    # Reuse the project-wide loader so Task Scheduler and direct CLI runs have
    # identical publish configuration without bespoke Company IR secret logic.
    load_project_env()
    parser = argparse.ArgumentParser(description="Nightly company IR material/video monitor")
    parser.add_argument("--db", default="data/company_ir_monitor.db")
    parser.add_argument("--sources", default="config/company_ir_sources.csv")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int)
    parser.add_argument("--request-interval", type=float)
    parser.add_argument(
        "--audit-output",
        help="Optional JSONL path for one auditable run; omitted during normal Nightly execution",
    )
    parser.add_argument(
        "--baseline-only", action="store_true",
        help="Fetch only sources whose initial baseline is not complete",
    )
    parser.add_argument(
        "--require-discovery-complete", action="store_true",
        help="Skip until no TSE company remains pending/official-only",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    db_path = ROOT / args.db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        imported = import_sources_csv(conn, ROOT / args.sources)
        if args.require_discovery_complete:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_ir_companies'"
            ).fetchone()
            unfinished = -1 if not table else conn.execute(
                "SELECT COUNT(*) FROM company_ir_companies WHERE discovery_status='pending'"
            ).fetchone()[0]
            if unfinished != 0:
                print("COMPANY_IR_NIGHTLY " + json.dumps({
                    "status": "skipped_discovery_incomplete",
                    "unfinished_companies": unfinished,
                    "sources_imported": imported,
                    "notifications_enabled": False,
                    "notified": 0,
                }, ensure_ascii=False, sort_keys=True))
                return 0
        gate = notifications_enabled(conn)
        publisher_configured = publisher_configuration_available()
        pending_before = conn.execute(
            "SELECT COUNT(*) FROM company_ir_assets "
            "WHERE notification_status='pending' AND notified=0"
        ).fetchone()[0]
        if publisher_preflight_failure(
            gate=gate,
            dry_run=args.dry_run,
            pending=pending_before,
            configured=publisher_configured,
        ):
            logging.error(
                "COMPANY_IR_PUBLISHER_UNAVAILABLE gate=ON pending=%s; "
                "pending assets remain unchanged",
                pending_before,
            )
            print("COMPANY_IR_NIGHTLY " + json.dumps({
                "status": "publisher_unavailable",
                "notifications_enabled": True,
                "publisher_configured": False,
                "pending": pending_before,
                "notified": 0,
                "publish_failed": pending_before,
            }, ensure_ascii=False, sort_keys=True))
            return 1
        if gate and not args.dry_run and not publisher_configured:
            logging.warning(
                "COMPANY_IR_PUBLISHER_UNAVAILABLE gate=ON pending=0; "
                "any newly discovered asset will remain pending and make this stage fail"
            )
        audit_records: list[dict[str, object]] | None = [] if args.audit_output else None
        run_id = datetime.now(timezone.utc).strftime("company_ir_%Y%m%dT%H%M%SZ")
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        stats = run_monitor(
            conn, dry_run=args.dry_run, baseline_only=args.baseline_only,
            allow_notifications=gate, max_workers=args.workers,
            request_interval_seconds=args.request_interval,
            audit_records=audit_records,
        )
        audit_path = None
        if args.audit_output:
            audit_path = Path(args.audit_output)
            if not audit_path.is_absolute():
                audit_path = ROOT / audit_path
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            with audit_path.open("w", encoding="utf-8", newline="\n") as handle:
                for record in audit_records or []:
                    handle.write(json.dumps({
                        "run_id": run_id,
                        "timestamp": timestamp,
                        **record,
                    }, ensure_ascii=False, sort_keys=True) + "\n")
        result = {
            "sources_imported": imported,
            "baseline_only": args.baseline_only,
            "notifications_enabled": gate,
            "publisher_configured": publisher_configured,
            "run_id": run_id,
            "audit_output": str(audit_path) if audit_path else None,
            **stats.__dict__,
        }
        print("COMPANY_IR_NIGHTLY " + json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1 if stats.publish_failed else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
