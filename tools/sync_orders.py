#!/usr/bin/env python3
# ============================================================
# sync_orders.py — SQLite order_metrics → Supabase order_kpis 同期
# ============================================================
#
# Usage:
#   .\.venv\Scripts\python.exe tools\sync_orders.py          # dry-run
#   .\.venv\Scripts\python.exe tools\sync_orders.py --apply   # 本番
#
# 安全設計:
#   - --apply なしはドライラン
#   - INSERT のみ（既存行は skip）
#   - source_system='tdnet_historical' で historical を区別
# ============================================================
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _PROJECT_ROOT)

_DEFAULT_DB = os.path.join(_PROJECT_ROOT, "data", "decision_db.db")
_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs")

_RETRY_MAX = 3
_RETRY_BASE_SEC = 1.0

JST = timezone(timedelta(hours=9))
logger = logging.getLogger("sync_orders")

# metric_name → canonical_kpi_name のマッピング
_METRIC_TO_CANONICAL = {
    "orders_total": "orders_received",
    "backlog_total": "order_backlog",
    "carryover_construction_total": "carried_forward_construction",
}

# quarter 正規化 (4Q→FY)
_QUARTER_MAP = {"4Q": "FY"}


# ============================================================
# .env 読み込み
# ============================================================
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


# ============================================================
# fiscal_year_end → fiscal_year 表記変換
# ============================================================
def _fye_to_fiscal_year(fye: str) -> str:
    """'2025-03-31' → '2025年3月期'"""
    try:
        parts = fye.split("-")
        year = int(parts[0])
        month = int(parts[1])
        return f"{year}年{month}月期"
    except (ValueError, IndexError):
        return fye


def _fye_to_period(fye: str, quarter: str) -> str:
    """fiscal_year_end + quarter → period_label (例: '2025-03')"""
    try:
        parts = fye.split("-")
        year = int(parts[0])
        month = int(parts[1])
        # quarter から期末月を推定
        q_months = {"1Q": 3, "2Q": 6, "3Q": 9, "FY": 0, "4Q": 0}
        q_offset = q_months.get(quarter, 0)
        if quarter in ("FY", "4Q") or q_offset == 0:
            return f"{year}-{month:02d}"
        # 決算期末月から逆算
        target_month = (month + q_offset) % 12
        target_year = year if target_month > 0 else year
        if target_month == 0:
            target_month = 12
        # 3月期の 3Q = 12月 → year-1
        if target_month > month:
            target_year -= 1
        return f"{target_year}-{target_month:02d}"
    except (ValueError, IndexError):
        return fye[:7] if len(fye) >= 7 else fye


# ============================================================
# SQLite 読み取り
# ============================================================
def read_order_metrics(db_path: str) -> list[dict]:
    """SQLite order_metrics → Supabase order_kpis 向けデータ。"""
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT company_code, fiscal_year_end, quarter, metric_name, "
        "       value, unit, confidence, source_doc_id "
        "FROM order_metrics "
        "ORDER BY company_code, fiscal_year_end, quarter, metric_name"
    ).fetchall()
    conn.close()

    from src.common_ticker import normalize_ticker as _norm

    _STOCK_METRICS = {"backlog_total", "carryover_construction_total"}

    data = []
    for r in rows:
        metric = r["metric_name"]
        canonical = _METRIC_TO_CANONICAL.get(metric)
        if not canonical:
            logger.warning(f"Unknown metric: {metric} — skipping")
            continue

        quarter = _QUARTER_MAP.get(r["quarter"], r["quarter"])
        period_type = "point_in_time" if metric in _STOCK_METRICS else "cumulative"
        ticker = _norm(r["company_code"])
        fye = r["fiscal_year_end"]

        data.append({
            "ticker": ticker,
            "filing_id": f"hist_{r['source_doc_id'] or 'unknown'}",
            "filing_date": fye,  # 決算期末日 = filing_date
            "source_system": "tdnet_historical",
            "source_type": "pdf",
            "source_doc_type": "決算短信",
            "canonical_kpi_name": canonical,
            "raw_label": metric,
            "raw_value_text": str(r["value"]),
            "normalized_value": r["value"],
            "unit_normalized": r["unit"] or "百万円",
            "currency": "JPY",
            "period_label": _fye_to_period(fye, quarter),
            "period_type": period_type,
            "fiscal_year": _fye_to_fiscal_year(fye),
            "quarter": quarter,
            "confidence_score": 0.7,
            "extraction_method": "historical_backfill",
            "parser_version": "2.0.0",
            "review_status": "auto_accepted",
        })

    logger.info(f"[SQLite] order_metrics: {len(data)} rows")
    return data


# ============================================================
# Supabase: 既存チェック + INSERT
# ============================================================
def _check_existing(
    rest_url: str, headers: dict, ticker: str, canonical: str,
    fiscal_year: str, quarter: str,
) -> bool:
    """同一 (ticker, canonical_kpi_name, fiscal_year, quarter) が既に存在するか。"""
    try:
        r = requests.get(
            f"{rest_url}/order_kpis",
            params={
                "select": "id",
                "ticker": f"eq.{ticker}",
                "canonical_kpi_name": f"eq.{canonical}",
                "fiscal_year": f"eq.{fiscal_year}",
                "quarter": f"eq.{quarter}",
                "limit": "1",
            },
            headers=headers,
            timeout=15,
        )
        if r.status_code in (200, 206):
            return len(r.json()) > 0
    except Exception as e:
        logger.warning(f"[CHECK] {e}")
    return False


