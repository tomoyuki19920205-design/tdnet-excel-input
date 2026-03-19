#!/usr/bin/env python3
# ============================================================
# audit_supabase_vs_sqlite.py
# SQLite (data/jquants.db) ↔ Supabase (public.financials) 整合性監査
# ============================================================
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

import requests

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
_DEFAULT_SQLITE = os.path.join(_PROJECT_ROOT, "data", "jquants.db")
_DEFAULT_OUTPUT = os.path.join(_PROJECT_ROOT, "artifacts", "audit")

JST = timezone(timedelta(hours=9))
logger = logging.getLogger("audit")

# ============================================================
# 比較キー: (ticker, period, quarter)
# 理由: Supabase financials の UNIQUE 制約 / sync_financials.py の on_conflict と一致
# ============================================================
VALUE_COLUMNS = ["sales", "gross_profit", "operating_profit"]

# ============================================================
# 単位正規化
# ============================================================
# SQLite jquants_financials_normalized は円単位、
# Supabase financials は百万円単位で保持されている。
# 比較前に SQLite 側を ÷ _UNIT_DIVISOR して百万円に変換する。
# sync_financials.py は変換なしで投入しているが、Supabase 側データは
# 過去の投入ルートで百万円化されている実態に合わせる。
_UNIT_DIVISOR = Decimal("1000000")

# source 表記揺れ辞書
_SOURCE_NORMALIZE_MAP = {
    "j-quants": "jquants",
    "j_quants": "jquants",
    "J-Quants": "jquants",
    "J_Quants": "jquants",
}


# ============================================================
# .env 読み込み (既存流儀踏襲)
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
# Supabase REST API (既存流儀踏襲・読み取り専用)
# ============================================================
class _SupabaseAPI:
    def __init__(self, url: str, key: str) -> None:
        self.rest_url = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "",
        }

    def select_all(
        self, table: str, columns: str = "*",
        filters: dict | None = None, page_size: int = 1000,
    ) -> list[dict]:
        """ページネーション付き全件取得"""
        all_rows: list[dict] = []
        offset = 0
        while True:
            params = {
                "select": columns,
                "limit": str(page_size),
                "offset": str(offset),
            }
            if filters:
                params.update(filters)
            r = requests.get(
                f"{self.rest_url}/{table}",
                headers=self.headers,
                params=params,
                timeout=60,
            )
            r.raise_for_status()
            rows = r.json()
            if not rows:
                break
            all_rows.extend(rows)
            if len(rows) < page_size:
                break
            offset += page_size
            logger.debug(f"  Supabase fetch: {len(all_rows)} rows so far...")
        return all_rows


# ============================================================
# 正規化関数
# ============================================================
def normalize_null(value: Any) -> Any:
    """None / 空文字 / "null" / "None" / NaN → None"""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, str):
        s = value.strip()
        if s == "" or s.lower() in ("null", "none", "nan"):
            return None
        return s
    return value


def normalize_numeric(value: Any) -> Optional[Decimal]:
    """数値を Decimal に正規化。None は None のまま。"""
    v = normalize_null(value)
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        logger.warning(f"数値変換失敗: {value!r}")
        return None


def normalize_ticker(value: Any) -> str:
    """ticker を4桁正規化する。common_ticker に委譲。"""
    from src.common_ticker import normalize_ticker as _norm
    v = normalize_null(value)
    if v is None:
        return ""
    return _norm(str(v))


def normalize_ticker_raw(value: Any) -> str:
    """ticker の生値（正規化前）を返す。"""
    v = normalize_null(value)
    return str(v).strip() if v is not None else ""


def normalize_period(value: Any) -> str:
    v = normalize_null(value)
    if v is None:
        return ""
    s = str(v).strip()
    # YYYY-MM-DD 形式ならそのまま
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    # datetime 文字列の場合は日付部分のみ
    if "T" in s:
        return s.split("T")[0]
    return s


def normalize_quarter(value: Any) -> str:
    v = normalize_null(value)
    return str(v).strip() if v is not None else ""


def normalize_source(value: Any) -> tuple[str, str]:
    """(raw, normalized) を返す"""
    v = normalize_null(value)
    if v is None:
        return ("", "")
    raw = str(v).strip()
    norm = raw.lower()
    norm = _SOURCE_NORMALIZE_MAP.get(raw, norm)
    norm = _SOURCE_NORMALIZE_MAP.get(norm, norm)
    return (raw, norm)


def normalize_updated_at(value: Any) -> Optional[datetime]:
    v = normalize_null(value)
    if v is None:
        return None
    s = str(v).strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    logger.warning(f"updated_at パース失敗: {s!r}")
    return None


def _scale_to_millions(v: Decimal | None) -> Decimal | None:
    """円単位の Decimal を百万円単位に変換する。"""
    if v is None:
        return None
    return v / _UNIT_DIVISOR


