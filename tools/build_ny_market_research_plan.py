#!/usr/bin/env python3
"""Create generic first/second-pass catalyst queries from a canonical packet."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.ny_market_research import build_catalyst_search_plan, market_data_packet_sha256


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("packet", type=Path)
    args = parser.parse_args()
    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    plans = [{
        "ticker": item["ticker"], "company_name": item["company_name"],
        "queries": build_catalyst_search_plan(
            item["ticker"], item["company_name"], packet["market_session_date"],
        ),
    } for item in packet["top_gainers_20"]]
    print(json.dumps({
        "market_data_packet_sha256": market_data_packet_sha256(packet), "plans": plans,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
