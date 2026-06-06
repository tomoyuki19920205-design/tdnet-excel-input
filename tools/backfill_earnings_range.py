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
    import hashlib

    def normalize_title(t: str) -> str:
        return t.replace(" ", "").replace("　", "").lower() if t else ""

    print(f"--- Backfill Earnings from {start_date} to {end_date} ---")

    # [1] Pre-fetch SQLite earnings_summaries keys
    print("[1] Fetching all existing SQLite earnings_summaries...")
    conn = sqlite3.connect('decision_db.db')
    es_fingerprints = set()
    for row in conn.execute("SELECT fingerprint FROM earnings_summaries").fetchall():
        es_fingerprints.add(row[0])
    print(f"  Found {len(es_fingerprints)} records in earnings_summaries.")
    conn.close()

    # [2] Pre-fetch Supabase tdnet_events keys
    print("[2] Fetching existing Supabase records for the date range...")
    start_iso = f"{args.start}T00:00:00Z"
    end_iso = f"{args.end}T23:59:59Z"
    
    sb_dedupe_keys = set()
    sb_fallback_keys = set()
    
    curr_date = start_date
    while curr_date <= end_date:
        d_str = curr_date.strftime("%Y%m%d")
        d_iso = curr_date.strftime("%Y-%m-%d")
        print(f"\n[Date: {d_iso}]")

        # Due to 1000 row limit in Supabase, we fetch day by day
        res = supabase.table('tdnet_events') \
            .select('dedupe_key, ticker, disclosed_at, headline') \
            .eq('event_type', 'earnings') \
            .gte('disclosed_at', f"{d_iso}T00:00:00Z") \
            .lte('disclosed_at', f"{d_iso}T23:59:59Z") \
            .execute()
        for row in res.data:
            if row.get('dedupe_key'):
                sb_dedupe_keys.add(row['dedupe_key'])
            
            disclosed_at = row.get('disclosed_at', '')
            jst_date = disclosed_at[:10] if disclosed_at else ""
            comp_key = f"{row.get('ticker', '')}|{jst_date}|{normalize_title(row.get('headline', ''))}"
            sb_fallback_keys.add(comp_key)

        # Fetch Yanoshin with limit=5000
        url_api = f"https://webapi.yanoshin.jp/webapi/tdnet/list/{d_str}.json?limit=5000"
        req = urllib.request.Request(url_api, headers={'User-Agent': 'Mozilla/5.0 tdnet-excel-input backfill'})
        
        raw_items = []
        try:
            with urllib.request.urlopen(req) as res_api:
                data = json.loads(res_api.read().decode('utf-8'))
                raw_items = data.get("items", [])
        except Exception as e:
            print(f"  [Warning] Failed to fetch Yanoshin for {d_iso}: {e}")

        print(f"  Fetched items: {len(raw_items)}")
        total_fetched += len(raw_items)

        if not raw_items:
            curr_date += timedelta(days=1)
            continue

        candidates = []
        for item in raw_items:
            t = item.get("Tdnet", item)
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

        day_skipped = 0
        day_to_process = 0
        skip_reasons = {"sqlite_fingerprint": 0, "supabase_dedupe_key": 0, "supabase_fallback": 0}

        for doc in candidates:
            doc_id = getattr(doc, "disclosure_id", "") or getattr(doc, "doc_id", "")
            title = getattr(doc, "title", "")
            published_at = getattr(doc, "published_at", "")
            jst_date = published_at[:10] if published_at else ""
            
            # 1. SQLite fingerprint (ticker|title|doc_id)
            s_fp = f"{doc.ticker}|{title}|{doc_id}"
            doc_fingerprint = hashlib.md5(s_fp.encode("utf-8")).hexdigest()
            
            # 2. Supabase dedupe_key (ticker|event_type|normalized_title|YYYY-MM-DD HH:MM)
            dt_part = published_at[:16] if published_at else ""
            s_dk = f"{doc.ticker}|earnings|{normalize_title(title)}|{dt_part}"
            doc_dedupe_key = hashlib.sha256(s_dk.encode("utf-8")).hexdigest()[:40]

            # 3. Fallback compound key (ticker|YYYY-MM-DD|normalized_title)
            comp_key = f"{doc.ticker}|{jst_date}|{normalize_title(title)}"
            
            if doc_fingerprint in es_fingerprints:
                day_skipped += 1
                skip_reasons["sqlite_fingerprint"] += 1
            elif doc_dedupe_key in sb_dedupe_keys:
                day_skipped += 1
                skip_reasons["supabase_dedupe_key"] += 1
            elif comp_key in sb_fallback_keys:
                day_skipped += 1
                skip_reasons["supabase_fallback"] += 1
            else:
                day_to_process += 1
                all_to_process.append(doc)
                
        if day_skipped > 0:
            print(f"  Existing skipped: {day_skipped} (sqlite_fingerprint: {skip_reasons['sqlite_fingerprint']}, supabase_dedupe_key: {skip_reasons['supabase_dedupe_key']}, supabase_fallback: {skip_reasons['supabase_fallback']})")
        if day_to_process > 0:
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
