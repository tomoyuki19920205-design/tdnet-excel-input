#!/usr/bin/env python3
"""Retry queued material URLs and publish recovered Viewer events."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.events.common_models import DocumentMeta
from src.events.env_loader import load_project_env
from src.events.event_pipeline import process_documents
from src.material_url_retry import (
    RetryCandidate, connect_retry_db, list_status_counts, run_due_retries,
)


def _publisher(event_db_path: str, company_ir_db_path: str, dry_run: bool):
    def publish(candidate: RetryCandidate) -> bool:
        if candidate.source == "company_ir":
            import requests
            from src.company_ir_monitor import (
                IrAsset, IrSource, _default_publish, hash_remote_pdf, init_db,
            )

            session = requests.Session()
            content_hash = hash_remote_pdf(candidate.document_url, session)
            if not content_hash:
                return False
            source = IrSource(0, candidate.ticker, candidate.company_name, candidate.source_page_url)
            asset = IrAsset(
                "earnings_material", candidate.title, candidate.document_url,
                candidate.source_page_url, content_hash,
            )
            if not _default_publish(source, asset, candidate.disclosure_datetime, dry_run):
                return False
            if not dry_run:
                company_conn = sqlite3.connect(company_ir_db_path, timeout=30)
                try:
                    init_db(company_conn)
                    company_conn.execute("""
                        UPDATE company_ir_assets
                        SET content_sha256=?,notified=1,notified_at=?,suppression_reason=NULL,
                            notification_status='notified'
                        WHERE asset_key=?
                    """, (content_hash, candidate.disclosure_datetime, candidate.source_key))
                    company_conn.commit()
                finally:
                    company_conn.close()
            return True
        result = process_documents(
            [DocumentMeta(
                doc_id=candidate.source_key,
                ticker=candidate.ticker,
                company_name=candidate.company_name,
                title=candidate.title,
                disclosure_datetime=candidate.disclosure_datetime,
                doc_url=candidate.document_url,
                source_doc_id=candidate.source_doc_id,
                link_validated=True,
            )],
            event_db_path,
            dry_run=dry_run,
            webhook_url="",
        )
        # saved=0 is a successful duplicate recovery.  The event store's
        # unique dedupe key is the final authority across competing runners.
        return result.errors == 0 and result.processed == 1 and result.detected == 1
    return publish


def _sync_company_ir_terminal_states(retry_conn, company_ir_db_path: str) -> None:
    """Prevent company_ir url_unverified audit rows accumulating forever."""
    rows = retry_conn.execute("""
        SELECT source_key,status FROM material_url_retries
        WHERE source='company_ir' AND status IN ('invalid_url','archived')
    """).fetchall()
    if not rows:
        return
    conn = sqlite3.connect(company_ir_db_path, timeout=30)
    try:
        for row in rows:
            conn.execute("""
                UPDATE company_ir_assets
                SET notification_status=?,suppression_reason=?
                WHERE asset_key=? AND notified=0
            """, (row["status"], row["status"], row["source_key"]))
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    load_project_env()
    parser = argparse.ArgumentParser(description="Retry unverified material URLs")
    parser.add_argument("--db", default="data/material_url_retry.db")
    parser.add_argument("--event-db", default="decision_db.db")
    parser.add_argument("--company-ir-db", default="data/company_ir_monitor.db")
    parser.add_argument("--runner", default="manual")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    conn = connect_retry_db(ROOT / args.db)
    try:
        if args.dry_run:
            counts = list_status_counts(conn)
            print("MATERIAL_URL_RETRY " + json.dumps({
                "dry_run": True,
                "pending": counts["pending_retry"],
                "valid": counts["valid"],
                "invalid_url": counts["invalid_url"],
                "archived": counts["archived"],
            }, ensure_ascii=False, sort_keys=True))
            return 0
        result = run_due_retries(
            conn,
            publish=_publisher(
                str(ROOT / args.event_db), str(ROOT / args.company_ir_db), args.dry_run,
            ),
            runner=args.runner,
            limit=args.limit,
        )
        _sync_company_ir_terminal_states(conn, str(ROOT / args.company_ir_db))
        print("MATERIAL_URL_RETRY " + json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1 if result["publish_failed"] else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
