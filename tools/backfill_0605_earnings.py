import sys
import os
import json
import urllib.request
import sqlite3
import argparse
from datetime import datetime, timezone
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.events.earnings_production_pipeline import run_earnings_production, _is_tanshin_title
from src.models import DisclosureItem, DisclosureType
from supabase import create_client, Client

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually save to DB (default is dry-run)")
    args = parser.parse_args()

    dry_run = not args.apply

    load_dotenv()
    sb_url = os.environ.get("SUPABASE_URL")
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not sb_url or not sb_key:
        print("[ERROR] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set")
        sys.exit(1)
    
    supabase: Client = create_client(sb_url, sb_key)

    # 1. Fetch 2026-06-05 data from Yanoshin
    print("[1] Fetching 2026-06-05 data from Yanoshin API...")
    url_api = "https://webapi.yanoshin.jp/webapi/tdnet/list/20260605.json"
    req = urllib.request.Request(url_api, headers={'User-Agent': 'Mozilla/5.0 tdnet-excel-input backfill'})
    with urllib.request.urlopen(req) as res:
        data = json.loads(res.read().decode('utf-8'))
    
    raw_items = data.get("items", [])
    print(f"  Fetched {len(raw_items)} items.")

    # 2. Filter for earnings (tanshin) with XBRL
    print("[2] Filtering for earnings with XBRL...")
    candidates = []
    for item in raw_items:
        t = item.get("Tdnet", {})
        title = t.get("title", "")
        xbrl_url = t.get("url_xbrl", "")
        if _is_tanshin_title(title) and xbrl_url:
            code = t.get("company_code", "")[:4]
            disclosed_at = t.get("pubdate", "")
            
            di = DisclosureItem(
                disclosure_id=t.get("id", ""),
                ticker=code,
                company_name=t.get("company_name", ""),
                title=title,
                published_at=disclosed_at,
                doc_url=t.get("document_url", ""),
                xbrl_url=xbrl_url,
                disclosure_type=DisclosureType.FINANCIAL_STATEMENT
            )
            candidates.append(di)
    
    print(f"  Found {len(candidates)} earnings candidates.")

    # 3. Check existing in Supabase
    print("[3] Checking existing Supabase records for 2026-06-05...")
    # Fetch all earnings on 2026-06-05
    res = supabase.table('tdnet_events').select('ticker, disclosed_at').eq('event_type', 'earnings').gte('disclosed_at', '2026-06-05T00:00:00Z').lte('disclosed_at', '2026-06-05T23:59:59Z').execute()
    existing_keys = set()
    for row in res.data:
        # Supabase returns disclosed_at in ISO format like 2026-06-05T15:00:00+00:00
        # Yanoshin gives 2026-06-05 15:00:00
        ticker = row['ticker']
        dt_str = row['disclosed_at'].replace('T', ' ')[:19] # Best effort match
        existing_keys.add(f"{ticker}") # Just using ticker for simplicity since it's one day
        
    print(f"  Found {len(existing_keys)} existing earnings in Supabase.")

    # 4. Filter out existing
    to_process = []
    skipped_count = 0
    for doc in candidates:
        if doc.ticker in existing_keys:
            skipped_count += 1
        else:
            to_process.append(doc)
            
    print(f"  Skipped {skipped_count} already existing records.")
    print(f"  To process: {len(to_process)}")

    if not to_process:
        print("[INFO] Nothing to process.")
        sys.exit(0)

    print("\n--- Summary ---")
    print(f"Target count: {len(candidates)}")
    print(f"Existing skip count: {skipped_count}")
    print(f"New process count: {len(to_process)}")
    print("Samples to process (first 5):")
    for doc in to_process[:5]:
        print(f"  {doc.ticker} {doc.company_name} - {doc.title}")
    
    if dry_run:
        print("\n[DRY-RUN] Finished. Run with --apply to execute.")
        sys.exit(0)

    print("\n[4] Running earnings production pipeline...")
    conn = sqlite3.connect('decision_db.db')
    
    # run_earnings_production already handles:
    # - downloading XBRL if missing
    # - saving regardless of YOY (since we fixed the code)
    # - saving to SQLite earnings_summaries
    # - saving to Supabase tdnet_events (pdf_url = doc_url)
    
    result = run_earnings_production(
        docs=to_process,
        conn=conn,
        dry_run=False,
        webhook_url="" # Disable discord notification
    )
    
    print("\n[DONE] Pipeline result:")
    print(f"  Processed: {result.tanshin_count}")
    print(f"  Saved: {result.saved_count}")
    print(f"  Already exists (SQLite): {result.already_exists_count}")
    print(f"  Errors: {len(result.errors)}")
    if result.errors:
        print("  Error details:")
        for e in result.errors[:10]:
            print(f"    {e}")

if __name__ == "__main__":
    main()
