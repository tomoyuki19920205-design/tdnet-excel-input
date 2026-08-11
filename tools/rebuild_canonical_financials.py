#!/usr/bin/env python3
r"""
rebuild_canonical_financials.py — canonical_financials 完全再生成ツール

初版スコープ: J-Quants source 限定

サブコマンド:
  init-sql    rebuild テーブル作成 SQL を stdout に出力
  rebuild     元データから canonical_financials_rebuild に再構築
  verify      rebuild テーブルの検証レポートを出力
  compare     本番テーブル (old) と rebuild テーブルの差分を比較
  switch-sql  本番切替 SQL (事前チェック + rename + ロールバック) を出力

使い方:
  .\.venv\Scripts\python.exe tools\rebuild_canonical_financials.py init-sql
  .\.venv\Scripts\python.exe tools\rebuild_canonical_financials.py rebuild --source-group jquants --dry-run
  .\.venv\Scripts\python.exe tools\rebuild_canonical_financials.py rebuild --source-group jquants --apply
  .\.venv\Scripts\python.exe tools\rebuild_canonical_financials.py verify
  .\.venv\Scripts\python.exe tools\rebuild_canonical_financials.py compare --source-group jquants
  .\.venv\Scripts\python.exe tools\rebuild_canonical_financials.py switch-sql
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from src.common_ticker import normalize_ticker
from lib.pipeline.canonical_writer import expand_financials_rows
from lib.pipeline.db import (
    load_env, get_supabase_write_config, get_supabase_read_config,
    supabase_upsert, supabase_select,
)

logger = logging.getLogger("rebuild_canonical")
JST = timezone(timedelta(hours=9))

# ============================================================
# 定数
# ============================================================
REBUILD_TABLE = "canonical_financials_rebuild"
OLD_TABLE = "canonical_financials"

# source-group → 実 source 値マッピング
SOURCE_GROUPS: dict[str, list[str]] = {
    "jquants": ["jquants"],
    "tdnet": [
        "summary_xbrl", "attachment_xbrl", "pdf_table",
        "html_table", "legacy_excel", "tdnet",
    ],
}

# J-Quants 円→百万円変換 (共通モジュール)
from lib.pipeline.unit_convert import to_millions as _to_millions


# migration SQL ファイルパス
_MIGRATION_SQL = os.path.join(
    _PROJECT_ROOT, "migrations", "003_rebuild_canonical_financials.sql"
)

BATCH_SIZE = 200


# ============================================================
# init-sql: rebuild テーブル作成 SQL 出力
# ============================================================
def cmd_init_sql(args: argparse.Namespace) -> int:
    """rebuild テーブル作成 SQL を stdout に出力。"""
    sql_path = _MIGRATION_SQL
    if os.path.isfile(sql_path):
        with open(sql_path, "r", encoding="utf-8") as f:
            print(f.read())
    else:
        logger.error(f"Migration SQL not found: {sql_path}")
        return 1
    print()
    print("-- ↑ Supabase SQL Editor に貼り付けて実行してください。")
    return 0


# ============================================================
# rebuild: 元データから再構築
# ============================================================

# J-Quants 読み取り CTE (sync_financials.py と同等)
_JQUANTS_QUERY_TEMPLATE = """\
WITH resolved_source AS (
  SELECT
    source.*,
    {resolved_period_expression} AS resolved_fiscal_year_end_date
  FROM jquants_financials_normalized AS source
),
latest AS (
  SELECT
    local_code,
    resolved_fiscal_year_end_date AS current_fiscal_year_end_date,
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
  FROM resolved_source
),
field_best AS (
  SELECT
    local_code,
    current_fiscal_year_end_date,
    type_of_current_period,
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

from lib.pipeline.jquants_fiscal_period import resolved_fiscal_year_end_sql

_JQUANTS_QUERY = _JQUANTS_QUERY_TEMPLATE.format(
    resolved_period_expression=resolved_fiscal_year_end_sql("source")
)


def _read_jquants_source(jquants_db: str) -> list[dict]:
    """jquants.db から重複排除 + 百万円変換済みデータを読み取る。"""
    if not os.path.isfile(jquants_db):
        logger.error(f"J-Quants DB not found: {jquants_db}")
        return []

    conn = sqlite3.connect(jquants_db)
    conn.row_factory = sqlite3.Row

    # 生データ統計
    raw_count = conn.execute(
        "SELECT COUNT(*) FROM jquants_financials_normalized"
    ).fetchone()[0]
    code_count = conn.execute(
        "SELECT COUNT(DISTINCT local_code) FROM jquants_financials_normalized"
    ).fetchone()[0]
    logger.info(
        f"[jquants] raw data: {raw_count:,} rows / {code_count:,} codes"
    )

    rows = conn.execute(_JQUANTS_QUERY).fetchall()
    conn.close()

    data: list[dict] = []
    for r in rows:
        ticker = normalize_ticker(r["ticker"])
        sales_m = _to_millions(r["sales"])
        gp_m = _to_millions(r["gross_profit"])
        op_m = _to_millions(r["operating_profit"])

        # 全 None ならスキップ
        if sales_m is None and gp_m is None and op_m is None:
            continue

        data.append({
            "ticker": ticker,
            "period": r["period"],
            "quarter": r["quarter"],
            "metrics": {
                "sales": sales_m,
                "gross_profit": gp_m,
                "operating_profit": op_m,
            },
            "source": "jquants",
        })

    # ticker 正規化後の重複排除
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict] = []
    dup_count = 0
    for d in data:
        key = (d["ticker"], d["period"], d["quarter"])
        if key in seen:
            dup_count += 1
            continue
        seen.add(key)
        deduped.append(d)
    if dup_count > 0:
        logger.info(
            f"[jquants] ticker dedup: {dup_count} removed "
            f"({len(data)} → {len(deduped)})"
        )

    logger.info(f"[jquants] deduped rows: {len(deduped):,}")
    return deduped


