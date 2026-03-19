#!/usr/bin/env python3
"""
fetch_jquants_prices.py — J-Quants V2 /equities/bars/daily から株価データ取得

使い方:
  python tools/fetch_jquants_prices.py --recent          # 直近7日
  python tools/fetch_jquants_prices.py --since 2025-01-01
  python tools/fetch_jquants_prices.py --code 72030 --recent  # 1銘柄テスト
  python tools/fetch_jquants_prices.py --resume           # 前回の続き
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, str(Path(__file__).parent))

from jquants_auth import get_auth_headers
from src.common_ticker import normalize_ticker

logger = logging.getLogger("jquants_prices")

BASE_URL = "https://api.jquants.com/v2"
_DEFAULT_DB = os.path.join(_PROJECT_ROOT, "data", "jquants.db")
_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs")
_PROGRESS_FILE = Path(_PROJECT_ROOT) / "data" / "jquants_prices_progress.json"
_MIGRATION_SQL = Path(_PROJECT_ROOT) / "migrations" / "003_market_per_share.sql"

SLEEP_BETWEEN_CODES = 0.3
SLEEP_BETWEEN_PAGES = 0.3
SLEEP_ON_429 = 60
MAX_RETRIES_429 = 5


# ============================================================
# DB セットアップ
# ============================================================
def _ensure_table(conn: sqlite3.Connection):
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "market_data" not in tables:
        if _MIGRATION_SQL.exists():
            conn.executescript(_MIGRATION_SQL.read_text(encoding="utf-8"))
            conn.commit()
            logger.info("[SCHEMA] market_data テーブルを作成しました")
        else:
            logger.error(f"[SCHEMA] マイグレーションファイルが見つかりません: {_MIGRATION_SQL}")
            sys.exit(1)


# ============================================================
# API (429リトライ付き)
# ============================================================
def _api_get(endpoint: str, params: dict, auth_headers: dict) -> requests.Response:
    url = f"{BASE_URL}{endpoint}"
    for attempt in range(MAX_RETRIES_429 + 1):
        try:
            resp = requests.get(url, params=params, headers=auth_headers, timeout=30)
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout: {endpoint}, retry {attempt+1}")
            time.sleep(5)
            continue
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Connection error: {endpoint}, {e}, retry {attempt+1}")
            time.sleep(10)
            continue
        if resp.status_code == 429:
            wait = SLEEP_ON_429 * (attempt + 1)
            logger.warning(f"Rate limit (429)! Waiting {wait}s (attempt {attempt+1}/{MAX_RETRIES_429})")
            time.sleep(wait)
            continue
        if resp.status_code == 401:
            raise RuntimeError("認証エラー (401): API KEYを確認してください。")
        return resp
    raise RuntimeError(f"429エラーが{MAX_RETRIES_429}回連続。")


# ============================================================
# 銘柄一覧
# ============================================================
def fetch_all_codes(auth_headers: dict) -> list[str]:
    """V2: /equities/listed/info で全銘柄コード取得。"""
    resp = _api_get("/equities/listed/info", {}, auth_headers)
    data = resp.json()
    items = data.get("info", data.get("data", []))
    codes = sorted(set(item.get("Code", "") for item in items if item.get("Code")))
    logger.info(f"Total listed codes: {len(codes)}")
    return codes


# ============================================================
# 株価取得 (日付ベース or 銘柄ベース)
# ============================================================
def fetch_daily_quotes_by_date(date_str: str, auth_headers: dict) -> list[dict]:
    """V2: 指定日の全銘柄の株価を取得。"""
    all_items = []
    pagination_key = None
    while True:
        params = {"date": date_str}
        if pagination_key:
            params["pagination_key"] = pagination_key
        resp = _api_get("/equities/bars/daily", params, auth_headers)
        if resp.status_code != 200:
            logger.warning(f"[{date_str}] HTTP {resp.status_code}, skip")
            return all_items
        data = resp.json()
        items = data.get("data", data.get("daily_quotes", []))
        all_items.extend(items)
        pagination_key = data.get("pagination_key")
        if not pagination_key:
            break
        time.sleep(SLEEP_BETWEEN_PAGES)
    return all_items


def fetch_daily_quotes_by_code(code5: str, date_from: str, date_to: str,
                                auth_headers: dict) -> list[dict]:
    """V2: 指定銘柄の株価を期間指定で取得。"""
    all_items = []
    pagination_key = None
    while True:
        params = {"code": code5, "from": date_from, "to": date_to}
        if pagination_key:
            params["pagination_key"] = pagination_key
        resp = _api_get("/equities/bars/daily", params, auth_headers)
        if resp.status_code != 200:
            return all_items
        data = resp.json()
        items = data.get("data", data.get("daily_quotes", []))
        all_items.extend(items)
        pagination_key = data.get("pagination_key")
        if not pagination_key:
            break
        time.sleep(SLEEP_BETWEEN_PAGES)
    return all_items


# ============================================================
# UPSERT
# ============================================================
def safe_float(val) -> float | None:
    if val is None or val == "" or val == "null":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def safe_int(val) -> int | None:
    if val is None or val == "" or val == "null":
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def upsert_quotes(conn: sqlite3.Connection, items: list[dict]) -> int:
    if not items:
        return 0

    sql = """
        INSERT INTO market_data
            (ticker, date, open, high, low, close, volume,
             turnover, adj_factor, adj_close, adj_volume,
             market_cap, source, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'jquants', datetime('now'))
        ON CONFLICT(ticker, date)
        DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            volume = excluded.volume,
            turnover = excluded.turnover,
            adj_factor = excluded.adj_factor,
            adj_close = excluded.adj_close,
            adj_volume = excluded.adj_volume,
            market_cap = excluded.market_cap,
            fetched_at = excluded.fetched_at
    """
    count = 0
    for item in items:
        code = (item.get("Code") or "").strip()
        date = (item.get("Date") or "").strip()
        if not code or not date:
            continue

        ticker = normalize_ticker(code)
        # V2 フィールド: O, H, L, C, Vo, Va, AdjFactor, AdjC, AdjVo
        # V1 互換: Open, High, Low, Close, Volume, TurnoverValue, etc.
        close_val = safe_float(item.get("C") or item.get("Close"))
        try:
            conn.execute(sql, (
                ticker,
                date,
                safe_float(item.get("O") or item.get("Open")),
                safe_float(item.get("H") or item.get("High")),
                safe_float(item.get("L") or item.get("Low")),
                close_val,
                safe_int(item.get("Vo") or item.get("Volume")),
                safe_float(item.get("Va") or item.get("TurnoverValue")),
                safe_float(item.get("AdjFactor") or item.get("AdjustmentFactor")),
                safe_float(item.get("AdjC") or item.get("AdjustmentClose")),
                safe_int(item.get("AdjVo") or item.get("AdjustmentVolume")),
                None,  # market_cap — 後で計算
            ))
            count += 1
        except Exception as e:
            logger.error(f"UPSERT error [{ticker} {date}]: {e}")

    conn.commit()
    return count


# ============================================================
# 進捗管理
# ============================================================
def load_progress() -> dict:
    if _PROGRESS_FILE.exists():
        try:
            return json.loads(_PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_progress(data: dict):
    _PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PROGRESS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ============================================================
# market_cap 算出（per_share_data から株式数を取得）
# ============================================================
def update_market_caps(conn: sqlite3.Connection) -> int:
    """per_share_data の最新 shares_outstanding/treasury_stock で market_cap を算出。"""
    sql = """
        UPDATE market_data
        SET market_cap = close * (
            SELECT COALESCE(p.shares_outstanding, 0) - COALESCE(p.treasury_stock, 0)
            FROM per_share_data p
            WHERE p.ticker = market_data.ticker
            ORDER BY p.period DESC, p.quarter DESC
            LIMIT 1
        )
        WHERE close IS NOT NULL
          AND market_cap IS NULL
          AND EXISTS (
            SELECT 1 FROM per_share_data p
            WHERE p.ticker = market_data.ticker
              AND p.shares_outstanding IS NOT NULL
          )
    """
    cursor = conn.execute(sql)
    conn.commit()
    updated = cursor.rowcount
    if updated > 0:
        logger.info(f"[MARKET_CAP] {updated:,} 行の時価総額を算出しました")
    return updated


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="J-Quants 株価一括取得")
    parser.add_argument("--db", default=_DEFAULT_DB)
    parser.add_argument("--since", default=None, help="取得開始日 (YYYY-MM-DD)")
    parser.add_argument("--recent", action="store_true", help="直近7日のみ")
    parser.add_argument("--code", default=None, help="特定銘柄のみ (5桁コード)")
    parser.add_argument("--resume", action="store_true", help="前回の続きから")
    parser.add_argument("--date-mode", action="store_true",
                        help="日付ベースで取得 (効率的、--since/--recent と組合せ)")
    args = parser.parse_args()

    # 日付範囲
    if args.recent:
        since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    elif args.since:
        since = args.since
    else:
        since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    to_date = datetime.now().strftime("%Y-%m-%d")

    # ログ
    Path(_LOG_DIR).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = Path(_LOG_DIR) / f"jquants_prices_{ts}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    logger.info(f"=== J-Quants Prices Fetch ===")
    logger.info(f"since={since}, to={to_date}, db={args.db}")

    auth_headers = get_auth_headers()
    conn = sqlite3.connect(args.db)
    _ensure_table(conn)

    total_upserted = 0
    total_errors = 0
    start_time = time.time()

    if args.code:
        # 単一銘柄モード
        code5 = args.code if len(args.code) == 5 else args.code + "0"
        logger.info(f"[SINGLE] code={code5}, {since} ~ {to_date}")
        items = fetch_daily_quotes_by_code(code5, since, to_date, auth_headers)
        n = upsert_quotes(conn, items)
        total_upserted += n
        logger.info(f"[SINGLE] {n} rows upserted for {code5}")

    elif args.date_mode or args.recent:
        # 日付ベースモード: 1日ずつ全銘柄分を取得（--recent 向きの高効率モード）
        current = datetime.strptime(since, "%Y-%m-%d")
        end = datetime.strptime(to_date, "%Y-%m-%d")
        days = (end - current).days + 1
        day_idx = 0
        while current <= end:
            day_idx += 1
            date_str = current.strftime("%Y-%m-%d")
            # 土日スキップ (0=月, 5=土, 6=日)
            if current.weekday() >= 5:
                current += timedelta(days=1)
                continue
            items = fetch_daily_quotes_by_date(date_str, auth_headers)
            if items:
                n = upsert_quotes(conn, items)
                total_upserted += n
                logger.info(f"  [{day_idx}/{days}] {date_str}: {len(items)} items, {n} upserted")
            else:
                logger.info(f"  [{day_idx}/{days}] {date_str}: no data (祝日?)")
            current += timedelta(days=1)
            time.sleep(SLEEP_BETWEEN_CODES)

    else:
        # 銘柄ベースモード: 全銘柄を1つずつ
        all_codes = fetch_all_codes(auth_headers)
        if args.resume:
            progress = load_progress()
        else:
            progress = {}
        completed = set(progress.get("completed_codes", []))
        remaining = [c for c in all_codes if c not in completed]
        logger.info(f"To process: {len(remaining)} / {len(all_codes)} codes")

        for idx, code5 in enumerate(remaining):
            ticker4 = normalize_ticker(code5)
            try:
                items = fetch_daily_quotes_by_code(code5, since, to_date, auth_headers)
                n = upsert_quotes(conn, items)
                total_upserted += n
                completed.add(code5)
                if idx % 50 == 0:
                    pct = (idx + 1) / len(remaining) * 100
                    logger.info(
                        f"[{idx+1}/{len(remaining)} {pct:.1f}%] "
                        f"{ticker4}: {n} rows, total={total_upserted}"
                    )
            except Exception as e:
                logger.error(f"[{ticker4}] Error: {e}")
                total_errors += 1
                if "401" in str(e):
                    break

            if (idx + 1) % 100 == 0 or idx == len(remaining) - 1:
                save_progress({
                    "completed_codes": sorted(completed),
                    "last_updated": datetime.now().isoformat(),
                })
            time.sleep(SLEEP_BETWEEN_CODES)

    # market_cap 算出
    update_market_caps(conn)

    elapsed = time.time() - start_time
    conn.close()

    logger.info("=" * 60)
    logger.info(f"DONE in {elapsed/60:.1f} min")
    logger.info(f"  Total upserted: {total_upserted}")
    logger.info(f"  Total errors  : {total_errors}")
    logger.info(f"  DB: {args.db}")
    logger.info(f"  Log: {log_file}")


if __name__ == "__main__":
    main()
