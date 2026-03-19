#!/usr/bin/env python3
# ============================================================
# reprocess_ticker.py — 特定 ticker の再処理
# ============================================================
"""
特定 ticker の既処理フラグと誤データをクリアし、再 ingest する。

手順:
1. state.db (processing_log) から該当 ticker の処理済みフラグを削除
2. decision_db.db (quarterly_results) から該当 ticker の行を削除
3. 該当 ticker を指定して run_ingest() を再実行
4. sqlite_to_supabase push を再実行

Usage:
    python tools/reprocess_ticker.py 2301
    python tools/reprocess_ticker.py 2301 --dry-run
    python tools/reprocess_ticker.py 2301 --skip-push
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.config import Config
from tools.tdnet_ingest import run_ingest

logger = logging.getLogger("reprocess")


def _clear_state_db(ticker: str, *, dry_run: bool = False) -> int:
    """state.db の processing_log から該当 ticker の行を削除"""
    state_path = os.path.join(_PROJECT_ROOT, "data", "state.db")
    if not os.path.exists(state_path):
        logger.warning(f"state.db not found: {state_path}")
        return 0

    conn = sqlite3.connect(state_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM processing_log WHERE code = ?",
        (ticker,),
    ).fetchone()[0]

    if count == 0:
        logger.info(f"[STATE] ticker={ticker}: 処理済みレコードなし")
        conn.close()
        return 0

    if dry_run:
        logger.info(
            f"[STATE] ticker={ticker}: {count} 件削除予定 (dry-run)"
        )
    else:
        conn.execute(
            "DELETE FROM processing_log WHERE code = ?",
            (ticker,),
        )
        conn.commit()
        logger.info(
            f"[STATE] ticker={ticker}: {count} 件削除完了"
        )

    conn.close()
    return count


def _clear_quarterly_results(
    ticker: str, *, dry_run: bool = False,
) -> int:
    """decision_db.db の quarterly_results から該当 ticker の行を削除"""
    db_path = os.path.join(_PROJECT_ROOT, "decision_db.db")
    if not os.path.exists(db_path):
        logger.warning(f"decision_db.db not found: {db_path}")
        return 0

    conn = sqlite3.connect(db_path)

    # 削除前に内容を表示
    rows = conn.execute(
        "SELECT id, company_code, fiscal_year_end, quarter, sales, "
        "operating_profit FROM quarterly_results "
        "WHERE company_code = ?",
        (ticker,),
    ).fetchall()

    if not rows:
        logger.info(
            f"[RESULTS] ticker={ticker}: quarterly_results にレコードなし"
        )
        conn.close()
        return 0

    for r in rows:
        logger.info(
            f"[RESULTS] 削除対象: id={r[0]} ticker={r[1]} "
            f"period={r[2]} quarter={r[3]} "
            f"sales={r[4]} op={r[5]}"
        )

    if dry_run:
        logger.info(
            f"[RESULTS] ticker={ticker}: {len(rows)} 件削除予定 (dry-run)"
        )
    else:
        conn.execute(
            "DELETE FROM quarterly_results WHERE company_code = ?",
            (ticker,),
        )
        conn.commit()
        logger.info(
            f"[RESULTS] ticker={ticker}: {len(rows)} 件削除完了"
        )

    conn.close()
    return len(rows)


def reprocess(
    ticker: str,
    *,
    dry_run: bool = False,
    skip_push: bool = False,
) -> dict:
    """
    特定 ticker を再処理する。

    Returns:
        {"ticker": str, "cleared_state": int, "cleared_results": int,
         "ingest": dict, "push": dict | None}
    """
    result: dict = {
        "ticker": ticker,
        "cleared_state": 0,
        "cleared_results": 0,
        "ingest": {},
        "push": None,
    }

    print()
    print("=" * 55)
    print(f"  Reprocess ticker: {ticker}")
    print("=" * 55)

    # Step 1: state.db クリア
    logger.info(f"[Step 1/3] state.db クリア (ticker={ticker})")
    result["cleared_state"] = _clear_state_db(
        ticker, dry_run=dry_run,
    )

    # Step 2: quarterly_results クリア
    logger.info(f"[Step 2/3] quarterly_results クリア (ticker={ticker})")
    result["cleared_results"] = _clear_quarterly_results(
        ticker, dry_run=dry_run,
    )

    # Step 3: 再 ingest
    logger.info(f"[Step 3/3] 再 ingest (ticker={ticker})")
    config = Config()
    try:
        ingest_result = run_ingest(
            config,
            company_code=ticker,
            dry_run=dry_run,
        )
        result["ingest"] = ingest_result
        summary = ingest_result.get("summary", {})
        logger.info(
            f"[INGEST] total={ingest_result.get('total', 0)} "
            f"success={summary.get('succeeded', 0)} "
            f"errors={summary.get('errors', 0)}"
        )
    except Exception as e:
        logger.error(f"[INGEST] 失敗: {e}")
        result["ingest"] = {"status": "error", "error": str(e)}

    # Step 4: push (optional)
    if not skip_push and not dry_run:
        logger.info("[Step 4] Supabase push")
        try:
            from tools.sqlite_to_supabase import push_sqlite_to_supabase
            db_path = os.path.join(_PROJECT_ROOT, "decision_db.db")
            push_stats = push_sqlite_to_supabase(db_path=db_path)
            result["push"] = push_stats
            logger.info(
                f"[PUSH] financials_upserted="
                f"{push_stats.get('financials_inserted', 0)} "
                f"errors={push_stats.get('errors', 0)}"
            )
        except Exception as e:
            logger.error(f"[PUSH] 失敗: {e}")
            result["push"] = {"status": "error", "error": str(e)}

    # 確認: quarterly_results の最新状態
    db_path = os.path.join(_PROJECT_ROOT, "decision_db.db")
    if os.path.exists(db_path) and not dry_run:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT company_code, fiscal_year_end, quarter, sales, "
            "operating_profit FROM quarterly_results "
            "WHERE company_code = ?",
            (ticker,),
        ).fetchall()
        conn.close()
        if rows:
            print()
            print(f"  [確認] quarterly_results (ticker={ticker}):")
            for r in rows:
                print(
                    f"    {r[0]} | {r[1]} | {r[2]} | "
                    f"sales={r[3]} | op={r[4]}"
                )
        else:
            print(f"  [確認] quarterly_results に {ticker} なし")

    print()
    print("=" * 55)
    print(f"  Reprocess complete: {ticker}")
    print("=" * 55)
    print()

    return result


def main():
    if sys.stdout and hasattr(sys.stdout, "encoding"):
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in (
            "utf-8", "utf8"
        ):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8",
                errors="replace",
            )

    parser = argparse.ArgumentParser(
        description="特定 ticker の再処理",
    )
    parser.add_argument(
        "ticker",
        help="再処理する銘柄コード (例: 2301)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="削除・書き込みをスキップ",
    )
    parser.add_argument(
        "--skip-push", action="store_true",
        help="Supabase push をスキップ",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    result = reprocess(
        args.ticker,
        dry_run=args.dry_run,
        skip_push=args.skip_push,
    )

    # 終了コード
    ingest = result.get("ingest", {})
    if isinstance(ingest, dict) and ingest.get("status") == "error":
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