def normalize_row(row: dict, scale_to_millions: bool = False) -> dict:
    """比較前の統一処理。

    scale_to_millions=True の場合、数値列を円→百万円に変換する
    （SQLite 側に適用）。raw 値は常に保持する。
    """
    sales_raw = normalize_numeric(row.get("sales"))
    gp_raw = normalize_numeric(row.get("gross_profit"))
    op_raw = normalize_numeric(row.get("operating_profit"))

    if scale_to_millions:
        sales_cmp = _scale_to_millions(sales_raw)
        gp_cmp = _scale_to_millions(gp_raw)
        op_cmp = _scale_to_millions(op_raw)
    else:
        sales_cmp = sales_raw
        gp_cmp = gp_raw
        op_cmp = op_raw

    return {
        "raw_ticker": normalize_ticker_raw(row.get("ticker", "")),
        "ticker": normalize_ticker(row.get("ticker", "")),
        "period": normalize_period(row.get("period", "")),
        "quarter": normalize_quarter(row.get("quarter", "")),
        "sales_raw": sales_raw,
        "gross_profit_raw": gp_raw,
        "operating_profit_raw": op_raw,
        "sales": sales_cmp,
        "gross_profit": gp_cmp,
        "operating_profit": op_cmp,
        "source_raw": normalize_source(row.get("source", ""))[0],
        "source_normalized": normalize_source(row.get("source", ""))[1],
        "updated_at": normalize_updated_at(row.get("updated_at")),
        "updated_at_raw": str(row.get("updated_at", "")),
    }


def build_comparison_key(row: dict) -> tuple[str, str, str]:
    return (
        normalize_ticker(row.get("ticker", "")),
        normalize_period(row.get("period", "")),
        normalize_quarter(row.get("quarter", "")),
    )


# ============================================================
# データ読み込み
# ============================================================
import sqlite3

# sync_financials.py と同じ重複排除 CTE
_SQLITE_CTE = """\
WITH ranked AS (
  SELECT
    local_code,
    current_fiscal_year_end_date,
    type_of_current_period,
    net_sales,
    gross_profit,
    operating_profit,
    disclosed_date,
    fetched_at,
    ROW_NUMBER() OVER (
      PARTITION BY local_code,
                   current_fiscal_year_end_date,
                   type_of_current_period
      ORDER BY disclosed_date DESC
    ) AS rn
  FROM jquants_financials_normalized
  {where_clause}
)
SELECT
  local_code                   AS ticker,
  current_fiscal_year_end_date AS period,
  type_of_current_period       AS quarter,
  net_sales                    AS sales,
  gross_profit,
  operating_profit
FROM ranked
WHERE rn = 1
ORDER BY ticker, period, quarter
"""

_SQLITE_RAW = """\
SELECT
  local_code                   AS ticker,
  current_fiscal_year_end_date AS period,
  type_of_current_period       AS quarter,
  net_sales                    AS sales,
  gross_profit,
  operating_profit,
  disclosed_date,
  fetched_at
FROM jquants_financials_normalized
{where_clause}
ORDER BY local_code, current_fiscal_year_end_date, type_of_current_period
"""


def _ticker_to_local_code(ticker: str) -> str:
    """4桁 ticker → 5桁 local_code 変換。common_ticker に委譲。"""
    from src.common_ticker import ticker_to_sec_code
    return ticker_to_sec_code(ticker)


def _build_where(ticker=None, tickers=None):
    clauses = []
    params = []
    if ticker:
        # 4桁 ticker → 5桁 local_code に変換して検索
        lc = _ticker_to_local_code(ticker)
        clauses.append("(local_code = ? OR local_code = ?)")
        params.extend([ticker, lc])
    elif tickers:
        # 各 ticker について4桁と5桁の両方を候補に
        all_codes = []
        for t in tickers:
            all_codes.append(t)
            lc = _ticker_to_local_code(t)
            if lc != t:
                all_codes.append(lc)
        placeholders = ",".join("?" for _ in all_codes)
        clauses.append(f"local_code IN ({placeholders})")
        params.extend(all_codes)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    return where, params


def load_sqlite_rows_for_comparison(
    db_path: str, ticker=None, tickers=None, limit=None,
) -> list[dict]:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"SQLite DB が見つかりません: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    where, params = _build_where(ticker, tickers)
    query = _SQLITE_CTE.format(where_clause=where)
    if limit and limit > 0:
        query += f" LIMIT ?"
        params.append(limit)
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    conn.close()
    logger.info(f"[SQLite] 比較用データ: {len(rows):,} rows (重複排除済)")
    return rows


def load_sqlite_rows_raw(
    db_path: str, ticker=None, tickers=None, limit=None,
) -> list[dict]:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"SQLite DB が見つかりません: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    where, params = _build_where(ticker, tickers)
    query = _SQLITE_RAW.format(where_clause=where)
    if limit and limit > 0:
        query += f" LIMIT ?"
        params.append(limit)
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    conn.close()
    logger.info(f"[SQLite] 生データ: {len(rows):,} rows")
    return rows