def _insert_row(
    rest_url: str, headers: dict, payload: dict,
) -> str:
    """order_kpis に INSERT。"""
    last_exc = None
    for attempt in range(_RETRY_MAX):
        try:
            r = requests.post(
                f"{rest_url}/order_kpis",
                json=payload,
                headers={**headers, "Prefer": "return=minimal"},
                timeout=30,
            )
            r.raise_for_status()
            return "inserted"
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            wait = _RETRY_BASE_SEC * (2 ** attempt)
            logger.warning(f"[API] 接続エラー ({attempt+1}/{_RETRY_MAX})")
            time.sleep(wait)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response else 0
            body = e.response.text[:300] if e.response else ""
            if status == 429 or status >= 500:
                last_exc = e
                wait = _RETRY_BASE_SEC * (2 ** attempt)
                time.sleep(wait)
            else:
                logger.error(f"[API] HTTP {status}: {body}")
                return "error"
    logger.error(f"[API] リトライ上限: {last_exc}")
    return "error"


# ============================================================
# メイン同期
# ============================================================
def sync(
    db_path: str,
    supabase_url: str,
    supabase_key: str,
    dry_run: bool = True,
) -> dict:
    """SQLite order_metrics → Supabase order_kpis 同期。"""
    rest_url = supabase_url.rstrip("/") + "/rest/v1"
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
    }

    stats = {
        "sqlite_rows": 0,
        "inserted": 0,
        "skipped_existing": 0,
        "errors": 0,
        "dry_run": dry_run,
    }

    # ---- 読み取り ----
    data = read_order_metrics(db_path)
    stats["sqlite_rows"] = len(data)

    if not data:
        logger.warning("[SYNC] 0 件。同期対象がありません。")
        return stats

    # per-metric 集計
    from collections import Counter
    mc = Counter(d["canonical_kpi_name"] for d in data)
    logger.info(f"[SYNC] per-metric: {dict(mc)}")

    # ---- ドライランチェック ----
    if dry_run:
        logger.info(
            f"\n{'='*60}\n"
            f"  DRY-RUN: {len(data)} rows → order_kpis\n"
            f"  本番反映するには --apply を付けて再実行\n"
            f"{'='*60}"
        )
        stats["inserted"] = len(data)
        return stats

    # ---- INSERT (既存チェック付き) ----
    t0 = time.time()
    logger.info(f"[SYNC] 開始: {len(data)} rows → order_kpis")

    for i, row in enumerate(data, 1):
        # 既存チェック
        if _check_existing(
            rest_url, headers,
            row["ticker"], row["canonical_kpi_name"],
            row["fiscal_year"], row["quarter"],
        ):
            stats["skipped_existing"] += 1
            logger.debug(
                f"  [{i}/{len(data)}] SKIP existing: "
                f"{row['ticker']} {row['canonical_kpi_name']} "
                f"{row['fiscal_year']} {row['quarter']}"
            )
            continue

        result = _insert_row(rest_url, headers, row)
        if result == "inserted":
            stats["inserted"] += 1
        else:
            stats["errors"] += 1

        if i % 5 == 0 or i == len(data):
            elapsed = time.time() - t0
            logger.info(
                f"  [{i}/{len(data)}] {elapsed:.1f}s | "
                f"ins={stats['inserted']} skip={stats['skipped_existing']} "
                f"err={stats['errors']}"
            )

    elapsed = time.time() - t0

    # ---- 検証 ----
    try:
        r = requests.get(
            f"{rest_url}/order_kpis?select=ticker&limit=0",
            headers={**headers, "Prefer": "count=exact"},
            timeout=15,
        )
        cr = r.headers.get("Content-Range", "")
        total = int(cr.split("/")[1]) if "/" in cr else -1
        logger.info(f"[VERIFY] order_kpis: {total} total rows in Supabase")
    except Exception as e:
        logger.warning(f"[VERIFY] {e}")

    logger.info(
        f"\n{'='*60}\n"
        f"  SYNC 完了\n"
        f"  inserted:        {stats['inserted']}\n"
        f"  skipped_existing: {stats['skipped_existing']}\n"
        f"  errors:          {stats['errors']}\n"
        f"  elapsed:         {elapsed:.1f}秒\n"
        f"{'='*60}"
    )

    return stats


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="SQLite order_metrics → Supabase order_kpis 同期",
    )
    parser.add_argument(
        "--db", default=_DEFAULT_DB,
        help="SQLite DB パス (default: data/decision_db.db)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="本番反映する（省略時はドライラン）",
    )
    args = parser.parse_args()

    is_dry_run = not args.apply

    # ---- ログ設定 ----
    os.makedirs(_LOG_DIR, exist_ok=True)
    ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    mode_label = "dryrun" if is_dry_run else "apply"
    log_file = os.path.join(_LOG_DIR, f"sync_orders_{mode_label}_{ts}.log")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    logger.info("=== sync_orders START ===")
    logger.info(f"  mode:   {'DRY-RUN' if is_dry_run else 'APPLY'}")
    logger.info(f"  sqlite: {args.db}")
    logger.info(f"  log:    {log_file}")

    # ---- .env ----
    _load_dotenv()
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        or os.environ.get("SUPABASE_ANON_KEY", "")
    )

    if not supabase_url or not supabase_key:
        logger.error("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY が未設定です。")
        sys.exit(1)

    # ---- 同期 ----
    try:
        stats = sync(
            db_path=args.db,
            supabase_url=supabase_url,
            supabase_key=supabase_key,
            dry_run=is_dry_run,
        )
    except Exception as e:
        logger.error(f"FATAL: {e}", exc_info=True)
        sys.exit(1)

    if stats["errors"] > 0:
        logger.error(f"エラー {stats['errors']} 件。ログ: {log_file}")
        sys.exit(1)

    logger.info(f"=== sync_orders END (log: {log_file}) ===")
    sys.exit(0)


if __name__ == "__main__":
    main()
