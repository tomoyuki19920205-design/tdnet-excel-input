#!/usr/bin/env python3
"""Validate a generated NY report and atomically publish it to news_inbox."""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.ny_market import validate_payload
from lib.ny_market_research import verify_market_data_packet_projection
from tools.company_news_atomic import atomic_write_json


def _load_and_validate(input_path: Path, market_data_packet_path: Path) -> dict:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    packet = json.loads(market_data_packet_path.read_text(encoding="utf-8"))
    verify_market_data_packet_projection(payload, packet)
    return validate_payload(payload).payload


def publish(input_path: Path, inbox: Path, market_data_packet_path: Path) -> Path:
    payload = _load_and_validate(input_path, market_data_packet_path)
    validated = validate_payload(payload)
    compact_date = validated.report["report_date_jst"].replace("-", "")
    run_id = uuid.uuid4().hex
    inbox.mkdir(parents=True, exist_ok=True)
    target = inbox / f"ny_market_daily_{compact_date}_{run_id}.json"
    if not re.fullmatch(r"ny_market_daily_\d{8}_[0-9a-f]{32}\.json", target.name):
        raise ValueError("unsafe NY market inbox filename")
    atomic_write_json(target, validated.payload)
    return target


def validate_only(input_path: Path, market_data_packet_path: Path) -> dict[str, str]:
    """Validate without touching inbox, SQLite, Supabase, or frontend state."""
    payload = _load_and_validate(input_path, market_data_packet_path)
    validated = validate_payload(payload)
    return {
        "status": "valid",
        "stable_key": validated.report["stable_key"],
        "report_markdown_sha256": validated.payload["report_delivery"]["sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--market-data-packet", required=True, type=Path)
    parser.add_argument("--inbox", type=Path, default=ROOT / "data" / "news_inbox")
    parser.add_argument("--validate-only", action="store_true", help="validate only; perform no writes")
    parser.add_argument("--emit-report", action="store_true", help="emit the exact canonical report_markdown")
    args = parser.parse_args()
    if args.validate_only:
        result = validate_only(args.input, args.market_data_packet)
        if args.emit_report:
            print(_load_and_validate(args.input, args.market_data_packet)["report_markdown"])
        else:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    target = publish(args.input, args.inbox, args.market_data_packet)
    if args.emit_report:
        print(_load_and_validate(args.input, args.market_data_packet)["report_markdown"])
    else:
        print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
