#!/usr/bin/env python3
r"""
sync_segments.py -- XBRL ZIP + SQLite(excel_legacy) -> segment_canonical sync

Standard operation syncs BOTH sources. Use --xbrl-only for XBRL-only mode.

Usage:
  .\.venv\Scripts\python.exe tools\sync_segments.py --dry-run
  .\.venv\Scripts\python.exe tools\sync_segments.py --apply
  .\.venv\Scripts\python.exe tools\sync_segments.py --apply --xbrl-only
"""
from __future__ import annotations

import argparse
import glob
import logging
import os
import re
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone, timedelta

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip
from src.segment.normalize import (
    classify_special_row,
    normalize_segment_key,
    resolve_segment_key_with_jp,
    is_english_dominant,
)

logger = logging.getLogger("sync_seg")
JST = timezone(timedelta(hours=9))


# ============================================================
# 過去EDINET日本語候補取得
# ============================================================
def get_jp_segment_names(
    rest_url: str,
    headers: dict,
    ticker: str,
    *,
    target_period: str | None = None,
    max_periods: int = 5,
    limit: int = 200,
) -> list[str]:
    """canonical_segments から同一 ticker の過去EDINET日本語セグメント名を取得する。

    設計方针:
      - period 完全一致は必須にしない。
        最新TDNET FYに対する同期 EDINETは未存在のため。
      - target_period があれば period <= target_period の過去候補を取得。
      - source = 'edinet_xbrl' を優先、なければ全 source から取得。
      - period desc で新しい順、最大 max_periods 期にわたる重複排除済み日本語名を返す。
      - ASCII比率 > 0.5 の行（英語優勢）は除外。

    Args:
        rest_url: Supabase REST URL
        headers: 認証ヘッダー
        ticker: 4桁銘柄コード
        target_period: TDNET同期対象 YYYY-MM-DD。None の場合は制限なし。
        max_periods: 参照する最大期数（デフォルト 5）
        limit: Supabase リクエスト最大件数（デフォルト 200）

    Returns:
        日本語セグメント名のリスト（重複排除済み）
    """
    import requests as _req
    try:
        # source='edinet_xbrl' 先行、なければ全 source
        for source_filter in ["edinet_xbrl", None]:
            params: dict = {
                "ticker": f"eq.{ticker}",
                "select": "segment_name,period,quarter,source",
                "order": "period.desc,quarter.desc",
                "limit": str(limit),
            }
            if target_period:
                params["period"] = f"lte.{target_period}"
            if source_filter:
                params["source"] = f"eq.{source_filter}"

            r = _req.get(
                f"{rest_url}/canonical_segments",
                headers=headers,
                params=params,
                timeout=15,
            )
            rows = r.json() if r.status_code == 200 else []

            # 日本語優勢（ASCII比率 <= 0.5）のみ重複排除して収集
            seen_names: set[str] = set()
            period_count: dict[str, int] = {}
            result_names: list[str] = []
            for row in rows:
                seg = (row.get("segment_name") or "").strip()
                period_val = row.get("period") or ""
                if not seg or seg in seen_names:
                    continue
                alpha = sum(1 for c in seg if c.isascii() and c.isalpha())
                if alpha / max(len(seg), 1) > 0.5:
                    continue  # 英語優勢は除外
                seen_names.add(seg)
                cnt = period_count.get(period_val, 0)
                if cnt == 0 and len(period_count) >= max_periods:
                    continue  # max_periods 期履歴を超えたら打ち切り
                period_count[period_val] = cnt + 1
                result_names.append(seg)

            if not result_names:
                continue  # edinet_xbrl なし → 全 source で再試行

            logger.debug(
                f"[sync_seg] jp_candidates ticker={ticker}"
                f" target_period={target_period} source={source_filter or 'any'}"
                f" found={len(result_names)} names={result_names[:4]}"
            )
            return result_names

        return []

    except Exception as e:
        logger.debug(f"[sync_seg] get_jp_segment_names error ticker={ticker}: {e}")
        return []


