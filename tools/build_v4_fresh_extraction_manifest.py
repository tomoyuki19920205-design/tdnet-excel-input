from __future__ import annotations

import argparse
import json
from pathlib import Path

from lib.backfill.fresh_campaign_manifest_adapter import build, write


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-db", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--output-manifest", type=Path, required=True)
    parser.add_argument("--write-output", action="store_true")
    parser.add_argument("--confirm-write-output", action="store_true")
    args = parser.parse_args()
    if not args.write_output or not args.confirm_write_output:
        raise SystemExit(2)
    output = args.output_manifest.resolve()
    if not str(output).lower().startswith("c:\\tmp\\"):
        raise SystemExit(2)
    rows = build(
        args.campaign_db, args.campaign_id, args.cache_root,
        source_manifest=args.source_manifest, state_db=args.state_db,
    )
    print(json.dumps({"count": len(rows), "sha256": write(rows, output)}, sort_keys=True))


if __name__ == "__main__":
    main()
