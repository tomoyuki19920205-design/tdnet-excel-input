"""Apply a V4 campaign identity plan to an explicitly temporary DB copy."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lib.backfill.campaign_identity_apply import apply_identity_plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-db", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--plan-results", required=True)
    parser.add_argument("--plan-results-sha256", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    try:
        result = apply_identity_plan(
            campaign_db=Path(args.campaign_db), campaign_id=args.campaign_id,
            plan_results=Path(args.plan_results),
            plan_results_sha256=args.plan_results_sha256,
            output_dir=Path(args.output_dir), apply=args.apply,
            repo_root=repo_root,
        )
    except (RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
