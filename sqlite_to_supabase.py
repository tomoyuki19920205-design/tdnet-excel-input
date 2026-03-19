#!/usr/bin/env python3
# ============================================================
# sqlite_to_supabase.py — SQLite (decision_db.db) → Supabase push
# ============================================================
#
# decision_db.db の quarterly_results を Supabase の
# companies / periods / disclosures / facts へ変換・push する。
#
# CLI:
#   .\.venv\Scripts\python.exe -m tools.sqlite_to_supabase
#   .\.venv\Scripts\python.exe -m tools.sqlite_to_supabase --db decision_db.db
#   .\.venv\Scripts\python.exe -m tools.sqlite_to_supabase --dry-run
#
# ============================================================
from __future__ import annotations

import argparse
import io
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger("sqlite2supa")

JST = timezone(timedelta(hours=9))

# ============================================================
# メトリクスマッピング
# (SQLite quarterly_results の列名 → Supabase facts の metric)
# ============================================================
_METRIC_MAP = {
    "sales":            "NET_SALES",
    "gross_profit":     "GROSS_PROFIT",
    "operating_profit": "OP_INCOME",
}

# SQLite の unit → 乗数
_UNIT_MULTIPLIER = {
    "百万円":  1_000_000,
    "千円":    1_000,
    "円":      1,
}


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
# Supabase REST API ヘルパー
# ============================================================
class _SupabaseAPI:
    """軽量 Supabase REST API ラッパー"""

    def __init__(self, url: str, key: str) -> None:
        self.rest_url = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation,resolution=merge-duplicates",
        }

    def select(self, table: str, params: dict | None = None) -> list[dict]:
        r = requests.get(
            f"{self.rest_url}/{table}",
            headers={**self.headers, "Prefer": ""},
            params=params or {},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def upsert(self, table: str, data: dict | list[dict],
               on_conflict: str = "") -> list[dict]:
        payload = data if isinstance(data, list) else [data]
        params = {}
        if on_conflict:
            params["on_conflict"] = on_conflict
        r = requests.post(
            f"{self.rest_url}/{table}",
            headers=self.headers,
            params=params,
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def insert(self, table: str, data: dict | list[dict]) -> list[dict]:
        payload = data if isinstance(data, list) else [data]
        headers = {**self.headers, "Prefer": "return=representation"}
        r = requests.post(
            f"{self.rest_url}/{table}",
            headers=headers,
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


# ============================================================
# SQLite → Supabase push 本体
# ============================================================
def push_sqlite_to_supabase(
    db_path: str,
    supabase_url: str = "",
    supabase_key: str = "",
    dry_run: bool = False,
) -> dict:
    """
    decision_db.db の quarterly_results → Supabase push。

    Returns:
        {
            "sqlite_rows": int,
            "companies_upserted": int,
            "periods_upserted": int,
            "facts_pushed": int,
            "skipped": int,
            "errors": int,
        }
    """
    # --- 接続情報 ---
    if not supabase_url or not supabase_key:
        _load_dotenv()
        supabase_url = supabase_url or os.environ.get("SUPABASE_URL", "")
        supabase_key = supabase_key or os.environ.get("SUPABASE_ANON_KEY", "")

    if not supabase_url or not supabase_key:
        raise ValueError(
            ".env ファイルが見つからないか、接続情報が未設定です。\n"
            "  SUPABASE_URL と SUPABASE_ANON_KEY を .env に設定してください。"
        )

    api = _SupabaseAPI(supabase_url, supabase_key)

    # --- SQLite読み取り ---
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DBファイルが見つかりません: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM quarterly_results ORDER BY company_code, fiscal_year_end, quarter"
    )
    rows = cur.fetchall()
    conn.close()

    logger.info(f"[PUSH] SQLite読み取り: {len(rows)} 件 ({db_path})")

    stats = {
        "sqlite_rows": len(rows),
        "companies_upserted": 0,
        "periods_upserted": 0,
        "facts_pushed": 0,
        "skipped": 0,
        "errors": 0,
    }

    if dry_run:
        logger.info("[PUSH] dry-run モード: Supabase への書き込みはスキップ")
        return stats

    # --- Supabase の既存データをキャッシュ（API呼び出し最小化）---
    # 既存 companies
    existing_companies = api.select("companies", {"select": "company_id,ticker_code"})
    ticker_to_cid: dict[str, int] = {
        c["ticker_code"]: c["company_id"] for c in existing_companies
    }
    logger.info(f"[PUSH] 既存companies: {len(ticker_to_cid)} 社")

    # 既存 periods
    existing_periods = api.select(
        "periods", {"select": "period_id,company_id,fiscal_year_end,quarter"}
    )
    period_key_to_pid: dict[tuple, int] = {}
    for p in existing_periods:
        key = (p["company_id"], str(p["fiscal_year_end"]), p["quarter"])
        period_key_to_pid[key] = p["period_id"]
    logger.info(f"[PUSH] 既存periods: {len(period_key_to_pid)} 件")

    # --- 行ごとに処理 ---
    for row in rows:
        ticker = row["company_code"]
        fye = row["fiscal_year_end"]
        quarter_str = row["quarter"]
        unit = row["unit"] or "百万円"

        try:
            quarter = int(quarter_str.replace("Q", "").strip())
        except (ValueError, AttributeError):
            logger.warning(f"[PUSH] quarter変換失敗: {quarter_str} ({ticker})")
            stats["errors"] += 1
            continue

        try:
            # 1. companies UPSERT
            if ticker not in ticker_to_cid:
                result = api.upsert(
                    "companies",
                    {"ticker_code": ticker, "is_active": True},
                    on_conflict="ticker_code",
                )
                cid = result[0]["company_id"]
                ticker_to_cid[ticker] = cid
                stats["companies_upserted"] += 1
                logger.debug(f"[PUSH] company upsert: {ticker} -> {cid}")
            cid = ticker_to_cid[ticker]

            # 2. periods UPSERT
            fiscal_year = int(fye.split("-")[0])
            is_full_year = (quarter == 4)
            period_key = (cid, fye, quarter)

            if period_key not in period_key_to_pid:
                result = api.upsert(
                    "periods",
                    {
                        "company_id": cid,
                        "fiscal_year_end": fye,
                        "fiscal_year": fiscal_year,
                        "quarter": quarter,
                        "is_full_year": is_full_year,
                    },
                    on_conflict="company_id,fiscal_year_end,quarter",
                )
                pid = result[0]["period_id"]
                period_key_to_pid[period_key] = pid
                stats["periods_upserted"] += 1
                logger.debug(f"[PUSH] period upsert: {ticker} {fye} Q{quarter} -> {pid}")
            pid = period_key_to_pid[period_key]

            # 3. disclosures（SQLite同期用の仮エントリ）
            # source_doc_id があればそれを使い、なければ自動生成キーで重複チェック
            sync_title = f"SQLite同期: {ticker} {fye} Q{quarter}"
            sync_sha = f"sqlite-sync-{ticker}-{fye}-Q{quarter}"

            existing_disc = api.select(
                "disclosures",
                {"sha256": f"eq.{sync_sha}", "select": "disclosure_id"},
            )
            if existing_disc:
                disc_id = existing_disc[0]["disclosure_id"]
            else:
                disc_result = api.insert("disclosures", {
                    "company_id": cid,
                    "source": "MANUAL",
                    "disclosed_at": datetime.now(JST).isoformat(),
                    "title": sync_title,
                    "doc_type": "TANSHIN",
                    "is_target": True,
                    "sha256": sync_sha,
                })
                disc_id = disc_result[0]["disclosure_id"]

            # 4. facts UPSERT
            multiplier = _UNIT_MULTIPLIER.get(unit, 1)
            facts_count = 0

            for sqlite_col, metric in _METRIC_MAP.items():
                raw_val = row[sqlite_col]
                if raw_val is None:
                    continue

                # 百万円 → 円に変換
                value_jpy = int(float(raw_val) * multiplier)

                # 既存 fact チェック（同一disclosure内の重複を防ぐ）
                existing_facts = api.select("facts", {
                    "disclosure_id": f"eq.{disc_id}",
                    "period_id": f"eq.{pid}",
                    "metric": f"eq.{metric}",
                    "scope": "eq.CONSOLIDATED",
                    "select": "fact_id,value",
                })

                if existing_facts:
                    # 値が同じならスキップ
                    if existing_facts[0]["value"] == value_jpy:
                        continue
                    # 値が違えばスキップ（追記型なので上書きしない）
                    # → 最新値として新しい disclosure で別行を追加
                    stats["skipped"] += 1
                    continue

                api.insert("facts", {
                    "company_id": cid,
                    "period_id": pid,
                    "disclosure_id": disc_id,
                    "scope": "CONSOLIDATED",
                    "metric": metric,
                    "value": value_jpy,
                    "unit": "JPY",
                    "quality": "IXBRL",
                })
                facts_count += 1

            stats["facts_pushed"] += facts_count

        except requests.HTTPError as e:
            err_body = e.response.text if e.response else str(e)
            logger.error(f"[PUSH] HTTPエラー: {ticker} - {err_body[:200]}")
            stats["errors"] += 1
        except Exception as e:
            logger.error(f"[PUSH] エラー: {ticker} - {e}")
            stats["errors"] += 1

    logger.info(
        f"[PUSH] 完了: sqlite_rows={stats['sqlite_rows']} "
        f"companies={stats['companies_upserted']} "
        f"periods={stats['periods_upserted']} "
        f"facts={stats['facts_pushed']} "
        f"skipped={stats['skipped']} "
        f"errors={stats['errors']}"
    )
    return stats


# ============================================================
# CLI
# ============================================================
def main():
    # Windows cp932 対策
    if sys.stdout and hasattr(sys.stdout, "encoding"):
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace"
            )
    if sys.stderr and hasattr(sys.stderr, "encoding"):
        if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
            sys.stderr = io.TextIOWrapper(
                sys.stderr.buffer, encoding="utf-8", errors="replace"
            )

    parser = argparse.ArgumentParser(
        description="SQLite (decision_db.db) → Supabase push"
    )
    parser.add_argument(
        "--db", default="decision_db.db",
        help="SQLiteファイルパス (default: decision_db.db)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Supabaseへの書き込みをスキップ")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # DB パス解決
    db_path = args.db
    if not os.path.isabs(db_path):
        db_path = os.path.join(_PROJECT_ROOT, db_path)

    print()
    print("=" * 55)
    print("  SQLite → Supabase push")
    print("=" * 55)
    print(f"  DB: {db_path}")
    if args.dry_run:
        print("  Mode: dry-run (書き込みなし)")
    print()

    try:
        stats = push_sqlite_to_supabase(
            db_path=db_path,
            dry_run=args.dry_run,
        )

        print("=" * 55)
        print("  ✅ push 完了")
        print("=" * 55)
        print(f"  SQLite行数         : {stats['sqlite_rows']}")
        print(f"  companies upsert   : {stats['companies_upserted']}")
        print(f"  periods upsert     : {stats['periods_upserted']}")
        print(f"  facts push         : {stats['facts_pushed']}")
        print(f"  スキップ           : {stats['skipped']}")
        print(f"  エラー             : {stats['errors']}")
        print("=" * 55)
        print()
        sys.exit(0)

    except Exception as e:
        print()
        print("=" * 55)
        print("  ❌ push 失敗")
        print("=" * 55)
        print(f"  {e}")
        print("=" * 55)
        print()
        sys.exit(1)


if __name__ == "__main__":
    main()
