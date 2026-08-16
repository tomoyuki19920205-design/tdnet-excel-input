#!/usr/bin/env python3
"""
sync_market_data.py — SQLite market_data → Supabase market_data 同期

使い方:
  python tools/sync_market_data.py              # ドライラン (直近30日)
  python tools/sync_market_data.py --apply      # 本番反映 (直近30日)
  python tools/sync_market_data.py --apply --full  # 全量
"""
import argparse
import hashlib
import json
import logging
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _PROJECT_ROOT)
from tools.file_lock import FileLock, read_lock_metadata

_DEFAULT_DB = os.path.join(_PROJECT_ROOT, "data", "jquants.db")
_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs")

# Keep market-data writes deliberately below the throughput that can interfere
# with the realtime pipeline and Company Viewer reads.
_BATCH_SIZE = 500
_BATCH_INTERVAL_SEC = 0.75
_RETRY_MAX = 3
_RETRY_BASE_SEC = 1.0
_RETRY_JITTER_SEC = 0.5
_CONNECT_TIMEOUT_SEC = 10
_READ_TIMEOUT_SEC = 30
_CIRCUIT_BREAKER_FAILURES = 3
_REALTIME_POLL_SEC = 15
_BLACKOUT_START = (15, 20)
_BLACKOUT_END = (16, 10)
_SYNC_LOCK_NAME = "market_data_sync"
_SYNC_LOCK_MAX_AGE_MINUTES = 5
_REALTIME_LOCK_MAX_AGE_MINUTES = 15
_LOCK_DIR = os.path.join(_PROJECT_ROOT, "state", "locks")
_DEFAULT_RECENT_DAYS = 30
JST = timezone(timedelta(hours=9))
MARKET_DATA_RETENTION_YEARS = 1

logger = logging.getLogger("sync_market_data")


def market_data_retention_start(reference: datetime | None = None) -> str:
    today = (reference or datetime.now(JST)).date()
    try:
        prior = today.replace(year=today.year - MARKET_DATA_RETENTION_YEARS)
    except ValueError:
        prior = today.replace(year=today.year - MARKET_DATA_RETENTION_YEARS, day=28)
    return (prior + timedelta(days=1)).isoformat()


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
                r = requests.request(
                    method,
                    url,
                    timeout=(_CONNECT_TIMEOUT_SEC, _READ_TIMEOUT_SEC),
                    **kwargs,
                )
                r.raise_for_status()
                return r
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                if attempt + 1 >= _RETRY_MAX:
                    break
                delay = _RETRY_BASE_SEC * (2 ** attempt) + random.uniform(0, _RETRY_JITTER_SEC)
                logger.warning(
                    "[RETRY] attempt=%d/%d delay_sec=%.2f error=%s",
                    attempt + 1, _RETRY_MAX, delay, type(e).__name__,
                )
                time.sleep(delay)
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                if status == 429 or status >= 500:
                    last_exc = e
                    if attempt + 1 >= _RETRY_MAX:
                        break
                    delay = _RETRY_BASE_SEC * (2 ** attempt) + random.uniform(0, _RETRY_JITTER_SEC)
                    logger.warning(
                        "[RETRY] attempt=%d/%d status=%d delay_sec=%.2f",
                        attempt + 1, _RETRY_MAX, status, delay,
                    )
                    time.sleep(delay)
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


def read_sqlite(db_path: str, recent_days: int = 0, limit: int = 0):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    has_universe = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_data_universe'"
    ).fetchone() is not None
    where_parts = []
    params = []
    where_parts.append("m.date >= ?")
    params.append(market_data_retention_start())
    if recent_days > 0:
        since = (datetime.now(JST) - timedelta(days=recent_days)).strftime("%Y-%m-%d")
        where_parts.append("m.date >= ?")
        params.append(since)
    # Once the qualified universe table exists, only records covered by the
    # J-Quants price source are allowed upstream.  Ordinary-stock identity is
    # a separate decision and must not imply J-Quants price coverage.
    if has_universe:
        universe_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(market_data_universe)")
        }
        eligibility_column = (
            "is_jquants_price_eligible"
            if "is_jquants_price_eligible" in universe_columns
            else "is_common_stock"
        )
        where_parts.append("EXISTS (SELECT 1 FROM market_data_universe u "
                           "WHERE u.date=m.date AND u.ticker=m.ticker "
                           f"AND u.{eligibility_column}=1)")
    where = "WHERE " + " AND ".join(where_parts) if where_parts else ""

    query = f"""
        SELECT m.ticker, m.date, m.open, m.high, m.low, m.close, m.volume,
               m.turnover, m.adj_factor, m.adj_close, m.adj_volume, m.market_cap
        FROM market_data m
        {where}
        ORDER BY m.date DESC, m.ticker
    """
    if limit > 0:
        query += f" LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    now_iso = datetime.now(JST).isoformat()
    data = []
    for r in rows:
        data.append({
            "ticker": r["ticker"],
            "date": r["date"],
            "open": r["open"],
            "high": r["high"],
            "low": r["low"],
            "close": r["close"],
            "volume": r["volume"],
            "turnover": r["turnover"],
            "adj_factor": r["adj_factor"],
            "adj_close": r["adj_close"],
            "adj_volume": r["adj_volume"],
            "market_cap": r["market_cap"],
            "source": "jquants",
            "fetched_at": now_iso,
        })
    return data