def _delete_source_from_rebuild(
    source_group: str, config: dict, session=None,
) -> int:
    """rebuild テーブルから指定 source-group の行を全削除。

    Supabase REST API の DELETE を使用。
    """
    import requests as _req

    sources = SOURCE_GROUPS.get(source_group, [])
    if not sources:
        return 0

    _post = session.request if session else _req.request

    total_deleted = 0
    for source_val in sources:
        url = f"{config['rest_url']}/{REBUILD_TABLE}"
        params = {"source": f"eq.{source_val}"}
        headers = {
            **config["headers"],
            "Prefer": "return=representation",
        }
        try:
            r = _post(
                "DELETE", url,
                headers=headers,
                params=params,
                timeout=60,
            )
            if hasattr(r, 'status_code') and r.status_code in (200, 204):
                try:
                    deleted = r.json()
                    count = len(deleted) if isinstance(deleted, list) else 0
                except Exception:
                    count = 0
                total_deleted += count
                logger.info(
                    f"[rebuild] deleted source={source_val}: {count} rows"
                )
            else:
                status = getattr(r, 'status_code', '?')
                body = getattr(r, 'text', '')[:200]
                logger.warning(
                    f"[rebuild] delete source={source_val} "
                    f"status={status}: {body}"
                )
        except Exception as e:
            logger.warning(f"[rebuild] delete source={source_val} error: {e}")

    return total_deleted


