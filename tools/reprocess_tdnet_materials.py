#!/usr/bin/env python3
"""Targeted, idempotent TDNET metadata-material reprocessing.

This command never deletes or bulk-rebuilds notifications.  It reads official
J-Quants TDNET listing metadata, validates each selected PDF, then routes only
the explicitly requested disclosure numbers through the metadata-only event
pipeline.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.events.common_models import DocumentMeta
from src.events.env_loader import load_project_env
from src.events.event_pipeline import process_documents
from src.fetcher import _filter_linkable_materials
from src.jquants.adapter import fetch_jquants_disclosures


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", action="append", required=True, help="JST listing date (YYYYMMDD)")
    parser.add_argument("--disc-no", action="append", required=True, help="Exact 14-digit TDNET DiscNo")
    parser.add_argument("--db-path", default=str(PROJECT_ROOT / "decision_db.db"))
    parser.add_argument("--retry-db-path", default=str(PROJECT_ROOT / "data" / "material_url_retry.db"))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    load_project_env()
    requested = set(args.disc_no)
    found = {}
    for date_str in dict.fromkeys(args.date):
        for disclosure in fetch_jquants_disclosures(date_str):
            if disclosure.disc_no in requested:
                found[disclosure.disc_no] = disclosure

    missing = sorted(requested - set(found))
    if missing:
        print(json.dumps({"status": "error", "missing_disc_no": missing}, ensure_ascii=False))
        return 2

    items = [found[disc_no].to_disclosure_item() for disc_no in args.disc_no]
    validated = _filter_linkable_materials(
        items,
        retry_db_path=args.retry_db_path,
        source="targeted_jquants_reprocess",
    )
    validated_ids = {item.source_doc_id or item.disclosure_id for item in validated}
    validation_missing = [
        item.source_doc_id or item.disclosure_id
        for item in items
        if (item.source_doc_id or item.disclosure_id) not in validated_ids
    ]
    if validation_missing:
        print(json.dumps({"status": "error", "pdf_validation_failed": validation_missing}, ensure_ascii=False))
        return 3

    docs = [
        DocumentMeta(
            doc_id=item.disclosure_id,
            source_doc_id=item.source_doc_id or item.disclosure_id,
            ticker=item.ticker,
            company_name=item.company_name,
            title=item.title,
            disclosure_datetime=item.published_at,
            doc_url=item.doc_url,
            link_validated=True,
        )
        for item in validated
    ]
    result = process_documents(
        docs,
        args.db_path,
        dry_run=args.dry_run,
        webhook_url="",
    )
    details = []
    for doc, detail in zip(docs, result.details):
        details.append({
            "disc_no": doc.source_doc_id,
            "ticker": doc.ticker,
            "title": doc.title,
            "pdf_url": doc.doc_url,
            "event_type": detail.get("event_type"),
            "action": detail.get("action"),
            "supabase_action": detail.get("supabase_action"),
        })
    print(json.dumps({
        "status": "ok",
        "dry_run": args.dry_run,
        "processed": result.processed,
        "detected": result.detected,
        "saved": result.saved,
        "supabase_saved": result.supabase_saved,
        "supabase_dedup_skipped": result.supabase_dedup_skipped,
        "errors": result.errors,
        "details": details,
    }, ensure_ascii=False, indent=2))
    return 0 if result.errors == 0 else 4


if __name__ == "__main__":
    raise SystemExit(main())
