#!/usr/bin/env python3
r"""
fix_ticker_normalization.py -- canonical_financials の5桁ticker補正（最適化版）

方式: INSERT (正規化後) + DELETE (旧5桁) — source_row_key にtickerが含まれるため UPDATE は危険

最適化:
  - PostgREST like フィルタで5文字ticker候補のみ取得（全件スキャン廃止）
  - 衝突判定は source_row_key ベースでバッチ化
  - apply は insert→delete 順序保証のバッチ処理

Usage:
  cd C:\Users\takuy\OneDrive\tdnet-excel-input
  .\.venv\Scripts\python.exe tools/fix_ticker_normalization.py --dry-run
  .\.venv\Scripts\python.exe tools/fix_ticker_normalization.py --apply
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta

_ROOT = r"C:\Users\takuy\OneDrive\tdnet-excel-input"
sys.path.insert(0, _ROOT)
from lib.pipeline.db import (
    load_env,
    get_supabase_config,
    get_supabase_write_config,
    supabase_upsert,
    supabase_select,
)
from src.common_ticker import normalize_ticker, is_valid_ticker

load_env()
import requests

logger = logging.getLogger("fix_ticker_norm")
JST = timezone(timedelta(hours=9))

OUT_DIR = r"C:\Users\takuy\.gemini\antigravity\scratch"

# ============================================================
# 定数
# ============================================================
BATCH_SIZE = 50          # Supabase REST API 1リクエストあたりのバッチサイズ
SCAN_PAGE_SIZE = 5000    # keyset pagination の 1ページサイズ (distinct収集は2列で軽量)
COLLISION_BATCH = 200    # 衝突判定の IN フィルタバッチサイズ
SAMPLE_LIMIT = 20        # dry-run サンプル表示件数
CONFLICTING_ABORT_PCT = 5.0  # conflicting 率がこれを超えたら apply 強制停止 (%)


# ============================================================
# normalize_ticker_for_fix — 補正ツール専用
# ============================================================

def normalize_ticker_for_fix(raw_ticker: str, enable_alpha_map: bool = True) -> str | None:
    """5桁tickerを正規化する。変換不要/不可ならNoneを返す。

    Args:
        raw_ticker: 元のticker文字列
        enable_alpha_map: True の場合 JQUANTS_ALPHA_MAP による数値→alpha変換を許可
                         False の場合 純粋な末尾0除去のみ (numeric→numeric)

    Examples:
        >>> normalize_ticker_for_fix("78490")
        '7849'
        >>> normalize_ticker_for_fix("12345")  # 末尾0でない
        >>> normalize_ticker_for_fix("1234")   # 4桁(変換不要)
        >>> normalize_ticker_for_fix("41800")  # JQUANTS_ALPHA_MAP → '418A'
        '418A'
        >>> normalize_ticker_for_fix("41800", enable_alpha_map=False)
        # → None (ALPHA_MAP 無効時、末尾0除去で '4180' だが ALPHA_MAP 対象なのでスキップ)
    """
    if not raw_ticker or len(raw_ticker) != 5:
        return None
    norm = normalize_ticker(raw_ticker)
    if norm == raw_ticker:
        return None
    if not is_valid_ticker(norm):
        return None
    # ALPHA_MAP 無効時: numeric→alpha 変換が発生していたらスキップ
    if not enable_alpha_map:
        if not norm.isdigit() and raw_ticker.isdigit():
            return None
    return norm


def make_new_source_row_key(old_key: str, raw_ticker: str, norm_ticker: str) -> str:
    """source_row_key 内の ticker を正規化後の値に置換する。

    source_row_key format: cf|ticker|period|quarter|metric|source|filing_id
    """
    return old_key.replace(f"|{raw_ticker}|", f"|{norm_ticker}|", 1)


# ============================================================
# Supabase helpers (requests.Session 対応)
# ============================================================

def _safe_get(session: requests.Session, config: dict, table: str,
              params: dict, retries: int = 3) -> list[dict] | None:
    """Supabase REST GET with retry. Returns list or None on failure."""
    rest_url = config["rest_url"]
    headers = config["headers"]
    for attempt in range(retries):
        try:
            r = session.get(f"{rest_url}/{table}", headers=headers,
                            params=params, timeout=90)
            if r.status_code == 200:
                return r.json()
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            logger.warning(f"GET {table} failed: status={r.status_code} body={r.text[:200]}")
            return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
            else:
                logger.warning(f"GET {table} exception: {e}")
                return None


def _safe_delete(session: requests.Session, config: dict, table: str,
                 params: dict, retries: int = 3) -> dict:
    """Supabase REST DELETE with retry."""
    rest_url = config["rest_url"]
    headers = config["headers"]
    for attempt in range(retries):
        try:
            r = session.delete(f"{rest_url}/{table}", headers=headers,
                               params=params, timeout=60)
            if r.status_code in (200, 204):
                return {"ok": True, "status": r.status_code}
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            return {"ok": False, "status": r.status_code, "body": r.text[:300]}
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
            else:
                return {"ok": False, "error": str(e)}


# ============================================================
# Phase 1: 候補抽出 — 2段階方式
#   Step 1: distinct ticker 収集 → Python で 5文字判定 + 正規化
#   Step 2: 対象 ticker だけ in フィルタで行取得
#
# PostgREST の like は SQL LIKE の _ (underscore) を使うが
# URL エンコーディング/PostgREST 側で不安定なため、
# REST 側フィルタは ticker=in.(...) に限定し、
# 5文字判定は Python 側で行う。
# ============================================================

def _collect_distinct_tickers(
    session: requests.Session, config: dict,
) -> set[str]:
    """canonical_financials から distinct ticker を keyset pagination で収集する。

    PostgREST には DISTINCT がないため、source_row_key 順で走査して
    ticker を set に集める。ただし全行ではなく ticker 列のみ取得。
    """
    tickers: set[str] = set()
    last_key = None
    pages = 0

    while True:
        params = {
            "select": "source_row_key,ticker",
            "order": "source_row_key.asc",
            "limit": str(SCAN_PAGE_SIZE),
        }
        if last_key is not None:
            params["source_row_key"] = f"gt.{last_key}"

        rows = _safe_get(session, config, "canonical_financials", params)
        if not rows:
            break

        for row in rows:
            tickers.add(row.get("ticker", ""))

        last_key = rows[-1]["source_row_key"]
        pages += 1
        if len(rows) < SCAN_PAGE_SIZE:
            break

        if pages % 50 == 0:
            logger.info(f"[SCAN] distinct ticker collection: pages={pages} tickers={len(tickers)}")

    logger.info(
        f"[SCAN] distinct tickers collected: {len(tickers)} (pages={pages})"
    )
    return tickers


def _find_target_tickers(
    all_tickers: set[str], enable_alpha_map: bool = True,
) -> tuple[list[str], int]:
    """5文字 ticker のうち正規化可能なものを特定する。

    Returns:
        (target_tickers, skipped_count)
        target_tickers: 正規化対象の5文字ticker一覧
        skipped_count: 5文字だが変換不可だった件数
    """
    targets = []
    skipped = 0
    for t in all_tickers:
        if len(t) != 5:
            continue
        norm = normalize_ticker_for_fix(t, enable_alpha_map=enable_alpha_map)
        if norm is None:
            skipped += 1
        else:
            targets.append(t)
    return targets, skipped


def _fetch_rows_by_tickers(
    session: requests.Session, config: dict, target_tickers: list[str],
) -> list[dict]:
    """対象 ticker の行だけを in フィルタで取得する。"""
    all_rows: list[dict] = []

    # in フィルタはURL長制限があるので 20 ticker ずつ
    TICKER_BATCH = 20
    for i in range(0, len(target_tickers), TICKER_BATCH):
        batch_tickers = target_tickers[i : i + TICKER_BATCH]
        in_value = "(" + ",".join(batch_tickers) + ")"

        # keyset pagination で該当 ticker の全行を取得
        last_key = None
        while True:
            params = {
                "select": "source_row_key,ticker,period,quarter,metric,value,unit,source,filing_id",
                "ticker": f"in.{in_value}",
                "order": "source_row_key.asc",
                "limit": str(SCAN_PAGE_SIZE),
            }
            if last_key is not None:
                params["source_row_key"] = f"gt.{last_key}"

            rows = _safe_get(session, config, "canonical_financials", params)
            if not rows:
                break

            all_rows.extend(rows)
            last_key = rows[-1]["source_row_key"]
            if len(rows) < SCAN_PAGE_SIZE:
                break

    return all_rows


def scan_candidates(
    session: requests.Session, config: dict,
    enable_alpha_map: bool = True,
) -> tuple[list[dict], int]:
    """2段階方式で正規化候補を抽出する。

    Step 1: distinct ticker 収集 → Python で 5文字判定 + 正規化判定
    Step 2: 対象 ticker だけ in フィルタで行取得 → 候補組み立て

    Returns:
        (candidates, skipped_invalid)
    """
    t0 = time.monotonic()

    # Step 1: distinct ticker 収集 + 対象特定
    all_tickers = _collect_distinct_tickers(session, config)
    target_tickers, skipped_invalid = _find_target_tickers(
        all_tickers, enable_alpha_map=enable_alpha_map,
    )

    logger.info(
        f"[SCAN] target tickers: {len(target_tickers)} "
        f"(5-char total={len(target_tickers)+skipped_invalid} "
        f"convertible={len(target_tickers)} skipped_invalid={skipped_invalid})"
    )

    if not target_tickers:
        elapsed = time.monotonic() - t0
        logger.info(f"[SCAN] no convertible tickers found. elapsed={elapsed:.1f}s")
        return [], skipped_invalid

    # Step 2: 対象 ticker の行だけ取得
    raw_rows = _fetch_rows_by_tickers(session, config, target_tickers)

    # 候補組み立て
    candidates = []
    for row in raw_rows:
        raw_t = row.get("ticker", "")
        norm_t = normalize_ticker_for_fix(raw_t, enable_alpha_map=enable_alpha_map)
        if norm_t is None:
            # safety: Step 1 で絞っているのでここには来ないはず
            continue

        old_key = row["source_row_key"]
        new_key = make_new_source_row_key(old_key, raw_t, norm_t)

        candidates.append({
            **row,
            "raw_ticker": raw_t,
            "norm_ticker": norm_t,
            "old_key": old_key,
            "new_key": new_key,
            "conversion_category": (
                "numeric_to_numeric" if raw_t.isdigit() and norm_t.isdigit()
                else "numeric_to_alpha" if raw_t.isdigit() and not norm_t.isdigit()
                else "other"
            ),
        })

    elapsed = time.monotonic() - t0
    logger.info(
        f"[SCAN] canonical_financials: fetched={len(raw_rows)} "
        f"convertible={len(candidates)} skipped_invalid={skipped_invalid} "
        f"target_tickers={len(target_tickers)} elapsed={elapsed:.1f}s"
    )
    return candidates, skipped_invalid


# ============================================================
# Phase 2: 衝突判定 — source_row_key ベースでバッチ照会 + 分類
#
# collision 分類:
#   - identical:      既存行と value/source が一致 → delete-only 候補
#   - unit_mismatch:  値が 1000000倍関係 (円↔百万円) + 全フィールド一致 → delete-only 候補
#   - true_conflict:  本当に異なるデータ → 手動確認必須
#   - blocked:        衝突行の内容取得に失敗 → 安全側でスキップ
# ============================================================

UNIT_MISMATCH_RATIO = 1_000_000  # 円 ↔ 百万円
UNIT_MISMATCH_TOLERANCE = 1e-6   # 浮動小数点誤差許容


def _is_unit_mismatch(old_val, existing_val) -> bool:
    """値が 1000000倍関係か判定する（誤差許容付き）。

    円↔百万円の単位差のみを検出。
    両方向 (old/1000000 == existing  or  old == existing*1000000) をチェック。
    """
    if old_val is None or existing_val is None:
        return False
    try:
        a, b = float(old_val), float(existing_val)
    except (ValueError, TypeError):
        return False
    if a == 0 and b == 0:
        return False  # 両方 0 は identical で捕捉する
    if b != 0:
        ratio = a / b
        if abs(ratio - UNIT_MISMATCH_RATIO) / UNIT_MISMATCH_RATIO < UNIT_MISMATCH_TOLERANCE:
            return True
    if a != 0:
        ratio = b / a
        if abs(ratio - UNIT_MISMATCH_RATIO) / UNIT_MISMATCH_RATIO < UNIT_MISMATCH_TOLERANCE:
            return True
    return False

def detect_collisions(
    session: requests.Session, config: dict, candidates: list[dict],
) -> tuple[list[dict], list[dict]]:
    """candidates の中から、正規化後 source_row_key が既存行と衝突するものを分離する。

    Returns:
        (updatable, collisions)
        collisions には collision_type (identical/conflicting/blocked) が付与される
    """
    t0 = time.monotonic()

    new_keys = [c["new_key"] for c in candidates]

    # バッチで既存行を照会 (value/source も取得して比較用)
    existing_rows: dict[str, dict] = {}
    for i in range(0, len(new_keys), COLLISION_BATCH):
        batch_keys = new_keys[i : i + COLLISION_BATCH]
        in_value = "(" + ",".join(batch_keys) + ")"
        rows = _safe_get(session, config, "canonical_financials", {
            "select": "source_row_key,ticker,value,source,period,quarter,metric",
            "source_row_key": f"in.{in_value}",
            "limit": str(len(batch_keys)),
        })
        if rows:
            for row in rows:
                existing_rows[row["source_row_key"]] = row

    # 分類
    updatable = []
    collisions = []
    for c in candidates:
        existing = existing_rows.get(c["new_key"])
        if existing is None:
            updatable.append(c)
        else:
            c["existing_ticker"] = existing.get("ticker", "")
            c["existing_value"] = existing.get("value")
            c["existing_source"] = existing.get("source", "")

            # identical: 同じ value + 同じ source
            if (existing.get("value") == c.get("value")
                    and existing.get("source") == c.get("source")):
                c["collision_type"] = "identical"
            # unit_mismatch: 全フィールド一致 + 値が1000000倍関係
            elif (
                existing.get("source") == c.get("source")
                and existing.get("period") == c.get("period")
                and existing.get("quarter") == c.get("quarter")
                and existing.get("metric") == c.get("metric")
                and _is_unit_mismatch(c.get("value"), existing.get("value"))
            ):
                c["collision_type"] = "unit_mismatch"
            else:
                c["collision_type"] = "true_conflict"

            collisions.append(c)

    elapsed = time.monotonic() - t0
    # 内訳
    n_identical = sum(1 for c in collisions if c.get("collision_type") == "identical")
    n_unit_mismatch = sum(1 for c in collisions if c.get("collision_type") == "unit_mismatch")
    n_true_conflict = sum(1 for c in collisions if c.get("collision_type") == "true_conflict")
    logger.info(
        f"[COLLISION] checked={len(candidates)} "
        f"collisions={len(collisions)} "
        f"(identical={n_identical} unit_mismatch={n_unit_mismatch} "
        f"true_conflict={n_true_conflict}) "
        f"updatable={len(updatable)} elapsed={elapsed:.1f}s"
    )
    return updatable, collisions


# ============================================================
# Phase 3: dry-run レポート（拡充版）
# ============================================================

def _count_by(items: list[dict], key: str) -> dict[str, int]:
    """items を key で集計して {value: count} を返す。"""
    counts: dict[str, int] = {}
    for item in items:
        v = str(item.get(key, "unknown"))
        counts[v] = counts.get(v, 0) + 1
    return dict(sorted(counts.items()))


def print_dry_run_report(
    candidates: list[dict],
    skipped_invalid: int,
    updatable: list[dict],
    collisions: list[dict],
):
    """dry-run の集計結果とサンプルを表示する（拡充版）。"""

    n_identical = sum(1 for c in collisions if c.get("collision_type") == "identical")
    n_unit_mismatch = sum(1 for c in collisions if c.get("collision_type") == "unit_mismatch")
    n_true_conflict = sum(1 for c in collisions if c.get("collision_type") == "true_conflict")
    n_blocked = sum(1 for c in collisions if c.get("collision_type") == "blocked")

    # ── 1. 変換仕様 ──
    print()
    print("=" * 70)
    print("  Ticker Normalization Fix - DRY-RUN REPORT")
    print("=" * 70)
    print()
    print("  [1. Conversion Spec]")
    print("    - Target: exactly 5-char ticker in canonical_financials")
    print("    - Condition: 5 numeric digits with trailing '0'  (e.g. 78490 -> 7849)")
    print("    - J-Quants ALPHA_MAP also applied (e.g. 41800 -> 418A)")
    print("    - Non-numeric / no trailing zero / invalid result -> SKIP")
    print()

    # ── 2. 全体サマリ ──
    total_5char = len(candidates) + skipped_invalid
    print("  [2. Summary]")
    print(f"    5-char ticker rows (total)   : {total_5char:>10,}")
    print(f"    convertible                  : {len(candidates):>10,}")
    print(f"    skipped_invalid              : {skipped_invalid:>10,}")
    print(f"    collisions (skip)            : {len(collisions):>10,}")
    print(f"    updatable (apply target)     : {len(updatable):>10,}")
    print()

    # ── 3. source 別内訳 ──
    print("  [3. Breakdown by source]")
    all_sources = set()
    for c in candidates:
        all_sources.add(c.get("source", "unknown"))
    src_cand = _count_by(candidates, "source")
    src_upd = _count_by(updatable, "source")
    src_col = _count_by(collisions, "source")
    print(f"    {'source':<20} {'candidates':>12} {'updatable':>12} {'collisions':>12}")
    print(f"    {'-'*20} {'-'*12} {'-'*12} {'-'*12}")
    for s in sorted(all_sources):
        print(f"    {s:<20} {src_cand.get(s, 0):>12,} {src_upd.get(s, 0):>12,} {src_col.get(s, 0):>12,}")
    print()

    # ── 4. collision 内訳 ──
    print("  [4. Collision Breakdown]")
    print(f"    identical     (same value+source)     : {n_identical:>10,}")
    print(f"    unit_mismatch (1M ratio, same fields) : {n_unit_mismatch:>10,}")
    print(f"    true_conflict (different data)        : {n_true_conflict:>10,}")
    print(f"    blocked       (lookup failed)         : {n_blocked:>10,}")
    n_delete_only = n_identical + n_unit_mismatch
    print(f"    delete-only candidates                : {n_delete_only:>10,}")
    if len(candidates) > 0:
        pct = n_true_conflict / len(candidates) * 100
        print(f"    true_conflict rate                    : {pct:>9.1f}%")
        if pct > CONFLICTING_ABORT_PCT:
            print(f"    ** WARNING: true_conflict > {CONFLICTING_ABORT_PCT}% -- apply will be BLOCKED **")
    print()

    # ── 4.5 変換パターン別内訳 ──
    cc = _count_by(candidates, "conversion_category")
    print("  [4.5 Conversion Pattern Breakdown]")
    print(f"    numeric_to_numeric (e.g. 78490->7849) : {cc.get('numeric_to_numeric', 0):>10,}")
    print(f"    numeric_to_alpha   (e.g. 41800->418A) : {cc.get('numeric_to_alpha', 0):>10,}")
    if cc.get('other', 0) > 0:
        print(f"    other                                 : {cc['other']:>10,}")
    print()

    # ── 5. true_conflict サンプル ──
    true_conflict_list = [c for c in collisions if c.get("collision_type") == "true_conflict"]
    if true_conflict_list:
        n_show = min(SAMPLE_LIMIT, len(true_conflict_list))
        print(f"  [5. True Conflicts - sample {n_show} / {len(true_conflict_list):,}]")
        print(f"    {'raw':>7} {'norm':>5} {'cat':<18} {'period':<12} {'qtr':<4} {'metric':<20} "
              f"{'src':<10} {'old_val':>12} {'exist_val':>12}")
        print(f"    {'-'*7} {'-'*5} {'-'*18} {'-'*12} {'-'*4} {'-'*20} "
              f"{'-'*10} {'-'*12} {'-'*12}")
        for c in true_conflict_list[:SAMPLE_LIMIT]:
            print(f"    {c['raw_ticker']:>7} {c['norm_ticker']:>5} "
                  f"{c.get('conversion_category',''):<18} "
                  f"{c.get('period',''):>12} {c.get('quarter',''):>4} "
                  f"{c.get('metric',''):<20} {c.get('source',''):<10} "
                  f"{str(c.get('value','')):>12} {str(c.get('existing_value','')):>12}")
        print()
    else:
        print("  [5. True Conflicts]")
        print("    (none)")
        print()

    # ── 5.5 unit_mismatch サンプル ──
    unit_mismatch_list = [c for c in collisions if c.get("collision_type") == "unit_mismatch"]
    if unit_mismatch_list:
        n_show = min(SAMPLE_LIMIT, len(unit_mismatch_list))
        print(f"  [5.5 Unit Mismatch (1M ratio) - sample {n_show} / {len(unit_mismatch_list):,}]")
        print(f"    {'raw':>7} {'norm':>5} {'period':<12} {'qtr':<4} {'metric':<20} "
              f"{'src':<10} {'5digit_val':>12} {'4digit_val':>12}")
        print(f"    {'-'*7} {'-'*5} {'-'*12} {'-'*4} {'-'*20} "
              f"{'-'*10} {'-'*12} {'-'*12}")
        for c in unit_mismatch_list[:SAMPLE_LIMIT]:
            print(f"    {c['raw_ticker']:>7} {c['norm_ticker']:>5} "
                  f"{c.get('period',''):>12} {c.get('quarter',''):>4} "
                  f"{c.get('metric',''):<20} {c.get('source',''):<10} "
                  f"{str(c.get('value','')):>12} {str(c.get('existing_value','')):>12}")
        print()
    else:
        print("  [5.5 Unit Mismatch]")
        print("    (none)")
        print()

    # ── 6. updatable サンプル ──
    if updatable:
        n_show = min(SAMPLE_LIMIT, len(updatable))
        print(f"  [6. Updatable - sample {n_show} / {len(updatable):,}]")
        print(f"    {'raw':>7} {'norm':>5} {'period':<12} {'qtr':<4} {'metric':<20} "
              f"{'source':<10} {'value':>12}")
        print(f"    {'-'*7} {'-'*5} {'-'*12} {'-'*4} {'-'*20} {'-'*10} {'-'*12}")
        for c in updatable[:SAMPLE_LIMIT]:
            print(f"    {c['raw_ticker']:>7} {c['norm_ticker']:>5} "
                  f"{c.get('period',''):>12} {c.get('quarter',''):>4} "
                  f"{c.get('metric',''):<20} {c.get('source',''):<10} "
                  f"{str(c.get('value','')):>12}")
        print()
    else:
        print("  [6. Updatable]")
        print("    (none)")
        print()

    # ── 7. safety option ガイド ──
    print("  [7. Safety Options for --apply]")
    print("    --limit-updates N        : Apply only first N updates")
    print("    --source SOURCE          : Apply only rows matching this source")
    print("    --tickers T1,T2,...       : Apply only these raw 5-digit tickers")
    print()
    print("    Example:")
    print("    python tools/fix_ticker_normalization.py --apply --limit-updates 1000")
    print("    python tools/fix_ticker_normalization.py --apply --source jquants --limit-updates 500")
    print("    python tools/fix_ticker_normalization.py --apply --tickers 67580,72030")
    print()

    # ── 8. apply 後検証コマンド ──
    print("  [8. Post-Apply Verification]")
    print("    # Re-run dry-run to check remaining 5-digit tickers:")
    print("    python tools/fix_ticker_normalization.py --dry-run")
    print()
    print("    # Quick remaining check:")
    print("    python tools/fix_ticker_normalization.py --dry-run 2>&1 | findstr updatable")
    print()


# ============================================================
# Phase 4: apply - insert->delete 順序保証バッチ処理
# ============================================================

def apply_updates(
    session: requests.Session,
    write_config: dict,
    read_config: dict,
    updatable: list[dict],
) -> dict:
    """updatable の各行に対し insert(upsert) -> delete をバッチ実行する。

    Returns:
        {"inserted": int, "deleted": int, "insert_failed": int,
         "delete_failed": int, "batches": int}
    """
    stats = {
        "inserted": 0, "deleted": 0,
        "insert_failed": 0, "delete_failed": 0,
        "batches": 0,
    }

    total = len(updatable)
    batches = [updatable[i : i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    num_batches = len(batches)

    logger.info(
        f"[APPLY] start rows={total} batches={num_batches} batch_size={BATCH_SIZE}"
    )

    for batch_idx, batch in enumerate(batches, 1):
        t0 = time.monotonic()
        stats["batches"] += 1

        # -- Step 1: INSERT (upsert) 正規化後の新行 --
        new_rows = []
        for c in batch:
            new_row = {}
            for col in ("ticker", "period", "quarter", "metric", "value", "unit",
                         "source", "source_priority", "filing_id", "source_row_key",
                         "disclosure_datetime", "correction_flag", "recency_key"):
                if col in c:
                    new_row[col] = c[col]
            new_row["ticker"] = c["norm_ticker"]
            new_row["source_row_key"] = c["new_key"]
            new_rows.append(new_row)

        result = supabase_upsert(
            "canonical_financials",
            new_rows,
            on_conflict="source_row_key",
            config=write_config,
            batch_size=BATCH_SIZE,
        )

        if not result.get("ok"):
            logger.error(
                f"[APPLY] batch {batch_idx}/{num_batches} INSERT FAILED: "
                f"{result.get('error', 'unknown')}"
            )
            stats["insert_failed"] += len(batch)
            continue

        stats["inserted"] += len(batch)

        # -- Step 2: DELETE 旧行 (INSERT 成功時のみ) --
        delete_failed_in_batch = 0
        for c in batch:
            del_result = _safe_delete(session, write_config, "canonical_financials", {
                "source_row_key": f"eq.{c['old_key']}",
            })
            if del_result.get("ok"):
                stats["deleted"] += 1
            else:
                delete_failed_in_batch += 1
                stats["delete_failed"] += 1
                logger.warning(
                    f"[APPLY] DELETE_FAILED: old_key={c['old_key'][:80]} "
                    f"result={del_result}"
                )

        elapsed = time.monotonic() - t0
        logger.info(
            f"[APPLY] batch {batch_idx}/{num_batches} "
            f"inserted={len(batch)} deleted={len(batch) - delete_failed_in_batch} "
            f"delete_failed={delete_failed_in_batch} elapsed={elapsed:.1f}s"
        )

    return stats


# ============================================================
# CSV 出力ヘルパー
# ============================================================

def write_plan_csv(filepath: str, items: list[dict], fieldnames: list[str]):
    """plan/collision CSV を書き出す。"""
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(items)


# ============================================================
# CLI
# ============================================================

def main():

    parser = argparse.ArgumentParser(description="Fix 5-digit ticker normalization (optimized)")
    parser.add_argument("--apply", action="store_true", help="Execute fixes")
    parser.add_argument("--dry-run", action="store_true", help="Scan and report only (default)")
    parser.add_argument("--limit-updates", type=int, default=0,
                        help="Apply only first N updates (0=unlimited)")
    parser.add_argument("--source", type=str, default="",
                        help="Apply only rows matching this source (e.g. jquants)")
    parser.add_argument("--tickers", type=str, default="",
                        help="Apply only these raw 5-digit tickers (comma-separated)")
    parser.add_argument("--enable-alpha-map", action="store_true",
                        help="Enable JQUANTS_ALPHA_MAP conversion (numeric->alpha). "
                             "Default: disabled (numeric->numeric only)")
    args = parser.parse_args()
    is_apply = args.apply and not args.dry_run

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    mode = "APPLY" if is_apply else "DRY-RUN"
    logger.info(f"=== fix_ticker_normalization START ({mode}) ===")

    config = get_supabase_config()
    session = requests.Session()
    try:
        # -- Phase 1: 候補抽出 --
        logger.info("[SCAN] canonical_financials ...")
        candidates, skipped_invalid = scan_candidates(
            session, config, enable_alpha_map=args.enable_alpha_map,
        )

        if not candidates:
            logger.info("[DONE] No convertible candidates found.")
            print("\n  No 5-digit ticker candidates found. Nothing to do.\n")
            return 0

        # -- Phase 2: 衝突判定 --
        logger.info("[COLLISION] Checking existing rows ...")
        updatable, collisions = detect_collisions(session, config, candidates)

        # -- Phase 3: dry-run レポート --
        print_dry_run_report(candidates, skipped_invalid, updatable, collisions)

        # CSV 出力
        plan_csv = os.path.join(OUT_DIR, f"ticker_fix_plan_{ts}.csv")
        write_plan_csv(plan_csv, candidates, [
            "raw_ticker", "norm_ticker", "old_key", "new_key",
            "period", "quarter", "metric", "source",
        ])
        print(f"  Plan CSV: {plan_csv}")

        if collisions:
            c_csv = os.path.join(OUT_DIR, f"ticker_collisions_{ts}.csv")
            write_plan_csv(c_csv, collisions, [
                "raw_ticker", "norm_ticker", "old_key", "new_key",
                "period", "quarter", "metric", "source",
                "collision_type", "existing_ticker", "existing_value", "existing_source",
            ])
            print(f"  Collisions CSV: {c_csv}")

        if not is_apply:
            print(f"\n  DRY-RUN complete. Use --apply to execute.\n")
            return 0

        # -- Phase 4: apply (safety filter 適用) --
        if not updatable:
            logger.info("[DONE] No updatable rows (all collisions).")
            print("\n  All candidates have collisions. Nothing to apply.\n")
            return 0

        # Safety gate: true_conflict 率が閾値を超えたら強制停止
        n_true_conflict = sum(1 for c in collisions if c.get("collision_type") == "true_conflict")
        if len(candidates) > 0 and n_true_conflict > 0:
            pct = n_true_conflict / len(candidates) * 100
            if pct > CONFLICTING_ABORT_PCT:
                logger.error(
                    f"[ABORT] true_conflict rate {pct:.1f}% > {CONFLICTING_ABORT_PCT}% -- "
                    f"apply is BLOCKED. Review collisions first."
                )
                print(f"\n  ** ABORT: true_conflict rate {pct:.1f}% exceeds {CONFLICTING_ABORT_PCT}% threshold **")
                print(f"  Review collisions CSV before proceeding.\n")
                return 1

        # 確認プロンプト
        print(f"\n  About to apply {len(updatable):,} updates.")
        answer = input("  Proceed? (yes/no): ").strip().lower()
        if answer != "yes":
            print("  Aborted by user.")
            return 0

        # Safety filter: --source
        apply_target = list(updatable)
        if args.source:
            before = len(apply_target)
            apply_target = [c for c in apply_target if c.get("source") == args.source]
            logger.info(f"[APPLY] --source={args.source}: {before} -> {len(apply_target)}")

        # Safety filter: --tickers
        if args.tickers:
            ticker_set = set(args.tickers.split(","))
            before = len(apply_target)
            apply_target = [c for c in apply_target if c.get("raw_ticker") in ticker_set]
            logger.info(f"[APPLY] --tickers={args.tickers}: {before} -> {len(apply_target)}")

        # Safety filter: --limit-updates
        if args.limit_updates > 0 and len(apply_target) > args.limit_updates:
            logger.info(
                f"[APPLY] --limit-updates={args.limit_updates}: "
                f"{len(apply_target)} -> {args.limit_updates}"
            )
            apply_target = apply_target[:args.limit_updates]

        if not apply_target:
            logger.info("[DONE] No rows match safety filters. Nothing to apply.")
            print("\n  No rows match the specified filters. Nothing to apply.\n")
            return 0

        write_config = get_supabase_write_config()
        if not write_config:
            logger.error("[APPLY] No write config available (service role key missing)")
            return 1

        logger.info(f"[APPLY] Executing updates (target={len(apply_target)}) ...")
        stats = apply_updates(session, write_config, config, apply_target)

        # -- 最終サマリ --
        print()
        print("=" * 60)
        print("  APPLY Results")
        print("=" * 60)
        print(f"    candidates       : {len(candidates):>10,}")
        print(f"    skipped_invalid  : {skipped_invalid:>10,}")
        print(f"    collisions       : {len(collisions):>10,}")
        print(f"    apply_target     : {len(apply_target):>10,}")
        print(f"    inserted         : {stats['inserted']:>10,}")
        print(f"    deleted          : {stats['deleted']:>10,}")
        print(f"    insert_failed    : {stats['insert_failed']:>10,}")
        print(f"    delete_failed    : {stats['delete_failed']:>10,}")
        print(f"    batches          : {stats['batches']:>10,}")
        if args.source:
            print(f"    filter: source   : {args.source}")
        if args.tickers:
            print(f"    filter: tickers  : {args.tickers}")
        if args.limit_updates > 0:
            print(f"    filter: limit    : {args.limit_updates}")
        print()

        logger.info(
            f"[DONE] updated={stats['inserted']} "
            f"deleted={stats['deleted']} "
            f"insert_failed={stats['insert_failed']} "
            f"delete_failed={stats['delete_failed']} "
            f"collision_skipped={len(collisions)} "
            f"skipped_invalid={skipped_invalid}"
        )

        has_errors = stats["insert_failed"] > 0
        return 1 if has_errors else 0

    finally:
        session.close()


if __name__ == "__main__":
    # Windows cp932 対策: UTF-8 で出力
    import io
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'buffer'):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    sys.exit(main())