def cmd_rebuild(args: argparse.Namespace) -> int:
    """元データから canonical_financials_rebuild に再構築。"""
    source_group = args.source_group
    dry_run = not args.apply

    if source_group not in SOURCE_GROUPS:
        logger.error(f"Unknown source-group: {source_group}")
        return 1

    if source_group != "jquants":
        logger.error(
            f"Initial version supports jquants only. "
            f"Got: {source_group}"
        )
        return 1

    mode = "DRY-RUN" if dry_run else "APPLY"
    logger.info(f"[rebuild] mode={mode} source-group={source_group}")

    # 1. 元データ読み取り
    jquants_db = os.path.join(_PROJECT_ROOT, "data", "jquants.db")
    data = _read_jquants_source(jquants_db)
    if not data:
        logger.warning("[rebuild] no data to rebuild")
        return 0

    # 2. canonical long rows に展開
    all_rows: list[dict] = []
    skipped_total = 0
    for d in data:
        expanded, skipped = expand_financials_rows(
            ticker=d["ticker"],
            period=d["period"],
            quarter=d["quarter"],
            metrics_dict=d["metrics"],
            source=d["source"],
            unit="millions_jpy",
        )
        all_rows.extend(expanded)
        skipped_total += skipped

    logger.info(
        f"[rebuild] expanded: {len(all_rows):,} long rows "
        f"(skipped={skipped_total:,})"
    )

    # 統計
    source_counts = Counter(r["source"] for r in all_rows)
    metric_counts = Counter(r["metric"] for r in all_rows)
    ticker_count = len(set(r["ticker"] for r in all_rows))
    period_count = len(set(r["period"] for r in all_rows))

    print()
    print("=" * 60)
    print(f"  canonical_financials rebuild - {mode}")
    print("=" * 60)
    print(f"  source-group        : {source_group}")
    print(f"  input rows          : {len(data):,}")
    print(f"  expanded long rows  : {len(all_rows):,}")
    print(f"  skipped (null)      : {skipped_total:,}")
    print(f"  tickers             : {ticker_count:,}")
    print(f"  periods             : {period_count:,}")
    print(f"  source breakdown    :")
    for src, cnt in sorted(source_counts.items()):
        print(f"    {src:25s}: {cnt:,}")
    print(f"  metric breakdown    :")
    for met, cnt in sorted(metric_counts.items()):
        print(f"    {met:25s}: {cnt:,}")
    print("=" * 60)

    if dry_run:
        print()
        print("  DRY-RUN: Supabase への書き込みはスキップ")
        print("  --apply を付けて再実行してください")
        print("=" * 60)
        return 0

    # 3. Supabase write
    load_env()
    config = get_supabase_write_config()
    if not config:
        logger.error("[rebuild] no write config (SUPABASE_SERVICE_ROLE_KEY missing)")
        return 1

    import requests as _req
    with _req.Session() as session:
        # 3a. source-group 単位で DELETE (冪等)
        logger.info(f"[rebuild] deleting existing {source_group} rows from {REBUILD_TABLE}")
        deleted = _delete_source_from_rebuild(source_group, config, session)
        logger.info(f"[rebuild] deleted {deleted:,} existing rows")

        # 3b. INSERT (supabase_upsert で source_row_key on conflict)
        logger.info(f"[rebuild] upserting {len(all_rows):,} rows to {REBUILD_TABLE}")
        t0 = time.monotonic()
        result = supabase_upsert(
            REBUILD_TABLE,
            all_rows,
            on_conflict="source_row_key",
            config=config,
            batch_size=BATCH_SIZE,
            session=session,
        )
        elapsed = time.monotonic() - t0

    written = result.get("count", 0)
    ok = result.get("ok", False)
    error = result.get("error")

    print()
    print("=" * 60)
    print(f"  rebuild result")
    print("=" * 60)
    print(f"  ok                  : {ok}")
    print(f"  written             : {written:,}")
    print(f"  elapsed             : {elapsed:.1f}s")
    print(f"  batches succeeded   : {result.get('batches_succeeded', 0)}")
    print(f"  batches failed      : {result.get('batches_failed', 0)}")
    if error:
        print(f"  error               : {error}")
    print("=" * 60)

    return 0 if ok else 1


# ============================================================
# verify: 検証レポート
# ============================================================

def _supabase_rpc_query(config: dict, sql: str) -> list[dict]:
    """Supabase REST API で SELECT クエリ実行 (RPC ではなく直接)。

    NOTE: 複雑な集計は Supabase REST だけでは困難なため、
    基本統計は REST select、パターンマッチは Python 側で処理する。
    """
    return supabase_select(REBUILD_TABLE, config=config)


