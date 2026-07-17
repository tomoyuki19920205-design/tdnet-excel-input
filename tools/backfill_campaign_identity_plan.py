"""Generate a read-only V4 campaign identity-resolution plan."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from lib.backfill.campaign_identity_plan import (
    IdentityPlanStop,
    build_plan,
    sha256_file,
    write_plan,
)


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-db", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--jquants-db", required=True)
    parser.add_argument("--legacy-state-db", required=True)
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--campaign-db-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    try:
        campaign_db = Path(args.campaign_db)
        jquants_db = Path(args.jquants_db)
        legacy_state_db = Path(args.legacy_state_db)
        cache_root = Path(args.cache_root)
        output_dir = Path(args.output_dir)
        for path in (campaign_db, jquants_db, legacy_state_db, cache_root, output_dir):
            if not path.is_absolute():
                raise IdentityPlanStop("STOP_V4_CAMPAIGN_IDENTITY_INPUT_INVALID")
        if args.campaign_db_sha256 and len(args.campaign_db_sha256) != 64:
            raise IdentityPlanStop("STOP_V4_CAMPAIGN_IDENTITY_INPUT_INVALID")
        head = _git(repo_root, "rev-parse", "HEAD")
        tracked = _git(repo_root, "diff", "--name-only")
        staged = _git(repo_root, "diff", "--cached", "--name-only")
        allowed = {
            "lib/backfill/campaign_identity_plan.py",
            "tools/backfill_campaign_identity_plan.py",
            "tests/test_backfill_campaign_identity_plan.py",
        }
        dirty = {line.replace("\\", "/") for line in (tracked + "\n" + staged).splitlines() if line}
        if dirty - allowed:
            raise IdentityPlanStop("STOP_V4_CAMPAIGN_IDENTITY_PLAN_SCOPE_VIOLATION")
        rows, source_schema = build_plan(
            campaign_db=campaign_db,
            campaign_id=args.campaign_id,
            jquants_db=jquants_db,
            legacy_state_db=legacy_state_db,
            cache_root=cache_root,
            expected_count=args.expected_count,
            campaign_db_sha256=args.campaign_db_sha256,
        )
        execution = {
            "git_head": head,
            "campaign_db_sha256": sha256_file(campaign_db),
            "jquants_db_sha256": sha256_file(jquants_db),
            "legacy_state_db_sha256": sha256_file(legacy_state_db),
            "input_count": len(rows), "network_calls": 0,
            "db_writes": 0, "cache_writes": 0, "zip_downloads": 0,
        }
        result = write_plan(
            output_dir=output_dir, rows=rows, source_schema=source_schema,
            execution=execution, repo_root=repo_root,
        )
    except (IdentityPlanStop, OSError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
