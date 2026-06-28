#!/usr/bin/env python3
import argparse
import glob
import json
import logging
import os
import re
import sys
import datetime
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.pipeline.db import load_env, supabase_select
from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip
from src.segment.prior_comparative_generator import generate_prior_comparative_payload

logger = logging.getLogger("batch_prior_comparative")

def normalize_doc_id(filename: str) -> str:
    basename = os.path.basename(filename)
    match = re.search(r'(20\d{12})', basename)
    if match:
        return f"1401{match.group(1)}"
    return "UNKNOWN"

def get_real_write_config():
    rest_url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    return {"rest_url": f"{rest_url}/rest/v1", "key": key}

def chunked_supabase_select(table, column, values, params=None):
    if not values: return []
    chunk_size = 300
    all_results = []
    values = list(set(values))
    for i in range(0, len(values), chunk_size):
        chunk = values[i:i+chunk_size]
        val_str = "(" + ",".join(str(v) for v in chunk) + ")"
        query_params = {column: f"in.{val_str}"}
        if params:
            query_params.update(params)
        res = supabase_select(table, params=query_params)
        if res:
            all_results.extend(res)
    return all_results

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--enable-prior-comparative", action="store_true")
    parser.add_argument("--save-prior-comparative", action="store_true")
    parser.add_argument("--prior-comparative-canary-tickers", type=str, default="")
    parser.add_argument("--prior-comparative-max-tickers", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # defaults must be safe
    is_save = args.enable_prior_comparative and args.save_prior_comparative and not args.dry_run

    allowlist = []
    if args.prior_comparative_canary_tickers:
        allowlist = [t.strip() for t in args.prior_comparative_canary_tickers.split(",")]

    if is_save and not allowlist:
        print("ERROR: allowlist is required for save.")
        sys.exit(1)

    load_env()
    
    zip_files = sorted(glob.glob(os.path.join(_PROJECT_ROOT, "data", "xbrl_archive", "*.zip")), reverse=True)
    exclude_tickers = {"6905", "2796", "7886", "9993"}
    
    candidates_info = []
    ready_payloads = {}
    
    # Pass A: Local Scan
    print("Pass A: Local Scan")
    pass_a_results = []
    processed_tickers = set()
    for zip_path in zip_files:
        if len(pass_a_results) >= args.prior_comparative_max_tickers:
            break
            
        doc_id = normalize_doc_id(zip_path)
        if doc_id == "UNKNOWN": continue
            
        try:
            xbrl_rows = extract_segments_from_xbrl_zip(zip_path)
        except Exception:
            continue
            
        if not xbrl_rows: continue
            
        ticker = xbrl_rows[0].normalized_ticker
        if not ticker or ticker in exclude_tickers: continue
        if ticker in processed_tickers: continue
        
        pass_a_results.append((zip_path, doc_id, ticker, xbrl_rows))
        processed_tickers.add(ticker)

    print(f"Pass A found {len(pass_a_results)} candidates.")

    # Pass B: DB Fetch
    print("Pass B: DB Fetch (chunked)")
    all_tickers = [r[2] for r in pass_a_results]
    
    tdnet_events_cache = {}
    try:
        events = chunked_supabase_select("tdnet_events", "ticker", all_tickers, params={"event_type": "eq.earnings"})
        for ev in events:
            s_url = ev.get("source_url", "")
            if s_url:
                match = re.search(r'(140120\d{12})', s_url)
                if match:
                    tdnet_events_cache[match.group(1)] = ev.get("disclosed_at")
    except Exception as e:
        print(f"Error fetching tdnet_events: {e}")
        # Will handle CONNECTION_ERROR in Pass C if cache is missing

    canonical_segments_cache = {}
    try:
        db_segments = chunked_supabase_select("canonical_segments", "ticker", all_tickers)
        for seg in db_segments:
            t = seg["ticker"]
            if t not in canonical_segments_cache:
                canonical_segments_cache[t] = []
            canonical_segments_cache[t].append(seg)
    except Exception as e:
        print(f"Error fetching canonical_segments: {e}")
        # Connection error here means we can't do anything
        db_segments = []

    print("Pass C: Process Candidates")
    
    for zip_path, doc_id, ticker, xbrl_rows in pass_a_results:
        periods = set(r.period for r in xbrl_rows if r.period)
        if len(periods) < 2:
            candidates_info.append({"ticker": ticker, "classification": "BLOCKED_NO_PRIOR", "doc_id": doc_id})
            continue
            
        current_period = max(periods)
        prior_period = sorted([p for p in periods if p != current_period])[-1]
        
        official = canonical_segments_cache.get(ticker, [])
        if not official:
            # Maybe CONNECTION_ERROR or genuinely no official current
            # Since we couldn't fetch or none exists
            if not db_segments:
                candidates_info.append({"ticker": ticker, "classification": "CONNECTION_ERROR", "doc_id": doc_id, "blocker": "Failed to fetch DB"})
                continue
            candidates_info.append({"ticker": ticker, "classification": "BLOCKED_CURRENT_MISMATCH", "doc_id": doc_id, "blocker": "No official segments found at all"})
            continue
            
        official.sort(key=lambda x: (x.get("period", ""), x.get("quarter", "")), reverse=True)
        official_priors = [r for r in official if r["period"] == prior_period and (r.get("data_basis") is None or r.get("data_basis") == "official_current")]
        
        if not official_priors:
            candidates_info.append({"ticker": ticker, "classification": "BLOCKED_CURRENT_MISMATCH", "doc_id": doc_id, "blocker": "No official current for prior period"})
            continue
            
        quarter = official_priors[0]["quarter"]
        
        disclosure_datetime = tdnet_events_cache.get(doc_id)
        disclosure_precision = "tdnet_events_exact"
        estimated_time = False
        
        if not disclosure_datetime:
            match = re.search(r'1401(20\d{2})(\d{2})(\d{2})', doc_id)
            if match:
                yyyy, mm, dd = match.groups()
                disclosure_datetime = f"{yyyy}-{mm}-{dd}T15:00:00+09:00"
                disclosure_precision = "date_from_doc_id"
                estimated_time = True
            else:
                candidates_info.append({"ticker": ticker, "classification": "BLOCKED_MISSING_SOURCE_DISCLOSURE_DATE", "doc_id": doc_id})
                continue
        
        try:
            planned_rows = generate_prior_comparative_payload(
                xbrl_rows,
                official_priors,
                ticker,
                doc_id,
                disclosure_datetime,
                quarter
            )
            for r in planned_rows:
                if "flags" not in r: r["flags"] = {}
                r["flags"]["source_disclosure_date_precision"] = disclosure_precision
                r["flags"]["source_disclosure_date_estimated_time"] = estimated_time
        except Exception as e:
            candidates_info.append({"ticker": ticker, "classification": "NEEDS_REVIEW", "doc_id": doc_id, "blocker": f"Exception: {str(e)}"})
            continue
            
        if not planned_rows:
            candidates_info.append({"ticker": ticker, "classification": "BLOCKED_CURRENT_MISMATCH", "doc_id": doc_id, "blocker": "Could not map to official keys"})
            continue
            
        # Check existing
        prior_existing = [r for r in official if r.get("period") == prior_period and r.get("quarter") == quarter and r.get("data_basis") == "prior_comparative"]
        if prior_existing:
            candidates_info.append({"ticker": ticker, "classification": "BLOCKED_ALREADY_EXISTS", "doc_id": doc_id})
            continue
            
        # Collision check
        collisions = []
        planned_srks = set([r['source_row_key'] for r in planned_rows])
        # We can check global official cache for collision
        for r in official:
            if r.get("source_row_key") in planned_srks:
                collisions.append(r)
        
        if collisions:
            candidates_info.append({"ticker": ticker, "classification": "BLOCKED_DUPLICATE_SOURCE_ROW_KEY", "doc_id": doc_id})
            continue

        flags_all = set()
        for r in planned_rows:
            if "flags" in r:
                flags_all.update(r["flags"].keys())

        classification = "READY_FOR_INSERT"
        if disclosure_precision == "tdnet_events_exact":
            classification = "READY_DATE_FROM_TDNET_EVENTS"
        elif disclosure_precision == "date_from_doc_id":
            classification = "READY_DATE_FROM_DOC_ID"

        candidates_info.append({
            "ticker": ticker,
            "company_name": "",
            "source_doc_id": doc_id,
            "period": prior_period,
            "quarter": quarter,
            "source_disclosure_period": current_period,
            "source_disclosure_date": disclosure_datetime,
            "source_disclosure_date_precision": disclosure_precision,
            "segment_count": len(set(r["segment_key"] for r in planned_rows)),
            "planned_row_count": len(planned_rows),
            "current_match": True,
            "source_row_key_collision": 0,
            "flags": list(flags_all),
            "classification": classification,
            "blocker": None
        })
        ready_payloads[ticker] = planned_rows

    now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs("scratch", exist_ok=True)
    
    with open(f"scratch/prior_comparative_limited_batch_dryrun_candidates_{now}.json", "w") as f:
        json.dump(candidates_info, f, indent=2)

    import csv
    with open(f"scratch/prior_comparative_limited_batch_dryrun_candidates_{now}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ticker", "company_name", "source_doc_id", "period", "quarter", "source_disclosure_period", "source_disclosure_date", "source_disclosure_date_precision", "segment_count", "planned_row_count", "current_match", "source_row_key_collision", "flags", "classification", "blocker", "doc_id"], extrasaction="ignore")
        w.writeheader()
        for c in candidates_info:
            c_copy = c.copy()
            c_copy["flags"] = ",".join(c["flags"]) if "flags" in c else ""
            w.writerow(c_copy)

    ready_tickers = [c["ticker"] for c in candidates_info if c["classification"] in ("READY_FOR_INSERT", "READY_DATE_FROM_TDNET_EVENTS", "READY_DATE_FROM_DOC_ID")]
    
    # Selection (limit 10)
    selected_tickers = []
    if is_save:
        selected_tickers = [t for t in ready_tickers if t in allowlist][:10]
    else:
        selected_tickers = ready_tickers[:10]

    with open(f"scratch/prior_comparative_limited_batch_selected_canaries_{now}.json", "w") as f:
        json.dump(selected_tickers, f, indent=2)

    # Insert logic
    if is_save and selected_tickers:
        print(f"Executing INSERT for {selected_tickers}")
        import requests
        cfg = get_real_write_config()
        headers = {
            "apikey": cfg["key"],
            "Authorization": f"Bearer {cfg['key']}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }
        
        inserted_all = []
        for ticker in selected_tickers:
            payload = ready_payloads[ticker]
            # safety check again just before insert
            if not payload: continue
            
            # double check collisions
            has_col = False
            for r in payload:
                if supabase_select("canonical_segments", params={"source_row_key": f"eq.{r['source_row_key']}"}):
                    has_col = True
                    break
            if has_col: continue

            r = requests.post(f"{cfg['rest_url']}/canonical_segments", json=payload, headers=headers)
            if r.status_code in (200, 201):
                inserted_all.extend(r.json())
            else:
                print(f"Error inserting {ticker}: {r.text}")
                
        if inserted_all:
            ids = [i["id"] for i in inserted_all]
            ids_str = ", ".join([str(i) for i in ids])
            rb_file = f"scratch/prior_comparative_limited_batch_rollback_preview_{now}.sql"
            with open(rb_file, "w") as f:
                f.write(f"DELETE FROM canonical_segments WHERE id IN ({ids_str});\n")
            
            report = {
                "final_judgment": "PASS_LIMITED_BATCH_PRIOR_COMPARATIVE_CANARY_AND_PUSH",
                "inserted_ids": ids,
                "inserted_count": len(inserted_all),
                "rollback_file": rb_file
            }
            with open(f"scratch/prior_comparative_limited_batch_canary_report_{now}.json", "w") as f:
                json.dump(report, f, indent=2)
            print("INSERT completed. Readback required.")
    else:
        print(f"Dry run complete. Ready tickers: {ready_tickers}")

if __name__ == "__main__":
    main()