def load_supabase_rows(
    api: _SupabaseAPI, ticker=None, tickers=None, table="financials",
) -> list[dict]:
    filters = {}
    if ticker:
        filters["ticker"] = f"eq.{ticker}"
    elif tickers:
        filters["ticker"] = f"in.({','.join(tickers)})"
    rows = api.select_all(table, filters=filters)
    logger.info(f"[Supabase] {table}: {len(rows):,} rows")
    return rows


# ============================================================
# 比較ロジック
# ============================================================
def detect_missing_rows(
    sqlite_map: dict[tuple, dict], supabase_map: dict[tuple, dict],
) -> tuple[list[dict], list[dict]]:
    missing_in_supa = []
    for k, row in sqlite_map.items():
        if k not in supabase_map:
            missing_in_supa.append({**row, "comparison_key": "|".join(k), "reason": "exists_in_sqlite_only"})
    missing_in_sqlite = []
    for k, row in supabase_map.items():
        if k not in sqlite_map:
            missing_in_sqlite.append({**row, "comparison_key": "|".join(k), "reason": "exists_in_supabase_only"})
    return missing_in_supa, missing_in_sqlite


def detect_duplicate_rows(rows: list[dict], label: str) -> list[dict]:
    key_counts: Counter = Counter()
    key_samples: dict[tuple, list] = defaultdict(list)
    for row in rows:
        k = build_comparison_key(row)
        key_counts[k] += 1
        if len(key_samples[k]) < 3:
            key_samples[k].append(row)
    dupes = []
    for k, count in key_counts.items():
        if count > 1:
            dupes.append({
                "comparison_key": "|".join(k),
                "ticker": k[0], "period": k[1], "quarter": k[2],
                "duplicate_count": count,
                "sample_rows_json": json.dumps(key_samples[k][:3], default=str, ensure_ascii=False),
            })
    return dupes


def compare_value_columns(
    sqlite_map: dict[tuple, dict], supabase_map: dict[tuple, dict],
    columns: list[str],
) -> tuple[list[dict], dict[str, int], int]:
    """比較列の値差分検出。正規化済み値（百万円単位に揃え済み）で比較し、
    CSV には raw 値と normalized 値の両方を出力する。"""
    mismatches = []
    col_mismatch_counts: dict[str, int] = {c: 0 for c in columns}
    perfect_match_count = 0
    common_keys = set(sqlite_map.keys()) & set(supabase_map.keys())
    for k in common_keys:
        s_row = sqlite_map[k]
        p_row = supabase_map[k]
        row_ok = True
        for col in columns:
            sv = s_row.get(col)  # normalized (百万円)
            pv = p_row.get(col)  # normalized (百万円)
            if sv is None and pv is None:
                continue
            if sv is None and pv is not None:
                diff_type = "sqlite_null_supabase_non_null"
            elif sv is not None and pv is None:
                diff_type = "sqlite_non_null_supabase_null"
            elif sv != pv:
                diff_type = "both_non_null_value_diff"
            else:
                continue
            row_ok = False
            col_mismatch_counts[col] += 1
            # raw 値を取得
            sv_raw = s_row.get(f"{col}_raw", sv)
            pv_raw = p_row.get(f"{col}_raw", pv)
            mismatches.append({
                "ticker": k[0], "period": k[1], "quarter": k[2],
                "comparison_key": "|".join(k),
                "column_name": col,
                "sqlite_value_raw": str(sv_raw) if sv_raw is not None else "",
                "sqlite_value_normalized": str(sv) if sv is not None else "",
                "supabase_value_raw": str(pv_raw) if pv_raw is not None else "",
                "supabase_value_normalized": str(pv) if pv is not None else "",
                "difference_type": diff_type,
            })
        if row_ok:
            perfect_match_count += 1
    return mismatches, col_mismatch_counts, perfect_match_count


def compare_nulls(
    sqlite_map: dict[tuple, dict], supabase_map: dict[tuple, dict],
    columns: list[str],
) -> tuple[list[dict], list[dict]]:
    null_summary = []
    null_rows = []
    common_keys = set(sqlite_map.keys()) & set(supabase_map.keys())
    n = len(common_keys) if common_keys else 1
    for col in columns:
        s_nulls = sum(1 for k in common_keys if sqlite_map[k].get(col) is None)
        p_nulls = sum(1 for k in common_keys if supabase_map[k].get(col) is None)
        null_summary.append({
            "column": col,
            "sqlite_null_count": s_nulls,
            "supabase_null_count": p_nulls,
            "sqlite_null_pct": round(s_nulls / n * 100, 2) if n else 0,
            "supabase_null_pct": round(p_nulls / n * 100, 2) if n else 0,
            "null_diff": abs(s_nulls - p_nulls),
        })
        for k in common_keys:
            s_null = sqlite_map[k].get(col) is None
            p_null = supabase_map[k].get(col) is None
            if s_null != p_null:
                null_rows.append({
                    "ticker": k[0], "period": k[1], "quarter": k[2],
                    "comparison_key": "|".join(k),
                    "column_name": col,
                    "sqlite_is_null": s_null,
                    "supabase_is_null": p_null,
                    "difference_type": "sqlite_null" if s_null else "supabase_null",
                })
    return null_summary, null_rows


