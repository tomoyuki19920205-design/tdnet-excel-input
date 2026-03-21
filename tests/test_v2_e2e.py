#!/usr/bin/env python3
"""V2 E2E test — captures Discord messages with reasons"""
import sys, os, glob, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.events.env_loader import load_project_env
from src.events.summary_financials import extract_earnings_data, extract_narrative_from_xbrl_zip, extract_company_info_from_zip
from src.events.summary_narrative_extractor import extract_narrative
from src.events.summary_notify import format_earnings_message, send_earnings_discord


def main():
    load_project_env()
    do_send = "--send" in sys.argv
    use_ai = "--ai" in sys.argv or do_send

    zips = sorted(
        glob.glob("data/docs/**/*.zip", recursive=True),
        key=os.path.getsize, reverse=True,
    )

    messages = []
    for zp in zips:
        # 企業名・ticker取得
        company_name, ticker = extract_company_info_from_zip(zp)
        if not ticker:
            ticker = "????"

        data = extract_earnings_data(xbrl_path=zp, title="", ticker=ticker)
        if not data:
            continue

        # --- Narrative extraction from qualitative.htm ---
        qual_text = extract_narrative_from_xbrl_zip(zp)
        company_reasons = []
        segment_reasons = []

        if qual_text:
            narrative = extract_narrative(qual_text)
            if narrative.has_reason:
                if use_ai:
                    try:
                        from src.events.summary_ai_client import call_reason_format_api
                        ai_result, usage = call_reason_format_api(
                            narrative.company_reason,
                            narrative.segment_reasons or None,
                        )
                        company_reasons = ai_result.get("company_reasons", [])
                        segment_reasons = ai_result.get("segment_reasons", [])
                        print(f"  AI: {len(company_reasons)} reasons, tokens={usage['input_tokens']}+{usage['output_tokens']}")
                    except Exception as e:
                        print(f"  AI error: {e}")
                        company_reasons = [s.strip()[:40] for s in narrative.company_reason.split("\u3002") if len(s.strip()) > 5][:3]
                else:
                    company_reasons = [s.strip()[:40] for s in narrative.company_reason.split("\u3002") if len(s.strip()) > 5][:3]

        msg = format_earnings_message(
            ticker=ticker,
            company_name=company_name,
            summary_line=data.format_summary_line(),
            segment_lines=data.format_segment_lines(),
            company_reasons=company_reasons,
            segment_reasons=segment_reasons,
            title=f"決算短信 ({os.path.basename(zp)})",
        )
        messages.append(msg)

        idx = len(messages)
        out_path = f"tests/_discord_msg_{idx}.txt"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(msg)
        print(f"\n=== Message #{idx} ({os.path.basename(zp)}) ===")
        print(msg)

        if len(messages) >= 3:
            break

    if do_send and messages:
        webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if webhook:
            for msg in messages:
                ok = send_earnings_discord(webhook, msg)
                print(f"  sent={ok}")

    print(f"\nTotal: {len(messages)} messages")


if __name__ == "__main__":
    main()