def cmd_verify(args: argparse.Namespace) -> int:
    """rebuild テーブルの検証レポートを出力。"""
    load_env()
    config = get_supabase_read_config()
    if not config:
        logger.error("[verify] no read config")
        return 1

    # 1. テーブル存在確認 + 全件取得
    logger.info(f"[verify] fetching all rows from {REBUILD_TABLE}...")
    try:
        # ページネーション対応: 全件取得
        all_rows = _fetch_all_rows(config, REBUILD_TABLE)
    except Exception as e:
        logger.error(f"[verify] failed to fetch {REBUILD_TABLE}: {e}")
        print(f"\n  ERROR: {REBUILD_TABLE} テーブルが存在しないか、アクセスできません。")
        print(f"  init-sql の SQL を Supabase SQL Editor で実行してください。")
        return 1

    total = len(all_rows)
    if total == 0:
        print(f"\n  WARNING: {REBUILD_TABLE} is empty (0 rows)")
        print("  rebuild --source-group jquants --apply を先に実行してください。")
        return 0

    # 2. 基本統計
    source_counts = Counter(r.get("source", "?") for r in all_rows)
    metric_counts = Counter(r.get("metric", "?") for r in all_rows)
    tickers = set(r.get("ticker", "") for r in all_rows)
    periods = set(r.get("period", "") for r in all_rows)

    # source_row_key 重複
    row_keys = [r.get("source_row_key", "") for r in all_rows]
    row_key_counts = Counter(row_keys)
    dup_row_keys = {k: v for k, v in row_key_counts.items() if v > 1}

    # 同一 (ticker, period, quarter, metric) で複数 source の件数
    combo_key_counts = Counter(
        (r.get("ticker"), r.get("period"), r.get("quarter"), r.get("metric"))
        for r in all_rows
    )
    multi_source_combos = {k: v for k, v in combo_key_counts.items() if v > 1}

    # unit mismatch 疑い: 同一 (ticker, period, quarter, metric) で 1M倍差
    unit_mismatch_count = 0
    values_by_combo: dict[tuple, list[float]] = {}
    for r in all_rows:
        key = (r.get("ticker"), r.get("period"), r.get("quarter"), r.get("metric"))
        val = r.get("value")
        if val is not None:
            values_by_combo.setdefault(key, []).append(float(val))
    for key, vals in values_by_combo.items():
        if len(vals) >= 2:
            vals_sorted = sorted(v for v in vals if v != 0)
            if len(vals_sorted) >= 2:
                ratio = vals_sorted[-1] / vals_sorted[0] if vals_sorted[0] != 0 else 0
                if ratio > 500_000:  # ~1M倍差
                    unit_mismatch_count += 1

    # jquants 以外の source 件数チェック (初版 warning)
    non_jquants_sources = {s: c for s, c in source_counts.items() if s != "jquants"}

    # 3. レポート出力
    print()
    print("=" * 70)
    print(f"  canonical_financials_rebuild — 検証レポート")
    print("=" * 70)
    print(f"  全件数                           : {total:,}")
    print(f"  ticker 数                        : {len(tickers):,}")
    print(f"  period 数                        : {len(periods):,}")
    print()
    print(f"  source 別件数:")
    for src, cnt in sorted(source_counts.items()):
        print(f"    {src:25s}: {cnt:,}")
    print()
    print(f"  metric 別件数:")
    for met, cnt in sorted(metric_counts.items()):
        print(f"    {met:25s}: {cnt:,}")
    print()
    print(f"  source_row_key 重複件数           : {len(dup_row_keys)}")
    if dup_row_keys:
        for k, v in list(dup_row_keys.items())[:5]:
            print(f"    {k}: {v}")
    print(f"  異常重複 (同一 combo, 複数行)     : {len(multi_source_combos)}")
    print(f"  unit mismatch 疑い (1M倍差)      : {unit_mismatch_count}")
    print()

    if non_jquants_sources:
        print(f"  ⚠ WARNING: jquants 以外の source が検出されました:")
        for src, cnt in sorted(non_jquants_sources.items()):
            print(f"    {src:25s}: {cnt:,}")
        print()

    # 4. サンプル出力
    _print_samples(all_rows)

    print("=" * 70)
    return 0


def _print_samples(all_rows: list[dict]):
    """検証用サンプルを出力。"""
    # jquants source サンプル 20件
    jquants_rows = [r for r in all_rows if r.get("source") == "jquants"]
    print(f"  --- jquants source サンプル (先頭 20件) ---")
    for r in jquants_rows[:20]:
        print(
            f"    {r.get('ticker'):>5s} | {r.get('period'):>10s} | "
            f"{r.get('quarter'):>4s} | {r.get('metric'):>20s} | "
            f"value={r.get('value'):>12} | unit={r.get('unit')}"
        )
    print()

    # recent ticker サンプル 20件 (updated_at DESC)
    sorted_by_updated = sorted(
        all_rows,
        key=lambda r: r.get("updated_at", ""),
        reverse=True,
    )
    recent_tickers = []
    seen_tickers: set[str] = set()
    for r in sorted_by_updated:
        t = r.get("ticker", "")
        if t not in seen_tickers:
            seen_tickers.add(t)
            recent_tickers.append(r)
        if len(recent_tickers) >= 20:
            break

    print(f"  --- recent ticker サンプル (20件) ---")
    for r in recent_tickers:
        print(
            f"    {r.get('ticker'):>5s} | {r.get('period'):>10s} | "
            f"{r.get('quarter'):>4s} | {r.get('metric'):>20s} | "
            f"value={r.get('value'):>12} | unit={r.get('unit')}"
        )
    print()


