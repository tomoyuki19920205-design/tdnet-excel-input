#!/usr/bin/env python3
"""cleanup_intermediate_data.py — SQLite 中間データ一括削除ツール

蓄積済みの中間データ（migration_log / quarantine / extracted_facts）を
削除し、VACUUM でファイルサイズを縮小する。

Usage:
  python tools/cleanup_intermediate_data.py                              # dry-run
  python tools/cleanup_intermediate_data.py --execute                    # 削除実行
  python tools/cleanup_intermediate_data.py --execute --vacuum           # 削除 + VACUUM
  python tools/cleanup_intermediate_data.py --execute --include-quarantine-db  # quarantine.db も
  python tools/cleanup_intermediate_data.py --execute --include-audit-log     # audit_log も
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger("cleanup")

# ============================================================
# 定数
# ============================================================

# デフォルト削除対象テーブル（decision_db.db 内）
_DEFAULT_TARGETS = [
    "migration_log",
    "quarantine",
    "extracted_facts",
]

# オプション対象
_OPTIONAL_TARGETS = {
    "audit_log": "audit_log",
}


# ============================================================
# ユーティリティ
# ============================================================


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """テーブルが存在するか確認する。"""
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    """テーブルの行数を取得する。"""
    if not _table_exists(conn, table):
        return -1
    cur = conn.execute(f"SELECT COUNT(*) FROM [{table}]")
    return cur.fetchone()[0]


def _db_file_size(path: str) -> int:
    """DB ファイルサイズを取得する。"""
    return os.path.getsize(path) if os.path.exists(path) else 0


def _db_page_info(conn: sqlite3.Connection) -> dict:
    """DB の page_count / freelist_count を取得する。"""
    info = {}
    for pragma in ("page_count", "freelist_count", "page_size"):
        cur = conn.execute(f"PRAGMA {pragma}")
        row = cur.fetchone()
        info[pragma] = row[0] if row else 0
    return info


# ============================================================
# メイン処理
# ============================================================


def cleanup_db(
    db_path: str,
    tables: list[str],
    *,
    execute: bool = False,
    vacuum: bool = False,
    label: str = "",
) -> dict:
    """指定 DB の指定テーブルを削除する。

    Returns:
        {"table": {"before": N, "after": N}, ..., "file_size_before": N, "file_size_after": N}
    """
    if not os.path.exists(db_path):
        logger.warning(f"DB not found: {db_path}")
        return {}

    result: dict = {}
    file_size_before = _db_file_size(db_path)
    result["file_size_before"] = file_size_before

    conn = sqlite3.connect(db_path)
    page_info_before = _db_page_info(conn)

    for table in tables:
        count = _count_rows(conn, table)
        if count < 0:
            logger.info(f"  [{label}] {table}: テーブル不存在 → skip")
            result[table] = {"before": 0, "after": 0, "exists": False}
            continue

        result[table] = {"before": count, "exists": True}

        if execute and count > 0:
            conn.execute(f"DELETE FROM [{table}]")
            conn.commit()
            logger.info(f"  [{label}] {table}: {count:,}行 削除完了")
        else:
            logger.info(f"  [{label}] {table}: {count:,}行 {'(dry-run)' if count > 0 else ''}")

        after = _count_rows(conn, table)
        result[table]["after"] = after

    if execute and vacuum:
        logger.info(f"  [{label}] VACUUM 実行中...")
        conn.execute("VACUUM")
        logger.info(f"  [{label}] VACUUM 完了")

    conn.close()

    file_size_after = _db_file_size(db_path)
    result["file_size_after"] = file_size_after

    return result


def print_report(results: dict[str, dict], db_labels: dict[str, str]) -> None:
    """削除レポートを表示する。"""
    print("\n" + "=" * 60)
    print("Cleanup Report")
    print("=" * 60)

    for db_path, info in results.items():
        label = db_labels.get(db_path, db_path)
        size_before = info.get("file_size_before", 0)
        size_after = info.get("file_size_after", 0)
        print(f"\n--- {label} ({db_path}) ---")
        print(f"  File size: {size_before / 1024:.1f}KB → {size_after / 1024:.1f}KB "
              f"(delta: {(size_after - size_before) / 1024:+.1f}KB)")

        for key, val in info.items():
            if isinstance(val, dict) and "before" in val:
                if not val.get("exists", True):
                    print(f"  {key}: 不存在")
                else:
                    print(f"  {key}: {val['before']:,}行 → {val['after']:,}行")

    print("\n" + "=" * 60)


# ============================================================
# CLI
# ============================================================


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SQLite 中間データ一括削除ツール",
    )
    parser.add_argument("--db-path", default="decision_db.db",
                        help="メイン DB パス")
    parser.add_argument("--execute", action="store_true",
                        help="実際に削除を実行する (省略時は dry-run)")
    parser.add_argument("--vacuum", action="store_true",
                        help="削除後に VACUUM を実行する")
    parser.add_argument("--include-quarantine-db", action="store_true",
                        help="data/quarantine.db も対象にする")
    parser.add_argument("--include-audit-log", action="store_true",
                        help="audit_log テーブルも削除対象にする")
    parser.add_argument("--yes", action="store_true",
                        help="確認プロンプトをスキップする")

    opts = parser.parse_args(args)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 削除対象テーブル
    targets = list(_DEFAULT_TARGETS)
    if opts.include_audit_log:
        targets.append("audit_log")

    # 確認
    mode = "EXECUTE" if opts.execute else "DRY-RUN"
    logger.info(f"モード: {mode}")
    logger.info(f"対象テーブル: {', '.join(targets)}")

    if opts.execute and not opts.yes:
        print(f"\n⚠️  {mode} モードで以下のテーブルを削除します:")
        for t in targets:
            print(f"  - {t}")
        if opts.include_quarantine_db:
            print(f"  - quarantine (quarantine.db)")
        if opts.vacuum:
            print(f"  + VACUUM 実行")
        answer = input("\n続行しますか？ (y/N): ").strip().lower()
        if answer not in ("y", "yes"):
            print("中断しました。")
            return 1

    results: dict[str, dict] = {}
    db_labels: dict[str, str] = {}

    # メイン DB
    db_path = opts.db_path
    db_labels[db_path] = "Decision DB"
    logger.info(f"Processing: {db_path}")
    results[db_path] = cleanup_db(
        db_path, targets,
        execute=opts.execute, vacuum=opts.vacuum,
        label="decision_db",
    )

    # quarantine.db
    if opts.include_quarantine_db:
        q_path = "data/quarantine.db"
        db_labels[q_path] = "Quarantine DB"
        logger.info(f"Processing: {q_path}")
        results[q_path] = cleanup_db(
            q_path, ["quarantine"],
            execute=opts.execute, vacuum=opts.vacuum,
            label="quarantine_db",
        )

    # レポート
    print_report(results, db_labels)

    return 0


if __name__ == "__main__":
    sys.exit(main())
