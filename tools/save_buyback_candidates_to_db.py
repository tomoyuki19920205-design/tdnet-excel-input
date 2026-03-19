#!/usr/bin/env python3
"""save_buyback_candidates_to_db.py — review_save_candidates.csv → buyback_events DB 保存

review_save_candidates.csv を読み込み、buyback_events テーブルへ安全に upsert する。
dry-run / 件数確認 / upsert 結果ログを備えた最小 auto-save ツール。

Usage:
  cd "C:\\Users\\takuy\\OneDrive\\tdnet-excel-input"
  .\\.venv\\Scripts\\python.exe tools/save_buyback_candidates_to_db.py \\
    --input artifacts/buyback_review_operation/review_save_candidates.csv \\
    --db data/decision_db.db --dry-run
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.events.buyback_models import BuybackEvent
from src.events.buyback_storage import (
    ensure_buyback_table,
    upsert_buyback_event,
    _find_existing_id,
)

# Windows cp932 対策
if sys.stdout and hasattr(sys.stdout, "encoding"):
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

JST = timezone(timedelta(hours=9))
logger = logging.getLogger("save_buyback_to_db")

EXTRACTOR_VERSION = "save_candidates_import_v1"


# ============================================================
# CSV 読み込み
# ============================================================
def load_save_candidates_csv(path: str) -> list[dict]:
    """review_save_candidates.csv を読み込む。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"CSV not found: {path}")
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# ============================================================
# 安全変換
# ============================================================
def _safe_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _safe_int(v: Any) -> int | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(float(str(v).strip()))
    except (ValueError, TypeError):
        return None


def _safe_float(v: Any) -> float | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return None


# ============================================================
# ハッシュ生成
# ============================================================
def build_raw_text_hash(row: dict) -> str:
    """CSV 行から deterministic なハッシュを生成する。"""
    parts = [
        _safe_str(row.get("file_path")),
        _safe_str(row.get("event_type")),
        _safe_str(row.get("disclosure_date")),
        _safe_str(row.get("ticker")),
        _safe_str(row.get("title")),
    ]
    key = "|".join(parts)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# ============================================================
# ソースタイプ推定
# ============================================================
def infer_source_type(file_path: str) -> str:
    if not file_path:
        return "unknown"
    ext = os.path.splitext(file_path)[1].lower()
    return {".pdf": "pdf", ".html": "html", ".htm": "html",
            ".txt": "text", ".xbrl": "xbrl"}.get(ext, "unknown")


# ============================================================
# バリデーション
# ============================================================
def validate_row(row: dict) -> tuple[bool, str]:
    """CSV 行が保存可能か検証する。

    Returns: (is_valid, skip_reason)
    """
    bucket = _safe_str(row.get("review_bucket"))
    if bucket and bucket != "high_confidence_extracted":
        return False, f"review_bucket={bucket}"

    conf = _safe_str(row.get("confidence_final"))
    if not conf:
        return False, "confidence_final_empty"

    event_type = _safe_str(row.get("event_type"))
    if not event_type:
        return False, "event_type_empty"

    file_path = _safe_str(row.get("file_path"))
    file_name = _safe_str(row.get("file_name"))
    if not file_path and not file_name:
        return False, "file_path_and_file_name_empty"

    ticker = _safe_str(row.get("ticker"))
    if not ticker:
        return False, "ticker_empty"

    return True, ""


