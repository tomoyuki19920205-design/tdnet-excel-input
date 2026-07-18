#!/usr/bin/env python3
"""Run bounded production V4 Fresh Download chunks through the formal child CLI."""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.backfill.campaign_fresh_download_loop import FreshDownloadLoopStop, run_loop


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-db", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--download-plan", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--parent-output-dir", type=Path, required=True)
    parser.add_argument("--chunk-size", type=int, required=True)
    parser.add_argument("--max-chunks", type=int, required=True)
    parser.add_argument("--min-idle-window-minutes", type=float, required=True)
    parser.add_argument("--source-route", choices=("JQUANTS_TD_FILES",), required=True)
    parser.add_argument("--confirm-production-cache-root", required=True)
    parser.add_argument("--confirm-campaign-id", required=True)
    parser.add_argument("--confirm-max-chunks", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--production-apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_loop(
            campaign_db=args.campaign_db, campaign_id=args.campaign_id,
            download_plan=args.download_plan, cache_root=args.cache_root,
            parent_output_dir=args.parent_output_dir, chunk_size=args.chunk_size,
            max_chunks=args.max_chunks, min_idle_window_minutes=args.min_idle_window_minutes,
            source_route=args.source_route, confirm_production_cache_root=args.confirm_production_cache_root,
            confirm_campaign_id=args.confirm_campaign_id, confirm_max_chunks=args.confirm_max_chunks,
            apply=args.apply, production_apply=args.production_apply,
            repo_root=Path(__file__).resolve().parents[1],
        )
    except (FreshDownloadLoopStop, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