def compare_source(
    sqlite_map: dict[tuple, dict], supabase_map: dict[tuple, dict],
) -> tuple[list[dict], dict, dict]:
    mismatches = []
    s_dist: Counter = Counter()
    p_dist: Counter = Counter()
    common_keys = set(sqlite_map.keys()) & set(supabase_map.keys())
    for k in common_keys:
        s_raw = sqlite_map[k].get("source_raw", "")
        s_norm = sqlite_map[k].get("source_normalized", "")
        p_raw = supabase_map[k].get("source_raw", "")
        p_norm = supabase_map[k].get("source_normalized", "")
        s_dist[s_norm] += 1
        p_dist[p_norm] += 1
        if s_norm != p_norm:
            mismatches.append({
                "ticker": k[0], "period": k[1], "quarter": k[2],
                "comparison_key": "|".join(k),
                "sqlite_source_raw": s_raw,
                "sqlite_source_normalized": s_norm,
                "supabase_source_raw": p_raw,
                "supabase_source_normalized": p_norm,
                "difference_type": "source_mismatch",
            })
    return mismatches, dict(s_dist), dict(p_dist)


def compare_updated_at(
    sqlite_map: dict[tuple, dict], supabase_map: dict[tuple, dict],
) -> tuple[list[dict], dict]:
    stats = {"equal": 0, "sqlite_newer": 0, "supabase_newer": 0, "one_side_null": 0}
    mismatches = []
    common_keys = set(sqlite_map.keys()) & set(supabase_map.keys())
    for k in common_keys:
        s_dt = sqlite_map[k].get("updated_at")
        p_dt = supabase_map[k].get("updated_at")
        if s_dt is None or p_dt is None:
            if s_dt != p_dt:
                stats["one_side_null"] += 1
                mismatches.append({
                    "ticker": k[0], "period": k[1], "quarter": k[2],
                    "comparison_key": "|".join(k),
                    "sqlite_updated_at": str(s_dt or ""),
                    "supabase_updated_at": str(p_dt or ""),
                    "difference_type": "one_side_null",
                    "comparison_result": "one_side_null",
                })
            else:
                stats["equal"] += 1
            continue
        # timezone-naive 同士の比較にそろえる
        s_cmp = s_dt.replace(tzinfo=None) if s_dt.tzinfo else s_dt
        p_cmp = p_dt.replace(tzinfo=None) if p_dt.tzinfo else p_dt
        if s_cmp == p_cmp:
            stats["equal"] += 1
        elif s_cmp > p_cmp:
            stats["sqlite_newer"] += 1
            mismatches.append({
                "ticker": k[0], "period": k[1], "quarter": k[2],
                "comparison_key": "|".join(k),
                "sqlite_updated_at": str(s_dt),
                "supabase_updated_at": str(p_dt),
                "difference_type": "timestamp_diff",
                "comparison_result": "sqlite_newer",
            })
        else:
            stats["supabase_newer"] += 1
            mismatches.append({
                "ticker": k[0], "period": k[1], "quarter": k[2],
                "comparison_key": "|".join(k),
                "sqlite_updated_at": str(s_dt),
                "supabase_updated_at": str(p_dt),
                "difference_type": "timestamp_diff",
                "comparison_result": "supabase_newer",
            })
    return mismatches, stats


def summarize_column_presence(sqlite_rows, supabase_rows):
    s_cols = set(sqlite_rows[0].keys()) if sqlite_rows else set()
    p_cols = set(supabase_rows[0].keys()) if supabase_rows else set()
    return {
        "sqlite_only": sorted(s_cols - p_cols),
        "supabase_only": sorted(p_cols - s_cols),
        "common": sorted(s_cols & p_cols),
        "compared": VALUE_COLUMNS,
    }


def summarize_top_mismatches(mismatches: list[dict], field: str, n: int = 10):
    c: Counter = Counter()
    for m in mismatches:
        c[m.get(field, "")] += 1
    return c.most_common(n)


