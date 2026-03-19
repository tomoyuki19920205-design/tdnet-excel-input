#!/usr/bin/env python3
# ============================================================
# quarantine_review.py — Quarantine 統合レビュー CLI
# ============================================================
"""
show_quarantine.py + quarantine_review_segment.jsonl を統合表示。
CI / cron 監視対応 (--fail-on-items)。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger("pipeline.quarantine")


def _load_sqlite_quarantine(
    db_path: str,
    *,
    limit: int = 0,
    ticker: str = "",
    stage: str = "",
) -> list[dict]:
    """SQLite quarantine テーブルからレコードを読む"""
    records: list[dict] = []
    if not os.path.exists(db_path):
        return records

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    query = "SELECT * FROM quarantine WHERE 1=1"
    params: list = []

    if ticker:
        query += " AND company_code = ?"
        params.append(ticker)
    if stage:
        query += " AND failed_stage = ?"
        params.append(stage)

    query += " ORDER BY id DESC"
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    try:
        for row in conn.execute(query, params):
            records.append(dict(row))
    except Exception as e:
        logger.warning(f"SQLite quarantine read error: {e}")
    finally:
        conn.close()

    return records


def _load_jsonl_review(
    review_dir: str,
    *,
    limit: int = 0,
    ticker: str = "",
    stage: str = "",
) -> list[dict]:
    """review/quarantine_review_segment.jsonl からレコードを読む"""
    records: list[dict] = []
    jsonl_path = os.path.join(review_dir, "quarantine_review_segment.jsonl")

    if not os.path.exists(jsonl_path):
        return records

    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if ticker and rec.get("ticker", "") != ticker:
                    continue
                if stage and rec.get("failed_stage", "") != stage:
                    continue

                records.append(rec)
    except Exception as e:
        logger.warning(f"JSONL read error: {e}")

    if limit > 0:
        records = records[-limit:]

    return records


def run(
    *,
    limit: int = 50,
    ticker: str = "",
    stage: str = "",
    fail_on_items: bool = False,
) -> dict:
    """
    Quarantine 統合レビューを実行。

    Returns:
        {"total": int, "sqlite_count": int, "jsonl_count": int, ...}
    """
    db_path = os.path.join(_PROJECT_ROOT, "decision_db.db")
    review_dir = os.environ.get(
        "TDNET_REVIEW_DIR",
        os.path.join(_PROJECT_ROOT, "review"),
    )

    # SQLite quarantine
    sqlite_records = _load_sqlite_quarantine(
        db_path, limit=limit, ticker=ticker, stage=stage,
    )
    # JSONL review
    jsonl_records = _load_jsonl_review(
        review_dir, limit=limit, ticker=ticker, stage=stage,
    )

    total = len(sqlite_records) + len(jsonl_records)

    # 表示
    if sqlite_records:
        print(f"\n{'='*60}")
        print(f"  SQLite Quarantine: {len(sqlite_records)} 件")
        print(f"{'='*60}")
        for rec in sqlite_records:
            code = rec.get("company_code", "?")
            reason = rec.get("quarantine_reason", rec.get("failed_stage", "?"))
            period = rec.get("fiscal_year_end", "?")
            quarter = rec.get("quarter", "?")
            hint = rec.get("review_hint", "")
            print(f"  {code} | {period} {quarter} | {reason}")
            if hint:
                print(f"    hint: {hint}")

    if jsonl_records:
        print(f"\n{'='*60}")
        print(f"  JSONL Review (segment): {len(jsonl_records)} 件")
        print(f"{'='*60}")
        for rec in jsonl_records:
            tk = rec.get("ticker", "?")
            reason = rec.get("quarantine_reason", "?")
            source = rec.get("source_file", "?")
            score = rec.get("best_table_score", "?")
            print(f"  {tk} | {reason} | score={score}")
            print(f"    source: {source}")

            # column_diagnosis があれば表示
            diag = rec.get("column_diagnosis", {})
            if diag:
                roles = diag.get("candidate_column_roles", [])
                if roles:
                    print(f"    roles: {roles[:6]}")

    if total == 0:
        print("\n  ✅ quarantine 0 件\n")

    print(f"\n  合計: {total} 件 (sqlite={len(sqlite_records)}, jsonl={len(jsonl_records)})")

    result = {
        "total": total,
        "sqlite_count": len(sqlite_records),
        "jsonl_count": len(jsonl_records),
    }

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Quarantine 統合レビュー CLI",
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--ticker", default="")
    parser.add_argument("--stage", default="")
    parser.add_argument(
        "--fail-on-items",
        action="store_true",
        help="quarantine 件数 > 0 なら exit 1",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    result = run(
        limit=args.limit,
        ticker=args.ticker,
        stage=args.stage,
        fail_on_items=args.fail_on_items,
    )

    if args.fail_on_items and result["total"] > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
