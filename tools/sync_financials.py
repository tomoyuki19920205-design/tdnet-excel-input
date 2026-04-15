#!/usr/bin/env python3
# ============================================================
# sync_financials.py — SQLite jquants_financials_normalized
#                      → Supabase public.financials 同期
# ============================================================
#
# 使い方:
#   # ① ドライラン（デフォルト: 直近30日分）
#   python tools/sync_financials.py
#
#   # ② 直近30日分を本番反映
#   python tools/sync_financials.py --apply
#
#   # ③ 全量同期
#   python tools/sync_financials.py --apply --full
#
#   # ④ 少量テスト
#   python tools/sync_financials.py --apply --limit 100
#
# 前提:
#   .env に SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY を設定済み
#   data/jquants.db に jquants_financials_normalized テーブルあり
#
# 安全設計:
#   - --apply を明示しないとドライランになる
#   - memos テーブルには一切触れない
#   - financials のみ UPSERT (ON CONFLICT ticker,period,quarter)
#   - 二重実行しても安全（UPSERT = 冪等）
#   - 失敗時は非0終了 + logs/ にログ保存
# ============================================================

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# パス定数
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _PROJECT_ROOT)
_DEFAULT_DB = os.path.join(_PROJECT_ROOT, "data", "jquants.db")
_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs")

# API 定数
_BATCH_SIZE = 500
_RETRY_MAX = 5
_RETRY_BASE_SEC = 1.0
_DEFAULT_RECENT_DAYS = 30

JST = timezone(timedelta(hours=9))

# ============================================================
# 単位正規化 (J-Quants 円 → 百万円)
# ============================================================
# J-Quants API は円単位で数値を返す。
# Viewer / canonical の基準単位は百万円なので、push 前に変換する。
from lib.pipeline.unit_convert import to_millions as _to_millions  # noqa: E402

# 百万円単位としては異常に大きい閾値 (= 元が円単位のまま混入した可能性)
_ABNORMAL_MILLIONS_THRESHOLD = 1_000_000_000  # 百万円で10億 = 円で1000兆

logger = logging.getLogger("sync_financials")


# ============================================================
# .env 読み込み
# ============================================================
def _load_dotenv():
    """簡易 .env パーサー（既存パターン踏襲）"""
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
# Supabase REST API（リトライ付き）
# ============================================================
class _SupabaseAPI:
    def __init__(self, url: str, key: str) -> None:
        self.rest_url = url.rstrip("/") + "/rest/v1"
        self.session = requests.Session()
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=headers-only,resolution=merge-duplicates",
        }
        self.session.headers.update({
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        })

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        last_exc = None
        for attempt in range(_RETRY_MAX):
            try:
                r = self.session.request(method, url, timeout=60, **kwargs)
                r.raise_for_status()
                return r
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                wait = _RETRY_BASE_SEC * (2 ** attempt)
                logger.warning(
                    f"[API] 接続エラー ({attempt+1}/{_RETRY_MAX}) — {wait:.0f}秒待機"
                )
                time.sleep(wait)
            except requests.HTTPError as e:
                status = e.response.status_code if e.response else 0
                # 重複キーエラー (PostgreSQL 21000) はリトライしても解決しない
                if status == 500 and e.response is not None and "21000" in e.response.text:
                    body = e.response.text[:500]
                    logger.error(
                        f"[API] HTTP 500 (duplicate key, code=21000) — リトライ不可\n"
                        f"  レスポンス: {body}"
                    )
                    raise
                if status == 429 or status >= 500:
                    last_exc = e
                    wait = _RETRY_BASE_SEC * (2 ** attempt)
                    if status == 429:
                        ra = e.response.headers.get("Retry-After")
                        if ra:
                            wait = max(wait, float(ra))
                    logger.warning(
                        f"[API] HTTP {status} ({attempt+1}/{_RETRY_MAX})"
                        f" — {wait:.0f}秒待機"
                    )
                    time.sleep(wait)
                else:
                    body = ""
                    if e.response is not None:
                        body = e.response.text[:500]
                    logger.error(
                        f"[API] HTTP {status} — リトライ不可\n"
                        f"  レスポンス: {body}"
                    )
                    raise
        raise last_exc  # type: ignore

    def close(self):
        """Session をクローズする。"""
        self.session.close()

    def upsert_batch(
        self, table: str, data: list[dict], on_conflict: str = ""
    ) -> int:
        """バッチ UPSERT。戻り値は送信件数。"""
        if not data:
            return 0
        params = {}
        if on_conflict:
            params["on_conflict"] = on_conflict
        self._request(
            "POST",
            f"{self.rest_url}/{table}",
            headers=self.headers,
            params=params,
            json=data,
        )
        return len(data)

    def select_count(self, table: str) -> dict:
        """テーブルの基本統計を取得。"""
        headers = {
            **self.headers,
            "Prefer": "count=exact",
        }
        # 行数取得
        r = self._request(
            "GET",
            f"{self.rest_url}/{table}?select=ticker&limit=0",
            headers=headers,
        )
        content_range = r.headers.get("Content-Range", "")
        total = 0
        if "/" in content_range:
            try:
                total = int(content_range.split("/")[1])
            except (ValueError, IndexError):
                pass
        return {"total_rows": total}