# ============================================================
# レポート生成
# ============================================================
def generate_markdown_report(results: dict) -> str:
    r = results
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    all_keys = r["all_unique_keys"]
    common = r["common_keys"]
    key_match_rate = (common / all_keys * 100) if all_keys else 0
    perfect = r["perfect_match_count"]
    perfect_rate = (perfect / common * 100) if common else 0

    lines = [
        f"# financials 整合性監査レポート",
        f"",
        f"## 実行情報",
        f"| 項目 | 値 |",
        f"|:---|:---|",
        f"| 実行時刻 | {now} |",
        f"| 対象テーブル | financials |",
        f"| SQLite DB | {r['sqlite_db']} |",
        f"| Supabase | public.financials |",
        f"| 対象 ticker | {r['ticker_condition']} |",
        f"| 比較キー | `(ticker, period, quarter)` |",
        f"| 比較対象列 | {', '.join(VALUE_COLUMNS)} |",
        f"| strict 判定対象 | missing / value_mismatch / source_mismatch / duplicate |",
        f"",
        f"> **ticker 正規化ルール**: SQLite の local_code は5桁末尾0形式（例: 67500）で格納されていますが、",
        f"> Supabase の ticker は4桁形式（例: 6750）です。比較時は5桁末尾0を4桁に正規化して統一しています。",
        f"",
        f"> **数値単位正規化**: SQLite は円単位、Supabase は百万円単位で保持されています。",
        f"> 比較前に SQLite 側の数値を ÷ 1,000,000 して百万円に変換しています。",
        f"> CSV の `sqlite_value_raw` は元の円単位、`sqlite_value_normalized` は百万円に変換後の値です。",
        f"",
    ]
    if r.get("source_skipped"):
        lines.append(f"> **source 比較スキップ**: SQLite 側に source 列がないため source 比較をスキップしています。")
        lines.append(f"> source_mismatch は 0 として扱い、strict 判定にも含めません。")
        lines.append(f"")
    lines.extend([
        f"> **注意**: 今回の比較対象列は {len(VALUE_COLUMNS)} 列のみです（{', '.join(VALUE_COLUMNS)}）。",
        f"> updated_at は参考指標として集計していますが、strict 判定や完全一致率には含めません。",
        f"",
        f"## 件数サマリ",
        f"| 指標 | 件数 |",
        f"|:---|---:|",
        f"| SQLite 件数 (重複排除後) | {r['sqlite_count']:,} |",
        f"| Supabase 件数 | {r['supabase_count']:,} |",
        f"| 全ユニーク key 数 | {all_keys:,} |",
        f"| 両DB共通 key 数 | {common:,} |",
        f"| missing_in_supabase | {r['missing_in_supabase_count']:,} |",
        f"| missing_in_sqlite | {r['missing_in_sqlite_count']:,} |",
        f"| value_mismatch 行 | {r['value_mismatch_count']:,} |",
        f"| source_mismatch | {r['source_mismatch_count']:,} |",
        f"| updated_at_mismatch | {r['updated_at_mismatch_count']:,} |",
        f"| duplicate_in_sqlite | {r['duplicate_in_sqlite_count']:,} |",
        f"| duplicate_in_supabase | {r['duplicate_in_supabase_count']:,} |",
        f"",
        f"## 一致率",
        f"| 指標 | 値 | 計算式 |",
        f"|:---|---:|:---|",
        f"| key ベース一致率 | {key_match_rate:.2f}% | 両DBに存在する key / 全ユニーク key |",
        f"| 完全一致率 (value columns) | {perfect_rate:.2f}% | value列すべて一致する行 / 共通key数 |",
    ])

    # 列別一致率
    lines.append("")
    lines.append("### 列別一致率")
    lines.append("| 列 | 一致セル数 | 比較対象セル数 | 一致率 |")
    lines.append("|:---|---:|---:|---:|")
    for col in VALUE_COLUMNS:
        mm = r["col_mismatch_counts"].get(col, 0)
        matched = common - mm
        rate = (matched / common * 100) if common else 0
        lines.append(f"| {col} | {matched:,} | {common:,} | {rate:.2f}% |")

    # NULL率
    lines.append("")
    lines.append("### NULL率比較")
    lines.append("| 列 | SQLite NULL数 | SQLite NULL率 | Supabase NULL数 | Supabase NULL率 | 差 |")
    lines.append("|:---|---:|---:|---:|---:|---:|")
    for ns in r.get("null_summary", []):
        lines.append(
            f"| {ns['column']} | {ns['sqlite_null_count']:,} | {ns['sqlite_null_pct']:.1f}% "
            f"| {ns['supabase_null_count']:,} | {ns['supabase_null_pct']:.1f}% | {ns['null_diff']:,} |"
        )

    # source 分布
    lines.append("")
    lines.append("### source 分布比較")
    lines.append("| source | SQLite件数 | Supabase件数 |")
    lines.append("|:---|---:|---:|")
    all_sources = sorted(set(list(r.get("sqlite_source_dist", {}).keys()) + list(r.get("supabase_source_dist", {}).keys())))
    for src in all_sources:
        sc = r.get("sqlite_source_dist", {}).get(src, 0)
        pc = r.get("supabase_source_dist", {}).get(src, 0)
        lines.append(f"| {src or '(empty)'} | {sc:,} | {pc:,} |")

    # updated_at サマリ
    lines.append("")
    lines.append("### updated_at 比較サマリ (参考指標)")
    uas = r.get("updated_at_stats", {})
    lines.append(f"| 状態 | 件数 |")
    lines.append(f"|:---|---:|")
    for k2, v2 in uas.items():
        lines.append(f"| {k2} | {v2:,} |")

    # 列存在サマリ
    cp = r.get("column_presence", {})
    if cp:
        lines.append("")
        lines.append("### 列存在サマリ")
        lines.append(f"- SQLite only: {', '.join(cp.get('sqlite_only', [])) or '(なし)'}")
        lines.append(f"- Supabase only: {', '.join(cp.get('supabase_only', [])) or '(なし)'}")
        lines.append(f"- 共通列: {', '.join(cp.get('common', [])) or '(なし)'}")
        lines.append(f"- 今回比較列: {', '.join(cp.get('compared', []))}")

    # 差分上位
    lines.append("")
    lines.append("### 差分上位 ticker")
    lines.append("| ticker | mismatch件数 |")
    lines.append("|:---|---:|")
    for t, c2 in r.get("top_mismatch_tickers", [])[:10]:
        lines.append(f"| {t} | {c2} |")

    lines.append("")
    lines.append("### 差分上位 period")
    lines.append("| period | mismatch件数 |")
    lines.append("|:---|---:|")
    for p2, c3 in r.get("top_mismatch_periods", [])[:10]:
        lines.append(f"| {p2} | {c3} |")

    # 所見
    lines.append("")
    lines.append("## 所見")
    findings = []
    for ns2 in r.get("null_summary", []):
        if ns2["null_diff"] > 0:
            findings.append(f"- {ns2['column']} の NULL 差が {ns2['null_diff']:,} 件あります")
    if r["source_mismatch_count"] > 0:
        findings.append(f"- source 不一致が {r['source_mismatch_count']:,} 件あります")
    if r.get("source_skipped"):
        findings.append("- SQLite 側に source 列がないため source 比較はスキップされました")
    if r.get("updated_at_mismatch_count", 0) > 0:
        findings.append(f"- updated_at 差分は {r['updated_at_mismatch_count']:,} 件（参考指標: 同期実行時刻差の可能性）")
    if r["missing_in_supabase_count"] > 0:
        findings.append(f"- Supabase に未反映の行が {r['missing_in_supabase_count']:,} 件あります")
    if r["missing_in_sqlite_count"] > 0:
        findings.append(f"- Supabase にのみ存在する行が {r['missing_in_sqlite_count']:,} 件あります（SQLite 側で削除された可能性）")
    if r["duplicate_in_sqlite_count"] > 0:
        findings.append(f"- SQLite 生データに重複が {r['duplicate_in_sqlite_count']:,} グループ あります")
    if not findings:
        findings.append("- 特記事項なし。一致率は良好です。")
    lines.extend(findings)

    lines.append("")
    lines.append("## 制約")
    lines.append(f"- 今回は {', '.join(VALUE_COLUMNS)} の {len(VALUE_COLUMNS)} 列のみ対象")
    lines.append("- updated_at は参考指標（strict 判定や完全一致率に含まない）")
    lines.append("- Supabase 側ページネーション取得のため、大量データ時に時間がかかる場合があります")
    lines.append("")

    return "\n".join(lines)


