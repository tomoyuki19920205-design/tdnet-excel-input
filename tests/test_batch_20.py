#!/usr/bin/env python3
"""Batch send 20 random earnings notifications to Discord"""
import sys, os, glob, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.events.env_loader import load_project_env
from src.events.summary_financials import (
    extract_earnings_data, extract_narrative_from_xbrl_zip, extract_company_info_from_zip,
)
from src.events.summary_narrative_extractor import extract_narrative
from src.events.summary_notify import format_earnings_message, send_earnings_discord
import time

def main():
    load_project_env()
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook:
        print("ERROR: DISCORD_WEBHOOK_URL not set")
        return

    zips = glob.glob("data/docs/**/*.zip", recursive=True)
    random.seed(42)
    random.shuffle(zips)

    sent = 0
    skipped = 0
    errors = 0

    for zp in zips:
        if sent >= 20:
            break

        company_name, ticker = extract_company_info_from_zip(zp)
        if not ticker:
            ticker = "????"

        data = extract_earnings_data(xbrl_path=zp, title="", ticker=ticker)
        if not data:
            skipped += 1
            continue

        # Narrative
        qual_text = extract_narrative_from_xbrl_zip(zp)
        company_reasons = []
        segment_reasons = []

        if qual_text:
            narrative = extract_narrative(qual_text)
            if narrative.has_reason:
                try:
                    from src.events.summary_ai_client import call_reason_format_api
                    ai_result, usage = call_reason_format_api(
                        narrative.company_reason,
                        narrative.segment_reasons or None,
                    )
                    company_reasons = ai_result.get("company_reasons", [])
                    segment_reasons = ai_result.get("segment_reasons", [])
                except Exception as e:
                    company_reasons = [
                        s.strip()[:40]
                        for s in narrative.company_reason.split("\u3002")
                        if len(s.strip()) > 5
                    ][:3]

        msg = format_earnings_message(
            ticker=ticker,
            company_name=company_name,
            summary_line=data.format_summary_line(),
            segment_lines=data.format_segment_lines(),
            company_reasons=company_reasons,
            segment_reasons=segment_reasons,
            title=f"\u6c7a\u7b97\u77ed\u4fe1 ({os.path.basename(zp)})",
        )

        ok = send_earnings_discord(webhook, msg)
        sent += 1
        status = "OK" if ok else "FAIL"
        print(f"[{sent:02d}] {status} {ticker} {company_name} ({os.path.basename(zp)})")

        if not ok:
            errors += 1

        # Rate limit: Discord webhook max 30 req/min
        time.sleep(2.5)

    print(f"\nDone: sent={sent} skipped={skipped} errors={errors}")

if __name__ == "__main__":
    main()
