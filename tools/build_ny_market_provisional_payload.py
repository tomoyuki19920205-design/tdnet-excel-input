#!/usr/bin/env python3
"""Render a clearly marked NY interim report from verified market sections."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.ny_market import render_provisional_markdown, report_markdown_sha256, validate_payload
from lib.ny_market_research import attach_market_data_packet_metadata
from tools.company_news_atomic import atomic_write_json


def build(packet: dict, report_date: date, market_status: str,
          additional_unresolved: list[dict] | None = None) -> dict:
    unresolved = list(packet.get("unresolved_items") or []) + list(additional_unresolved or [])
    unresolved.extend([
        {"field": "話題の値上がり・値下がり、主要決算、引け後発表、主要ニュース、コモディティ",
         "reason": "調査と照合が未完了のため、この暫定版には掲載していません。"},
    ])
    payload = {
        "schema_version": "ny_market_daily_v1", "report_type": "ny_market_daily",
        "report_date_jst": report_date.isoformat(),
        "market_session_date": packet["market_session_date"],
        "market_status": market_status,
        "stable_key": f"ny_market_daily:{report_date.isoformat()}",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "quality_status": "provisional", "unresolved_items": unresolved,
        "headline": f"【暫定版】NY市場モーニングレポート {report_date.isoformat()}",
        "summary_bullets": [], "index_moves": packet.get("indexes", []),
        "sector_moves": packet.get("sectors", []),
        "notable_gainers": [], "notable_losers": [], "top_gainers_20": [],
        "earnings": [], "after_hours_earnings": [], "major_news": [],
        "commodities": [], "canonical_market_data": packet,
    }
    attach_market_data_packet_metadata(payload)
    payload["report_markdown"] = render_provisional_markdown(payload)
    payload["report_delivery"] = {"source_field": "report_markdown",
                                  "sha256": report_markdown_sha256(payload["report_markdown"])}
    return validate_payload(payload).payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--report-date-jst", type=date.fromisoformat, required=True)
    parser.add_argument("--market-status", choices=("open", "holiday_or_weekend"), required=True)
    parser.add_argument("--additional-unresolved", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError(f"refusing to overwrite existing payload: {args.output}")
    packet = json.loads(args.packet.read_text(encoding="utf-8-sig"))
    extra = json.loads(args.additional_unresolved.read_text(encoding="utf-8-sig")) if args.additional_unresolved else None
    payload = build(packet, args.report_date_jst, args.market_status, extra)
    atomic_write_json(args.output, payload)
    print(json.dumps({"status": "provisional", "stable_key": payload["stable_key"],
                      "output": str(args.output), "sha256": payload["report_delivery"]["sha256"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