# ============================================================
# CSV 出力
# ============================================================
def _write_csv(filepath: str, rows: list[dict], fieldnames: list[str] | None = None):
    if not rows:
        # 空の場合もヘッダ付きで出力
        fn = fieldnames or []
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fn)
            w.writeheader()
        return
    fn = fieldnames or list(rows[0].keys())
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fn, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_csv_outputs(results: dict, output_dir: str, sample: int = 0):
    os.makedirs(output_dir, exist_ok=True)

    def _limit(rows):
        return rows[:sample] if sample > 0 else rows

    _write_csv(
        os.path.join(output_dir, "missing_in_supabase.csv"),
        _limit(results.get("missing_in_supabase", [])),
    )
    _write_csv(
        os.path.join(output_dir, "missing_in_sqlite.csv"),
        _limit(results.get("missing_in_sqlite", [])),
    )
    _write_csv(
        os.path.join(output_dir, "value_mismatch.csv"),
        _limit(results.get("value_mismatches", [])),
        ["ticker", "period", "quarter", "comparison_key", "column_name",
         "sqlite_value_raw", "sqlite_value_normalized",
         "supabase_value_raw", "supabase_value_normalized",
         "difference_type"],
    )
    _write_csv(
        os.path.join(output_dir, "null_mismatch.csv"),
        _limit(results.get("null_mismatch_rows", [])),
    )
    _write_csv(
        os.path.join(output_dir, "source_mismatch.csv"),
        _limit(results.get("source_mismatches", [])),
    )
    _write_csv(
        os.path.join(output_dir, "updated_at_mismatch.csv"),
        _limit(results.get("updated_at_mismatches", [])),
    )
    _write_csv(
        os.path.join(output_dir, "duplicate_in_sqlite.csv"),
        _limit(results.get("duplicate_in_sqlite", [])),
    )
    _write_csv(
        os.path.join(output_dir, "duplicate_in_supabase.csv"),
        _limit(results.get("duplicate_in_supabase", [])),
    )

    # Markdown
    md_path = os.path.join(output_dir, "audit_summary.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(results["markdown_report"])
    logger.info(f"[OUTPUT] {output_dir} に 9 ファイル出力完了")


# ============================================================
# メインオーケストレーション
# ============================================================
def run_audit(
    sqlite_path: str,
    supabase_url: str,
    supabase_key: str,
    table: str = "financials",
    ticker: str | None = None,
    tickers: list[str] | None = None,
    limit: int | None = None,
    ignore_updated_at: bool = False,
    sample: int = 0,
    output_dir: str = _DEFAULT_OUTPUT,
) -> dict:
    api = _SupabaseAPI(supabase_url, supabase_key)

    # 1. データ取得
    sqlite_comp = load_sqlite_rows_for_comparison(sqlite_path, ticker, tickers, limit)
    sqlite_raw = load_sqlite_rows_raw(sqlite_path, ticker, tickers, limit)
    supa_rows = load_supabase_rows(api, ticker, tickers, table)

    # 2. 正規化
    # SQLite 側: 円単位 → 百万円に変換 (scale_to_millions=True)
    # Supabase 側: 既に百万円のためそのまま
    s_normalized = [normalize_row(r, scale_to_millions=True) for r in sqlite_comp]
    p_normalized = [normalize_row(r, scale_to_millions=False) for r in supa_rows]

    # 列存在サマリ (正規化前の生列)
    col_presence = summarize_column_presence(
        sqlite_comp[:1] if sqlite_comp else [],
        supa_rows[:1] if supa_rows else [],
    )

    # source 列の有無を確認
    sqlite_has_source = any("source" in r for r in sqlite_comp[:1]) if sqlite_comp else False
    source_skipped = not sqlite_has_source
    if source_skipped:
        logger.info("[AUDIT] SQLite 側に source 列がないため source 比較をスキップします")

    # 3. Map 構築
    s_map: dict[tuple, dict] = {}
    for row in s_normalized:
        k = (row["ticker"], row["period"], row["quarter"])
        s_map[k] = row
    p_map: dict[tuple, dict] = {}
    for row in p_normalized:
        k = (row["ticker"], row["period"], row["quarter"])
        p_map[k] = row

    # 4. 比較
    missing_supa, missing_sqlite = detect_missing_rows(s_map, p_map)
    dup_sqlite = detect_duplicate_rows(sqlite_raw, "sqlite")
    dup_supabase = detect_duplicate_rows(supa_rows, "supabase")
    val_mm, col_mm, perfect = compare_value_columns(s_map, p_map, VALUE_COLUMNS)
    null_summary, null_rows = compare_nulls(s_map, p_map, VALUE_COLUMNS)

    # source 比較: SQLite に source 列がない場合はスキップ
    if source_skipped:
        src_mm, s_dist, p_dist = [], {}, {}
    else:
        src_mm, s_dist, p_dist = compare_source(s_map, p_map)

    ua_mm, ua_stats = ([], {"equal": 0, "sqlite_newer": 0, "supabase_newer": 0, "one_side_null": 0})
    if not ignore_updated_at:
        ua_mm, ua_stats = compare_updated_at(s_map, p_map)

    all_keys = len(set(s_map.keys()) | set(p_map.keys()))
    common_keys = len(set(s_map.keys()) & set(p_map.keys()))

    ticker_cond = "全件"
    if ticker:
        ticker_cond = ticker
    elif tickers:
        ticker_cond = f"{len(tickers)} tickers"

    results = {
        "sqlite_db": sqlite_path,
        "ticker_condition": ticker_cond,
        "sqlite_count": len(s_normalized),
        "supabase_count": len(p_normalized),
        "all_unique_keys": all_keys,
        "common_keys": common_keys,
        "missing_in_supabase": missing_supa,
        "missing_in_supabase_count": len(missing_supa),
        "missing_in_sqlite": missing_sqlite,
        "missing_in_sqlite_count": len(missing_sqlite),
        "value_mismatches": val_mm,
        "value_mismatch_count": len(set(m["comparison_key"] for m in val_mm)),
        "col_mismatch_counts": col_mm,
        "perfect_match_count": perfect,
        "null_summary": null_summary,
        "null_mismatch_rows": null_rows,
        "source_mismatches": src_mm,
        "source_mismatch_count": len(src_mm),
        "source_skipped": source_skipped,
        "sqlite_source_dist": s_dist,
        "supabase_source_dist": p_dist,
        "updated_at_mismatches": ua_mm,
        "updated_at_mismatch_count": len(ua_mm),
        "updated_at_stats": ua_stats,
        "duplicate_in_sqlite": dup_sqlite,
        "duplicate_in_sqlite_count": len(dup_sqlite),
        "duplicate_in_supabase": dup_supabase,
        "duplicate_in_supabase_count": len(dup_supabase),
        "column_presence": col_presence,
        "top_mismatch_tickers": summarize_top_mismatches(val_mm, "ticker"),
        "top_mismatch_periods": summarize_top_mismatches(val_mm, "period"),
    }
    results["markdown_report"] = generate_markdown_report(results)
    write_csv_outputs(results, output_dir, sample)
    return results


def check_strict(results: dict) -> bool:
    """strict 判定: 主要差分があれば True (= 失敗)"""
    if results["missing_in_supabase_count"] > 0:
        return True
    if results["missing_in_sqlite_count"] > 0:
        return True
    if results["value_mismatch_count"] > 0:
        return True
    # source_skipped の場合は source_mismatch を無視
    if not results.get("source_skipped") and results["source_mismatch_count"] > 0:
        return True
    if results["duplicate_in_sqlite_count"] > 0:
        return True
    if results["duplicate_in_supabase_count"] > 0:
        return True
    return False


# ============================================================
# CLI
# ============================================================
def main():
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
        description="SQLite ↔ Supabase financials 整合性監査",
    )
    parser.add_argument("--sqlite", default=_DEFAULT_SQLITE, help="SQLite DB パス")
    parser.add_argument("--table", default="financials", help="Supabase テーブル名")
    parser.add_argument("--ticker", default=None, help="単一 ticker 指定")
    parser.add_argument("--tickers-file", default=None, help="ticker 一覧ファイル")
    parser.add_argument("--limit", type=int, default=None, help="SQLite 取得件数制限")
    parser.add_argument("--output-dir", default=_DEFAULT_OUTPUT, help="出力先ディレクトリ")
    parser.add_argument("--strict", action="store_true", help="差分ありで exit 1")
    parser.add_argument("--ignore-updated-at", action="store_true", help="updated_at 集計省略")
    parser.add_argument("--sample", type=int, default=0, help="CSV出力サンプル件数 (0=全件)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # .env
    _load_dotenv()
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_ANON_KEY", "")

    if not supabase_url or not supabase_key:
        logger.error(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY(or ANON_KEY) が未設定です。\n"
            "  .env ファイルに設定してください。"
        )
        sys.exit(1)

    # ticker 解決
    tickers = None
    if args.tickers_file:
        p = Path(args.tickers_file)
        if not p.exists():
            logger.error(f"tickers-file が見つかりません: {args.tickers_file}")
            sys.exit(1)
        tickers = [line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]

    # sqlite パス解決
    sqlite_path = args.sqlite
    if not os.path.isabs(sqlite_path):
        sqlite_path = os.path.join(_PROJECT_ROOT, sqlite_path)

    print()
    print("=" * 60)
    print("  SQLite ↔ Supabase financials 整合性監査")
    print("=" * 60)
    print(f"  SQLite:     {sqlite_path}")
    print(f"  Table:      {args.table}")
    print(f"  Ticker:     {args.ticker or (f'{len(tickers)} tickers' if tickers else '全件')}")
    print(f"  Output:     {args.output_dir}")
    print(f"  Strict:     {args.strict}")
    print("=" * 60)
    print()

    try:
        results = run_audit(
            sqlite_path=sqlite_path,
            supabase_url=supabase_url,
            supabase_key=supabase_key,
            table=args.table,
            ticker=args.ticker,
            tickers=tickers,
            limit=args.limit,
            ignore_updated_at=args.ignore_updated_at,
            sample=args.sample,
            output_dir=args.output_dir,
        )

        print()
        print("=" * 60)
        print("  ✅ 監査完了")
        print("=" * 60)
        print(f"  SQLite:              {results['sqlite_count']:,} rows")
        print(f"  Supabase:            {results['supabase_count']:,} rows")
        print(f"  missing_in_supabase: {results['missing_in_supabase_count']:,}")
        print(f"  missing_in_sqlite:   {results['missing_in_sqlite_count']:,}")
        print(f"  value_mismatch:      {results['value_mismatch_count']:,}")
        print(f"  source_mismatch:     {results['source_mismatch_count']:,}")
        print(f"  updated_at_mismatch: {results['updated_at_mismatch_count']:,}")
        print(f"  duplicate_sqlite:    {results['duplicate_in_sqlite_count']:,}")
        print(f"  duplicate_supabase:  {results['duplicate_in_supabase_count']:,}")
        print(f"  出力先: {args.output_dir}")
        print("=" * 60)
        print()

        if args.strict and check_strict(results):
            logger.error("--strict: 主要差分が検出されました。exit 1")
            sys.exit(1)

        sys.exit(0)

    except FileNotFoundError as e:
        logger.error(f"ファイルエラー: {e}")
        sys.exit(1)
    except requests.HTTPError as e:
        body = e.response.text[:300] if e.response else ""
        logger.error(f"Supabase API エラー: HTTP {e.response.status_code if e.response else '?'}\n  {body}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"実行失敗: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
