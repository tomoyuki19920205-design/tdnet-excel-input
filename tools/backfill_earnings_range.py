import sys
import os
import json
import urllib.request
import sqlite3
import argparse
from datetime import datetime, timedelta, date
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.events.earnings_production_pipeline import run_earnings_production, _is_tanshin_title
from src.models import DisclosureItem, DisclosureType
from supabase import create_client, Client

def main():
    parser = argparse.ArgumentParser(description="Backfill earnings missing due to YOY guard clause")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--apply", action="store_true", help="Actually save to DB (default is dry-run)")
    args = parser.parse_args()

    dry_run = not args.apply

    try:
        start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
        end_date = datetime.strptime(args.end, "%Y-%m-%d").date()
    except ValueError:
        print("[ERROR] Date format must be YYYY-MM-DD")
        sys.exit(1)

    load_dotenv()
    sb_url = os.environ.get("SUPABASE_URL")
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not sb_url or not sb_key:
        print("[ERROR] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set")
        sys.exit(1)
    
    supabase: Client = create_client(sb_url, sb_key)

    total_fetched = 0
    total_candidates = 0
    total_skipped = 0
    total_to_process = 0
    all_to_process = []

    print(f"--- Backfill Earnings from {start_date} to {end_date} ---")

    # Pre-fetch existing records from Supabase in the whole range
    print("[1] Fetching existing Supabase records for the date range...")
    start_iso = f"{args.start}T00:00:00Z"
    end_iso = f"{args.end}T23:59:59Z"
    
    # Supabase select might return max 1000 records if not paginated, so we might need to be careful.
    # We will do it day by day to be safe.
    
    curr_date = start_date
    while curr_date <= end_date:
        d_str = curr_date.strftime("%Y%m%d")
        d_iso = curr_date.strftime("%Y-%m-%d")
        print(f"\n[Date: {d_iso}]")

        # Fetch Yanoshin
        url_api = f"https://webapi.yanoshin.jp/webapi/tdnet/list/{d_str}.json"
        req = urllib.request.Request(url_api, headers={'User-Agent': 'Mozilla/5.0 tdnet-excel-input backfill'})
        
        raw_items = []
        try:
            with urllib.request.urlopen(req) as res:
                data = json.loads(res.read().decode('utf-8'))
                raw_items = data.get("items", [])
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"  [Skip] No Yanoshin data for {d_iso} (404 Not Found)")
            else:
                print(f"  [Warning] HTTP Error {e.code} for {d_iso}: {e}")
        except Exception as e:
            print(f"  [Warning] Failed to fetch Yanoshin for {d_iso}: {e}")

        print(f"  Fetched items: {len(raw_items)}")
        total_fetched += len(raw_items)

        if not raw_items:
            curr_date += timedelta(days=1)
            continue

        # Filter candidates
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
        
        print(f"  Earnings candidates: {len(candidates)}")
        total_candidates += len(candidates)

        if not candidates:
            curr_date += timedelta(days=1)
            continue

        # Fetch existing for this day
        res = supabase.table('tdnet_events') \
            .select('ticker, disclosed_at, headline') \
            .eq('event_type', 'earnings') \
            .gte('disclosed_at', f"{d_iso}T00:00:00Z") \
            .lte('disclosed_at', f"{d_iso}T23:59:59Z") \
            .execute()
            
        existing_keys = set()
        for row in res.data:
            existing_keys.add(row['ticker'])
            
        day_skipped = 0
        day_to_process = 0
        for doc in candidates:
            if doc.ticker in existing_keys:
                day_skipped += 1
            else:
                day_to_process += 1
                all_to_process.append(doc)
                
        print(f"  Existing skipped: {day_skipped}")
        print(f"  New to process: {day_to_process}")
        
        total_skipped += day_skipped
        total_to_process += day_to_process
        curr_date += timedelta(days=1)

    print("\n==============================")
    print("--- Summary ---")
    print(f"Date range: {args.start} to {args.end}")
    print(f"Total fetched from Yanoshin: {total_fetched}")
    print(f"Total earnings candidates: {total_candidates}")
    print(f"Total existing skip count: {total_skipped}")
    print(f"Total new process count: {total_to_process}")
    
    if all_to_process:
        print("\nSamples to process (first 10):")
        for doc in all_to_process[:10]:
            print(f"  {doc.published_at} | {doc.ticker} {doc.company_name} - {doc.title}")
            
    if not all_to_process:
        print("[INFO] Nothing to process.")
        sys.exit(0)

    if dry_run:
        print("\n[DRY-RUN] Finished. Run with --apply to actually save.")
        sys.exit(0)

    print("\n[4] Running earnings production pipeline...")
    conn = sqlite3.connect('decision_db.db')
    
    # Process all collected missing items
    result = run_earnings_production(
        docs=all_to_process,
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