# ============================================================
# canonical 条件 (conservative)
# ============================================================
_VALID_QUARTERS = {"1Q", "2Q", "3Q", "FY"}


def _is_canonical_candidate(row) -> tuple[bool, str]:
    """canonical に採用できるかチェック。

    Returns:
        (ok, reason)
    """
    if row.quarter not in _VALID_QUARTERS:
        return False, f"invalid quarter: {row.quarter}"
    if row.special_row_type != "ordinary_segment":
        return False, f"special_row_type: {row.special_row_type}"
    if not row.normalized_segment_name:
        return False, "no normalized_segment_name"
    if row.sales is None and row.profit is None:
        return False, "no sales or profit"
    return True, ""


# ============================================================
# Supabase upsert
# ============================================================
def _upsert_segment_raw(row, rest_url: str, headers: dict, dry_run: bool) -> str:
    """segment_raw に insert。"""
    import requests
    # デプロイ仕様 DDL に準拠したカラムのみ
    payload = {
        "source": row.source,
        "source_doc_type": row.source_doc_type,
        "raw_ticker": row.raw_ticker,
        "normalized_ticker": row.normalized_ticker,
        "period": row.period,
        "quarter": row.quarter,
        "raw_segment_name": row.raw_segment_name,
        "normalized_segment_name": row.normalized_segment_name,
        "special_row_type": row.special_row_type,
        "sales": row.sales,
        "profit": row.profit,
        "confidence_score": float(row.confidence_score),
        "extraction_method": row.extraction_method,
        "is_consolidated": row.is_consolidated,
        "accounting_standard": row.accounting_standard,
    }
    if dry_run:
        return "dry_run"
    
    r = requests.post(
        f"{rest_url}/segment_raw",
        json=payload,
        headers={**headers, "Prefer": "return=minimal"},
        timeout=30,
    )
    if r.status_code in (200, 201):
        return "upserted"
    else:
        logger.warning(f"[RAW] insert failed: {r.status_code} {r.text[:200]}")
        return "error"


