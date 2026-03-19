#!/usr/bin/env python3
# ============================================================
# migrate_db.py — XBRL ETL用 SQLiteスキーマ適用CLI
# ============================================================
#
# 使い方:
#   python -m tools.migrate_db --db data/xbrl.db
#
# ============================================================
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCHEMA_PATH = os.path.join(_PROJECT_ROOT, "schema.sql")


def migrate(db_path: str, schema_path: str = _SCHEMA_PATH) -> None:
    """
    SQLiteデータベースにschema.sqlを適用する。

    - PRAGMA foreign_keys=ON を設定
    - CREATE TABLE IF NOT EXISTS / CREATE VIEW IF NOT EXISTS のため冪等
    """
    if not os.path.isfile(schema_path):
        print(f"[ERROR] schema.sql が見つかりません: {schema_path}", file=sys.stderr)
        sys.exit(1)

    with open(schema_path, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    # DB作成（親ディレクトリも作成）
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(schema_sql)
        conn.commit()

        # テーブル一覧を取得して表示
        cur = conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('table','view') ORDER BY type, name"
        )
        items = cur.fetchall()
        tables = [name for typ, name in items if typ == "table"]
        views = [name for typ, name in items if typ == "view"]

        print(f"[OK] スキーマ適用完了: {db_path}")
        print(f"  テーブル ({len(tables)}): {', '.join(tables)}")
        print(f"  ビュー   ({len(views)}): {', '.join(views)}")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="XBRL ETL用 SQLiteスキーマ適用"
    )
    parser.add_argument(
        "--db", type=str, required=True,
        help="SQLiteデータベースファイルパス（なければ新規作成）",
    )
    parser.add_argument(
        "--schema", type=str, default=_SCHEMA_PATH,
        help=f"schema.sqlのパス（デフォルト: {_SCHEMA_PATH}）",
    )
    args = parser.parse_args()
    migrate(args.db, args.schema)


if __name__ == "__main__":
    main()
