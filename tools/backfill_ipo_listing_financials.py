#!/usr/bin/env python3
"""Backfill one IPO listing-day financial disclosure into production stores."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.pipeline.db import get_supabase_write_config, load_env
from src.common_ticker import is_valid_ticker, normalize_ticker
from src.downloader import download_document
from src.ipo_listing_financials import (
    IPO_LISTING_FINANCIALS,
    disclosure_identity,
    extract_ipo_listing_financials,
    ensure_ipo_company,
    is_ipo_listing_financial_title,
    save_ipo_notification,
    write_ipo_financials,
)
from src.utils import sha256

logger = logging.getLogger("backfill_ipo_listing_financials")


def run(args: argparse.Namespace) -> dict:
    load_env(str(ROOT))
    ticker = normalize_ticker(args.ticker)
    if not is_valid_ticker(ticker):
        raise ValueError(f"invalid ticker: {args.ticker!r}")
    if not is_ipo_listing_financial_title(args.title):
        raise ValueError("title is not an IPO listing financial disclosure")

    pdf_path = Path(args.pdf).resolve() if args.pdf else None
    if pdf_path is None:
        downloaded = download_document(args.url, str(ROOT / "data" / "docs"))
        if not downloaded:
            raise RuntimeError(f"download failed: {args.url}")
        pdf_path = Path(downloaded)

    extraction = extract_ipo_listing_financials(pdf_path)
    if extraction.ticker and extraction.ticker != ticker:
        raise ValueError(f"ticker mismatch metadata={ticker} pdf={extraction.ticker}")

    item = SimpleNamespace(
        disclosure_id=sha256(args.url),
        ticker=ticker,
        company_name=args.company_name,
        title=args.title,
        doc_url=args.url,
        published_at=args.disclosed_at,
        disclosure_type=IPO_LISTING_FINANCIALS,
        source_doc_id=args.source_doc_id or None,
    )
    identity = disclosure_identity(args.url, args.source_doc_id)
    output = {
        "mode": "apply" if args.apply else "dry_run",
        "identity": identity,
        "pdf": str(pdf_path),
        "extraction": extraction.to_dict(),
    }
    if not args.apply:
        output["notification"] = save_ipo_notification(item, dry_run=True)
        output["canonical"] = {"action": "dry_run"}
        return output

    config = get_supabase_write_config()
    if not config:
        raise RuntimeError("SUPABASE service-role write configuration is missing")
    output["company"] = ensure_ipo_company(
        ticker, args.company_name or extraction.company_name, config=config,
    )
    # The card write is first and independent: a subsequent PL error does not
    # make the listing itself disappear.
    output["notification"] = save_ipo_notification(item)
    output["canonical"] = write_ipo_financials(
        extraction,
        ticker=ticker,
        filing_id=identity,
        disclosed_at=args.disclosed_at,
        config=config,
    )
    if not output["canonical"].get("ok"):
        raise RuntimeError(output["canonical"].get("error") or "canonical write failed")
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--company-name", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--disclosed-at", required=True)
    parser.add_argument("--source-doc-id", default="")
    parser.add_argument("--pdf")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    try:
        result = run(args)
    except Exception as exc:
        logger.error("backfill failed: %s", exc, exc_info=True)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