# ============================================================
# CSV行 → BuybackEvent 変換
# ============================================================
def row_to_buyback_event(row: dict) -> BuybackEvent:
    """CSV 行を BuybackEvent に変換する。"""
    # extracted_json に元CSV情報を埋め込む
    meta = {
        "source": "review_save_candidates_csv",
        "manifest_candidate_score": _safe_str(row.get("manifest_candidate_score")),
        "manifest_review_priority": _safe_str(row.get("manifest_review_priority")),
        "matched_keywords": _safe_str(row.get("matched_keywords")),
        "review_bucket": _safe_str(row.get("review_bucket")),
        "extracted_fields_count": _safe_str(row.get("extracted_fields_count")),
        "missing_key_fields": _safe_str(row.get("missing_key_fields")),
        "save_reason": _safe_str(row.get("save_reason")),
    }

    file_path = _safe_str(row.get("file_path"))
    file_name = _safe_str(row.get("file_name"))
    source_path = file_path or file_name

    return BuybackEvent(
        ticker=_safe_str(row.get("ticker")),
        disclosure_date=_safe_str(row.get("disclosure_date")),
        event_type=_safe_str(row.get("event_type")),
        title=_safe_str(row.get("title")),
        source_type=infer_source_type(source_path),
        source_path=source_path,
        source_doc_id=None,
        source_url=None,
        raw_text_hash=build_raw_text_hash(row),
        shares_limit=_safe_int(row.get("shares_limit")),
        shares_acquired=_safe_int(row.get("shares_acquired")),
        shares_cancelled=_safe_int(row.get("shares_cancelled")),
        amount_limit_million_yen=_safe_float(row.get("amount_limit_million_yen")),
        amount_acquired_million_yen=_safe_float(
            row.get("amount_acquired_million_yen")),
        ratio_to_outstanding=None,
        start_date=_safe_str(row.get("start_date")) or None,
        end_date=_safe_str(row.get("end_date")) or None,
        cancel_date=_safe_str(row.get("cancel_date")) or None,
        acquisition_method=_safe_str(row.get("acquisition_method")) or None,
        board_resolution_date=None,
        status_period_label=None,
        status_notes=None,
        extraction_confidence=_safe_float(row.get("confidence_final")) or 0.0,
        extractor_version=EXTRACTOR_VERSION,
        extracted_json=json.dumps(meta, ensure_ascii=False),
    )


# ============================================================
# メイン保存ロジック
# ============================================================
class SaveResult:
    def __init__(self):
        self.input_rows: int = 0
        self.valid_rows: int = 0
        self.inserted: int = 0
        self.updated: int = 0
        self.skipped: int = 0
        self.errors: int = 0
        self.skipped_rows: list[dict] = []
        self.error_rows: list[dict] = []
        self.event_types: dict[str, int] = {}
        self.tickers: dict[str, int] = {}


