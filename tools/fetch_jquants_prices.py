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
from dataclasses import dataclass
import json
import logging
import math
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
from src.common_ticker import strip_tdnet_trailing_zero

logger = logging.getLogger("jquants_prices")

BASE_URL = "https://api.jquants.com/v2"
_DEFAULT_DB = os.path.join(_PROJECT_ROOT, "data", "jquants.db")
_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs")
_PROGRESS_FILE = Path(_PROJECT_ROOT) / "data" / "jquants_prices_progress.json"
_MIGRATION_SQL = Path(_PROJECT_ROOT) / "migrations" / "003_market_per_share.sql"

# J-Quants V2 equities/master values.  The selection deliberately uses master
# attributes, never a ticker-number heuristic.  ProdCat=011 is equities, and
# these Mkt values cover both the post-2022 TSE boards and the historical
# TSE 1st/2nd/Mothers/JASDAQ boards.  Preferred
# shares share ProdCat=011, so the official master security-name flag is an
# explicit exclusion until J-Quants exposes a separate share-class field.
COMMON_STOCK_PRODUCT_CATEGORY = "011"
TSE_EQUITY_MARKETS = {"0101", "0102", "0103", "0104", "0106", "0107", "0111", "0112", "0113"}
COMMON_STOCK_RULE_VERSION = "jquants_master_v2_20260801"
MARKET_DATA_RETENTION_YEARS = 1


