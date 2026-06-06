import argparse
import os
import sqlite3
import sys
import json
import requests
from datetime import datetime
from dateutil import parser
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client
import unicodedata

def normalize_title(t: str) -> str:
    if not t:
        return ""
    # Normalize unicode (NFKC) and remove spaces
    n = unicodedata.normalize("NFKC", t)
    return n.replace(" ", "").replace("　", "")

def fetch_yanoshin_date(date_str: str, cache: dict) -> list:
    """Fetch tdnet list for a specific YYYYMMDD date, with caching."""
    if date_str in cache:
        return cache[date_str]
    
    url = f"https://webapi.yanoshin.jp/webapi/tdnet/list/{date_str}.json"
    print(f"Fetching API for {date_str}...")
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        items = []
        if isinstance(data, dict):
            if "items" in data:
                items = data["items"]
            elif "list" in data:
                items = data["list"]
        elif isinstance(data, list):
            items = data
            
        # Unwrap Tdnet
        unwrapped = []
        for it in items:
            if isinstance(it, dict) and "Tdnet" in it:
                unwrapped.append(it["Tdnet"])
            else:
                unwrapped.append(it)
                
        cache[date_str] = unwrapped
        return unwrapped
    except Exception as e:
        print(f"Failed to fetch {date_str}: {e}")
        cache[date_str] = []
        return []

def main():
    argparser = argparse.ArgumentParser(description="Backfill pdf_url in Supabase from SQLite and TDNET API")
    argparser.add_argument("--apply", action="store_true", help="Apply updates to Supabase (default is dry-run)")
    args = argparser.parse_args()

    # Load environment
    load_dotenv(Path(__file__).parent.parent.parent.parent / ".env")
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url or not supabase_key:
        print("Error: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set.")
        sys.exit(1)

    supabase = create_client(supabase_url, supabase_key)
    
    db_path = Path(__file__).parent.parent.parent.parent / "decision_db.db"
    if not db_path.exists():
        print(f"Error: Database not found at {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print("Fetching candidate events from Supabase...")
    
    candidates = {}
    try:
        # Fetch latest 2000 events to avoid timeout
        res = supabase.table("tdnet_events").select("id, event_type, pdf_url, ticker, headline, disclosed_at, detected_at, raw_payload").order("detected_at", desc=True).limit(2000).execute()
        for r in res.data:
            event_type = r.get("event_type")
            if event_type not in ("earnings", "forecast"):
                continue
            if r.get("pdf_url") is None or str(r.get("pdf_url")).endswith(".zip"):
                candidates[r["id"]] = r
    except Exception as e:
        print(f"Error fetching from Supabase: {e}")
        sys.exit(1)

    print(f"Found {len(candidates)} candidate records in Supabase (out of last 2000).")

    updates = []
    skipped_count = 0
    api_cache = {}

    for rec_id, rec in candidates.items():
        pdf_url = rec["pdf_url"]
        
        raw_payload_str = rec.get("raw_payload")
        fingerprint = None
        source_doc_id = None
        if raw_payload_str:
            try:
                rp = json.loads(raw_payload_str)
                fingerprint = rp.get("fingerprint")
                source_doc_id = rp.get("source_doc_id")
            except Exception:
                pass
                
        ticker = rec.get("ticker")
        title = rec.get("headline") or rec.get("source_title")
        detected_at = rec.get("detected_at")
        disclosed_at = rec.get("disclosed_at") or detected_at
        
        doc_url = None
        
        # 1. Try to find in SQLite events table first
        try:
            query = "SELECT doc_url FROM events WHERE "
            params = []
            conditions = []
            if fingerprint:
                conditions.append("fingerprint = ?")
                params.append(fingerprint)
            if source_doc_id:
                conditions.append("source_doc_id = ?")
                params.append(source_doc_id)
                
            if conditions:
                query += " OR ".join(conditions) + " LIMIT 1"
                row = conn.execute(query, tuple(params)).fetchone()
                if row and row["doc_url"]:
                    doc_url = row["doc_url"]
        except Exception:
            pass
            
        # 2. If null, fetch from TDNET Yanoshin API
        if not doc_url and disclosed_at and ticker and title:
            try:
                from datetime import timezone, timedelta
                JST = timezone(timedelta(hours=9))
                dt = parser.parse(disclosed_at).astimezone(JST)
                date_str = dt.strftime("%Y%m%d")
                
                items = fetch_yanoshin_date(date_str, api_cache)
                norm_title = normalize_title(title)
                
                for it in items:
                    it_ticker = it.get("Tcode") or it.get("company_code") or it.get("code") or ""
                    # strip trailing zero
                    if it_ticker.endswith("0") and len(it_ticker) == 5:
                        it_ticker = it_ticker[:-1]
                        
                    if it_ticker == ticker:
                        it_title = it.get("Ttitle") or it.get("title") or ""
                        if normalize_title(it_title) == norm_title:
                            fetched_url = it.get("TdocURL") or it.get("document_url") or it.get("url")
                            if fetched_url:
                                doc_url = fetched_url
                                break
            except Exception as e:
                print(f"Error matching API for {ticker}: {e}")
            
        # 3. Fallback: reconstruct URL from source_doc_id
        if not doc_url and source_doc_id and isinstance(source_doc_id, str) and source_doc_id.isdigit():
            doc_url = f"https://webapi.yanoshin.jp/rd.php?https://www.release.tdnet.info/inbs/{source_doc_id}.pdf"
            
        # 4. Final verification
        if not doc_url:
            if ticker == "7800" or ticker == "6309" or ticker == "9824" or ticker == "3172" or rec["event_type"] == "earnings":
                print(f"Failed to find doc_url for {ticker} - {title} on {detected_at}")
            skipped_count += 1
            continue
            
        if doc_url.endswith(".zip"):
            # Should not happen with TdocURL, but just in case
            skipped_count += 1
            continue

        updates.append({
            "id": rec_id,
            "event_type": rec["event_type"],
            "ticker": ticker,
            "old_pdf_url": pdf_url,
            "new_pdf_url": doc_url
        })

    print(f"Updates prepared: {len(updates)}")
    print(f"Skipped (could not reconstruct): {skipped_count}")

    # Category-wise planned updates
    category_counts = {}
    for u in updates:
        category_counts[u['event_type']] = category_counts.get(u['event_type'], 0) + 1
    
    print("\n--- Updates by Category ---")
    for cat, count in category_counts.items():
        print(f"  {cat}: {count}")

    if updates:
        print("\n--- Sample Updates ---")
        for u in updates[:10]:
            print(f"ID: {u['id']} | Ticker: {u['ticker']} | Type: {u['event_type']}")
            print(f"  Old pdf_url: {u['old_pdf_url']}")
            print(f"  New pdf_url: {u['new_pdf_url']}")

    if args.apply:
        print("\nApplying updates to Supabase...")
        success_count = 0
        error_count = 0
        for u in updates:
            try:
                # source_url にも XBRL ZIP を入れない (PDF URLを入れる)
                supabase.table("tdnet_events").update({
                    "pdf_url": u["new_pdf_url"],
                    "source_url": u["new_pdf_url"]
                }).eq("id", u["id"]).execute()
                success_count += 1
            except Exception as e:
                print(f"Failed to update {u['id']}: {e}")
                error_count += 1
        print(f"Applied: {success_count} success, {error_count} errors.")
    else:
        print("\n[DRY-RUN] No changes applied to Supabase. Run with --apply to execute.")

if __name__ == "__main__":
    main()
