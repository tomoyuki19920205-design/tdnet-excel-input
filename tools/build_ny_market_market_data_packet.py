#!/usr/bin/env python3
"""Build a deterministic canonical NY market-data packet before LLM research."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.ny_market_market_data import build_canonical_market_data_packet, LiveDiscrepancyArbitrator
from lib.ny_market_research import market_data_packet_sha256
from tools.company_news_atomic import atomic_write_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market-session-date", required=True, type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--issuer-components", type=Path)
    parser.add_argument("--corporate-action-notices", type=Path, help="ticker-to-official-Nasdaq-Trader-notice-URL JSON")
    args = parser.parse_args()
    issuer_components = None
    if args.issuer_components:
        issuer_components = json.loads(args.issuer_components.read_text(encoding="utf-8"))
        if not isinstance(issuer_components, dict):
            raise ValueError("issuer-components must be a ticker-to-components object")
    notices = json.loads(args.corporate_action_notices.read_text(encoding="utf-8")) if args.corporate_action_notices else {}
    if not isinstance(notices, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in notices.items()):
        raise ValueError("corporate-action-notices must map tickers to official URLs")
    packet = build_canonical_market_data_packet(
        args.market_session_date, issuer_components=issuer_components,
        discrepancy_arbitrator=LiveDiscrepancyArbitrator(corporate_action_notices=notices),
    )
    atomic_write_json(args.output, packet)
    print(json.dumps({
        "status": "created", "output": str(args.output),
        "market_data_packet_sha256": market_data_packet_sha256(packet),
        "indices": len(packet["indexes"]), "sectors": len(packet["sectors"]),
        "top20": len(packet["top_gainers_20"]),
        "discrepancy_count": packet["discrepancy_count"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
