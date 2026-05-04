#!/usr/bin/env python3
"""migrate_order_metrics.py — data/decision_db.db → decision_db.db 移行

data/decision_db.db の order_metrics を本番DB decision_db.db に統合する。
重複は UNIQUE制約 (company_code, fiscal_year_end, quarter, metric_name) で
INSERT OR IGNORE により安全にスキップする。

Usage:
    # スキーマ比較のみ
    python tools/migrate_order_metrics.py --compare-only

    # dry-run（件数確認のみ、書き込みなし）
    python tools/migrate_order_metrics.py --dry-run

    # 本番移行
    python tools/migrate_order_metrics.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
_SRC_DB  = os.path.join(_PROJECT_ROOT, "data", "decision_db.db")
_DST_DB  = os.path.join(_PROJECT_ROOT, "decision_db.db")
_TABLE   = "order_metrics"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("migrate_order_metrics")


# ============================================================
# スキーマ取得
# ============================================================
def get_schema(db_path: str, table: str) -> list[tuple]:
    """PRAGMA table_info() を返す。[(cid, name, type, notnull, dflt, pk), ...]"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    conn.close()
    return rows


def get_create_sql(db_path: str, table: str) -> str:
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    conn.close()
    return row[0] if row else "(テーブルなし)"


def get_count(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cnt = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        cnt = -1
    conn.close()
    return cnt


def get_indexes(db_path: str, table: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows if r[0]]


# ============================================================
# スキーマ比較
# ============================================================
def compare_schemas(src: str, dst: str, table: str) -> bool:
    """True = 互換あり（カラム名・型が一致）"""
    src_cols = get_schema(src, table)
    dst_cols = get_schema(dst, table)

    print("\n" + "=" * 60)
    print(f"  スキーマ比較: {table}")
    print("=" * 60)

    print(f"\n[SRC] {src}  ({get_count(src, table)} rows)")
    for c in src_cols:
        print(f"  {c[0]:2d}  {c[1]:<30s}  {c[2]:<15s}  notnull={c[3]}  pk={c[5]}")

    print(f"\n[DST] {dst}  ({get_count(dst, table)} rows)")
    for c in dst_cols:
        print(f"  {c[0]:2d}  {c[1]:<30s}  {c[2]:<15s}  notnull={c[3]}  pk={c[5]}")

    src_names = {c[1] for c in src_cols}
    dst_names = {c[1] for c in dst_cols}
    only_src  = src_names - dst_names
    only_dst  = dst_names - src_names

    print(f"\n[DIFF]")
    if only_src:
        print(f"  SRC のみ: {only_src}")
    if only_dst:
        print(f"  DST のみ: {only_dst}")
    if not only_src and not only_dst:
        print("  カラム差異なし ✅")

    # 共通カラムの型不一致
    src_map = {c[1]: c[2] for c in src_cols}
    dst_map = {c[1]: c[2] for c in dst_cols}
    type_mismatches = []
    for name in src_names & dst_names:
        if src_map[name].upper() != dst_map[name].upper():
            type_mismatches.append(
                f"  {name}: SRC={src_map[name]} / DST={dst_map[name]}"
            )
    if type_mismatches:
        print("  型不一致:")
        for m in type_mismatches:
            print(m)
    else:
        print("  型差異なし ✅")

    compatible = not only_src and not type_mismatches
    print(f"\n互換性: {'✅ 互換あり' if compatible else '⚠️  差異あり（要確認）'}")
    return compatible


# ============================================================
# 移行
# ============================================================
def migrate(src_path: str, dst_path: str, table: str, dry_run: bool) -> dict:
    """SRC の order_metrics を DST へ INSERT OR IGNORE で移行する。"""
    src_conn = sqlite3.connect(src_path)
    src_conn.row_factory = sqlite3.Row
    src_rows = src_conn.execute(f"SELECT * FROM {table}").fetchall()
    src_conn.close()

    total     = len(src_rows)
    inserted  = 0
    skipped   = 0
    errors    = 0

    if total == 0:
        logger.info("[MIGRATE] SRC rows=0 — 移行対象なし")
        return {"total": 0, "inserted": 0, "skipped": 0, "errors": 0}

    # カラム名を SRC から取得
    col_names = list(src_rows[0].keys())
    placeholders = ", ".join("?" for _ in col_names)
    col_list = ", ".join(col_names)

    logger.info(f"[MIGRATE] SRC={src_path} → DST={dst_path}")
    logger.info(f"[MIGRATE] 移行対象: {total} rows | dry_run={dry_run}")

    if dry_run:
        # dry-run: SRC全件と DST既存件数を表示するのみ
        dst_cnt = get_count(dst_path, table)
        logger.info(f"[DRY-RUN] DST 現在: {dst_cnt} rows")
        logger.info(f"[DRY-RUN] 移行予定: {total} rows (重複は INSERT OR IGNORE でスキップ)")

        # 重複数の事前計算
        dst_conn = sqlite3.connect(dst_path)
        dst_conn.row_factory = sqlite3.Row
        for row in src_rows:
            try:
                existing = dst_conn.execute(
                    f"SELECT COUNT(*) FROM {table} "
                    f"WHERE company_code=? AND fiscal_year_end=? AND quarter=? AND metric_name=?",
                    (row["company_code"], row["fiscal_year_end"],
                     row["quarter"], row["metric_name"]),
                ).fetchone()[0]
                if existing:
                    skipped += 1
                else:
                    inserted += 1
            except Exception as e:
                errors += 1
        dst_conn.close()

        logger.info(f"[DRY-RUN] 予測: inserted={inserted} / skipped={skipped} / errors={errors}")
        return {"total": total, "inserted": inserted, "skipped": skipped, "errors": errors}

    # 実移行
    dst_conn = sqlite3.connect(dst_path)
    try:
        for row in src_rows:
            vals = [row[c] for c in col_names]
            try:
                dst_conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})",
                    vals,
                )
                # rowcount=1 なら inserted、0 なら UNIQUE 衝突でスキップ
                if dst_conn.execute("SELECT changes()").fetchone()[0] > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as e:
                errors += 1
                logger.warning(
                    f"[MIGRATE] ERROR: company_code={row.get('company_code')} "
                    f"fy={row.get('fiscal_year_end')} q={row.get('quarter')} "
                    f"metric={row.get('metric_name')}: {e}"
                )

        dst_conn.commit()
        logger.info(
            f"[MIGRATE] 完了: total={total} inserted={inserted} "
            f"skipped={skipped} errors={errors}"
        )
    finally:
        dst_conn.close()

    return {"total": total, "inserted": inserted, "skipped": skipped, "errors": errors}


