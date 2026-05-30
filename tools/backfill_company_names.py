#!/usr/bin/env python3
# ============================================================
# backfill_company_names.py — companies.name_ja 一括補完
# ============================================================
#
# Supabase の companies テーブルで name_ja が NULL の銘柄に対し、
# ローカル SQLite (decision_db.db) の複数テーブルから企業名を取得して補完する。
#
# これにより札証・名証・福証の銘柄が銘柄名検索でヒットするようになる。
#
# 使い方:
#   # ドライラン（確認のみ）
#   python -X utf8 tools/backfill_company_names.py
#
#   # 本番反映
#   python -X utf8 tools/backfill_company_names.py --apply
#
# ============================================================
from __future__ import annotations

import io
import logging
import os
import sqlite3
import sys
from datetime import timedelta, timezone
from pathlib import Path

import requests

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

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

JST = timezone(timedelta(hours=9))
logger = logging.getLogger("backfill_company_names")

_DECISION_DB = os.path.join(_PROJECT_ROOT, "decision_db.db")
_BATCH_SIZE = 200

# decision_db.db 内で (ticker, company_name) を持つテーブル一覧
_NAME_SOURCES = [
    ("events", "ticker", "company_name"),
    ("filing_diff_summaries", "ticker", "company_name"),
    ("earnings_summaries", "ticker", "company_name"),
    ("ai_summaries", "ticker", "company_name"),
]


def _load_dotenv():
    env_path = Path(_PROJECT_ROOT) / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _get_supabase_config() -> tuple[str, str]:
    _load_dotenv()
    url = os.environ.get("SUPABASE_URL", "")
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        or os.environ.get("SUPABASE_ANON_KEY", "")
    )
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY が未設定です。\n"
            ".env ファイルに設定してください。"
        )
    return url, key


def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i: i + n]