# ============================================================
# SQLite から重複排除済みデータ読み取り
# ============================================================
_QUERY_BASE = """\
WITH latest AS (
  SELECT
    local_code,
    current_fiscal_year_end_date,
    type_of_current_period,
    ROW_NUMBER() OVER (
      PARTITION BY local_code,
                   current_fiscal_year_end_date,
                   type_of_current_period
      ORDER BY disclosed_date DESC
    ) AS rn,
    net_sales,
    gross_profit,
    operating_profit
  FROM jquants_financials_normalized
  {where_clause}
),
field_best AS (
  -- field-level COALESCE: 同一 (ticker, period, quarter) の全行から
  -- 最初に見つかる非NULL値を採用。disclosed_date DESC 順で探索するため
  -- 訂正開示で gross_profit=NULL でも、元開示の値が保持される。
  SELECT
    local_code,
    current_fiscal_year_end_date,
    type_of_current_period,
    -- latest row (rn=1) の値を優先、NULL なら次に新しい行から取得
    (SELECT s.net_sales FROM latest s
     WHERE s.local_code = latest.local_code
       AND s.current_fiscal_year_end_date = latest.current_fiscal_year_end_date
       AND s.type_of_current_period = latest.type_of_current_period
       AND s.net_sales IS NOT NULL
     ORDER BY s.rn LIMIT 1) AS net_sales,
    (SELECT s.gross_profit FROM latest s
     WHERE s.local_code = latest.local_code
       AND s.current_fiscal_year_end_date = latest.current_fiscal_year_end_date
       AND s.type_of_current_period = latest.type_of_current_period
       AND s.gross_profit IS NOT NULL
     ORDER BY s.rn LIMIT 1) AS gross_profit,
    (SELECT s.operating_profit FROM latest s
     WHERE s.local_code = latest.local_code
       AND s.current_fiscal_year_end_date = latest.current_fiscal_year_end_date
       AND s.type_of_current_period = latest.type_of_current_period
       AND s.operating_profit IS NOT NULL
     ORDER BY s.rn LIMIT 1) AS operating_profit
  FROM latest
  WHERE rn = 1
)
SELECT
  local_code                   AS ticker,
  current_fiscal_year_end_date AS period,
  type_of_current_period       AS quarter,
  net_sales                    AS sales,
  gross_profit,
  operating_profit
FROM field_best
ORDER BY ticker, period, quarter
"""

_QUERY_COUNT = """\
SELECT
  COUNT(*) AS total_raw,
  COUNT(DISTINCT local_code) AS codes
FROM jquants_financials_normalized
"""


