#!/usr/bin/env python3
# ============================================================
# reextract_all_zips.py — TDnet由来行 GP/COS 補完バッチ
# ============================================================
"""
data/docs/*.zip を再抽出し、Attachment PL Fallback で
gross_profit / cost_of_sales を補完する。

Usage:
    python tools/reextract_all_zips.py --dry-run
    python tools/reextract_all_zips.py --company-code 2301
    python tools/reextract_all_zips.py
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from src.extractor import _extract_from_xbrl

logger = logging.getLogger("reextract")

JST = timezone(timedelta(hours=9))


# ============================================================
# Utilities
# ============================================================

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_jst_str() -> str:
    return datetime.now(JST).strftime("%Y%m%d_%H%M%S")


def _backup_db(db_path: str) -> str:
    ts = _now_jst_str()
    bak = f"{db_path}.bak_{ts}"
    shutil.copy2(db_path, bak)
    logger.info(f"[BACKUP] {bak}")
    return bak


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """field_sources カラムがなければ追加"""
    cur = conn.execute("PRAGMA table_info(quarterly_results)")
    cols = {r[1] for r in cur.fetchall()}
    if "field_sources" not in cols:
        conn.execute(
            "ALTER TABLE quarterly_results ADD COLUMN field_sources TEXT"
        )
        conn.commit()
        logger.info("[DB] field_sources カラム追加")


# ============================================================
# Step 1: Build ZIP hash → path map
# ============================================================

def _build_zip_hash_map(docs_dir: str) -> dict[str, str]:
    """SHA256(ZIP) → ZIP path の辞書を構築"""
    import glob
    result: dict[str, str] = {}
    for zp in sorted(glob.glob(os.path.join(docs_dir, "*.zip"))):
        h = _sha256(zp)
        result[h] = zp
    return result


# ============================================================
# Step 2: Load target rows from DB
# ============================================================

def _load_tdnet_rows(
    conn: sqlite3.Connection,
    *,
    company_code: str | None = None,
    doc_id: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """source_doc_id IS NOT NULL の行を取得"""
    sql = (
        "SELECT id, company_code, fiscal_year_end, quarter, "
        "sales, gross_profit, operating_profit, "
        "zip_hash, field_sources, source_doc_id "
        "FROM quarterly_results "
        "WHERE source_doc_id IS NOT NULL AND source_doc_id != ''"
    )
    params: list = []
    if company_code:
        sql += " AND company_code = ?"
        params.append(company_code)
    if doc_id:
        sql += " AND source_doc_id = ?"
        params.append(doc_id)
    sql += " ORDER BY company_code, fiscal_year_end, quarter"
    if limit:
        sql += f" LIMIT {int(limit)}"

    cur = conn.execute(sql, tuple(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ============================================================
# Before/After stats
# ============================================================

def _count_stats(conn: sqlite3.Connection) -> dict:
    """Before/After 計測用"""
    total = conn.execute("SELECT COUNT(*) FROM quarterly_results").fetchone()[0]
    gp_all = conn.execute(
        "SELECT COUNT(*) FROM quarterly_results WHERE gross_profit IS NOT NULL"
    ).fetchone()[0]

    tdnet_total = conn.execute(
        "SELECT COUNT(*) FROM quarterly_results "
        "WHERE source_doc_id IS NOT NULL AND source_doc_id != ''"
    ).fetchone()[0]
    tdnet_gp = conn.execute(
        "SELECT COUNT(*) FROM quarterly_results "
        "WHERE source_doc_id IS NOT NULL AND source_doc_id != '' "
        "AND gross_profit IS NOT NULL"
    ).fetchone()[0]
    # cost_of_sales is not a DB column; tracked via field_sources only

    return {
        "total": total,
        "gp_all": gp_all,
        "tdnet_total": tdnet_total,
        "tdnet_gp": tdnet_gp,
    }


def _count_field_sources(rows: list[dict]) -> dict[str, int]:
    """field_sources の source 別件数(TDnet行)"""
    counts: dict[str, int] = {}
    for row in rows:
        fs_raw = row.get("field_sources")
        if not fs_raw:
            counts["none"] = counts.get("none", 0) + 1
            continue
        try:
            fs = json.loads(fs_raw) if isinstance(fs_raw, str) else fs_raw
        except (json.JSONDecodeError, TypeError):
            counts["none"] = counts.get("none", 0) + 1
            continue

        # Count GP source
        gp_src = fs.get("gross_profit", "none")
        counts[gp_src] = counts.get(gp_src, 0) + 1
    return counts


# ============================================================
# Core: re-extract and complement
# ============================================================

def run_reextract(
    *,
    dry_run: bool = False,
    company_code: str | None = None,
    doc_id: str | None = None,
    limit: int | None = None,
) -> dict:
    """
    メイン処理。

    Returns:
        結果サマリ dict
    """
    db_path = os.path.join(_PROJECT_ROOT, "decision_db.db")
    docs_dir = os.path.join(_PROJECT_ROOT, "data", "docs")

    if not os.path.isfile(db_path):
        logger.error(f"DB not found: {db_path}")
        return {"error": "DB not found"}

    # ── Backup ──
    if not dry_run:
        _backup_db(db_path)

    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_columns(conn)

    # ── Before stats ──
    before = _count_stats(conn)
    logger.info(
        f"[BEFORE] DB total={before['total']} GP_NOT_NULL={before['gp_all']} "
        f"TDnet={before['tdnet_total']} TDnet_GP={before['tdnet_gp']}"
    )

    # ── Load target rows ──
    rows = _load_tdnet_rows(
        conn, company_code=company_code, doc_id=doc_id, limit=limit,
    )
    logger.info(f"[TARGET] TDnet rows loaded: {len(rows)}")

    # ── Build ZIP hash map ──
    zip_hash_map = _build_zip_hash_map(docs_dir)
    logger.info(f"[ZIP] Hash map built: {len(zip_hash_map)} ZIPs")

    # ── Step 1: zip_hash group ──
    # Group rows by zip_hash where hash exists in our ZIP map
    step1_groups: dict[str, list[dict]] = {}  # zip_hash -> [rows]
    step2_rows: list[dict] = []  # rows needing Step 2

    for row in rows:
        zh = row.get("zip_hash")
        if zh and zh in zip_hash_map:
            step1_groups.setdefault(zh, []).append(row)
        else:
            step2_rows.append(row)

    logger.info(
        f"[RESOLVE] Step1 zip_hash match: {len(step1_groups)} ZIPs "
        f"({sum(len(v) for v in step1_groups.values())} rows), "
        f"Step2 unresolved: {len(step2_rows)} rows"
    )

    # ── Extract cache: zip_hash -> ExtractedFinancials ──
    extract_cache: dict[str, object] = {}  # zip_hash -> result (or None)

    # Step 1 extractions
    for zh, zip_rows in step1_groups.items():
        zip_path = zip_hash_map[zh]
        try:
            result = _extract_from_xbrl(zip_path)
            extract_cache[zh] = result
            if result:
                logger.info(
                    f"[EXTRACT] Step1 {os.path.basename(zip_path)}: "
                    f"sales={result.sales} gp={result.gross_profit} "
                    f"op={result.operating_profit} cos={getattr(result, 'cost_of_sales', None)}"
                )
            else:
                logger.warning(
                    f"[EXTRACT] Step1 {os.path.basename(zip_path)}: extraction returned None"
                )
        except Exception as e:
            logger.error(f"[EXTRACT] Step1 {os.path.basename(zip_path)}: error {e}")
            extract_cache[zh] = None

    # ── Step 2: 抽出値マッチ (厳格条件) ──
    # For each unresolved row, try all ZIPs not already used in Step 1
    step1_hashes = set(step1_groups.keys())
    step2_unused_zips = {
        h: p for h, p in zip_hash_map.items() if h not in step1_hashes
    }

    # Pre-extract all unused ZIPs
    unused_extracts: dict[str, object] = {}  # hash -> result
    for zh, zp in step2_unused_zips.items():
        if zh not in extract_cache:
            try:
                result = _extract_from_xbrl(zp)
                unused_extracts[zh] = result
                extract_cache[zh] = result
            except Exception as e:
                logger.debug(f"[EXTRACT] Step2 {os.path.basename(zp)}: {e}")
                unused_extracts[zh] = None
        else:
            unused_extracts[zh] = extract_cache[zh]

    step2_resolved: dict[int, tuple[str, object]] = {}  # row_id -> (zip_hash, result)
    step2_multi = 0
    step2_no_match = 0
    step2_value_mismatch = 0

    for row in step2_rows:
        row_code = row["company_code"]
        row_fye = row["fiscal_year_end"]
        row_q = row["quarter"]
        row_sales = row["sales"]
        row_op = row["operating_profit"]

        # Find ZIP candidates that match this row
        candidates: list[tuple[str, object]] = []  # (zip_hash, result)

        for zh, result in unused_extracts.items():
            if result is None:
                continue
            # Check sales or op match
            ext_sales = result.sales
            ext_op = result.operating_profit

            sales_match = (
                row_sales is not None
                and ext_sales is not None
                and abs(row_sales - ext_sales) < 1  # float comparison
            )
            op_match = (
                row_op is not None
                and ext_op is not None
                and abs(row_op - ext_op) < 1
            )

            if sales_match or op_match:
                candidates.append((zh, result))

        if len(candidates) == 0:
            step2_no_match += 1
            logger.info(
                f"[STEP2] {row_code} {row_fye} {row_q}: "
                f"no matching ZIP found (sales={row_sales} op={row_op})"
            )
        elif len(candidates) == 1:
            zh_match, result_match = candidates[0]
            step2_resolved[row["id"]] = (zh_match, result_match)
            logger.info(
                f"[STEP2] {row_code} {row_fye} {row_q}: "
                f"unique match -> {os.path.basename(zip_hash_map.get(zh_match, zh_match))}"
            )
        else:
            step2_multi += 1
            logger.info(
                f"[STEP2] {row_code} {row_fye} {row_q}: "
                f"MULTI match ({len(candidates)} candidates) -> SKIP"
            )

    logger.info(
        f"[STEP2] resolved={len(step2_resolved)} no_match={step2_no_match} "
        f"multi_skip={step2_multi}"
    )

    # ── Apply updates ──
    updated_gp = 0
    updated_cos = 0
    skipped_already_has = 0
    skipped_no_new = 0
    anomaly_gp_gt_sales = 0
    anomaly_cos_gt_sales = 0
    warning_gp_neg = 0
    warning_cos_neg = 0

    def _apply_update(row: dict, result, *, conn: sqlite3.Connection) -> tuple[bool, bool]:
        """Apply GP/COS complement to a single row. Returns (gp_updated, cos_updated)."""
        nonlocal updated_gp, updated_cos, skipped_already_has, skipped_no_new
        nonlocal anomaly_gp_gt_sales, anomaly_cos_gt_sales
        nonlocal warning_gp_neg, warning_cos_neg

        if result is None:
            skipped_no_new += 1
            return False, False

        gp_update = False
        cos_update = False
        new_gp = result.gross_profit
        new_cos = getattr(result, "cost_of_sales", None)
        row_sales = row["sales"]

        # GP: update only if existing is NULL and new value exists
        if row["gross_profit"] is None and new_gp is not None:
            # Anomaly check: GP > Sales means unit mismatch — skip update
            if row_sales is not None and row_sales > 0 and new_gp > row_sales:
                anomaly_gp_gt_sales += 1
                logger.warning(
                    f"[ANOMALY-SKIP] {row['company_code']} {row['fiscal_year_end']} "
                    f"{row['quarter']}: GP ({new_gp}) > Sales ({row_sales}) — unit mismatch, skipping GP"
                )
                # Do NOT set gp_update = True
            else:
                if new_gp < 0:
                    warning_gp_neg += 1
                    logger.info(
                        f"[WARNING] {row['company_code']} {row['fiscal_year_end']} "
                        f"{row['quarter']}: GP < 0 ({new_gp})"
                    )
                gp_update = True
        elif row["gross_profit"] is not None:
            skipped_already_has += 1

        # COS: track in field_sources only (not a DB column)
        if new_cos is not None:
            if row_sales is not None and row_sales > 0 and new_cos > row_sales:
                anomaly_cos_gt_sales += 1
                logger.warning(
                    f"[ANOMALY] {row['company_code']} {row['fiscal_year_end']} "
                    f"{row['quarter']}: COS ({new_cos}) > Sales ({row_sales})"
                )
            if new_cos is not None and new_cos < 0:
                warning_cos_neg += 1
            cos_update = True  # Track for field_sources

        if not gp_update and not cos_update:
            return False, False

        # Build updated field_sources (merge)
        existing_fs = {}
        fs_raw = row.get("field_sources")
        if fs_raw:
            try:
                existing_fs = json.loads(fs_raw) if isinstance(fs_raw, str) else {}
            except (json.JSONDecodeError, TypeError):
                existing_fs = {}

        if gp_update:
            existing_fs["gross_profit"] = "attachment_xbrl"
        if cos_update:
            existing_fs["cost_of_sales"] = "attachment_xbrl"

        new_fs_json = json.dumps(existing_fs)

        if dry_run:
            action = []
            if gp_update:
                action.append(f"gp={new_gp}")
            if cos_update:
                action.append(f"cos={new_cos}")
            logger.info(
                f"[DRY-RUN] {row['company_code']} {row['fiscal_year_end']} "
                f"{row['quarter']}: would update {', '.join(action)}"
            )
        else:
            if gp_update:
                conn.execute(
                    "UPDATE quarterly_results SET gross_profit = ?, "
                    "field_sources = ?, updated_at = datetime('now','localtime') "
                    "WHERE id = ?",
                    (new_gp, new_fs_json, row["id"]),
                )
            elif cos_update:
                # Only field_sources update (COS is not a separate column)
                conn.execute(
                    "UPDATE quarterly_results SET field_sources = ?, "
                    "updated_at = datetime('now','localtime') "
                    "WHERE id = ?",
                    (new_fs_json, row["id"]),
                )

        if gp_update:
            updated_gp += 1
        if cos_update:
            updated_cos += 1

        return gp_update, cos_update

    # Begin transaction for all updates
    if not dry_run:
        conn.execute("BEGIN")

    try:
        # Apply Step 1 updates
        for zh, zip_rows in step1_groups.items():
            result = extract_cache.get(zh)
            for row in zip_rows:
                _apply_update(row, result, conn=conn)

        # Apply Step 2 updates
        for row in step2_rows:
            if row["id"] in step2_resolved:
                _, result = step2_resolved[row["id"]]
                _apply_update(row, result, conn=conn)

        if not dry_run:
            conn.execute("COMMIT")
            logger.info("[DB] COMMIT successful")

    except Exception as e:
        if not dry_run:
            conn.execute("ROLLBACK")
            logger.error(f"[DB] ROLLBACK due to error: {e}")
        raise

    # ── After stats ──
    after = _count_stats(conn)

    # ── Source breakdown for TDnet rows ──
    after_rows = _load_tdnet_rows(
        conn, company_code=company_code, doc_id=doc_id, limit=limit,
    )
    source_counts = _count_field_sources(after_rows)

    conn.close()

    # ── Print report ──
    print()
    print("=" * 60)
    print("  TDnet GP/COS Complement Report")
    print("=" * 60)
    mode = "DRY-RUN" if dry_run else "EXECUTED"
    print(f"  Mode: {mode}")
    print()

    print("  === TDnet対象行 ===")
    print(f"    対象行数: {len(rows)}")
    print(f"    GP NOT NULL: Before {before['tdnet_gp']} -> After {after['tdnet_gp']}")
    gp_delta = after["tdnet_gp"] - before["tdnet_gp"]
    print(f"    GP 補完: +{gp_delta} 件")
    print()

    print("  === DB全体 ===")
    print(f"    総行数: {before['total']}")
    print(f"    GP NOT NULL: Before {before['gp_all']} -> After {after['gp_all']}")
    overall_delta = after["gp_all"] - before["gp_all"]
    print(f"    GP 増分: +{overall_delta} 件")
    print()

    print("  === source別 (TDnet行 GP) ===")
    for src, cnt in sorted(source_counts.items()):
        print(f"    {src}: {cnt}")
    print()

    print("  === ZIP解決 ===")
    step1_rows_count = sum(len(v) for v in step1_groups.values())
    print(f"    Step1 zip_hash一致: {len(step1_groups)} ZIP ({step1_rows_count} rows)")
    print(f"    Step2 抽出値マッチ: {len(step2_resolved)} rows")
    print(f"    Step2 未解決: {step2_no_match} rows")
    print(f"    Step2 多重候補skip: {step2_multi} rows")
    print()

    print("  === 更新詳細 ===")
    print(f"    GP updated: {updated_gp}")
    print(f"    COS field_sources updated: {updated_cos}")
    print(f"    Skipped (already has GP): {skipped_already_has}")
    print()

    print("  === 異常値/警告 ===")
    print(f"    GP > Sales: {anomaly_gp_gt_sales} (異常)")
    print(f"    GP < 0: {warning_gp_neg} (警告)")
    print(f"    COS > Sales: {anomaly_cos_gt_sales} (異常)")
    print(f"    COS < 0: {warning_cos_neg} (警告)")
    print()
    print("=" * 60)
    print()

    return {
        "mode": mode,
        "target_rows": len(rows),
        "step1_zips": len(step1_groups),
        "step1_rows": step1_rows_count,
        "step2_resolved": len(step2_resolved),
        "step2_no_match": step2_no_match,
        "step2_multi": step2_multi,
        "gp_updated": updated_gp,
        "cos_updated": updated_cos,
        "before_tdnet_gp": before["tdnet_gp"],
        "after_tdnet_gp": after["tdnet_gp"],
        "before_gp_all": before["gp_all"],
        "after_gp_all": after["gp_all"],
        "anomaly_gp_gt_sales": anomaly_gp_gt_sales,
        "anomaly_cos_gt_sales": anomaly_cos_gt_sales,
        "warning_gp_neg": warning_gp_neg,
        "warning_cos_neg": warning_cos_neg,
    }


# ============================================================
# CLI
# ============================================================

def main():
    if sys.stdout and hasattr(sys.stdout, "encoding"):
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace",
            )

    parser = argparse.ArgumentParser(
        description="TDnet由来行 GP/COS 補完バッチ",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="DBに書き込まず結果表示のみ")
    parser.add_argument("--company-code", type=str, default=None,
                        help="特定銘柄コードのみ処理")
    parser.add_argument("--doc-id", type=str, default=None,
                        help="特定 source_doc_id のみ処理")
    parser.add_argument("--limit", type=int, default=None,
                        help="処理件数上限")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    result = run_reextract(
        dry_run=args.dry_run,
        company_code=args.company_code,
        doc_id=args.doc_id,
        limit=args.limit,
    )

    if result.get("error"):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
