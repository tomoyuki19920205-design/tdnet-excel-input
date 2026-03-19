#!/usr/bin/env python3
# ============================================================
# supabase_loader.py — JSON抽出結果 → Supabase (PostgREST API)
# ============================================================
#
# CLI:
#   python -m tools.supabase_loader --input results/
#   python -m tools.supabase_loader --input results/081220260213561316.json
#
# 環境変数 (.env):
#   SUPABASE_URL      https://xxx.supabase.co
#   SUPABASE_ANON_KEY eyJ...
#
# ============================================================
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger("supabase_loader")

JST = timezone(timedelta(hours=9))

# ============================================================
# Supabase REST API client (軽量、依存なし)
# ============================================================

class SupabaseClient:
    """PostgREST / Supabase REST API ラッパー"""

    def __init__(self, url: str, anon_key: str):
        self.base_url = url.rstrip("/")
        self.rest_url = f"{self.base_url}/rest/v1"
        self.headers = {
            "apikey": anon_key,
            "Authorization": f"Bearer {anon_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def select(self, table: str, params: dict | None = None) -> list[dict]:
        """テーブルからSELECT"""
        r = requests.get(
            f"{self.rest_url}/{table}",
            headers=self.headers,
            params=params or {},
        )
        r.raise_for_status()
        return r.json()

    def insert(self, table: str, data: dict | list[dict]) -> list[dict]:
        """INSERT (単一 or 複数)"""
        payload = data if isinstance(data, list) else [data]
        r = requests.post(
            f"{self.rest_url}/{table}",
            headers=self.headers,
            json=payload,
        )
        r.raise_for_status()
        return r.json()

    def upsert(self, table: str, data: dict | list[dict],
               on_conflict: str = "") -> list[dict]:
        """UPSERT (ON CONFLICT)"""
        payload = data if isinstance(data, list) else [data]
        headers = {**self.headers, "Prefer": "return=representation,resolution=merge-duplicates"}
        params = {}
        if on_conflict:
            params["on_conflict"] = on_conflict
        r = requests.post(
            f"{self.rest_url}/{table}",
            headers=headers,
            params=params,
            json=payload,
        )
        r.raise_for_status()
        return r.json()


# ============================================================
# メトリクスマッピング (extract_to_json.py の出力 → DB)
# ============================================================
_METRIC_MAP = {
    "sales":            "NET_SALES",
    "gross_profit":     "GROSS_PROFIT",
    "operating_profit": "OP_INCOME",
}


# ============================================================
# ローダー本体
# ============================================================

def load_json_to_supabase(
    client: SupabaseClient,
    json_data: dict,
    source_file: str = "",
) -> dict:
    """
    1件のJSON → Supabaseへロード。

    Returns: {"status": "ok"|"error", "ticker": str, "detail": str}
    """
    ticker = json_data.get("ticker_code", "")
    if not ticker:
        return {"status": "error", "ticker": "", "detail": "ticker_code missing"}

    fiscal_year_end = json_data.get("fiscal_year_end", "")
    quarter = json_data.get("quarter")
    if not fiscal_year_end or quarter is None:
        return {"status": "error", "ticker": ticker, "detail": "fiscal_year_end/quarter missing"}

    title = json_data.get("title", "不明")
    disclosed_at = json_data.get("disclosed_at", datetime.now(JST).isoformat())
    sha256 = json_data.get("sha256", "")

    try:
        # 1. companies UPSERT
        company_rows = client.upsert(
            "companies",
            {"ticker_code": ticker, "is_active": True},
            on_conflict="ticker_code",
        )
        company_id = company_rows[0]["company_id"]

        # 2. periods UPSERT
        # fiscal_year_end → fiscal_year
        fiscal_year = int(fiscal_year_end.split("-")[0])
        is_full_year = (quarter == 4)

        period_rows = client.upsert(
            "periods",
            {
                "company_id": company_id,
                "fiscal_year_end": fiscal_year_end,
                "fiscal_year": fiscal_year,
                "quarter": quarter,
                "is_full_year": is_full_year,
            },
            on_conflict="company_id,fiscal_year_end,quarter",
        )
        period_id = period_rows[0]["period_id"]

        # 3. disclosures INSERT (sha256で重複チェック)
        if sha256:
            existing = client.select(
                "disclosures",
                {"sha256": f"eq.{sha256}", "select": "disclosure_id"},
            )
            if existing:
                return {
                    "status": "skipped",
                    "ticker": ticker,
                    "detail": f"sha256重複: {sha256[:16]}...",
                }

        disc_rows = client.insert("disclosures", {
            "company_id": company_id,
            "source": "TDNET",
            "disclosed_at": disclosed_at,
            "title": title,
            "doc_type": "TANSHIN",
            "is_target": True,
            "sha256": sha256 or None,
        })
        disclosure_id = disc_rows[0]["disclosure_id"]

        # 4. facts INSERT
        scope = "CONSOLIDATED"
        quality = json_data.get("quality", "IXBRL")
        source_unit = json_data.get("source_unit", "yen")
        values = json_data.get("values", {})

        facts_inserted = 0
        for json_key, metric in _METRIC_MAP.items():
            raw_val = values.get(json_key)
            if raw_val is None:
                continue

            # 円整数に変換
            value_jpy = _to_jpy(raw_val, source_unit)
            if value_jpy is None:
                continue

            client.insert("facts", {
                "company_id": company_id,
                "period_id": period_id,
                "disclosure_id": disclosure_id,
                "scope": scope,
                "metric": metric,
                "value": value_jpy,
                "unit": "JPY",
                "quality": quality,
            })
            facts_inserted += 1

        logger.info(
            f"[LOAD] {ticker} {fiscal_year_end} Q{quarter} "
            f"facts={facts_inserted} (disclosure_id={disclosure_id})"
        )
        return {
            "status": "ok",
            "ticker": ticker,
            "detail": f"facts={facts_inserted}",
        }

    except requests.HTTPError as e:
        err_body = e.response.text if e.response else str(e)
        logger.error(f"[LOAD] HTTP error: {ticker} - {err_body}")
        return {"status": "error", "ticker": ticker, "detail": err_body[:200]}
    except Exception as e:
        logger.error(f"[LOAD] error: {ticker} - {e}")
        return {"status": "error", "ticker": ticker, "detail": str(e)[:200]}


def _to_jpy(value, source_unit: str) -> int | None:
    """値を円整数に変換"""
    if value is None:
        return None
    try:
        v = float(value)
    except (ValueError, TypeError):
        return None

    if source_unit in ("million_yen", "百万円"):
        return int(v * 1_000_000)
    elif source_unit in ("thousand_yen", "千円"):
        return int(v * 1_000)
    elif source_unit in ("yen", "円", "JPY"):
        return int(v)
    else:
        return int(v)


# ============================================================
# バッチローダー
# ============================================================

def load_batch(
    client: SupabaseClient,
    input_path: str,
) -> dict:
    """
    ディレクトリまたは単一JSONファイルをロード。

    Returns: {"processed": int, "ok": int, "skipped": int, "errors": int}
    """
    p = Path(input_path)
    files: list[Path] = []

    if p.is_file() and p.suffix == ".json":
        files = [p]
    elif p.is_dir():
        files = sorted(p.glob("*.json"))
    else:
        logger.error(f"[BATCH] 無効な入力: {input_path}")
        return {"processed": 0, "ok": 0, "skipped": 0, "errors": 0}

    stats = {"processed": 0, "ok": 0, "skipped": 0, "errors": 0}

    for f in files:
        with open(f, "r", encoding="utf-8") as fp:
            data = json.load(fp)

        result = load_json_to_supabase(client, data, str(f))
        stats["processed"] += 1

        if result["status"] == "ok":
            stats["ok"] += 1
        elif result["status"] == "skipped":
            stats["skipped"] += 1
        else:
            stats["errors"] += 1

    return stats


# ============================================================
# .env読み込み
# ============================================================

def _load_dotenv():
    """簡易 .env パーサー"""
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
# CLI
# ============================================================

def main():
    import io as _io

    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = _io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

    _load_dotenv()

    parser = argparse.ArgumentParser(
        description="XBRL抽出JSON → Supabase ローダー"
    )
    parser.add_argument("--input", "-i", required=True,
                        help="JSONファイル or ディレクトリ")
    parser.add_argument("--url", default=os.environ.get("SUPABASE_URL", ""),
                        help="Supabase URL (default: $SUPABASE_URL)")
    parser.add_argument("--key", default=os.environ.get("SUPABASE_ANON_KEY", ""),
                        help="Supabase Anon Key (default: $SUPABASE_ANON_KEY)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not args.url or not args.key:
        print("[ERROR] SUPABASE_URL / SUPABASE_ANON_KEY が未設定です",
              file=sys.stderr)
        print("  .env ファイルか --url/--key 引数で指定してください",
              file=sys.stderr)
        sys.exit(1)

    client = SupabaseClient(args.url, args.key)

    print("=" * 60)
    print("  XBRL → Supabase ローダー")
    print("=" * 60)
    print(f"  URL   : {args.url}")
    print(f"  入力  : {args.input}")
    print()

    stats = load_batch(client, args.input)

    print("=" * 60)
    print("  結果サマリ")
    print("=" * 60)
    print(f"  処理件数  : {stats['processed']}")
    print(f"  成功      : {stats['ok']}")
    print(f"  スキップ  : {stats['skipped']}")
    print(f"  エラー    : {stats['errors']}")
    print()


if __name__ == "__main__":
    main()