def read_sqlite(
    db_path: str, limit: int = 0, recent_days: int = 0
) -> tuple[list[dict], dict]:
    """SQLite から重複排除済みデータを読み取る。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"SQLite DB が見つかりません: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 生データ統計
    raw_stats = dict(conn.execute(_QUERY_COUNT).fetchone())
    logger.info(
        f"[SQLite] 生データ: {raw_stats['total_raw']:,} rows / "
        f"{raw_stats['codes']:,} codes"
    )

    # WHERE句構築（差分同期）
    where_clause = ""
    params: list = []
    if recent_days > 0:
        since = (datetime.now(JST) - timedelta(days=recent_days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        where_clause = "WHERE fetched_at >= ?"
        params.append(since)
        logger.info(f"[SQLite] 差分モード: fetched_at >= {since} (直近{recent_days}日)")

    # 重複排除済みクエリ
    query = _QUERY_BASE.format(where_clause=where_clause)
    if limit > 0:
        query += f" LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    # dict に変換
    now_iso = datetime.now(JST).isoformat()
    data = []

    # ticker 正規化 (5桁 local_code → 4桁 canonical ticker)
    from src.common_ticker import normalize_ticker as _norm_ticker

    abnormal_count = 0
    for r in rows:
        # J-Quants は円単位 → 百万円に正規化
        sales_m = _to_millions(r["sales"])
        gp_m = _to_millions(r["gross_profit"])
        op_m = _to_millions(r["operating_profit"])

        # 正規化後のバリデーション: 百万円として異常に大きい値を検知
        for label, val in [("sales", sales_m), ("gross_profit", gp_m), ("operating_profit", op_m)]:
            if val is not None and abs(val) > _ABNORMAL_MILLIONS_THRESHOLD:
                abnormal_count += 1
                if abnormal_count <= 5:  # 最初の5件だけログ
                    logger.warning(
                        f"[VALIDATE] 百万円として異常値: ticker={_norm_ticker(r['ticker'])} "
                        f"period={r['period']} quarter={r['quarter']} "
                        f"{label}={val} (raw={r[label]})"
                    )

        data.append(
            {
                "ticker": _norm_ticker(r["ticker"]),
                "period": r["period"],
                "quarter": r["quarter"],
                "sales": sales_m,
                "gross_profit": gp_m,
                "operating_profit": op_m,
                "source": "jquants",
                "updated_at": now_iso,
            }
        )

    if abnormal_count > 0:
        logger.warning(
            f"[VALIDATE] 百万円として異常値が {abnormal_count} 件検出されました。"
            f" 元データの単位を確認してください。"
        )

    # ── P4: ticker 正規化後の重複排除 ──
    # SQL では 5桁 local_code で dedupe するが、normalize_ticker で 4桁化すると
    # 同じ (ticker, period, quarter) が生まれうる → バッチ内 duplicate key 500 エラーの原因
    seen_keys: set[tuple[str, str, str]] = set()
    deduped_data: list[dict] = []
    dup_count = 0
    for d in data:
        key = (d["ticker"], d["period"], d["quarter"])
        if key in seen_keys:
            dup_count += 1
            continue
        seen_keys.add(key)
        deduped_data.append(d)
    if dup_count > 0:
        logger.info(
            f"[DEDUPE] ticker正規化後の重複: {dup_count}件除去 "
            f"({len(data)} → {len(deduped_data)})"
        )
    data = deduped_data

    logger.info(f"[SQLite] 重複排除後: {len(data):,} rows")
    return data, raw_stats


# ============================================================
# チャンク分割
# ============================================================
def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


# ============================================================
# メイン同期処理
# ============================================================
def sync(
    db_path: str,
    supabase_url: str,
    supabase_key: str,
    table: str = "financials",
    dry_run: bool = True,
    limit: int = 0,
    batch_size: int = _BATCH_SIZE,
    recent_days: int = 0,
) -> dict:
    """SQLite → Supabase 同期。"""

    stats = {
        "sqlite_raw_rows": 0,
        "sqlite_codes": 0,
        "deduped_rows": 0,
        "upserted": 0,
        "batches": 0,
        "errors": 0,
        "dry_run": dry_run,
        "elapsed_sec": 0,
    }

    # ---- SQLite 読み取り ----
    data, raw_stats = read_sqlite(db_path, limit=limit, recent_days=recent_days)
    stats["sqlite_raw_rows"] = raw_stats["total_raw"]
    stats["sqlite_codes"] = raw_stats["codes"]
    stats["deduped_rows"] = len(data)

    if not data:
        logger.warning("[SYNC] 0 件。同期対象がありません。")
        return stats

    # ---- 分布ログ ----
    quarters: dict[str, int] = {}
    for d in data:
        q = d["quarter"]
        quarters[q] = quarters.get(q, 0) + 1
    logger.info(f"[SYNC] quarter 分布: {json.dumps(quarters, sort_keys=True)}")

    # ---- ドライランチェック ----
    if dry_run:
        logger.info(
            f"\n{'='*60}\n"
            f"  DRY-RUN: Supabase への送信はスキップ\n"
            f"  対象: {len(data):,} rows → {table}\n"
            f"  バッチサイズ: {batch_size}\n"
            f"  バッチ数: {(len(data) + batch_size - 1) // batch_size}\n"
            f"{'='*60}\n"
            f"  本番反映するには --apply を付けて再実行してください\n"
            f"{'='*60}"
        )
        return stats

    # ---- Supabase UPSERT ----
    api = _SupabaseAPI(supabase_url, supabase_key)
    logger.info("[SYNC] requests.Session enabled (connection reuse)")
    total_batches = (len(data) + batch_size - 1) // batch_size
    t0 = time.time()

    logger.info(
        f"[SYNC] 開始: {len(data):,} rows → {table} "
        f"({total_batches} batches × {batch_size})"
    )

    try:
        for i, chunk in enumerate(_chunks(data, batch_size), 1):
            try:
                n = api.upsert_batch(table, chunk, on_conflict="ticker,period,quarter")
                stats["upserted"] += n
                stats["batches"] += 1
                elapsed = time.time() - t0
                logger.info(
                    f"  batch {i}/{total_batches}: "
                    f"{n} rows upserted "
                    f"(累計 {stats['upserted']:,} / {len(data):,}, "
                    f"{elapsed:.1f}秒)"
                )
            except Exception as e:
                stats["errors"] += 1
                logger.error(
                    f"  batch {i}/{total_batches}: FAILED — {e}\n"
                    f"  (先頭行: {chunk[0] if chunk else 'empty'})"
                )

        stats["elapsed_sec"] = round(time.time() - t0, 1)

        # ---- 同期後の検証 ----
        logger.info("[VERIFY] Supabase 側の検証中...")
        try:
            verify = api.select_count(table)
            logger.info(f"[VERIFY] public.{table}: {verify['total_rows']:,} rows")
        except Exception as e:
            logger.warning(f"[VERIFY] 検証スキップ: {e}")

        logger.info(
            f"\n{'='*60}\n"
            f"  SYNC 完了\n"
            f"  upserted: {stats['upserted']:,} / {stats['deduped_rows']:,}\n"
            f"  errors:   {stats['errors']}\n"
            f"  elapsed:  {stats['elapsed_sec']}秒\n"
            f"{'='*60}"
        )

        # ── Phase 2-A: canonical dual-write (best-effort, batched) ──
        if not dry_run and stats["upserted"] > 0:
            try:
                from lib.pipeline.canonical_writer import expand_financials_rows
                from lib.pipeline.db import load_env, get_supabase_write_config, supabase_upsert
                load_env()
                canonical_config = get_supabase_write_config()
                if canonical_config:
                    all_canonical_rows: list[dict] = []
                    canonical_skipped = 0
                    for d in data:
                        metrics_dict = {
                            k: d.get(k)
                            for k in ("sales", "gross_profit", "operating_profit")
                        }
                        expanded, skipped = expand_financials_rows(
                            ticker=d["ticker"],
                            period=d["period"],
                            quarter=d["quarter"],
                            metrics_dict=metrics_dict,
                            source="jquants",
                            unit="millions_jpy",
                        )
                        all_canonical_rows.extend(expanded)
                        canonical_skipped += skipped

                    if all_canonical_rows:
                        upsert_result = supabase_upsert(
                            "canonical_financials",
                            all_canonical_rows,
                            on_conflict="source_row_key",
                            config=canonical_config,
                            batch_size=200,
                            session=api.session,
                        )
                        logger.info(
                            f"[CANONICAL] financials dual-write (jquants, batched): "
                            f"expanded={len(all_canonical_rows)} skipped={canonical_skipped} "
                            f"ok={upsert_result.get('ok')} count={upsert_result.get('count', 0)} "
                            f"batches={upsert_result.get('batches_succeeded', 0)}"
                        )
                    else:
                        logger.info(
                            f"[CANONICAL] financials dual-write: no rows to write "
                            f"(skipped={canonical_skipped})"
                        )
                else:
                    logger.warning(
                        "[CANONICAL] financials dual-write skipped: no write config"
                    )
            except Exception as _cw_err:
                logger.warning(
                    f"[CANONICAL] financials dual-write failed "
                    f"(best-effort, legacy unaffected): {_cw_err}"
                )

        return stats
    finally:
        api.close()


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="SQLite jquants_financials_normalized → Supabase 同期",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
━━━ 安全な実行手順 ━━━

  Step 1: ドライラン（何も変更しない）
    python tools/sync_financials.py

  Step 2: 少量テスト（100件だけ反映）
    python tools/sync_financials.py --apply --limit 100

  Step 3: 直近30日分を反映（デフォルト）
    python tools/sync_financials.py --apply

  Step 4: 全量反映（初回 or リセット時）
    python tools/sync_financials.py --apply --full

━━━ 注意 ━━━
  - --apply を付けないと常にドライラン
  - --full を付けないと直近30日分のみ
  - memos テーブルには一切触れません
  - 二重実行しても安全（UPSERT = 冪等）
""",
    )
    parser.add_argument(
        "--sqlite",
        default=_DEFAULT_DB,
        help=f"SQLite DB パス (default: data/jquants.db)",
    )
    parser.add_argument(
        "--table",
        default="financials",
        help="Supabase テーブル名 (default: financials)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="本番反映する（省略時はドライラン）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="ドライラン（デフォルト動作、明示用）",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="全量同期（省略時は直近30日分のみ）",
    )
    parser.add_argument(
        "--recent",
        type=int,
        default=_DEFAULT_RECENT_DAYS,
        help=f"差分同期の日数 (default: {_DEFAULT_RECENT_DAYS})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="同期する行数の上限 (0=無制限)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_BATCH_SIZE,
        help=f"1バッチあたりの行数 (default: {_BATCH_SIZE})",
    )
    args = parser.parse_args()

    # apply が指定されなければ常に dry-run
    is_dry_run = not args.apply
    # --full なら全量、そうでなければ直近N日
    recent_days = 0 if args.full else args.recent

    # ---- ログ設定 ----
    os.makedirs(_LOG_DIR, exist_ok=True)
    ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    mode_label = "dryrun" if is_dry_run else "apply"
    scope_label = "full" if args.full else f"recent{args.recent}d"
    log_file = os.path.join(
        _LOG_DIR, f"sync_financials_{mode_label}_{scope_label}_{ts}.log"
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    logger.info(f"=== sync_financials START ===")
    logger.info(f"  mode:       {'DRY-RUN' if is_dry_run else 'APPLY (本番反映)'}")
    logger.info(f"  scope:      {'FULL (全量)' if args.full else f'RECENT {args.recent}日'}")
    logger.info(f"  sqlite:     {args.sqlite}")
    logger.info(f"  table:      {args.table}")
    logger.info(f"  limit:      {args.limit or '無制限'}")
    logger.info(f"  batch-size: {args.batch_size}")
    logger.info(f"  log:        {log_file}")

    # ---- .env ----
    _load_dotenv()
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get(
        "SUPABASE_ANON_KEY", ""
    )

    if not supabase_url or not supabase_key:
        logger.error(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY が未設定です。\n"
            "  .env ファイルに以下を追加してください:\n"
            "  SUPABASE_URL=https://xxx.supabase.co\n"
            "  SUPABASE_SERVICE_ROLE_KEY=eyJ..."
        )
        sys.exit(1)

    # ---- 同期 ----
    try:
        stats = sync(
            db_path=args.sqlite,
            supabase_url=supabase_url,
            supabase_key=supabase_key,
            table=args.table,
            dry_run=is_dry_run,
            limit=args.limit,
            batch_size=args.batch_size,
            recent_days=recent_days,
        )
    except Exception as e:
        logger.error(f"FATAL: {e}", exc_info=True)
        sys.exit(1)

    # ---- 終了処理 ----
    if stats["errors"] > 0:
        logger.error(
            f"エラーが {stats['errors']} 件発生しました。ログを確認してください: {log_file}"
        )
        sys.exit(1)

    logger.info(f"=== sync_financials END (log: {log_file}) ===")
    sys.exit(0)


if __name__ == "__main__":
    main()
