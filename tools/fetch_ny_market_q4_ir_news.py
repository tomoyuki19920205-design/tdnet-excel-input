#!/usr/bin/env python3
"""Capture an issuer's Q4 investor-news chronology and raw source hashes."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.ny_market_ir import fetch_q4_ir_news


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ir-url", required=True)
    parser.add_argument("--issuer-name", required=True)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--market-session-date", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prefix", default="ny-ir")
    parser.add_argument("--identity-url", help="Same-origin official release with issuer name and exchange ticker")
    args = parser.parse_args()
    result = fetch_q4_ir_news(
        args.ir_url, args.issuer_name, args.ticker,
        args.market_session_date, args.output_dir, prefix=args.prefix,
        identity_url=args.identity_url,
    )
    print(json.dumps({
        "summary": str(args.output_dir / f"{args.prefix}-summary.json"),
        "feed_sha256": result["feed_raw"]["sha256"],
        "year_record_count": result["year_record_count"],
        "target_day_count": result["target_day_count"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
