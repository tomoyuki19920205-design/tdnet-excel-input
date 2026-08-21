#!/usr/bin/env python3
"""Build/repair all-company IR sources from existing authoritative inputs."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.company_ir_source_discovery import (
    DiscoveryResult, apply_discovery_result, cached_tdnet_pdf, discover_ir_pages,
    discovery_report, extract_official_url_from_tdnet_pdf, latest_tdnet_documents,
    load_tse_universe, sync_universe,
)
from src.company_ir_monitor import normalize_url, run_monitor


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded TSE company IR source discovery")
    parser.add_argument("--db", default="data/company_ir_monitor.db")
    parser.add_argument("--jquants-db", default="data/jquants.db")
    parser.add_argument("--cache-root", default="cache")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--request-interval", type=float, default=0.1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--batch-size", type=int, help="Resume at most this many unfinished companies")
    parser.add_argument("--ticker")
    parser.add_argument("--local-only", action="store_true", help="Never fetch missing TDnet PDFs or company sites")
    parser.add_argument("--repair", action="store_true", help="Re-run failed/not-found/404 source discovery")
    args = parser.parse_args()
    if args.request_interval < 0:
        parser.error("request interval must be non-negative")

    db_path = ROOT / args.db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    universe = load_tse_universe(ROOT / args.jquants_db)
    if args.ticker:
        universe = [row for row in universe if row.ticker == args.ticker]
    if args.limit:
        universe = universe[:args.limit]
    sync_universe(conn, universe)
    pending_before = conn.execute(
        "SELECT COUNT(*) FROM company_ir_companies WHERE discovery_status='pending'"
    ).fetchone()[0]
    repair_allowed = bool(args.repair and pending_before == 0)
    repair_tickers: set[str] = set()
    if repair_allowed:
        repair_limit = args.batch_size or len(universe)
        repair_tickers = {
            row[0] for row in conn.execute("""
                SELECT c.ticker FROM company_ir_companies c
                WHERE c.discovery_status<>'discovered'
                   OR EXISTS (SELECT 1 FROM company_ir_sources s
                              WHERE s.ticker=c.ticker AND s.failure_count>0
                                AND s.last_error LIKE '%404%')
                ORDER BY COALESCE(c.last_validated_at,''),c.ticker LIMIT ?
            """, (repair_limit,))
        }
    documents = latest_tdnet_documents(ROOT / args.jquants_db)
    work: list[tuple[str, str]] = []
    missing_pdf_work: list[tuple[str, str]] = []
    baseline_source_ids: list[int] = []
    pace_lock = threading.Lock()
    next_request_at = [0.0]

    def pace() -> None:
        with pace_lock:
            now_mono = time.monotonic()
            wait = max(0.0, next_request_at[0] - now_mono)
            next_request_at[0] = max(now_mono, next_request_at[0]) + args.request_interval
        if wait:
            time.sleep(wait)
    selected = 0
    for company in universe:
        existing = conn.execute(
            "SELECT official_url,discovery_status,ir_top_url FROM company_ir_companies WHERE ticker=?",
            (company.ticker,),
        ).fetchone()
        official_url = existing[0] if existing else None
        should_repair = bool(repair_allowed and company.ticker in repair_tickers and existing)
        unfinished = not existing or existing[1] == "pending" or should_repair
        if not unfinished:
            continue
        if args.batch_size is not None and selected >= args.batch_size:
            continue
        selected += 1
        if not official_url:
            # Reuse an already configured IR source before consulting a filing.
            source = conn.execute(
                "SELECT source_url FROM company_ir_sources WHERE ticker=? AND status='active' ORDER BY id LIMIT 1",
                (company.ticker,),
            ).fetchone()
            if source:
                parts = requests.utils.urlparse(source[0])
                official_url = f"{parts.scheme}://{parts.netloc}/"
            else:
                document_id = documents.get(company.ticker)
                data = cached_tdnet_pdf(ROOT / args.cache_root, document_id) if document_id else None
                if data is None and document_id and not args.local_only:
                    missing_pdf_work.append((company.ticker, document_id))
                    continue
                if data:
                    official_url = extract_official_url_from_tdnet_pdf(data)
        needs_discovery = not existing or existing[1] == "pending"
        if official_url and (needs_discovery or should_repair):
            official_url = normalize_url(official_url)
            if args.local_only:
                apply_discovery_result(
                    conn, company.ticker,
                    DiscoveryResult(company.ticker, official_url, None, None, None, "pending"),
                    "existing_sources_then_cached_tdnet_statement",
                )
            else:
                work.append((company.ticker, official_url))
        elif not official_url:
            apply_discovery_result(
                conn, company.ticker,
                DiscoveryResult(company.ticker, None, None, None, None, "no_official_url"),
                "existing_sources_then_latest_tdnet_statement",
            )

    if not args.local_only:
        def fetch_official(item):
            ticker, document_id = item
            try:
                pace()
                response = requests.get(
                    f"https://www.release.tdnet.info/inbs/{document_id}.pdf",
                    headers={"User-Agent": "tdnet-company-ir-discovery/1.0 (+nightly; bounded)"},
                    timeout=(3, 15),
                )
                url = extract_official_url_from_tdnet_pdf(response.content) if response.status_code == 200 else None
                return ticker, url, response.status_code
            except requests.RequestException:
                return ticker, None, 0

        with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 4))) as pool:
            for ticker, url, status in pool.map(fetch_official, missing_pdf_work):
                if url:
                    url = normalize_url(url)
                    apply_discovery_result(
                        conn, ticker,
                        DiscoveryResult(ticker, url, None, None, None, "pending", status),
                        "latest_tdnet_statement",
                    )
                    work.append((ticker, url))
                else:
                    terminal = "no_official_url" if status == 200 else "fetch_failed"
                    apply_discovery_result(
                        conn, ticker,
                        DiscoveryResult(ticker, None, None, None, None, terminal, status),
                        "latest_tdnet_statement",
                    )
        with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 128))) as pool:
            futures = {pool.submit(discover_ir_pages, url, before_request=pace): (ticker, url) for ticker, url in work}
            for future in as_completed(futures):
                ticker, url = futures[future]
                try:
                    raw = future.result()
                    result = DiscoveryResult(ticker, raw.official_url, raw.ir_top_url, raw.ir_library_url,
                                             raw.ir_event_url, raw.status, raw.http_status, raw.error,
                                             raw.sources)
                except Exception as exc:
                    result = DiscoveryResult(ticker, url, None, None, None, "fetch_failed", error=str(exc)[:500])
                baseline_source_ids.extend(
                    apply_discovery_result(conn, ticker, result, "existing_sources_then_latest_tdnet_statement")
                )
    baseline_stats = run_monitor(
        conn, baseline_only=True, allow_notifications=False,
        hash_initial_baseline=True,
        source_ids=sorted(set(baseline_source_ids)), max_workers=min(args.workers, 64),
        request_interval_seconds=args.request_interval,
    )
    report = discovery_report(conn)
    report.update({
        "batch_selected": selected,
        "baseline_sources_attempted": baseline_stats.sources,
        "baseline_assets_added": baseline_stats.baseline,
        "baseline_fetch_failed": baseline_stats.failed_sources,
        "batch_pending_after": report["status_pending"],
    })
    print("COMPANY_IR_DISCOVERY " + json.dumps(report, ensure_ascii=False, sort_keys=True))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