def save_candidates_to_db(
    rows: list[dict],
    db_path: str,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> SaveResult:
    """CSV 行を buyback_events DB に保存する。"""
    result = SaveResult()
    result.input_rows = len(rows)

    if limit and limit > 0:
        rows = rows[:limit]

    conn = sqlite3.connect(db_path)
    ensure_buyback_table(conn)

    for i, row in enumerate(rows):
        valid, skip_reason = validate_row(row)
        if not valid:
            result.skipped += 1
            result.skipped_rows.append({
                "row_number": i + 1,
                "file_path": _safe_str(row.get("file_path")),
                "ticker": _safe_str(row.get("ticker")),
                "event_type": _safe_str(row.get("event_type")),
                "skip_reason": skip_reason,
            })
            continue

        result.valid_rows += 1

        try:
            event = row_to_buyback_event(row)

            # event_type / ticker 分布
            et = event.event_type
            result.event_types[et] = result.event_types.get(et, 0) + 1
            tk = event.ticker
            result.tickers[tk] = result.tickers.get(tk, 0) + 1

            # 既存チェック
            existing_id = _find_existing_id(conn, event)

            if dry_run:
                if existing_id:
                    result.updated += 1
                else:
                    result.inserted += 1
            else:
                if existing_id:
                    upsert_buyback_event(conn, event)
                    result.updated += 1
                else:
                    upsert_buyback_event(conn, event)
                    result.inserted += 1
        except Exception as e:
            result.errors += 1
            result.error_rows.append({
                "row_number": i + 1,
                "file_path": _safe_str(row.get("file_path")),
                "ticker": _safe_str(row.get("ticker")),
                "event_type": _safe_str(row.get("event_type")),
                "error_type": type(e).__name__,
                "error_message": str(e),
            })

    conn.close()
    return result


# ============================================================
# 出力
# ============================================================
def write_summary(
    result: SaveResult,
    *,
    input_path: str,
    db_path: str,
    dry_run: bool,
    output_dir: str | None = None,
) -> str:
    """save_to_db_summary.md を生成する。"""
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    mode = "**DRY-RUN**" if dry_run else "LIVE"

    lines = [
        "# Buyback Save to DB — Summary",
        "",
        f"- **実行時刻**: {now}",
        f"- **モード**: {mode}",
        f"- **入力 CSV**: `{input_path}`",
        f"- **DB**: `{db_path}`",
        "",
        "## 結果",
        "",
        "| 項目 | 件数 |",
        "|:---|---:|",
        f"| input rows | {result.input_rows} |",
        f"| valid rows | {result.valid_rows} |",
        f"| **inserted** | **{result.inserted}** |",
        f"| **updated** | **{result.updated}** |",
        f"| skipped | {result.skipped} |",
        f"| errors | {result.errors} |",
        "",
    ]

    if result.event_types:
        lines.append("## event_type 分布")
        lines.append("")
        lines.append("| event_type | 件数 |")
        lines.append("|:---|---:|")
        for et, cnt in sorted(result.event_types.items(), key=lambda x: -x[1]):
            lines.append(f"| {et} | {cnt} |")
        lines.append("")

    if result.tickers:
        lines.append("## ticker 分布 (上位10)")
        lines.append("")
        lines.append("| ticker | 件数 |")
        lines.append("|:---|---:|")
        for tk, cnt in sorted(result.tickers.items(), key=lambda x: -x[1])[:10]:
            lines.append(f"| {tk} | {cnt} |")
        lines.append("")

    md = "\n".join(lines)

    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        path = os.path.join(output_dir, "save_to_db_summary.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        logger.info(f"summary: {path}")

    return md


def write_errors_csv(result: SaveResult, output_dir: str) -> None:
    if not result.error_rows:
        return
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    path = os.path.join(output_dir, "save_to_db_errors.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "row_number", "file_path", "ticker", "event_type",
            "error_type", "error_message",
        ])
        w.writeheader()
        w.writerows(result.error_rows)
    logger.info(f"errors: {path} ({len(result.error_rows)} rows)")


def write_skipped_csv(result: SaveResult, output_dir: str) -> None:
    if not result.skipped_rows:
        return
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    path = os.path.join(output_dir, "save_to_db_skipped.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "row_number", "file_path", "ticker", "event_type", "skip_reason",
        ])
        w.writeheader()
        w.writerows(result.skipped_rows)
    logger.info(f"skipped: {path} ({len(result.skipped_rows)} rows)")


# ============================================================
# メイン
# ============================================================
def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="review_save_candidates.csv → buyback_events DB 保存",
    )
    parser.add_argument("--input", required=True,
                        help="review_save_candidates.csv パス")
    parser.add_argument("--db", default="data/decision_db.db",
                        help="SQLite DB パス")
    parser.add_argument("--dry-run", action="store_true",
                        help="保存せず件数のみ表示")
    parser.add_argument("--limit", type=int, default=None,
                        help="処理する最大行数")
    parser.add_argument("--output-dir", default=None,
                        help="summary/errors/skipped 出力先")
    parser.add_argument("--verbose", action="store_true")
    opts = parser.parse_args(args)

    level = logging.DEBUG if opts.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    rows = load_save_candidates_csv(opts.input)
    logger.info(f"入力: {len(rows)} rows from {opts.input}")

    mode = "DRY-RUN" if opts.dry_run else "LIVE"
    logger.info(f"モード: {mode}")

    result = save_candidates_to_db(
        rows, opts.db, dry_run=opts.dry_run, limit=opts.limit,
    )

    # summary
    out_dir = opts.output_dir
    md = write_summary(
        result,
        input_path=opts.input,
        db_path=opts.db,
        dry_run=opts.dry_run,
        output_dir=out_dir,
    )
    if out_dir:
        write_errors_csv(result, out_dir)
        write_skipped_csv(result, out_dir)

    # コンソール
    print()
    print(f"  モード: {mode}")
    print(f"  input: {result.input_rows}")
    print(f"  valid: {result.valid_rows}")
    print(f"  inserted: {result.inserted}")
    print(f"  updated: {result.updated}")
    print(f"  skipped: {result.skipped}")
    print(f"  errors: {result.errors}")
    if out_dir:
        print(f"  出力先: {out_dir}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
