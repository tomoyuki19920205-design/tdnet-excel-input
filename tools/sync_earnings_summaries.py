#!/usr/bin/env python3
"""sync_earnings_summaries.py — earnings_summaries を Supabase に同期

Usage:
    python -m tools.sync_earnings_summaries
    python -m tools.sync_earnings_summaries --dry-run
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import requests

logger = logging.getLogger("sync_earnings")


def _load_dotenv():
    env_path = Path(_PROJECT_ROOT) / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


class _SupabaseAPI:
    def __init__(self, url: str, key: str):
        self.rest_url = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation,resolution=merge-duplicates",
        }

    def upsert(self, table: str, data: list[dict], on_conflict: str = "") -> list[dict]:
        params = {}
        if on_conflict:
            params["on_conflict"] = on_conflict
        r = requests.post(
            f"{self.rest_url}/{table}",
            headers=self.headers,
            params=params,
            json=data,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


def sync_earnings_summaries(
    db_path: str,
    supabase_url: str = "",
    supabase_key: str = "",
    dry_run: bool = False,
) -> dict:
    """SQLite earnings_summaries → Supabase push"""

    if not supabase_url or not supabase_key:
        _load_dotenv()
        supabase_url = supabase_url or os.environ.get("SUPABASE_URL", "")
        supabase_key = supabase_key or os.environ.get("SUPABASE_ANON_KEY", "")

    if not supabase_url or not supabase_key:
        raise ValueError("SUPABASE_URL / SUPABASE_ANON_KEY が未設定です")

    api = _SupabaseAPI(supabase_url, supabase_key)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # earnings_summaries テーブルの存在チェック
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='earnings_summaries'"
    ).fetchall()]
    if "earnings_summaries" not in tables:
        conn.close()
        return {"error": "earnings_summaries テーブルが存在しません", "pushed": 0}

    rows = conn.execute("SELECT * FROM earnings_summaries ORDER BY created_at").fetchall()
    conn.close()

    cols = [k for k in rows[0].keys()] if rows else []
    stats = {"total": len(rows), "pushed": 0, "skipped": 0, "errors": 0}

    if dry_run:
        logger.info(f"[SYNC] dry-run: {len(rows)} 件")
        return stats

    # バッチ upsert (50件ずつ)
    batch_size = 50
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        payload = []
        for row in batch:
            d = dict(row)
            # SQLite の id は除外（Supabase は独自PK）
            d.pop("id", None)
            payload.append(d)

        try:
            api.upsert("earnings_summaries", payload, on_conflict="fingerprint")
            stats["pushed"] += len(payload)
        except requests.HTTPError as e:
            err_body = e.response.text if e.response else str(e)
            logger.error(f"[SYNC] HTTPエラー: {err_body[:200]}")
            stats["errors"] += len(payload)
        except Exception as e:
            logger.error(f"[SYNC] エラー: {e}")
            stats["errors"] += len(payload)

    logger.info(f"[SYNC] 完了: total={stats['total']} pushed={stats['pushed']} errors={stats['errors']}")
    return stats


def main():
    if sys.stdout and hasattr(sys.stdout, "encoding"):
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="earnings_summaries → Supabase sync")
    parser.add_argument("--db", default=str(Path(_PROJECT_ROOT) / "decision_db.db"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")

    stats = sync_earnings_summaries(db_path=args.db, dry_run=args.dry_run)

    print()
    print("=" * 55)
    print("  earnings_summaries → Supabase sync")
    print("=" * 55)
    for k, v in stats.items():
        print(f"  {k:20s}: {v}")
    print("=" * 55)


if __name__ == "__main__":
    main()
