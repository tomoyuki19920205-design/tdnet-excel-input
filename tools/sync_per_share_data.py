#!/usr/bin/env python3
"""
sync_per_share_data.py — SQLite per_share_data → Supabase per_share_data 同期

使い方:
  python tools/sync_per_share_data.py              # ドライラン
  python tools/sync_per_share_data.py --apply      # 本番反映
  python tools/sync_per_share_data.py --apply --full  # 全量
"""
import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _PROJECT_ROOT)
_DEFAULT_DB = os.path.join(_PROJECT_ROOT, "data", "jquants.db")
_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs")

_BATCH_SIZE = 500
_RETRY_MAX = 5
_RETRY_BASE_SEC = 1.0
JST = timezone(timedelta(hours=9))

logger = logging.getLogger("sync_per_share_data")


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
            "Prefer": "return=headers-only,resolution=merge-duplicates",
        }

    def _request(self, method, url, **kwargs):
        last_exc = None
        for attempt in range(_RETRY_MAX):
            try:
                r = requests.request(method, url, timeout=60, **kwargs)
                r.raise_for_status()
                return r
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                time.sleep(_RETRY_BASE_SEC * (2 ** attempt))
            except requests.HTTPError as e:
                status = e.response.status_code if e.response else 0
                if status == 429 or status >= 500:
                    last_exc = e
                    time.sleep(_RETRY_BASE_SEC * (2 ** attempt))
                else:
                    raise
        raise last_exc

    def upsert_batch(self, table, data, on_conflict=""):
        if not data:
            return 0
        params = {}
        if on_conflict:
            params["on_conflict"] = on_conflict
        self._request("POST", f"{self.rest_url}/{table}",
                       headers=self.headers, params=params, json=data)
        return len(data)


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


_COLS = [
    "ticker", "period", "quarter", "disclosed_date",
    "eps", "diluted_eps", "bps",
    "dividend_q1", "dividend_q2", "dividend_q3", "dividend_fy_end",
    "dividend_annual", "payout_ratio",
    "forecast_eps", "forecast_dividend_annual", "forecast_payout_ratio",
    "shares_outstanding", "treasury_stock", "avg_shares",
    "total_assets", "equity", "equity_ratio",
    "source", "updated_at",
]


def read_sqlite(db_path: str, limit: int = 0):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    query = f"SELECT {', '.join(_COLS)} FROM per_share_data ORDER BY ticker, period DESC"
    if limit > 0:
        query += f" LIMIT {limit}"

    rows = conn.execute(query).fetchall()
    conn.close()

    now_iso = datetime.now(JST).isoformat()
    data = []
    for r in rows:
        rec = {c: r[c] for c in _COLS}
        rec["updated_at"] = now_iso
        data.append(rec)
    return data


def sync(db_path, supabase_url, supabase_key, dry_run=True, limit=0):
    data = read_sqlite(db_path, limit=limit)
    logger.info(f"[SYNC] {len(data):,} rows from SQLite per_share_data")

    if not data:
        logger.warning("[SYNC] 0件。")
        return {"upserted": 0, "errors": 0}

    if dry_run:
        logger.info(f"\n{'='*60}\n  DRY-RUN: {len(data):,} rows → per_share_data\n{'='*60}")
        # サンプル
        for rec in data[:3]:
            logger.info(f"  {rec['ticker']} {rec['period']} {rec['quarter']} "
                        f"EPS={rec['eps']} BPS={rec['bps']} 配当={rec['dividend_annual']}")
        return {"upserted": 0, "errors": 0}

    api = _SupabaseAPI(supabase_url, supabase_key)
    total = 0
    errors = 0
    for i, chunk in enumerate(_chunks(data, _BATCH_SIZE), 1):
        try:
            n = api.upsert_batch("per_share_data", chunk, on_conflict="ticker,period,quarter")
            total += n
            logger.info(f"  batch {i}: {n} rows (累計 {total:,})")
        except Exception as e:
            errors += 1
            logger.error(f"  batch {i}: FAILED — {e}")

    logger.info(f"[SYNC] 完了: upserted={total:,} errors={errors}")
    return {"upserted": total, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description="SQLite per_share_data → Supabase 同期")
    parser.add_argument("--sqlite", default=_DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    is_dry_run = not args.apply

    os.makedirs(_LOG_DIR, exist_ok=True)
    ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    mode = "dryrun" if is_dry_run else "apply"
    log_file = os.path.join(_LOG_DIR, f"sync_per_share_{mode}_{ts}.log")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    _load_dotenv()
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_ANON_KEY", "")
    if not url or not key:
        logger.error("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 未設定")
        sys.exit(1)

    stats = sync(args.sqlite, url, key, dry_run=is_dry_run, limit=args.limit)
    sys.exit(1 if stats["errors"] > 0 else 0)


if __name__ == "__main__":
    main()