def _get_ticker_names_from_decision_db() -> dict[str, str]:
    """
    decision_db.db の複数テーブルから (ticker → company_name) を収集。
    
    対象テーブル:
    - events
    - filing_diff_summaries
    - earnings_summaries
    - ai_summaries
    """
    result: dict[str, str] = {}
    if not os.path.exists(_DECISION_DB):
        logger.warning(f"[SOURCE] decision_db.db が見つかりません: {_DECISION_DB}")
        return result

    try:
        conn = sqlite3.connect(_DECISION_DB)

        for table_name, ticker_col, name_col in _NAME_SOURCES:
            try:
                rows = conn.execute(
                    f"SELECT DISTINCT {ticker_col}, {name_col} FROM {table_name} "
                    f"WHERE {name_col} IS NOT NULL AND {name_col} != ''"
                ).fetchall()

                added = 0
                for ticker, name in rows:
                    if not ticker or not name:
                        continue
                    name = name.strip()
                    existing = result.get(ticker)
                    # より長い名前を優先（略称より正式名を採用）
                    if existing is None or len(name) > len(existing):
                        result[ticker] = name
                        added += 1

                logger.info(
                    f"[SOURCE] {table_name}: {len(rows)} 行, "
                    f"新規 {added} 銘柄 (累計 {len(result)})"
                )
            except Exception as e:
                logger.warning(f"[SOURCE] {table_name}: 読み取り失敗: {e}")

        conn.close()
    except Exception as e:
        logger.warning(f"[SOURCE] decision_db.db 接続失敗: {e}")

    return result


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Supabase companies.name_ja 一括補完"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="本番反映する（省略時はドライラン）"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="詳細ログ"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    print("=" * 60)
    print("  companies.name_ja 一括補完ツール")
    print("=" * 60)

    # Step 1: ローカル decision_db から全企業名を収集
    ticker_to_name = _get_ticker_names_from_decision_db()
    print(f"  ローカルDB合計: {len(ticker_to_name)} 銘柄の企業名を取得")

    if not ticker_to_name:
        print("  企業名が取得できませんでした。終了します。")
        return

    # Step 2: Supabase から name_ja=NULL の銘柄を取得
    supabase_url, supabase_key = _get_supabase_config()
    supabase_headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    print(f"  Supabase URL: {supabase_url[:40]}...")

    session = requests.Session()

    # Supabase のデフォルト LIMIT=1000 を回避するためページネーションで全件取得
    null_tickers: set[str] = set()
    page_size = 1000
    offset = 0
    while True:
        resp = session.get(
            f"{supabase_url.rstrip('/')}/rest/v1/companies",
            headers={
                **supabase_headers,
                "Range": f"{offset}-{offset + page_size - 1}",
            },
            params={
                "select": "ticker_code,name_ja",
                "name_ja": "is.null",
                "order": "ticker_code",
            },
            timeout=30,
        )
        if resp.status_code not in (200, 206):
            resp.raise_for_status()
        page = resp.json()
        for c in page:
            null_tickers.add(c["ticker_code"])
        if len(page) < page_size:
            break
        offset += page_size

    print(f"  Supabase name_ja=NULL の銘柄数: {len(null_tickers)}")

    # Step 3: 更新対象を決定
    # ローカルDBは4桁ticker、Supabaseには4桁と5桁（末尾0の local_code）が混在
    # 5桁→4桁の正規化も行って照合する
    def _normalize_to_4(t: str) -> str:
        """5桁末尾0 → 4桁に正規化"""
        t = t.strip()
        if len(t) == 5 and t.isdigit() and t.endswith("0"):
            return t[:4]
        return t

    updates: list[dict] = []
    for ticker in sorted(null_tickers):
        # まず完全一致
        name = ticker_to_name.get(ticker)
        if not name:
            # 5桁→4桁に正規化して再照合
            name = ticker_to_name.get(_normalize_to_4(ticker))
        if name:
            updates.append({
                "ticker_code": ticker,
                "name_ja": name,
                "is_active": True,
            })

    # 残りの未カバー銘柄
    missing = null_tickers - set(u["ticker_code"] for u in updates)

    print(f"  更新対象（name_ja補完可能）: {len(updates)} 銘柄")
    if missing:
        print(f"  ローカルDBにも企業名なし: {len(missing)} 銘柄")

    if not updates:
        print("  更新対象がありません。終了します。")
        return

    # 先頭20件を表示
    print()
    print("  [サンプル] 更新予定:")
    for u in updates[:20]:
        print(f"    {u['ticker_code']} → {u['name_ja']}")
    if len(updates) > 20:
        print(f"    ... 他 {len(updates) - 20} 件")
    print()

    if not args.apply:
        print("  ★ ドライランモード: Supabase への書き込みはスキップ")
        print("  本番反映するには --apply を付けて再実行してください")
        print("=" * 60)
        return

    # Step 4: Supabase に UPSERT
    upsert_headers = {
        **supabase_headers,
        "Prefer": "return=headers-only,resolution=merge-duplicates",
    }

    upserted = 0
    errors = 0
    for chunk in _chunks(updates, _BATCH_SIZE):
        try:
            resp = session.post(
                f"{supabase_url.rstrip('/')}/rest/v1/companies",
                headers=upsert_headers,
                params={"on_conflict": "ticker_code"},
                json=chunk,
                timeout=30,
            )
            resp.raise_for_status()
            upserted += len(chunk)
            print(f"  UPSERT: {upserted}/{len(updates)}")
        except Exception as e:
            errors += 1
            print(f"  ERROR: {e}")

    print()
    print("=" * 60)
    print("  結果サマリ")
    print("=" * 60)
    print(f"  更新成功: {upserted}")
    print(f"  エラー  : {errors}")
    if missing:
        print(f"  未カバー: {len(missing)} 銘柄 (ローカルDBに企業名なし)")
    print("=" * 60)


if __name__ == "__main__":
    main()
