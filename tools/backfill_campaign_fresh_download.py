#!/usr/bin/env python3
"""Download a manifest-scoped V4 campaign ZIP canary into temporary cache."""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.backfill.campaign_fresh_downloader import (
    FreshDownloaderStop,
    run_downloads,
    run_production_downloads,
)


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-db", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--download-plan", type=Path, required=True)
    parser.add_argument("--manifest-list", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-route", choices=("JQUANTS_TD_FILES",), required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--production-apply", action="store_true")
    parser.add_argument("--campaign-db-sha256")
    parser.add_argument("--download-plan-sha256")
    parser.add_argument("--manifest-byte-sha256")
    parser.add_argument("--manifest-semantic-sha256")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--max-items", type=int)
    parser.add_argument("--confirm-production-cache-root")
    parser.add_argument("--confirm-campaign-id")
    parser.add_argument("--min-interval-seconds", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--max-consecutive-failures", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    try:
        code_sha = _git(repo_root, "rev-parse", "HEAD")
        common = {
            "campaign_db": args.campaign_db, "campaign_id": args.campaign_id,
            "download_plan": args.download_plan, "manifest_list": args.manifest_list,
            "cache_root": args.cache_root, "output_dir": args.output_dir,
            "apply": args.apply, "repo_root": repo_root, "code_sha": code_sha,
            "min_interval_seconds": args.min_interval_seconds,
            "timeout_seconds": args.timeout_seconds, "max_retries": args.max_retries,
            "source_route": args.source_route,
        }
        if args.production_apply:
            result = run_production_downloads(
                **common, production_apply=True,
                campaign_db_sha256=args.campaign_db_sha256,
                download_plan_sha256=args.download_plan_sha256,
                manifest_byte_sha256=args.manifest_byte_sha256,
                manifest_semantic_sha256_value=args.manifest_semantic_sha256,
                expected_count=args.expected_count, max_items=args.max_items,
                confirm_production_cache_root=args.confirm_production_cache_root,
                confirm_campaign_id=args.confirm_campaign_id,
            )
        else:
            result = run_downloads(
                **common, max_consecutive_failures=args.max_consecutive_failures,
            )
    except (FreshDownloaderStop, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result.get("summary", result), ensure_ascii=False, sort_keys=True))
    if result.get("summary", {}).get("standard_failed"):
        print("STOP_V4_CAMPAIGN_FRESH_DOWNLOADER_STANDARD_CANARY_FAILED", file=sys.stderr)
        return 2
    if result.get("summary", {}).get("consecutive_failure_stop"):
        print("STOP_V4_CAMPAIGN_FRESH_DOWNLOADER_CONSECUTIVE_FAILURES", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