def market_data_retention_start(reference: datetime | None = None) -> str:
    """Inclusive rolling one-calendar-year retention boundary (JST local date)."""
    today = (reference or datetime.now()).date()
    try:
        prior = today.replace(year=today.year - MARKET_DATA_RETENTION_YEARS)
    except ValueError:  # Feb 29
        prior = today.replace(year=today.year - MARKET_DATA_RETENTION_YEARS, day=28)
    return (prior + timedelta(days=1)).isoformat()

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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_data_universe (
          date TEXT NOT NULL, ticker TEXT NOT NULL, code TEXT NOT NULL,
          company_name TEXT NOT NULL, product_category TEXT NOT NULL,
          market_code TEXT NOT NULL, is_common_stock INTEGER NOT NULL,
          rule_version TEXT NOT NULL, fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
          PRIMARY KEY (date, ticker)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_market_data_universe_common "
                 "ON market_data_universe(date, is_common_stock, ticker)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_data_quarantine (
          ticker TEXT NOT NULL, date TEXT NOT NULL, reason TEXT NOT NULL,
          raw_json TEXT NOT NULL, quarantined_at TEXT NOT NULL DEFAULT (datetime('now')),
          PRIMARY KEY (ticker, date, reason)
        )
    """)
    conn.commit()


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
def is_common_stock(master: dict) -> bool:
    """Return whether a J-Quants master record is an in-scope TSE common stock.

    This is intentionally data-attribute based: product and market categories
    identify equities and the regular TSE boards.  The preferred-share
    exclusion is based on the *official master security name*, not the code.
    """
    name = str(master.get("CoName") or master.get("CompanyName") or "")
    name_en = str(master.get("CoNameEn") or master.get("CompanyNameEnglish") or "").lower()
    return (
        str(master.get("ProdCat") or "") == COMMON_STOCK_PRODUCT_CATEGORY
        and str(master.get("Mkt") or "") in TSE_EQUITY_MARKETS
        and "優先株" not in name
        and "preferred stock" not in name_en
    )


def normalize_jquants_code(code: object) -> str:
    """Normalize a V2 security code without cross-security alpha aliases.

    V2 can return both a numeric security such as ``13800`` and alpha security
    ``138A0`` on the same date.  The legacy crosswalk used elsewhere would map
    both to ``138A``; daily market-data identity must retain the actual V2 code.
    """
    return strip_tdnet_trailing_zero(str(code or "").strip().upper())


def fetch_master_by_date(date_str: str, auth_headers: dict) -> list[dict]:
    """Fetch the authoritative V2 master snapshot for a historical date."""
    resp = _api_get("/equities/master", {"date": date_str}, auth_headers)
    if resp.status_code != 200:
        raise RuntimeError(f"master {date_str}: HTTP {resp.status_code} {resp.text[:300]}")
    return resp.json().get("data", [])


def store_universe_snapshot(conn: sqlite3.Connection, date_str: str, items: list[dict]) -> set[str]:
    """Persist the decision record and return eligible 5-digit codes."""
    eligible: set[str] = set()
    rows = []
    for item in items:
        code = str(item.get("Code") or "").strip()
        if not code:
            continue
        ticker = normalize_jquants_code(code)
        allowed = is_common_stock(item)
        if allowed:
            eligible.add(code)
        rows.append((date_str, ticker, code,
                     str(item.get("CoName") or item.get("CompanyName") or ""),
                     str(item.get("ProdCat") or ""), str(item.get("Mkt") or ""),
                     int(allowed), COMMON_STOCK_RULE_VERSION))
    conn.executemany("""
        INSERT INTO market_data_universe
          (date,ticker,code,company_name,product_category,market_code,is_common_stock,rule_version)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(date,ticker) DO UPDATE SET
          code=excluded.code, company_name=excluded.company_name,
          product_category=excluded.product_category, market_code=excluded.market_code,
          is_common_stock=excluded.is_common_stock, rule_version=excluded.rule_version,
          fetched_at=datetime('now')
    """, rows)
    conn.commit()
    return eligible


def fetch_all_codes(conn: sqlite3.Connection, auth_headers: dict) -> list[str]:
    """Current common-stock universe from the authoritative V2 master."""
    today = datetime.now().strftime("%Y-%m-%d")
    return sorted(store_universe_snapshot(conn, today,
                                         fetch_master_by_date(today, auth_headers)))


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
            fetched_at = excluded.fetched_at
        WHERE market_data.open IS NOT excluded.open
           OR market_data.high IS NOT excluded.high
           OR market_data.low IS NOT excluded.low
           OR market_data.close IS NOT excluded.close
           OR market_data.volume IS NOT excluded.volume
           OR market_data.turnover IS NOT excluded.turnover
           OR market_data.adj_factor IS NOT excluded.adj_factor
           OR market_data.adj_close IS NOT excluded.adj_close
           OR market_data.adj_volume IS NOT excluded.adj_volume
    """
    count = 0
    has_quarantine = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_data_quarantine'"
    ).fetchone() is not None
    retention_start = market_data_retention_start()
    for item in items:
        code = (item.get("Code") or "").strip()
        date = (item.get("Date") or "").strip()
        if not code or not date:
            continue
        if date < retention_start:
            continue

        ticker = normalize_jquants_code(code)
        # V2 フィールド: O, H, L, C, Vo, Va, AdjFactor, AdjC, AdjVo
        # V1 互換: Open, High, Low, Close, Volume, TurnoverValue, etc.
        close_val = safe_float(item.get("C") if item.get("C") is not None else item.get("Close"))
        volume_val = safe_int(item.get("Vo") if item.get("Vo") is not None else item.get("Volume"))
        turnover_val = safe_float(item.get("Va") if item.get("Va") is not None else item.get("TurnoverValue"))
        adj_factor_val = safe_float(item.get("AdjFactor") if item.get("AdjFactor") is not None else item.get("AdjustmentFactor"))
        adj_close_val = safe_float(item.get("AdjC") if item.get("AdjC") is not None else item.get("AdjustmentClose"))
        adj_volume_val = safe_int(item.get("AdjVo") if item.get("AdjVo") is not None else item.get("AdjustmentVolume"))
        # A master-listed security can legitimately have no bar on a date.  It
        # is not a zero price/volume observation; preserve the raw record in a
        # quarantine table and keep it out of the feature-source ledger.
        if None in (volume_val, turnover_val, adj_factor_val, adj_close_val, adj_volume_val):
            if has_quarantine:
                conn.execute("""
                    INSERT OR REPLACE INTO market_data_quarantine(ticker,date,reason,raw_json)
                    VALUES (?,?,?,?)
                """, (ticker, date, "JQUANTS_MISSING_REQUIRED_DAILY_FIELDS",
                      json.dumps(item, ensure_ascii=False, sort_keys=True)))
            continue
        try:
            changes_before = conn.total_changes
            conn.execute(sql, (
                ticker,
                date,
                safe_float(item.get("O") or item.get("Open")),
                safe_float(item.get("H") or item.get("High")),
                safe_float(item.get("L") or item.get("Low")),
                close_val,
                volume_val,
                turnover_val,
                adj_factor_val,
                adj_close_val,
                adj_volume_val,
                None,  # market_cap — 後で計算
            ))
            count += conn.total_changes - changes_before
        except Exception as e:
            logger.error(f"UPSERT error [{ticker} {date}]: {e}")

    conn.commit()
    return count