# ============================================================
# 移行後 検証
# ============================================================
def verify(dst_path: str, table: str, expected_min: int) -> None:
    cnt = get_count(dst_path, table)
    status = "✅" if cnt >= expected_min else "⚠️ "
    print(f"\n[VERIFY] {dst_path} — {table}: {cnt} rows  (期待最小: {expected_min}) {status}")

    # サンプル表示
    conn = sqlite3.connect(dst_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT company_code, fiscal_year_end, quarter, metric_name, value "
        f"FROM {table} ORDER BY created_at DESC LIMIT 5"
    ).fetchall()
    conn.close()
    print("  最新5件:")
    for r in rows:
        print(f"    {r['company_code']} {r['fiscal_year_end']} {r['quarter']} "
              f"{r['metric_name']}={r['value']}")


# ============================================================
# エントリポイント
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="order_metrics: data/decision_db.db → decision_db.db 移行"
    )
    parser.add_argument("--src", default=_SRC_DB, help="移行元DB")
    parser.add_argument("--dst", default=_DST_DB, help="移行先DB")
    parser.add_argument("--compare-only", action="store_true", help="スキーマ比較のみ")
    parser.add_argument("--dry-run", action="store_true", help="件数確認のみ（書き込みなし）")
    args = parser.parse_args()

    # 存在確認
    for label, path in [("SRC", args.src), ("DST", args.dst)]:
        if not os.path.isfile(path):
            logger.error(f"{label} DB が見つかりません: {path}")
            sys.exit(1)
        logger.info(f"{label}: {path}  ({os.path.getsize(path):,} bytes)")

    # スキーマ比較
    compatible = compare_schemas(args.src, args.dst, _TABLE)

    if args.compare_only:
        print("\n--compare-only: スキーマ比較のみ完了。移行は実施しません。")
        sys.exit(0)

    if not compatible:
        print("\n⚠️  スキーマに差異があります。移行を中断します。")
        print("   差異を確認してから再実行してください。")
        sys.exit(1)

    # 移行
    result = migrate(args.src, args.dst, _TABLE, dry_run=args.dry_run)

    # 検証
    if not args.dry_run:
        src_cnt = get_count(args.src, _TABLE)
        verify(args.dst, _TABLE, expected_min=src_cnt)

    # サマリ
    print("\n" + "=" * 60)
    print("  MIGRATION SUMMARY")
    print("=" * 60)
    print(f"  mode      : {'DRY-RUN' if args.dry_run else 'LIVE'}")
    print(f"  src       : {args.src}")
    print(f"  dst       : {args.dst}")
    print(f"  total     : {result['total']}")
    print(f"  inserted  : {result['inserted']}")
    print(f"  skipped   : {result['skipped']}")
    print(f"  errors    : {result['errors']}")
    print("=" * 60)

    if args.dry_run:
        print("\n本番移行するには --dry-run を外して再実行してください。")


if __name__ == "__main__":
    main()
