#!/usr/bin/env python3
"""Build broad discovery and uniform primary verification queries for after-hours coverage."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.ny_market_after_hours import build_after_hours_discovery_plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market-session-date", required=True)
    parser.add_argument("--candidates", type=Path)
    args = parser.parse_args()
    candidates = None
    if args.candidates is not None:
        candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
    print(json.dumps(
        build_after_hours_discovery_plan(args.market_session_date, candidates),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