# ============================================================
# 進捗管理
# ============================================================
def load_progress(progress_file: Path = _PROGRESS_FILE) -> dict:
    if progress_file.exists():
        try:
            return json.loads(progress_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_progress(data: dict, progress_file: Path = _PROGRESS_FILE):
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = progress_file.with_suffix(progress_file.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(progress_file)


# ============================================================
# market_cap 算出（price-date basis の発行済株式数を使用）
# ============================================================
@dataclass(frozen=True)
class MarketCapUpdateStats:
    scanned_rows: int
    changed_rows: int
    changed_tickers: int
    null_rows: int
    errors: int


def _same_market_cap(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-6)


def recalculate_market_caps(
    conn: sqlite3.Connection,
    *,
    apply: bool = True,
) -> MarketCapUpdateStats:
    """Recalculate point-in-time market caps on the unadjusted-price basis.

    For each price row, use the latest positive ``shares_outstanding`` that was
    disclosed on or before that price date.  J-Quants ``adj_factor`` is the
    price adjustment multiplier on the corporate-action effective date
    (0.5 for a 2-for-1 split, 10 for a 1-for-10 consolidation).  The matching
    price-date share count is therefore the disclosed count divided by the
    cumulative factors strictly after its disclosure and through the price
    date.  A new disclosure resets the cumulative factor and prevents double
    application.

    ``treasury_stock`` is intentionally not subtracted: Viewer market cap is
    defined as unadjusted close times total issued shares.
    """
    share_rows: dict[str, list[tuple[str, str, str, int]]] = {}
    for row in conn.execute(
        """
        SELECT ticker, disclosed_date, period, quarter, shares_outstanding
        FROM per_share_data
        WHERE shares_outstanding > 0
          AND disclosed_date IS NOT NULL
          AND disclosed_date <> ''
        ORDER BY ticker, disclosed_date, period, quarter
        """
    ):
        ticker, disclosed_date, period, quarter, shares = row
        share_rows.setdefault(ticker, []).append(
            (str(disclosed_date), str(period), str(quarter), int(shares))
        )

    updates: list[tuple[float | None, int]] = []
    changed_tickers: set[str] = set()
    scanned_rows = 0
    null_rows = 0
    errors = 0

    current_ticker: str | None = None
    ticker_shares: list[tuple[str, str, str, int]] = []
    share_index = 0
    selected_disclosure: str | None = None
    selected_shares: int | None = None
    cumulative_factor = 1.0

    market_cursor = conn.execute(
        """
        SELECT rowid, ticker, date, close, market_cap, adj_factor
        FROM market_data
        ORDER BY ticker, date
        """
    )
    for rowid, ticker, price_date, close, old_cap, adj_factor in market_cursor:
        scanned_rows += 1
        ticker = str(ticker)
        price_date = str(price_date)

        if ticker != current_ticker:
            current_ticker = ticker
            ticker_shares = share_rows.get(ticker, [])
            share_index = 0
            selected_disclosure = None
            selected_shares = None
            cumulative_factor = 1.0

        while (
            share_index < len(ticker_shares)
            and ticker_shares[share_index][0] <= price_date
        ):
            disclosure, _period, _quarter, shares = ticker_shares[share_index]
            selected_disclosure = disclosure
            selected_shares = shares
            cumulative_factor = 1.0
            share_index += 1

        factor = float(adj_factor) if adj_factor is not None else 1.0
        if not math.isfinite(factor) or factor <= 0:
            errors += 1
            continue

        # Effective-date boundary: the unadjusted close changes basis on the
        # action date itself.  A same-day share disclosure is treated as the
        # new basis and must not receive the factor again.
        if (
            factor != 1.0
            and selected_disclosure is not None
            and selected_disclosure < price_date
        ):
            cumulative_factor *= factor

        new_cap: float | None = None
        if close is not None and selected_shares is not None:
            new_cap = float(close) * (selected_shares / cumulative_factor)
        if new_cap is None:
            null_rows += 1

        old_value = float(old_cap) if old_cap is not None else None
        if not _same_market_cap(old_value, new_cap):
            updates.append((new_cap, int(rowid)))
            changed_tickers.add(ticker)

    if apply and updates:
        for start in range(0, len(updates), 10_000):
            conn.executemany(
                "UPDATE market_data SET market_cap = ? WHERE rowid = ?",
                updates[start:start + 10_000],
            )
        conn.commit()

    return MarketCapUpdateStats(
        scanned_rows=scanned_rows,
        changed_rows=len(updates),
        changed_tickers=len(changed_tickers),
        null_rows=null_rows,
        errors=errors,
    )


def update_market_caps(conn: sqlite3.Connection) -> int:
    stats = recalculate_market_caps(conn, apply=True)
    logger.info(
        "[MARKET_CAP] scanned=%s changed=%s tickers=%s null=%s errors=%s",
        f"{stats.scanned_rows:,}",
        f"{stats.changed_rows:,}",
        f"{stats.changed_tickers:,}",
        f"{stats.null_rows:,}",
        stats.errors,
    )
    if stats.errors:
        raise RuntimeError(
            f"market-cap recalculation failed for {stats.errors} rows"
        )
    return stats.changed_rows


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="J-Quants 株価一括取得")
    parser.add_argument("--db", default=_DEFAULT_DB)
    parser.add_argument("--since", default=None, help="取得開始日 (YYYY-MM-DD)")
    parser.add_argument("--until", default=None, help="取得終了日 (YYYY-MM-DD、指定時は当日を含む)")
    parser.add_argument("--recent", action="store_true", help="直近7日のみ")
    parser.add_argument("--code", default=None, help="特定銘柄のみ (5桁コード)")
    parser.add_argument("--resume", action="store_true", help="前回の続きから")
    parser.add_argument("--date-mode", action="store_true",
                        help="日付ベースで取得 (効率的、--since/--recent と組合せ)")
    parser.add_argument("--backfill", action="store_true",
                        help="日付ごとの銘柄マスター判定とcheckpointを有効化した履歴取得")
    parser.add_argument("--progress-file", default=None,
                        help="checkpoint JSON path (backfill defaults to a dedicated file)")
    parser.add_argument("--sync-supabase", action="store_true",
                        help="successful fetch completion後に対象普通株をSupabaseへ全量upsert")
    args = parser.parse_args()

    progress_file = Path(args.progress_file) if args.progress_file else (
        Path(_PROJECT_ROOT) / "data" / "jquants_prices_backfill_progress.json"
        if args.backfill else _PROGRESS_FILE
    )

    # 日付範囲
    if args.recent:
        since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    elif args.since:
        since = args.since
    else:
        since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    retention_start = market_data_retention_start()
    since = max(since, retention_start)
    to_date = args.until or datetime.now().strftime("%Y-%m-%d")
    if to_date < retention_start:
        logger.info("requested range is outside one-year retention; nothing to fetch")
        return

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

    elif args.date_mode or args.recent or args.backfill:
        # 日付ベースモード: 1日ずつ全銘柄分を取得（--recent 向きの高効率モード）
        current = datetime.strptime(since, "%Y-%m-%d")
        end = datetime.strptime(to_date, "%Y-%m-%d")
        days = (end - current).days + 1
        day_idx = 0
        progress = load_progress(progress_file) if args.resume else {}
        completed_dates = set(progress.get("completed_dates", []))
        while current <= end:
            day_idx += 1
            date_str = current.strftime("%Y-%m-%d")
            # 土日スキップ (0=月, 5=土, 6=日)
            if current.weekday() >= 5:
                current += timedelta(days=1)
                continue
            if date_str in completed_dates:
                logger.info(f"  [{day_idx}/{days}] {date_str}: checkpoint skip")
                current += timedelta(days=1)
                continue
            # Each historical date is qualified with the master snapshot from
            # that date, preserving IPOs and delistings without admitting ETFs,
            # ETNs, REITs, infrastructure funds, or preferred shares.
            master_items = fetch_master_by_date(date_str, auth_headers)
            eligible_codes = store_universe_snapshot(conn, date_str, master_items)
            if not eligible_codes:
                raise RuntimeError(
                    f"{date_str}: master returned no in-scope ordinary shares; "
                    "refusing to checkpoint an unverifiable date"
                )
            items = [item for item in fetch_daily_quotes_by_date(date_str, auth_headers)
                     if str(item.get("Code") or "") in eligible_codes]
            if items:
                n = upsert_quotes(conn, items)
                total_upserted += n
                logger.info(f"  [{day_idx}/{days}] {date_str}: {len(items)} eligible items, {n} upserted")
            else:
                logger.info(f"  [{day_idx}/{days}] {date_str}: no eligible data (holiday?)")
            completed_dates.add(date_str)
            save_progress({
                "completed_dates": sorted(completed_dates),
                "last_completed_date": date_str,
                "last_updated": datetime.now().isoformat(),
                "rule_version": COMMON_STOCK_RULE_VERSION,
            }, progress_file)
            current += timedelta(days=1)
            time.sleep(SLEEP_BETWEEN_CODES)

    else:
        # 銘柄ベースモード: 全銘柄を1つずつ
        all_codes = fetch_all_codes(conn, auth_headers)
        if args.resume:
            progress = load_progress(progress_file)
        else:
            progress = {}
        completed = set(progress.get("completed_codes", []))
        remaining = [c for c in all_codes if c not in completed]
        logger.info(f"To process: {len(remaining)} / {len(all_codes)} codes")

        for idx, code5 in enumerate(remaining):
            ticker4 = normalize_jquants_code(code5)
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
                }, progress_file)
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

    if args.sync_supabase and total_errors == 0:
        from tools.sync_market_data import _load_dotenv, sync
        _load_dotenv()
        supabase_url = os.environ.get("SUPABASE_URL", "")
        supabase_key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
                        or os.environ.get("SUPABASE_ANON_KEY", ""))
        if not supabase_url or not supabase_key:
            raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY is required for --sync-supabase")
        sync_stats = sync(args.db, supabase_url, supabase_key,
                          dry_run=False, recent_days=0)
        if sync_stats["errors"]:
            raise RuntimeError(f"Supabase sync failed: {sync_stats}")


if __name__ == "__main__":
    main()