# ============================================================
# compare: old vs rebuild 差分比較
# ============================================================

def cmd_compare(args: argparse.Namespace) -> int:
    """本番テーブルと rebuild テーブルの差分を比較。"""
    source_group = args.source_group
    if source_group not in SOURCE_GROUPS:
        logger.error(f"Unknown source-group: {source_group}")
        return 1

    sources = SOURCE_GROUPS[source_group]

    load_env()
    config = get_supabase_read_config()
    if not config:
        logger.error("[compare] no read config")
        return 1

    # 1. 両テーブルから対象 source の行を取得
    logger.info(f"[compare] fetching {OLD_TABLE} rows...")
    old_rows = _fetch_all_rows(config, OLD_TABLE)
    logger.info(f"[compare] fetching {REBUILD_TABLE} rows...")
    rebuild_rows = _fetch_all_rows(config, REBUILD_TABLE)

    # source フィルタ
    old_filtered = [r for r in old_rows if r.get("source") in sources]
    rebuild_filtered = [r for r in rebuild_rows if r.get("source") in sources]

    logger.info(
        f"[compare] old={len(old_filtered):,} rebuild={len(rebuild_filtered):,} "
        f"(source-group={source_group})"
    )

    # 2. 比較キー: (ticker, period, quarter, metric, source)
    def _make_key(r: dict) -> tuple:
        return (
            r.get("ticker", ""),
            r.get("period", ""),
            r.get("quarter", ""),
            r.get("metric", ""),
            r.get("source", ""),
        )

    old_by_key: dict[tuple, dict] = {}
    for r in old_filtered:
        k = _make_key(r)
        old_by_key[k] = r  # 後勝ち (重複がある場合)

    rebuild_by_key: dict[tuple, dict] = {}
    for r in rebuild_filtered:
        k = _make_key(r)
        rebuild_by_key[k] = r

    old_keys = set(old_by_key.keys())
    rebuild_keys = set(rebuild_by_key.keys())

    only_old = old_keys - rebuild_keys
    only_rebuild = rebuild_keys - old_keys
    common = old_keys & rebuild_keys

    # 3. 共通キーの差分分析
    value_match = 0
    value_diff = 0
    million_x_diff = 0
    source_row_key_diff = 0
    unit_diff = 0
    recency_key_diff = 0

    value_diff_samples: list[dict] = []
    million_x_samples: list[dict] = []

    for k in common:
        old_r = old_by_key[k]
        reb_r = rebuild_by_key[k]

        old_val = old_r.get("value")
        reb_val = reb_r.get("value")

        # value 比較
        if old_val == reb_val:
            value_match += 1
        else:
            value_diff += 1
            # 1M倍差チェック
            try:
                ov = float(old_val) if old_val is not None else 0
                rv = float(reb_val) if reb_val is not None else 0
                if rv != 0 and ov != 0:
                    ratio = ov / rv
                    if 500_000 < ratio < 2_000_000:
                        million_x_diff += 1
                        if len(million_x_samples) < 20:
                            million_x_samples.append({
                                "key": k,
                                "old_value": old_val,
                                "rebuild_value": reb_val,
                                "ratio": f"{ratio:.0f}x",
                            })
            except (ValueError, TypeError, ZeroDivisionError):
                pass

            if len(value_diff_samples) < 20:
                value_diff_samples.append({
                    "key": k,
                    "old_value": old_val,
                    "rebuild_value": reb_val,
                })

        # source_row_key 差異
        if old_r.get("source_row_key") != reb_r.get("source_row_key"):
            source_row_key_diff += 1

        # unit 差異
        if old_r.get("unit") != reb_r.get("unit"):
            unit_diff += 1

        # recency_key 差異
        if old_r.get("recency_key") != reb_r.get("recency_key"):
            recency_key_diff += 1

    # 4. レポート出力
    print()
    print("=" * 70)
    print(f"  compare: {OLD_TABLE} vs {REBUILD_TABLE}")
    print(f"  source-group: {source_group}")
    print("=" * 70)
    print(f"  old 件数 (filtered)          : {len(old_filtered):,}")
    print(f"  rebuild 件数 (filtered)      : {len(rebuild_filtered):,}")
    print(f"  件数差                       : {len(rebuild_filtered) - len(old_filtered):+,}")
    print()
    print(f"  old のみ存在 (rebuild に欠落): {len(only_old):,}")
    print(f"  rebuild のみ存在 (old に欠落): {len(only_rebuild):,}")
    print(f"  両方に存在                   : {len(common):,}")
    print()
    print(f"  value 一致                   : {value_match:,}")
    print(f"  value 差異                   : {value_diff:,}")
    print(f"    うち 1,000,000 倍差        : {million_x_diff:,}")
    print()
    print(f"  source_row_key 差異          : {source_row_key_diff:,}")
    print(f"  unit 差異                    : {unit_diff:,}")
    print(f"  recency_key 差異             : {recency_key_diff:,}")
    print()

    # サンプル
    if value_diff_samples:
        print(f"  --- value 差分サンプル (最大 20件) ---")
        for s in value_diff_samples:
            k = s["key"]
            print(
                f"    {k[0]:>5s} | {k[1]:>10s} | {k[2]:>4s} | "
                f"{k[3]:>20s} | {k[4]:>15s} | "
                f"old={s['old_value']} rebuild={s['rebuild_value']}"
            )
        print()

    if million_x_samples:
        print(f"  --- 1,000,000 倍差サンプル (最大 20件) ---")
        for s in million_x_samples:
            k = s["key"]
            print(
                f"    {k[0]:>5s} | {k[1]:>10s} | {k[2]:>4s} | "
                f"{k[3]:>20s} | {k[4]:>15s} | "
                f"old={s['old_value']} rebuild={s['rebuild_value']} "
                f"ratio={s['ratio']}"
            )
        print()

    # only_old サンプル
    if only_old:
        print(f"  --- old のみ存在サンプル (最大 10件) ---")
        for k in list(only_old)[:10]:
            r = old_by_key[k]
            print(
                f"    {k[0]:>5s} | {k[1]:>10s} | {k[2]:>4s} | "
                f"{k[3]:>20s} | {k[4]:>15s} | value={r.get('value')}"
            )
        print()

    # only_rebuild サンプル
    if only_rebuild:
        print(f"  --- rebuild のみ存在サンプル (最大 10件) ---")
        for k in list(only_rebuild)[:10]:
            r = rebuild_by_key[k]
            print(
                f"    {k[0]:>5s} | {k[1]:>10s} | {k[2]:>4s} | "
                f"{k[3]:>20s} | {k[4]:>15s} | value={r.get('value')}"
            )
        print()

    print("=" * 70)
    return 0


