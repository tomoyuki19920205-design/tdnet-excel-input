#!/usr/bin/env python3
"""Store Tokyo-only old weekly buy amounts from validated JPX/NSE reports.

Both publishers report contract value in million yen. The historical JPX series
is Tokyo+Nagoya; subtract same-date NSE Nagoya before joining to TSE balances.
The official 2026-09-18 single-week JPX Tokyo figure independently verifies
the subtraction. Issue-level/sector amounts remain unavailable before 09-25.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date
import logging
from pathlib import Path
import requests
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.pipeline.db import get_supabase_write_config, load_env
from tools.sync_margin_interest import rpc

SOURCE = Path(__file__).resolve().parent / "reference" / "margin_weekly_tokyo_2026q3.csv"
LOG = logging.getLogger("margin_weekly_jpx_market")


def source_rows() -> list[dict]:
    with SOURCE.open(encoding="utf-8", newline="") as stream:
        records = list(csv.DictReader(stream))
    expected = (date(2026, 7, 17), date(2026, 7, 24), date(2026, 7, 31),
                date(2026, 8, 7), date(2026, 8, 14), date(2026, 8, 21),
                date(2026, 8, 28), date(2026, 9, 4), date(2026, 9, 11),
                date(2026, 9, 18))
    if tuple(date.fromisoformat(row["date"]) for row in records) != expected:
        raise ValueError("official source dates incomplete or duplicated")
    for row in records:
        combined = int(row["jpx_two_market_million_yen"])
        nagoya = int(row["nse_nagoya_million_yen"])
        if combined <= nagoya or nagoya < 0 or not row["jpx_url"].startswith("https://www.jpx.co.jp/") \
                or not row["nse_url"].startswith("https://www.nse.or.jp/"):
            raise ValueError(f"invalid official source: {row['date']}")
        if row["jpx_tokyo_control_million_yen"] and \
                combined - nagoya != int(row["jpx_tokyo_control_million_yen"]):
            raise ValueError(f"JPX Tokyo control mismatch: {row['date']}")
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    records = source_rows()
    if args.apply:
        load_env()
        cfg = get_supabase_write_config()
        if cfg is None:
            raise RuntimeError("Supabase service role connection is required")
        session = requests.Session()
    for row in records:
        tokyo = int(row["jpx_two_market_million_yen"]) - int(row["nse_nagoya_million_yen"])
        if args.apply:
            result = rpc(session, cfg, "fill_margin_weekly_jpx_market", {
                "p_date": row["date"],
                "p_two_market_million": row["jpx_two_market_million_yen"],
                "p_nagoya_million": row["nse_nagoya_million_yen"],
                "p_jpx_url": row["jpx_url"], "p_nse_url": row["nse_url"],
            })
            LOG.info("saved %s %s", row["date"], result)
        else:
            LOG.info("plan %s Tokyo million yen=%s", row["date"], tokyo)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