def _upsert_segment_canonical(
    row,
    rest_url: str,
    headers: dict,
    dry_run: bool,
    *,
    jp_segment_names: list[str] | None = None,
    match_stats: dict | None = None,
) -> str:
    """segment_canonical に upsert (PK: ticker, period, quarter, segment_name)。

    segment_key 決定ロジック:
      - 英語値加（ASCII比率 > 0.5）かつ jp_segment_names があれば過去EDINETマッチングを試みる。
        score >= 2 → マッチ日本語の segment_key
        score < 2  → normalize_segment_key(en_name) でフォールバック
      - 日本語値加または候補ゼロ: normalize_segment_key(segment_name)
    """
    import requests
    seg_name = row.normalized_segment_name or ""

    # segment_key 決定
    matched_jp = ""
    best_score = 0
    if jp_segment_names is not None and is_english_dominant(seg_name):
        if jp_segment_names:
            # EDINET 候補あり: マッチング実行
            if match_stats is not None:
                match_stats["jp_candidate_hit"] = match_stats.get("jp_candidate_hit", 0) + 1

            seg_key, matched_jp, best_score = resolve_segment_key_with_jp(
                seg_name, jp_segment_names
            )

            if matched_jp:
                # score >= 2 マッチ成功
                if match_stats is not None:
                    match_stats["jp_match_success"] = match_stats.get("jp_match_success", 0) + 1
                logger.debug(
                    f"[sync_seg] jp_match ticker={row.normalized_ticker}"
                    f" period={row.period} en={seg_name!r}"
                    f" jp={matched_jp!r} score={best_score} key={seg_key!r}"
                )
            else:
                # score < 2 または候補にマッチなし
                if best_score > 0:
                    if match_stats is not None:
                        match_stats["jp_match_low_score"] = match_stats.get("jp_match_low_score", 0) + 1
                    logger.debug(
                        f"[sync_seg] jp_low_score ticker={row.normalized_ticker}"
                        f" period={row.period} en={seg_name!r}"
                        f" best_score={best_score} fallback_key={seg_key!r}"
                    )
                else:
                    if match_stats is not None:
                        match_stats["jp_match_fallback"] = match_stats.get("jp_match_fallback", 0) + 1
                    logger.debug(
                        f"[sync_seg] jp_no_match ticker={row.normalized_ticker}"
                        f" period={row.period} en={seg_name!r} key={seg_key!r}"
                    )
        else:
            # EDINET 候補ゼロ: フォールバック
            if match_stats is not None:
                match_stats["jp_candidate_empty"] = match_stats.get("jp_candidate_empty", 0) + 1
                match_stats["jp_match_fallback"] = match_stats.get("jp_match_fallback", 0) + 1
            seg_key = normalize_segment_key(seg_name)
            logger.debug(
                f"[sync_seg] jp_no_candidates ticker={row.normalized_ticker}"
                f" period={row.period} en={seg_name!r} fallback_key={seg_key!r}"
            )
    else:
        # 日本語セグメント: 直接 normalize
        seg_key = normalize_segment_key(seg_name)

    # デプロイ仕様 DDL に準拠したカラムのみ
    payload = {
        "ticker": row.normalized_ticker,
        "period": row.period,
        "quarter": row.quarter,
        "segment_name": seg_name,
        "segment_key": seg_key,
        "sales": row.sales,
        "profit": row.profit,
        "source": row.source,
        "confidence_score": float(row.confidence_score),
        "updated_at": datetime.now(JST).isoformat(),
    }
    if dry_run:
        return "dry_run"

    r = requests.post(
        f"{rest_url}/segment_canonical",
        json=payload,
        headers={**headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
        timeout=30,
    )
    if r.status_code in (200, 201):
        return "upserted"
    else:
        logger.warning(f"[CANONICAL] upsert failed: {r.status_code} {r.text[:200]}")
        return "error"


# ============================================================
# SQLite segment_financials -> segment_canonical sync
# ============================================================

# segment_name としてスキップする値（ヘッダー行や無効行）
_SKIP_SEGMENT_NAMES = {
    "売上", "利益", "月次売上", "累計", "0", "#VALUE!", "",
}

_QUARTER_MAP = {"4Q": "FY"}


def _classify_skip_reason(row: dict) -> str:
    """スキップ理由を分類して返す。valid なら空文字列。"""
    name = (row.get("segment_name") or "").strip()
    if not name:
        return "empty_name"
    if name in _SKIP_SEGMENT_NAMES:
        return "header"
    if name.startswith("UNKNOWN_"):
        return "unknown"
    sales = row.get("segment_sales") or 0
    profit = row.get("segment_profit") or 0
    if sales == 0 and profit == 0:
        return "zero_value"
    quarter = (row.get("quarter") or "")
    if quarter == "?Q":
        return "invalid_quarter"
    # ratio check
    if sales is not None and abs(sales) > 0 and abs(sales) < 1:
        return "ratio"
    if profit is not None and abs(profit) > 0 and abs(profit) < 1:
        return "ratio"
    return ""


def _is_valid_sqlite_segment(row: dict) -> bool:
    """SQLite segment_financials 行が canonical 対象か判定。"""
    return _classify_skip_reason(row) == ""


def count_sqlite_valid_rows(db_path: str) -> int:
    """SQLite に有効セグメント行が何件あるか返す (guard 用)。"""
    if not os.path.isfile(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT segment_name, segment_sales, segment_profit, quarter "
        "FROM segment_financials"
    ).fetchall()
    conn.close()
    return sum(1 for r in rows if _is_valid_sqlite_segment(dict(r)))


def sync_sqlite_segments(
    db_path: str, rest_url: str, headers: dict, dry_run: bool,
) -> dict:
    """SQLite segment_financials (excel_legacy) -> segment_canonical に push."""
    stats = {
        "sqlite_total": 0,
        "sqlite_valid": 0,
        "sqlite_upserted": 0,
        "sqlite_errors": 0,
        "sqlite_skip_header": 0,
        "sqlite_skip_zero": 0,
        "sqlite_skip_unknown": 0,
        "sqlite_skip_quarter": 0,
        "sqlite_skip_ratio": 0,
        "sqlite_skip_empty": 0,
    }

    if not os.path.isfile(db_path):
        logger.warning(f"[SQLITE] DB not found: {db_path}")
        return stats

    import requests
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT company_code, fiscal_year_end, quarter, segment_name, "
        "segment_sales, segment_profit, data_source "
        "FROM segment_financials"
    ).fetchall()

    stats["sqlite_total"] = len(rows)
    logger.info(f"[SQLITE] segment_financials: {len(rows):,} rows")

    for row in rows:
        rdict = dict(row)
        reason = _classify_skip_reason(rdict)
        if reason:
            skip_key = f"sqlite_skip_{reason}"
            if skip_key in stats:
                stats[skip_key] += 1
            continue

        stats["sqlite_valid"] += 1
        quarter = _QUARTER_MAP.get(rdict["quarter"], rdict["quarter"])
        seg_name = rdict["segment_name"].strip()

        # SQLite REAL -> Supabase bigint: int() で変換
        raw_sales = rdict["segment_sales"]
        raw_profit = rdict["segment_profit"]
        sales = int(raw_sales) if raw_sales is not None else None
        profit = int(raw_profit) if raw_profit is not None else None

        payload = {
            "ticker": rdict["company_code"],
            "period": rdict["fiscal_year_end"],
            "quarter": quarter,
            "segment_name": seg_name,
            "sales": sales,
            "profit": profit,
            "source": rdict.get("data_source") or "excel_legacy",
            "updated_at": datetime.now(JST).isoformat(),
        }
        if dry_run:
            stats["sqlite_upserted"] += 1
            continue

        r = requests.post(
            f"{rest_url}/segment_canonical",
            json=payload,
            headers={**headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
            timeout=30,
        )
        if r.status_code in (200, 201):
            stats["sqlite_upserted"] += 1
        else:
            stats["sqlite_errors"] += 1
            logger.warning(f"[SQLITE] upsert failed: {r.status_code} {r.text[:200]}")

    conn.close()

    # ── Phase 2-A: canonical dual-write (best-effort) ──
    if not dry_run and stats["sqlite_valid"] > 0:
        try:
            from lib.pipeline.canonical_writer import write_segments_canonical
            from lib.pipeline.db import load_env, get_supabase_write_config
            load_env()
            canonical_config = get_supabase_write_config()
            if canonical_config:
                # per-ticker batch に再構成
                ticker_batches: dict[tuple, list[dict]] = {}
                conn2 = sqlite3.connect(db_path)
                conn2.row_factory = sqlite3.Row
                for row in conn2.execute(
                    "SELECT company_code, fiscal_year_end, quarter, segment_name, "
                    "segment_sales, segment_profit, data_source "
                    "FROM segment_financials"
                ).fetchall():
                    rdict = dict(row)
                    if _classify_skip_reason(rdict):
                        continue
                    quarter = _QUARTER_MAP.get(rdict["quarter"], rdict["quarter"])
                    key = (rdict["company_code"], rdict["fiscal_year_end"], quarter)
                    if key not in ticker_batches:
                        ticker_batches[key] = []
                    raw_sales = rdict["segment_sales"]
                    raw_profit = rdict["segment_profit"]
                    ticker_batches[key].append({
                        "segment_name": rdict["segment_name"].strip(),
                        "sales": int(raw_sales) if raw_sales is not None else None,
                        "profit": int(raw_profit) if raw_profit is not None else None,
                    })
                conn2.close()

                canonical_total = 0
                canonical_errors = 0
                for (ticker, period, quarter), segs in ticker_batches.items():
                    cw_result = write_segments_canonical(
                        ticker=ticker,
                        period=period,
                        quarter=quarter,
                        segments=segs,
                        source="excel_legacy",
                        config=canonical_config,
                    )
                    canonical_total += cw_result["written"]
                    canonical_errors += cw_result["errors"]
                logger.info(
                    f"[CANONICAL] segments dual-write: "
                    f"written={canonical_total} errors={canonical_errors}"
                )
            else:
                logger.warning(
                    "[CANONICAL] segments dual-write skipped: no write config"
                )
        except Exception as _cw_err:
            logger.warning(
                f"[CANONICAL] segments dual-write failed "
                f"(best-effort, legacy unaffected): {_cw_err}"
            )

    return stats


# ============================================================
# メイン処理
# ============================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="segment sync: XBRL + SQLite(excel_legacy) -> Supabase segment_canonical",
    )
    parser.add_argument("--apply", action="store_true", help="Supabase に書き込み")
    parser.add_argument("--dry-run", action="store_true", help="書き込みなし")
    parser.add_argument("--source-dir", default="data/docs", help="XBRL ZIP ディレクトリ")
    parser.add_argument("--xbrl-only", action="store_true",
                        help="XBRL のみ sync (SQLite excel_legacy を除外)")
    # 後方互換: --include-sqlite は受け付けるが無視 (デフォルトで含まれるため)
    parser.add_argument("--include-sqlite", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--db", default="decision_db.db",
                        help="SQLite DB パス")
    return parser


