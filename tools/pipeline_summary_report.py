#!/usr/bin/env python3
"""
pipeline_summary_report.py — パイプライン集計レポート

state DB / segment_canonical から現在の状態を集計し、
コンソール表示 + JSON 出力する。

Usage:
  .\\.venv\\Scripts\\python.exe tools\\pipeline_summary_report.py
  .\\.venv\\Scripts\\python.exe tools\\pipeline_summary_report.py --json
"""
from __future__ import annotations

import argparse
import json
import io
import logging
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger("summary_report")


def _count_states(state_db: str) -> dict:
    """state DB から status 別件数を集計。"""
    conn = sqlite3.connect(state_db)
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM filing_state GROUP BY status"
    ).fetchall()
    counts = {r[0]: r[1] for r in rows}

    # review_hint 内訳
    hint_rows = conn.execute(
        "SELECT review_hint, COUNT(*) FROM filing_state "
        "WHERE status = 'quarantined' AND review_hint IS NOT NULL "
        "GROUP BY review_hint ORDER BY COUNT(*) DESC"
    ).fetchall()
    hint_breakdown = {r[0]: r[1] for r in hint_rows}

    conn.close()
    return {
        "counts": counts,
        "hint_breakdown": hint_breakdown,
    }


def _count_parse_quality(db_path: str) -> dict:
    """segment_canonical から parse_quality 別件数を集計。"""
    if not os.path.exists(db_path):
        return {}
    try:
        conn = sqlite3.connect(db_path)
        # parse_quality カラムがあるか確認
        columns = [r[1] for r in conn.execute("PRAGMA table_info(segment_canonical)").fetchall()]
        if "parse_quality" not in columns:
            conn.close()
            return {}
        rows = conn.execute(
            "SELECT parse_quality, COUNT(*) FROM segment_canonical "
            "GROUP BY parse_quality"
        ).fetchall()
        conn.close()
        return {r[0] or "unknown": r[1] for r in rows}
    except Exception:
        return {}


def generate_report(state_db: str, data_db: str | None = None) -> dict:
    """集計レポートを生成。"""
    state_info = _count_states(state_db)
    counts = state_info["counts"]

    total = sum(counts.values())
    upserted = counts.get("upserted", 0)
    quarantined = counts.get("quarantined", 0)
    pending = counts.get("pending", 0)
    success_rate = (upserted / total * 100) if total > 0 else 0

    report = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_processed": total,
        "upserted": upserted,
        "quarantined": quarantined,
        "pending": pending,
        "success_rate": round(success_rate, 1),
        "review_hint_breakdown": state_info["hint_breakdown"],
    }

    if data_db:
        pq = _count_parse_quality(data_db)
        if pq:
            report["parse_quality_breakdown"] = pq

    return report


def print_report(report: dict) -> None:
    """コンソール表示。"""
    print()
    print("=" * 55)
    print("  Pipeline Summary Report")
    print("=" * 55)
    print(f"  date                : {report['date']}")
    print(f"  total_processed     : {report['total_processed']}")
    print(f"  upserted            : {report['upserted']}")
    print(f"  quarantined         : {report['quarantined']}")
    print(f"  pending             : {report.get('pending', 0)}")
    print(f"  success_rate        : {report['success_rate']}%")

    hints = report.get("review_hint_breakdown", {})
    if hints:
        print()
        print(f"  review_hint_breakdown:")
        for hint, cnt in sorted(hints.items(), key=lambda x: -x[1]):
            print(f"    {hint:40s}: {cnt}")

    pq = report.get("parse_quality_breakdown", {})
    if pq:
        print()
        print(f"  parse_quality_breakdown:")
        for quality, cnt in sorted(pq.items(), key=lambda x: -x[1]):
            print(f"    {quality:40s}: {cnt}")

    print("=" * 55)
    print()


def main(args: list[str] | None = None) -> int:
    # UTF-8 出力確保
    if sys.stdout and hasattr(sys.stdout, "encoding"):
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace",
            )

    parser = argparse.ArgumentParser(description="パイプライン集計レポート")
    parser.add_argument("--state-db", default="data/backfill_state.db", help="state DB path")
    parser.add_argument("--data-db", default="data/quarterly_results.db", help="data DB path")
    parser.add_argument("--json", action="store_true", help="JSON 出力")
    parser.add_argument("--save", action="store_true", help="logs/ に JSON 保存")
    opts = parser.parse_args(args)

    state_db = os.path.join(_PROJECT_ROOT, opts.state_db)
    data_db = os.path.join(_PROJECT_ROOT, opts.data_db)

    if not os.path.exists(state_db):
        print(f"[ERROR] state DB not found: {state_db}")
        return 1

    report = generate_report(state_db, data_db if os.path.exists(data_db) else None)

    if opts.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)

    if opts.save:
        log_dir = os.path.join(_PROJECT_ROOT, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "daily_pipeline_summary.json")
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"  report saved: {log_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
