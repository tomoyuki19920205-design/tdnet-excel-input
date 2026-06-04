#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_detected_at.py — Supabase tdnet_events の detected_at を正しい日付に修正

問題:
  SQLite events.disclosure_datetime が "18:00" 形式（時刻のみ）のため
  _sanitize_timestamp が fallback(now) を使い detected_at=6/4 になっている。

対応:
  - バックフィル由来レコード（created_at=2026-06-04, event_type in forecast/dividend/buyback）
  - dedupe_key で SQLite の fingerprint と突合
  - SQLite の first_seen_at（= 実際の検知日時）を detected_at に UPDATE
  - Discord通知なし・データ削除なし

Usage:
  python tools/fix_detected_at.py --dry-run
  python tools/fix_detected_at.py
"""
from __future__ import annotations
import argparse, logging, sqlite3, sys, os
from datetime import datetime, timezone, timedelta
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.pipeline.db import load_env
from src.events.tdnet_event_store import build_dedupe_key
from src.events.common_models import EventRecord

logger = logging.getLogger("fix_detected_at")
JST = timezone(timedelta(hours=9))


def sqlite_rows_in_range(db_path: str, since: str, until: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT event_id, ticker, company_name, event_type, subtype,
               fingerprint, first_seen_at, disclosure_datetime,
               raw_payload_json, extracted_payload_json, summary_text, title,
               source_doc_id, status
        FROM events
        WHERE event_type IN ('buyback','forecast_revision','dividend_revision')
          AND first_seen_at >= ? AND first_seen_at < ?
        ORDER BY first_seen_at
        """,
        (since, until)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def run(*, dry_run: bool = False, since: str = "2026-04-01", until: str = "2026-06-05") -> dict:
    load_env(_PROJECT_ROOT)
    from supabase import create_client
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    sb = create_client(url, key)

    db_path = str(Path(_PROJECT_ROOT) / "decision_db.db")

    # SQLite のデータを全件取得して dedupe_key → first_seen_at マップを作成
    logger.info(f"[FIX] Loading SQLite events {since}〜{until}...")
    sqlite_rows = sqlite_rows_in_range(db_path, since, until)
    logger.info(f"[FIX] SQLite rows: {len(sqlite_rows)}")

    # dedupe_key を計算（save_event_to_supabase と同じロジック）
    dedup_to_first_seen: dict[str, str] = {}
    for r in sqlite_rows:
        # EventRecord を構築して dedupe_key を計算
        rec = EventRecord(
            event_id=r.get("event_id",""),
            source_doc_id=r.get("source_doc_id",""),
            ticker=r.get("ticker",""),
            company_name=r.get("company_name",""),
            disclosure_datetime=r.get("disclosure_datetime",""),
            title=r.get("title",""),
            event_type=r.get("event_type",""),
            subtype=r.get("subtype",""),
            importance=50,
            summary_text=r.get("summary_text","") or "",
            raw_payload_json=r.get("raw_payload_json","") or "{}",
            extracted_payload_json=r.get("extracted_payload_json","") or "{}",
            fingerprint=r.get("fingerprint",""),
            status=r.get("status","new"),
            first_seen_at=r.get("first_seen_at",""),
        )
        try:
            dkey = build_dedupe_key(rec)
            fs = r.get("first_seen_at","")
            if dkey and fs:
                dedup_to_first_seen[dkey] = fs
        except Exception as e:
            logger.warning(f"[FIX] dedupe_key error ticker={r.get('ticker')}: {e}")

    logger.info(f"[FIX] dedupe_key map: {len(dedup_to_first_seen)}件")

    # Supabase から created_at=6/4 かつ forecast/dividend/buyback のレコードを取得
    logger.info("[FIX] Fetching Supabase records to fix...")
    sb_res = sb.table("tdnet_events").select("id,dedupe_key,detected_at,event_type,ticker").gte("created_at","2026-06-04").in_("event_type",["forecast","dividend","buyback"]).execute()
    sb_rows = sb_res.data
    logger.info(f"[FIX] Supabase target rows: {len(sb_rows)}")

    updated = 0
    skipped_no_match = 0
    skipped_already_correct = 0
    errors = 0

    for sb_row in sb_rows:
        dkey = sb_row.get("dedupe_key","")
        if not dkey:
            skipped_no_match += 1
            continue

        first_seen = dedup_to_first_seen.get(dkey)
        if not first_seen:
            skipped_no_match += 1
            logger.debug(f"[FIX] no match: dedupe={dkey[:12]} ticker={sb_row.get('ticker')}")
            continue

        # first_seen_at → JST iso → UTC iso
        try:
            # first_seen_at は JST で保存されている
            if "+" in first_seen or "Z" in first_seen:
                dt_jst = datetime.fromisoformat(first_seen)
            else:
                dt_jst = datetime.fromisoformat(first_seen).replace(tzinfo=JST)
            new_detected_at = dt_jst.astimezone(timezone.utc).isoformat()
        except Exception as e:
            logger.warning(f"[FIX] ts parse error: {first_seen}: {e}")
            errors += 1
            continue

        # 既に正しい値なら skip
        current = sb_row.get("detected_at","")
        if current and str(current)[:10] == str(new_detected_at)[:10]:
            skipped_already_correct += 1
            continue

        logger.info(
            f"[FIX] {'DRY-RUN ' if dry_run else ''}UPDATE: "
            f"id={sb_row['id']} ticker={sb_row.get('ticker')} type={sb_row.get('event_type')} "
            f"detected_at: {str(current)[:16]} -> {str(new_detected_at)[:16]}"
        )

        if not dry_run:
            try:
                sb.table("tdnet_events").update({"detected_at": new_detected_at}).eq("id", sb_row["id"]).execute()
                updated += 1
            except Exception as e:
                logger.error(f"[FIX] UPDATE error id={sb_row['id']}: {e}")
                errors += 1
        else:
            updated += 1  # dry-runは件数だけカウント

    logger.info(
        f"[FIX] done: updated={updated} skipped_no_match={skipped_no_match} "
        f"already_correct={skipped_already_correct} errors={errors}"
    )
    return {
        "updated": updated,
        "skipped_no_match": skipped_no_match,
        "skipped_already_correct": skipped_already_correct,
        "errors": errors,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description="Supabase tdnet_events detected_at 修正")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--since", default="2026-04-01")
    parser.add_argument("--until", default="2026-06-05")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run, since=args.since, until=args.until)
    print()
    print("=" * 55)
    print("  FIX_DETECTED_AT SUMMARY")
    print("=" * 55)
    print(f"  updated            : {result['updated']}")
    print(f"  skipped_no_match   : {result['skipped_no_match']}")
    print(f"  already_correct    : {result['skipped_already_correct']}")
    print(f"  errors             : {result['errors']}")
    print("=" * 55)


if __name__ == "__main__":
    main()
