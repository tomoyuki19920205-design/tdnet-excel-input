# ============================================================
# migrate.py — CLI エントリーポイント
# ============================================================
"""
使用例:
  python -m src.migration.migrate --excel path/to/copy.xlsx --sheet PL --db data/earnings.db
  python -m src.migration.migrate --excel path/to/copy.xlsx --sheet PL --db data/earnings.db --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
import uuid
from datetime import datetime, timezone, timedelta

from .excel_parser import parse_excel
from .migration_db import MigrationDB
from .migrator import run_migration

JST = timezone(timedelta(hours=9))


def _generate_run_id() -> str:
    """実行IDを自動採番する: YYYYMMDD-HHMMSS-xxxxxxxx"""
    ts = datetime.now(JST).strftime("%Y%m%d-%H%M%S")
    short_uuid = uuid.uuid4().hex[:8]
    return f"{ts}-{short_uuid}"


def _setup_logger() -> logging.Logger:
    """コンソールロガーをセットアップ"""
    logger = logging.getLogger("migration")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(ch)
    return logger


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Excel業績シート → SQLite 移行ツール",
    )
    parser.add_argument(
        "--excel", required=True,
        help="対象Excelファイルのパス（専用コピー）",
    )
    parser.add_argument(
        "--sheet", default="PL",
        help="対象シート名（デフォルト: PL）",
    )
    parser.add_argument(
        "--db", required=True,
        help="SQLiteファイルの保存先パス",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="DB書き込みなし、サマリのみ出力",
    )
    opts = parser.parse_args(args)

    logger = _setup_logger()
    run_id = _generate_run_id()

    logger.info("=" * 60)
    logger.info("Excel → DB 移行開始")
    logger.info(f"  Run ID  : {run_id}")
    logger.info(f"  Excel   : {opts.excel}")
    logger.info(f"  Sheet   : {opts.sheet}")
    logger.info(f"  DB      : {opts.db}")
    logger.info(f"  Dry-run : {opts.dry_run}")
    logger.info("=" * 60)

    # 1. Excelパース
    logger.info("Step 1: Excelファイルを読み込み中...")
    try:
        result = parse_excel(opts.excel, opts.sheet)
    except Exception as e:
        logger.error(f"Excelファイルの読み込みに失敗: {e}")
        return 1

    logger.info(
        f"  パース完了: {len(result.blocks)} 社, "
        f"{result.total_rows_scanned} 行走査, "
        f"{len(result.logs)} ログエントリ"
    )

    # 2. DB書き込み
    logger.info("Step 2: DBへ書き込み中...")
    db = MigrationDB(opts.db)
    try:
        summary = run_migration(
            result, db, run_id,
            sheet_name=opts.sheet,
            dry_run=opts.dry_run,
        )
    finally:
        db.close()

    # 3. サマリ出力
    print()
    print(f"Run ID: {run_id}")
    if opts.dry_run:
        print("[DRY-RUN] DB書き込みは行われていません")
    print(summary)

    if summary.errors > 0:
        logger.warning(f"⚠️  {summary.errors} 件のエラーがありました")
        return 1

    logger.info("✅ 移行完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