# ============================================================
# switch-sql: 本番切替 SQL 出力
# ============================================================

def cmd_switch_sql(args: argparse.Namespace) -> int:
    """事前チェック SQL + 切替 SQL + ロールバック SQL を出力。"""
    today = datetime.now(JST).strftime("%Y%m%d")
    backup_name = f"canonical_financials_backup_{today}"

    print("""\
-- =============================================================
-- canonical_financials 本番切替
-- =============================================================

-- ===== 1. 事前チェック: dependent objects の確認 =====
-- ↓ Supabase SQL Editor で先に実行して結果を確認してください。

-- views that reference canonical_financials
SELECT viewname, definition
FROM pg_views
WHERE definition ILIKE '%canonical_financials%'
  AND schemaname = 'public';

-- materialized views
SELECT matviewname, definition
FROM pg_matviews
WHERE definition ILIKE '%canonical_financials%'
  AND schemaname = 'public';

-- functions that reference canonical_financials
SELECT proname, prosrc
FROM pg_proc p
JOIN pg_namespace n ON p.pronamespace = n.oid
WHERE prosrc ILIKE '%canonical_financials%'
  AND n.nspname = 'public';

-- RLS policies
SELECT policyname, tablename, cmd, qual, with_check
FROM pg_policies
WHERE tablename = 'canonical_financials';

-- foreign keys referencing canonical_financials
SELECT conname, conrelid::regclass AS referencing_table,
       confrelid::regclass AS referenced_table
FROM pg_constraint
WHERE confrelid = 'canonical_financials'::regclass;

-- indexes
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'canonical_financials';

-- triggers
SELECT tgname, tgrelid::regclass
FROM pg_trigger
WHERE tgrelid = 'canonical_financials'::regclass
  AND NOT tgisinternal;
""")

    print(f"""\
-- ===== 2. 切替 SQL (事前チェック確認後に手動実行) =====
-- ⚠ 事前チェックで dependent objects がある場合は、
--   それらの再作成 SQL もここに追加してください。

BEGIN;
  -- バックアップ
  ALTER TABLE canonical_financials
    RENAME TO {backup_name};

  -- rebuild → 本番
  ALTER TABLE canonical_financials_rebuild
    RENAME TO canonical_financials;

  -- RLS policies の再作成 (rename では policy 名が変わらないため)
  -- ↓ 事前チェック結果に基づいて必要に応じて修正
  DROP POLICY IF EXISTS cfr_anon_read ON canonical_financials;
  DROP POLICY IF EXISTS cfr_service_write ON canonical_financials;
  CREATE POLICY cf_anon_read ON canonical_financials
    FOR SELECT USING (true);
  CREATE POLICY cf_service_write ON canonical_financials
    FOR ALL USING (auth.role() = 'service_role');
COMMIT;


-- ===== 3. ロールバック SQL (問題が見つかった場合) =====
BEGIN;
  ALTER TABLE canonical_financials
    RENAME TO canonical_financials_rebuild;
  ALTER TABLE {backup_name}
    RENAME TO canonical_financials;

  -- ロールバック時も RLS policies を復元
  DROP POLICY IF EXISTS cf_anon_read ON canonical_financials;
  DROP POLICY IF EXISTS cf_service_write ON canonical_financials;
  CREATE POLICY cf_anon_read ON canonical_financials
    FOR SELECT USING (true);
  CREATE POLICY cf_service_write ON canonical_financials
    FOR ALL USING (auth.role() = 'service_role');
COMMIT;
""")
    return 0


