#!/usr/bin/env python3
"""Create a read-only V4 campaign fresh-download plan."""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.backfill.campaign_fresh_download_plan import (
    FreshDownloadPlanStop,
    build_download_plan,
    load_campaign_rows,
    sha256_file,
    write_download_plan,
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
    parser.add_argument("--target-cache-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument("--campaign-db-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    try:
        if len(args.campaign_db_sha256) != 64:
            raise FreshDownloadPlanStop("STOP_V4_CAMPAIGN_FRESH_DOWNLOAD_CAMPAIGN_CHANGED")
        rows = load_campaign_rows(
            args.campaign_db, campaign_id=args.campaign_id,
            expected_count=args.expected_count,
            campaign_db_sha256=args.campaign_db_sha256,
        )
        plan, path_audit, duplicates = build_download_plan(
            rows, campaign_id=args.campaign_id,
            target_cache_root=args.target_cache_root,
        )
        execution = {
            "git_head": _git(repo_root, "rev-parse", "HEAD"),
            "campaign_db_path": str(args.campaign_db),
            "campaign_db_sha256": sha256_file(args.campaign_db),
            "campaign_id": args.campaign_id,
            "target_cache_root": str(args.target_cache_root),
            "expected_count": args.expected_count,
            "network_calls": 0, "db_writes": 0, "cache_writes": 0,
            "zip_accesses": 0, "downloads": 0,
            "implementation_sha256": {
                "lib/backfill/campaign_fresh_download_plan.py": sha256_file(
                    repo_root / "lib" / "backfill" / "campaign_fresh_download_plan.py"
                ),
                "tools/backfill_campaign_fresh_download_plan.py": sha256_file(
                    repo_root / "tools" / "backfill_campaign_fresh_download_plan.py"
                ),
            },
        }
        result = write_download_plan(
            output_dir=args.output_dir, repo_root=repo_root, rows=plan,
            path_audit=path_audit, duplicate_groups=duplicates,
            execution=execution,
        )
    except (FreshDownloadPlanStop, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
