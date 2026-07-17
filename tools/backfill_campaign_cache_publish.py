#!/usr/bin/env python3
"""CLI for isolated V4 campaign cache publication."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.backfill.campaign_cache_publish import publish_campaign_cache


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish verified V4 campaign cache files atomically")
    parser.add_argument("--campaign-db", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--manifest-list", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    try:
        result = publish_campaign_cache(
            campaign_db=args.campaign_db, campaign_id=args.campaign_id,
            cache_root=args.cache_root, manifest_list=args.manifest_list,
            output_dir=args.output_dir, apply=args.apply, repo_root=repo_root,
        )
    except RuntimeError as exc:
        print(str(exc))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