# ============================================================
# ヘルパー: Supabase 全件取得 (ページネーション対応)
# ============================================================

def _fetch_all_rows(
    config: dict, table: str,
    page_size: int = 1000,
) -> list[dict]:
    """Supabase REST API で全件取得 (Range ヘッダー ページネーション)。"""
    import requests as _req

    all_rows: list[dict] = []
    offset = 0

    while True:
        headers = {
            **config["headers"],
            "Range": f"{offset}-{offset + page_size - 1}",
        }
        r = _req.get(
            f"{config['rest_url']}/{table}",
            headers=headers,
            params={"select": "*"},
            timeout=60,
        )
        if r.status_code == 416:
            # Range Not Satisfiable = no more rows
            break
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size

    return all_rows


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="canonical_financials 完全再生成ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="サブコマンド")

    # init-sql
    sub.add_parser("init-sql", help="rebuild テーブル作成 SQL を出力")

    # rebuild
    p_rebuild = sub.add_parser(
        "rebuild", help="元データから rebuild テーブルに再構築"
    )
    p_rebuild.add_argument(
        "--source-group", required=True,
        choices=list(SOURCE_GROUPS.keys()) + ["all"],
        help="再生成対象の source グループ",
    )
    p_rebuild.add_argument(
        "--apply", action="store_true",
        help="Supabase に書き込む (省略時は dry-run)",
    )
    p_rebuild.add_argument(
        "--dry-run", action="store_true",
        help="dry-run (デフォルト動作、明示用)",
    )

    # verify
    sub.add_parser("verify", help="rebuild テーブルの検証レポート")

    # compare
    p_compare = sub.add_parser(
        "compare", help="old vs rebuild の差分比較"
    )
    p_compare.add_argument(
        "--source-group", required=True,
        choices=list(SOURCE_GROUPS.keys()) + ["all"],
        help="比較対象の source グループ",
    )

    # switch-sql
    sub.add_parser("switch-sql", help="本番切替 SQL を出力")

    return parser


def main(args: list[str] | None = None) -> int:
    parser = build_parser()
    opts = parser.parse_args(args)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not opts.command:
        parser.print_help()
        return 1

    cmd_map = {
        "init-sql": cmd_init_sql,
        "rebuild": cmd_rebuild,
        "verify": cmd_verify,
        "compare": cmd_compare,
        "switch-sql": cmd_switch_sql,
    }

    handler = cmd_map.get(opts.command)
    if not handler:
        parser.print_help()
        return 1

    return handler(opts)


if __name__ == "__main__":
    sys.exit(main())
