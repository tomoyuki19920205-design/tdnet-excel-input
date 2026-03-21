#!/usr/bin/env python3
# ============================================================
# ingest_monitor.py — 取りこぼし監視・ingest ヘルスチェック
# ============================================================
"""
TDnet ingest の取りこぼしを検知する監視ツール。

Usage:
    python tools/ingest_monitor.py --check-today     # 当日分サマリ
    python tools/ingest_monitor.py --check-date 2026-03-16  # 特定日の照合
    python tools/ingest_monitor.py --health           # ingest ヘルスチェック
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.fetcher import fetch_new_disclosures, classify_disclosure, _parse_target_date
from src.models import DisclosureType
from src.utils import today_yyyymmdd

logger = logging.getLogger("monitor")
JST = timezone(timedelta(hours=9))


def _find_db() -> str:
    """decision_db.db を探す。"""
    for p in [
        Path(_PROJECT_ROOT) / "decision_db.db",
        Path(r"C:\Users\takuy\OneDrive\tdnet-excel-input\decision_db.db"),
    ]:
        if p.exists():
            return str(p)
    raise FileNotFoundError("decision_db.db が見つかりません")


def _find_state_db() -> str:
    """state.db を探す。"""
    for p in [
        Path(_PROJECT_ROOT) / "data" / "state.db",
        Path(r"C:\Users\takuy\OneDrive\tdnet-excel-input\data\state.db"),
    ]:
        if p.exists():
            return str(p)
    raise FileNotFoundError("state.db が見つかりません")


# ============================================================
# 当日分サマリ
# ============================================================

def check_today_summary(target_date: str | None = None) -> dict:
    """当日（または指定日）の ingest サマリを生成。

    Returns:
        {tdnet_seen, tanshin_count, system_inserted, system_total,
         missing_suspects, zero_warning}
    """
    date_str = _parse_target_date(target_date)
    is_today = (date_str == today_yyyymmdd())

    # TDnet から当日分を直接取得して件数チェック
    try:
        items = fetch_new_disclosures(target_date=target_date or None)
        tdnet_seen = len(items)
        tanshin_items = [
            i for i in items
            if i.disclosure_type == DisclosureType.FINANCIAL_STATEMENT
        ]
        tanshin_count = len(tanshin_items)
    except Exception as e:
        logger.warning(f"TDnet 取得エラー: {e}")
        tdnet_seen = -1
        tanshin_count = -1
        tanshin_items = []

    # state.db の processing_log からその日の処理件数を取得
    system_inserted = 0
    system_total = 0
    try:
        state_db_path = _find_state_db()
        conn = sqlite3.connect(state_db_path)
        cur = conn.cursor()
        iso_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        cur.execute(
            "SELECT COUNT(*) FROM processing_log WHERE created_at LIKE ?",
            (f"{iso_date}%",)
        )
        system_total = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM processing_log WHERE created_at LIKE ? AND status = 'success'",
            (f"{iso_date}%",)
        )
        system_inserted = cur.fetchone()[0]
        conn.close()
    except Exception as e:
        logger.warning(f"state.db 読み取りエラー: {e}")

    # 取りこぼし候補検出
    missing_suspects = []
    if tanshin_count > 0:
        try:
            state_db_path = _find_state_db()
            conn = sqlite3.connect(state_db_path)
            cur = conn.cursor()
            for item in tanshin_items:
                cur.execute(
                    "SELECT COUNT(*) FROM processing_log WHERE disclosure_id = ?",
                    (item.disclosure_id,)
                )
                if cur.fetchone()[0] == 0:
                    missing_suspects.append({
                        "ticker": item.ticker,
                        "title": item.title[:60],
                        "disclosure_id": item.disclosure_id[:16],
                    })
            conn.close()
        except Exception as e:
            logger.warning(f"missing 検出エラー: {e}")

    # 0件警告
    zero_warning = False
    if is_today and tanshin_count == 0:
        now = datetime.now(JST)
        # 平日16時以降なのに0件 → 警告
        if now.weekday() < 5 and now.hour >= 16:
            zero_warning = True

    summary = {
        "target_date": date_str,
        "tdnet_seen": tdnet_seen,
        "tanshin_count": tanshin_count,
        "system_inserted": system_inserted,
        "system_total": system_total,
        "missing_suspects": len(missing_suspects),
        "missing_details": missing_suspects[:10],
        "zero_warning": zero_warning,
    }

    return summary


def print_check_summary(summary: dict) -> None:
    """サマリを表示。"""
    print()
    print("=" * 55)
    print(f"  INGEST MONITOR — {summary['target_date']}")
    print("=" * 55)
    for label, key in [
        ("TDnet 開示総数", "tdnet_seen"),
        ("決算短信数", "tanshin_count"),
        ("システム処理数", "system_total"),
        ("システム成功数", "system_inserted"),
        ("取りこぼし候補", "missing_suspects"),
    ]:
        val = summary.get(key, "?")
        warn = " ⚠" if key == "missing_suspects" and val > 0 else ""
        print(f"  {label:18s}: {val}{warn}")

    if summary.get("zero_warning"):
        print("  ⚠ 0件警告: 平日の引け後なのに決算短信が0件です")

    if summary.get("missing_details"):
        print()
        print("  [取りこぼし候補一覧]")
        for m in summary["missing_details"]:
            print(f"    {m['ticker']} | {m['title']}")

    print("=" * 55)

    # grep 用ログ
    log_line = (
        f"[MONITOR] date={summary['target_date']} "
        f"tdnet_seen={summary['tdnet_seen']} "
        f"tanshin={summary['tanshin_count']} "
        f"system_total={summary['system_total']} "
        f"system_ok={summary['system_inserted']} "
        f"missing={summary['missing_suspects']} "
        f"zero_warning={summary['zero_warning']}"
    )
    print(log_line)
    logger.info(log_line)


# ============================================================
# ヘルスチェック
# ============================================================

def health_check() -> dict:
    """パイプラインヘルスチェック。"""
    results = {}

    # 1. state.db 最終成功時刻
    try:
        state_db_path = _find_state_db()
        conn = sqlite3.connect(state_db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT MAX(created_at) FROM processing_log WHERE status = 'success'"
        )
        row = cur.fetchone()
        last_success = row[0] if row else None
        results["last_success_at"] = last_success

        # 最終成功から何時間経過
        if last_success:
            try:
                last_dt = datetime.strptime(last_success, "%Y-%m-%d %H:%M:%S")
                elapsed_hours = (datetime.now() - last_dt).total_seconds() / 3600
                results["hours_since_last_success"] = round(elapsed_hours, 1)
                if elapsed_hours > 24:
                    results["health_warning"] = f"最終成功から{elapsed_hours:.0f}時間経過"
            except Exception:
                pass

        cur.execute("SELECT COUNT(*) FROM processing_log")
        results["total_processed"] = cur.fetchone()[0]
        conn.close()
    except Exception as e:
        results["state_db_error"] = str(e)

    # 2. decision_db.db 件数
    try:
        db_path = _find_db()
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM quarterly_results")
        results["quarterly_results_count"] = cur.fetchone()[0]
        conn.close()
    except Exception as e:
        results["decision_db_error"] = str(e)

    return results


def print_health(results: dict) -> None:
    """ヘルスチェック結果を表示。"""
    print()
    print("=" * 55)
    print("  PIPELINE HEALTH CHECK")
    print("=" * 55)
    for k, v in results.items():
        warn = " ⚠" if "warning" in k or "error" in k else ""
        print(f"  {k:30s}: {v}{warn}")
    print("=" * 55)


# ============================================================
# CLI
# ============================================================

def main():
    if sys.stdout and hasattr(sys.stdout, "encoding"):
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace",
            )

    parser = argparse.ArgumentParser(description="Ingest 取りこぼし監視")
    parser.add_argument("--check-today", action="store_true", help="当日分サマリ")
    parser.add_argument("--check-date", type=str, default=None, help="指定日の照合")
    parser.add_argument("--health", action="store_true", help="ヘルスチェック")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.health:
        results = health_check()
        print_health(results)
    elif args.check_date:
        summary = check_today_summary(target_date=args.check_date)
        print_check_summary(summary)
    elif args.check_today:
        summary = check_today_summary()
        print_check_summary(summary)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