def main(args: list[str] | None = None) -> int:
    parser = build_parser()
    opts = parser.parse_args(args)
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    
    dry_run = not opts.apply or opts.dry_run
    mode = "DRY-RUN" if dry_run else "APPLY"
    include_sqlite = not opts.xbrl_only
    sync_mode = "XBRL + SQLite" if include_sqlite else "XBRL ONLY"
    
    # Supabase 接続
    env_path = os.path.join(_PROJECT_ROOT, ".env")
    if os.path.exists(env_path):
        for line in open(env_path, "r", encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_ANON_KEY", "")
    rest_url = f"{supabase_url}/rest/v1"
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
    }
    
    # テーブル存在チェック (apply モードのみ)
    if not dry_run:
        import requests as _req
        for tbl in ["segment_raw", "segment_canonical"]:
            r = _req.get(
                f"{rest_url}/{tbl}?select=*&limit=0",
                headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
                timeout=15,
            )
            if r.status_code != 200:
                logger.error(
                    f"[SYNC] テーブル '{tbl}' が存在しません。\n"
                    f"  Supabase SQL Editor で docs/segment_ddl.sql を実行してください。"
                )
                return 1
        logger.info("[SYNC] テーブル存在確認 OK")

    # ============================
    # Guard: --xbrl-only 時に SQLite valid rows があれば警告
    # ============================
    db_path = os.path.join(_PROJECT_ROOT, opts.db)
    if opts.xbrl_only:
        valid_count = count_sqlite_valid_rows(db_path)
        if valid_count > 0:
            msg = (
                f"[GUARD] SQLite に有効セグメント行が {valid_count:,} 件ありますが "
                f"--xbrl-only のため未同期です。\n"
                f"  excel_legacy 由来セグメントが COMPANYVIEW に表示されません。\n"
                f"  通常運用では --xbrl-only を外して実行してください。"
            )
            logger.warning(msg)
            if not dry_run:
                print()
                print("!" * 60)
                print(f"  WARNING: {valid_count:,} excel_legacy rows NOT synced")
                print("  Remove --xbrl-only for standard operation")
                print("!" * 60)
                print()

    logger.info(f"[SYNC] mode={mode} sync={sync_mode}")
    
    # ZIP 収集
    source_dir = os.path.join(_PROJECT_ROOT, opts.source_dir)
    zip_files = sorted(glob.glob(os.path.join(source_dir, "*.zip")))
    
    logger.info(f"[SYNC] source={source_dir} zips={len(zip_files)}")
    
    stats = {
        "sync_mode": sync_mode,
        "zips_total": len(zip_files),
        "zips_with_segments": 0,
        "xbrl_raw_total": 0,
        "xbrl_raw_upserted": 0,
        "xbrl_canonical_total": 0,
        "xbrl_canonical_upserted": 0,
        "xbrl_skipped_special": 0,
        "xbrl_skipped_no_data": 0,
        "xbrl_skipped_quarter": 0,
        "xbrl_errors": 0,
        # Phase 2-A: xbrl → canonical_segments (EAV) dual-write
        "xbrl_cs_batches": 0,
        "xbrl_cs_rows": 0,
        "xbrl_cs_written": 0,
        "xbrl_cs_errors": 0,
    }
    # JPマッチング統計
    match_stats: dict = {}
    # ticker 単位の日本語候補キャッシュ {ticker: list[str]}
    jp_cache: dict[str, list[str]] = {}

    # canonical 通過済み xbrl 行を蓄積 (dual-write 用)
    _xbrl_canonical_rows: list = []
    
    for zpath in zip_files:
        basename = os.path.basename(zpath)
        
        rows = extract_segments_from_xbrl_zip(zpath)
        if not rows:
            continue
        
        stats["zips_with_segments"] += 1
        ticker = rows[0].normalized_ticker if rows else "?"
        period = max((r.period for r in rows), default="") if rows else ""  # prior rows が先頭でも current period を使う

        # JP 候補取得（ticker 単位キャッシュ）
        # target_period 以前の過去EDINET候補を利用。period完全一致は要求しない。
        if ticker not in jp_cache:
            jp_names_fetched = get_jp_segment_names(
                rest_url, headers, ticker, target_period=period
            )
            jp_cache[ticker] = jp_names_fetched
            if jp_names_fetched:
                logger.debug(
                    f"[sync_seg] jp_cache ticker={ticker} target_period={period}"
                    f" candidates={jp_names_fetched[:4]}"
                )
            else:
                logger.debug(
                    f"[sync_seg] jp_cache_empty ticker={ticker} target_period={period}"
                )
        jp_names = jp_cache[ticker]

        for row in rows:
            stats["xbrl_raw_total"] += 1

            # raw upsert
            result = _upsert_segment_raw(row, rest_url, headers, dry_run)
            if result == "upserted":
                stats["xbrl_raw_upserted"] += 1
            elif result == "error":
                stats["xbrl_errors"] += 1

            # canonical 判定
            ok, reason = _is_canonical_candidate(row)
            if not ok:
                if "quarter" in reason:
                    stats["xbrl_skipped_quarter"] += 1
                elif "special" in reason:
                    stats["xbrl_skipped_special"] += 1
                else:
                    stats["xbrl_skipped_no_data"] += 1
                continue

            stats["xbrl_canonical_total"] += 1
            result = _upsert_segment_canonical(
                row, rest_url, headers, dry_run,
                jp_segment_names=jp_names,
                match_stats=match_stats,
            )
            if result == "upserted":
                stats["xbrl_canonical_upserted"] += 1
            elif result == "error":
                stats["xbrl_errors"] += 1

            # dual-write 用に蓄積
            _xbrl_canonical_rows.append(row)
        
        seg_names = [r.normalized_segment_name or r.raw_segment_name for r in rows 
                     if r.special_row_type == "ordinary_segment"]
        logger.info(f"  [{mode}] {ticker} {basename[:20]}: {len(rows)} raw, segments: {seg_names}")

    # ── Phase 2-A: xbrl → canonical_segments (EAV) dual-write ──
    if not dry_run and _xbrl_canonical_rows:
        try:
            from lib.pipeline.canonical_writer import write_segments_canonical
            from lib.pipeline.db import load_env, get_supabase_write_config
            load_env()
            canonical_config = get_supabase_write_config()
            if canonical_config:
                # ticker/period/quarter ごとにバッチ集約
                xbrl_batches: dict[tuple, list[dict]] = {}
                for row in _xbrl_canonical_rows:
                    key = (row.normalized_ticker, row.period, row.quarter)
                    if key not in xbrl_batches:
                        xbrl_batches[key] = []
                    xbrl_batches[key].append({
                        "segment_name": row.normalized_segment_name,
                        "sales": row.sales,
                        "profit": row.profit,
                    })

                stats["xbrl_cs_batches"] = len(xbrl_batches)
                stats["xbrl_cs_rows"] = len(_xbrl_canonical_rows)

                for (t, p, q), segs in xbrl_batches.items():
                    cw_result = write_segments_canonical(
                        ticker=t, period=p, quarter=q,
                        segments=segs, source="xbrl", config=canonical_config,
                    )
                    stats["xbrl_cs_written"] += cw_result["written"]
                    stats["xbrl_cs_errors"] += cw_result["errors"]

                logger.info(
                    f"[CANONICAL] xbrl dual-write: "
                    f"batches={stats['xbrl_cs_batches']} "
                    f"rows={stats['xbrl_cs_rows']} "
                    f"written={stats['xbrl_cs_written']} "
                    f"errors={stats['xbrl_cs_errors']}"
                )
            else:
                logger.warning("[CANONICAL] xbrl dual-write skipped: no write config")
        except Exception as _cw_err:
            logger.warning(
                f"[CANONICAL] xbrl dual-write failed (best-effort): {_cw_err}"
            )
    
    # SQLite 連携 (デフォルトで有効)
    if include_sqlite:
        logger.info(f"[SYNC] SQLite sync: {db_path}")
        sq_stats = sync_sqlite_segments(db_path, rest_url, headers, dry_run)
        stats.update(sq_stats)

    # サマリ
    print()
    print("=" * 60)
    print(f"  Segment Sync - {mode} ({sync_mode})")
    print("=" * 60)

    # XBRL
    print("  [XBRL]")
    print(f"    zips_total               : {stats['zips_total']}")
    print(f"    zips_with_segments       : {stats['zips_with_segments']}")
    print(f"    raw_upserted             : {stats['xbrl_raw_upserted']}")
    print(f"    canonical_upserted       : {stats['xbrl_canonical_upserted']}")
    if stats["xbrl_cs_batches"] > 0:
        print(f"    cs_dual_write_batches    : {stats['xbrl_cs_batches']}")
        print(f"    cs_dual_write_written    : {stats['xbrl_cs_written']}")
        if stats["xbrl_cs_errors"]:
            print(f"    cs_dual_write_errors     : {stats['xbrl_cs_errors']}")
    if stats["xbrl_errors"]:
        print(f"    errors                   : {stats['xbrl_errors']}")

    # SQLite
    if include_sqlite:
        print("  [SQLite excel_legacy]")
        print(f"    total_rows               : {stats.get('sqlite_total', 0)}")
        print(f"    valid                    : {stats.get('sqlite_valid', 0)}")
        print(f"    upserted                 : {stats.get('sqlite_upserted', 0)}")
        print(f"    errors                   : {stats.get('sqlite_errors', 0)}")
        print(f"    skip_header              : {stats.get('sqlite_skip_header', 0)}")
        print(f"    skip_zero                : {stats.get('sqlite_skip_zero', 0)}")
        print(f"    skip_unknown             : {stats.get('sqlite_skip_unknown', 0)}")
        print(f"    skip_quarter             : {stats.get('sqlite_skip_quarter', 0)}")
        print(f"    skip_ratio               : {stats.get('sqlite_skip_ratio', 0)}")
        print(f"    skip_empty               : {stats.get('sqlite_skip_empty', 0)}")
    else:
        print("  [SQLite excel_legacy]")
        print("    ** NOT SYNCED (--xbrl-only) **")

    # JP マッチング統計
    if match_stats:
        print("  [JP Segment Matching]")
        hit     = match_stats.get("jp_candidate_hit", 0)
        empty   = match_stats.get("jp_candidate_empty", 0)
        success = match_stats.get("jp_match_success", 0)
        low     = match_stats.get("jp_match_low_score", 0)
        fall    = match_stats.get("jp_match_fallback", 0)
        total_en = success + low + fall
        print(f"    jp_candidate_hit         : {hit}")
        print(f"    jp_candidate_empty       : {empty}")
        print(f"    jp_match_success         : {success}")
        print(f"    jp_match_low_score       : {low}")
        print(f"    jp_match_fallback        : {fall}")
        if total_en > 0:
            jp_match_rate = success / total_en * 100
            print(f"    jp_match_rate            : {jp_match_rate:.1f}%")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
