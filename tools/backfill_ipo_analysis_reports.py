#!/usr/bin/env python3
"""Generate or retry idempotent Company Viewer IPO analysis reports."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.pipeline.db import load_env
from src.ipo_analysis_report import process_pending_reports, seed_existing_ipo_cards


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="today", help="YYYY-MM-DD or today")
    parser.add_argument("--tickers", default="", help="comma-separated normalized or TDnet codes")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--retry-partial", action="store_true")
    parser.add_argument("--seed-existing", action="store_true")
    parser.add_argument("--best-effort", action="store_true")
    args = parser.parse_args()
    load_env(os.environ.get("TDNET_SETTINGS_ROOT", str(Path(__file__).resolve().parents[1])))
    target_date = date.today().isoformat() if args.date == "today" else args.date
    tickers = [value.strip() for value in args.tickers.split(",") if value.strip()]
    try:
        seed = seed_existing_ipo_cards(target_date, tickers) if args.seed_existing and args.apply else None
        result = process_pending_reports(listing_date=target_date, tickers=tickers,
                                         apply=args.apply, pending_only=True,
                                         retry_partial=args.retry_partial)
        print(json.dumps({"seed": seed, **result}, ensure_ascii=False, indent=2))
        return 0 if args.best_effort or not result["failed"] else 1
    except Exception as exc:
        logging.exception("IPO analysis backfill failed independently")
        if args.best_effort:
            print(json.dumps({"processed": 0, "failed": 1, "error": str(exc)}, ensure_ascii=False))
            return 0
        raise


if __name__ == "__main__":
    raise SystemExit(main())