def _payload_hash(row: dict) -> str:
    """Hash values that matter to the remote record (not sync timestamp)."""
    payload = {key: value for key, value in row.items() if key not in {"fetched_at", "_sync_hash"}}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _open_ledger(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_data_sync_ledger (
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            synced_at TEXT NOT NULL,
            PRIMARY KEY (ticker, date)
        )
    """)
    conn.commit()
    return conn


def _pending_rows(data: list[dict], ledger: sqlite3.Connection) -> tuple[list[dict], int]:
    # On the initial full sync this short-circuit avoids millions of point lookups.
    if ledger.execute("SELECT 1 FROM market_data_sync_ledger LIMIT 1").fetchone() is None:
        for row in data:
            row["_sync_hash"] = _payload_hash(row)
        return data, 0
    pending: list[dict] = []
    unchanged = 0
    lookup = ledger.execute
    for row in data:
        digest = _payload_hash(row)
        previous = lookup(
            "SELECT payload_hash FROM market_data_sync_ledger WHERE ticker=? AND date=?",
            (row["ticker"], row["date"]),
        ).fetchone()
        if previous is not None and previous[0] == digest:
            unchanged += 1
            continue
        row["_sync_hash"] = digest
        pending.append(row)
    return pending, unchanged


def _record_synced(ledger: sqlite3.Connection, rows: list[dict]) -> None:
    synced_at = datetime.now(JST).isoformat()
    ledger.executemany(
        """INSERT INTO market_data_sync_ledger(ticker, date, payload_hash, synced_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(ticker, date) DO UPDATE SET
             payload_hash=excluded.payload_hash, synced_at=excluded.synced_at""",
        [(row["ticker"], row["date"], row["_sync_hash"], synced_at) for row in rows],
    )
    ledger.commit()


def _is_full_sync_blackout(reference: datetime | None = None) -> bool:
    now = (reference or datetime.now(JST)).astimezone(JST)
    current = (now.hour, now.minute, now.second, now.microsecond)
    start = (*_BLACKOUT_START, 0, 0)
    end = (*_BLACKOUT_END, 0, 0)
    return start <= current < end


def _lock_is_active(name: str, state_dir: str, max_age_minutes: int) -> bool:
    lock = FileLock(name, max_age_minutes=max_age_minutes, state_dir=state_dir)
    return os.path.exists(lock.lock_path) and not lock.is_stale()


def _realtime_lock_active(state_dir: str = _LOCK_DIR) -> bool:
    return _lock_is_active("realtime", state_dir, _REALTIME_LOCK_MAX_AGE_MINUTES)


def _log_lock_holder(lock_path: str) -> None:
    meta = read_lock_metadata(lock_path)
    logger.error(
        "[SYNC_LOCK] acquire rejected holder_pid=%s acquired_at=%s command=%s",
        meta.get("pid"), meta.get("started_at"),
        meta.get("command_line") or meta.get("script_name"),
    )


def _wait_for_realtime_clear(state_dir: str, sleep_fn=time.sleep) -> int:
    pauses = 0
    while _realtime_lock_active(state_dir):
        pauses += 1
        meta = read_lock_metadata(os.path.join(state_dir, "realtime.lock"))
        logger.warning(
            "[REALTIME_PAUSE] pause=%d holder_pid=%s acquired_at=%s sleep_sec=%s",
            pauses, meta.get("pid"), meta.get("started_at"), _REALTIME_POLL_SEC,
        )
        sleep_fn(_REALTIME_POLL_SEC)
    if pauses:
        logger.info("[REALTIME_PAUSE] released; resuming after %d poll(s)", pauses)
    return pauses


def _sync_with_lock(
    db_path, supabase_url, supabase_key, *, recent_days, limit,
    state_dir, sleep_fn,
):
    data = read_sqlite(db_path, recent_days=recent_days, limit=limit)
    logger.info(f"[SYNC] {len(data):,} rows from SQLite")

    if not data:
        logger.warning("[SYNC] 0件。同期対象なし。")
        return {"upserted": 0, "errors": 0, "dry_run": False,
                "pending": 0, "realtime_pauses": 0}

    ledger = _open_ledger(db_path)
    try:
        data, unchanged = _pending_rows(data, ledger)
        logger.info(f"[SYNC] pending={len(data):,} unchanged={unchanged:,}")
        if not data:
            return {"upserted": 0, "unchanged": unchanged, "errors": 0,
                    "dry_run": False, "pending": 0, "realtime_pauses": 0}

        api = _SupabaseAPI(supabase_url, supabase_key)
        total = 0
        errors = 0
        attempted = 0
        succeeded_batches = 0
        failed_batches = 0
        consecutive_failures = 0
        realtime_pauses = 0
        chunks = list(_chunks(data, _BATCH_SIZE))
        for i, chunk in enumerate(chunks, 1):
            realtime_pauses += _wait_for_realtime_clear(state_dir, sleep_fn)
            attempted += len(chunk)
            try:
                payload = [{key: value for key, value in row.items() if key != "_sync_hash"} for row in chunk]
                n = api.upsert_batch("market_data", payload, on_conflict="ticker,date")
                _record_synced(ledger, chunk)
                total += n
                succeeded_batches += 1
                consecutive_failures = 0
                logger.info(
                    "  batch %d/%d: %d rows (累計 %s)",
                    i, len(chunks), n, f"{total:,}",
                )
            except Exception as e:
                errors += 1
                failed_batches += 1
                consecutive_failures += 1
                logger.error(
                    "  batch %d/%d: FAILED consecutive=%d — %s",
                    i, len(chunks), consecutive_failures, e,
                )
                if consecutive_failures >= _CIRCUIT_BREAKER_FAILURES:
                    logger.error(
                        "[CIRCUIT_BREAKER] OPEN consecutive_failures=%d; stopping sync",
                        consecutive_failures,
                    )
                    break
            if i < len(chunks):
                sleep_fn(_BATCH_INTERVAL_SEC)

        remaining = len(data) - total
        stats = {
            "upserted": total, "unchanged": unchanged, "errors": errors,
            "dry_run": False, "pending": remaining,
            "batches_total": len(chunks), "batches_succeeded": succeeded_batches,
            "batches_failed": failed_batches, "rows_attempted": attempted,
            "realtime_pauses": realtime_pauses,
            "circuit_breaker_open": consecutive_failures >= _CIRCUIT_BREAKER_FAILURES,
        }
        logger.info("[SYNC] 完了: %s", stats)
        return stats
    finally:
        ledger.close()


def sync(db_path, supabase_url, supabase_key, dry_run=True,
         recent_days=_DEFAULT_RECENT_DAYS, limit=0, *,
         state_dir=_LOCK_DIR, reference_time=None, sleep_fn=time.sleep):
    """Synchronize market data with an apply-only, process-wide safety guard."""
    is_full = recent_days == 0

    if dry_run:
        data = read_sqlite(db_path, recent_days=recent_days, limit=limit)
        logger.info(f"[SYNC] {len(data):,} rows from SQLite")
        if not data:
            logger.warning("[SYNC] 0件。同期対象なし。")
            return {"upserted": 0, "errors": 0, "dry_run": True}
        logger.info(f"\n{'='*60}\n  DRY-RUN: {len(data):,} rows → market_data\n{'='*60}")
        return {"upserted": 0, "errors": 0, "dry_run": True}

    sync_lock = FileLock(
        _SYNC_LOCK_NAME,
        max_age_minutes=_SYNC_LOCK_MAX_AGE_MINUTES,
        state_dir=state_dir,
    )
    if not sync_lock.acquire():
        _log_lock_holder(sync_lock.lock_path)
        return {"upserted": 0, "errors": 1, "dry_run": False,
                "rejected": "single_instance_lock"}

    try:
        if is_full and _is_full_sync_blackout(reference_time):
            logger.error("[SYNC_GATE] full apply rejected during 15:20-16:10 JST blackout")
            return {"upserted": 0, "errors": 1, "dry_run": False,
                    "rejected": "realtime_blackout"}
        if is_full and _realtime_lock_active(state_dir):
            meta = read_lock_metadata(os.path.join(state_dir, "realtime.lock"))
            logger.error(
                "[SYNC_GATE] full apply rejected: realtime active holder_pid=%s acquired_at=%s command=%s",
                meta.get("pid"), meta.get("started_at"),
                meta.get("command_line") or meta.get("script_name"),
            )
            return {"upserted": 0, "errors": 1, "dry_run": False,
                    "rejected": "realtime_active"}
        return _sync_with_lock(
            db_path, supabase_url, supabase_key,
            recent_days=recent_days, limit=limit,
            state_dir=state_dir, sleep_fn=sleep_fn,
        )
    finally:
        sync_lock.release()


def main():
    parser = argparse.ArgumentParser(description="SQLite market_data → Supabase 同期")
    parser.add_argument("--sqlite", default=_DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--recent", type=int, default=_DEFAULT_RECENT_DAYS)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    is_dry_run = not args.apply
    recent_days = 0 if args.full else args.recent

    os.makedirs(_LOG_DIR, exist_ok=True)
    ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    mode = "dryrun" if is_dry_run else "apply"
    log_file = os.path.join(_LOG_DIR, f"sync_market_data_{mode}_{ts}.log")

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

    stats = sync(args.sqlite, url, key, dry_run=is_dry_run,
                 recent_days=recent_days, limit=args.limit)
    sys.exit(1 if stats["errors"] > 0 else 0)


if __name__ == "__main__":
    main()
