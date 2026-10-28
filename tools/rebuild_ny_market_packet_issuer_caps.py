#!/usr/bin/env python3
"""Version an unpublished packet with evidence-backed issuer-total caps."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.ny_market_issuer_cap import issuer_cap_from_evidence
from lib.ny_market_research import market_data_packet_sha256
from tools.company_news_atomic import atomic_write_json


def rebuild(source: dict, evidence: dict) -> dict:
    packet = deepcopy(source)
    session = date.fromisoformat(packet["market_session_date"])
    top = packet.get("top_gainers_20")
    if not isinstance(top, list) or len(top) != 20:
        raise ValueError("source packet needs exactly twenty Top20 entries")
    expected = {row["ticker"] for row in top}
    if set(evidence) != expected:
        raise ValueError(f"issuer evidence ticker mismatch: missing={sorted(expected-set(evidence))}, extra={sorted(set(evidence)-expected)}")
    for row in top:
        ticker = row["ticker"]
        action = (row.get("basis_evidence") or {}).get("official_action")
        cap, method, components, details = issuer_cap_from_evidence(
            ticker, row["close"], session, evidence[ticker], action,
        )
        row.update(market_cap=cap, market_cap_method=method,
                   share_class_components=components, market_cap_evidence=details)
        row["review_flags"] = [flag for flag in row.get("review_flags", []) if flag != "issuer_total_market_cap_review"]
        if method == "unavailable" and "issuer_total_unavailable" not in row["review_flags"]:
            row["review_flags"].append("issuer_total_unavailable")
    packet["market_data_contract_version"] = "ny_market_data_v2"
    packet["issuer_cap_generated_at"] = datetime.now(timezone.utc).isoformat()
    packet["derived_from_packet_sha256"] = market_data_packet_sha256(source)
    return packet


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-packet", type=Path, required=True)
    parser.add_argument("--issuer-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite packet version: {args.output}")
    source_bytes = args.source_packet.read_bytes()
    source = json.loads(source_bytes)
    evidence = json.loads(args.issuer_evidence.read_text(encoding="utf-8"))
    packet = rebuild(source, evidence)
    packet["derived_from_file_sha256"] = hashlib.sha256(source_bytes).hexdigest()
    atomic_write_json(args.output, packet)
    print(json.dumps({"output": str(args.output), "sha256": market_data_packet_sha256(packet),
                      "parent_sha256": packet["derived_from_packet_sha256"],
                      "parent_file_sha256": packet["derived_from_file_sha256"],
                      "verified": sum(row["market_cap_method"] != "unavailable" for row in packet["top_gainers_20"]),
                      "unavailable": [row["ticker"] for row in packet["top_gainers_20"] if row["market_cap_method"] == "unavailable"]},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
