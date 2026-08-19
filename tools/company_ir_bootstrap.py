#!/usr/bin/env python3
"""One-time resumable bootstrap: continuously run bounded discovery batches."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.company_ir_source_discovery import discovery_report, init_discovery_db


def main() -> int:
    parser = argparse.ArgumentParser(description="Continuous resumable company IR bootstrap")
    parser.add_argument("--db", default="data/company_ir_monitor.db")
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--batch-delay-seconds", type=float, default=30.0)
    parser.add_argument("--request-interval", type=float, default=0.2)
    parser.add_argument("--max-batches", type=int)
    args = parser.parse_args()
    if args.batch_size <= 0 or args.batch_delay_seconds < 0 or args.request_interval < 0:
        parser.error("batch-size must be positive and batch delay non-negative")

    db_path = ROOT / args.db
    batches = 0

    def run_monitor(*extra: str) -> int:
        command = [
            sys.executable, "-X", "utf8", str(ROOT / "tools/company_ir_nightly.py"),
            "--db", args.db, "--workers", str(args.workers), *extra,
            "--request-interval", str(args.request_interval),
        ]
        return subprocess.run(command, cwd=ROOT, check=False).returncode

    # Covers sources discovered by an earlier interrupted/legacy run. New
    # sources are baselined inside each discovery batch below.
    rc = run_monitor("--baseline-only")
    if rc:
        return rc
    while args.max_batches is None or batches < args.max_batches:
        command = [
            sys.executable, "-X", "utf8", str(ROOT / "tools/company_ir_source_discovery.py"),
            "--db", args.db, "--batch-size", str(args.batch_size),
            "--workers", str(args.workers),
            "--request-interval", str(args.request_interval),
        ]
        completed = subprocess.run(command, cwd=ROOT, check=False)
        if completed.returncode:
            print(f"COMPANY_IR_BOOTSTRAP batch={batches + 1} rc={completed.returncode} status=stopped", flush=True)
            return completed.returncode
        batches += 1
        conn = sqlite3.connect(db_path)
        try:
            init_discovery_db(conn)
            report = discovery_report(conn)
        finally:
            conn.close()
        print("COMPANY_IR_BOOTSTRAP " + json.dumps({"batches_completed": batches, **report}, ensure_ascii=False, sort_keys=True), flush=True)
        if report["first_discovery_pass_complete"]:
            # First persist post-baseline arrivals as pending with the gate
            # still OFF, then perform the requested no-write verification pass.
            rc = run_monitor()
            if rc:
                return rc
            return run_monitor("--dry-run")
        time.sleep(args.batch_delay_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
