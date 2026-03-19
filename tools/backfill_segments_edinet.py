#!/usr/bin/env python3
r"""tools/backfill_segments_edinet.py — EDINET セグメント全社バックフィル

マニフェストの doc_id を順次処理し、EDINET XBRL ZIP からセグメントを抽出して
canonical_segments テーブルに UPSERT する。

Usage:
  # Phase 1: 500 filings (dry-run)
  python tools/backfill_segments_edinet.py --manifest data/edinet_manifest.json --limit 500 --dry-run

  # Phase 1: 500 filings (write)
  python tools/backfill_segments_edinet.py --manifest data/edinet_manifest.json --limit 500

  # Phase 2: full run
  python tools/backfill_segments_edinet.py --manifest data/edinet_manifest.json

  # Resume
  python tools/backfill_segments_edinet.py --manifest data/edinet_manifest.json --resume

  # 特定 ticker
  python tools/backfill_segments_edinet.py --manifest data/edinet_manifest.json --tickers 7203,6758

  # Index range
  python tools/backfill_segments_edinet.py --manifest data/edinet_manifest.json --from-index 100 --to-index 200
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.backfill.edinet_client import EdinetClient
from lib.pipeline.db import load_env, get_supabase_write_config
from lib.pipeline.canonical_writer import write_segments_canonical
from src.segment.edinet_segment_extractor import extract_edinet_segments
from src.segment.edinet_canonical_bridge import edinet_result_to_canonical_segments

logger = logging.getLogger("edinet_backfill")
JST = timezone(timedelta(hours=9))

# ============================================================
# Progress / Resume
# ============================================================

PROGRESS_FILE = "data/edinet_backfill_progress.json"


def _load_progress(path: str) -> dict:
    """進捗ファイルを読み込む。"""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"processed_doc_ids": [], "status_counts": {}, "last_updated": ""}


def _save_progress(path: str, progress: dict):
    """進捗ファイルを保存。"""
    progress["last_updated"] = datetime.now(JST).isoformat()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(progress, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ============================================================
# Manifest Loading
# ============================================================

def load_manifest(path: str) -> list[dict]:
    """マニフェスト JSON を読み込む。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "filings" in data:
        return data["filings"]
    raise ValueError(f"Unsupported manifest format: {path}")


# ============================================================
# Metrics
# ============================================================

class BackfillMetrics:
    """バックフィルのメトリクス集計。"""

    def __init__(self):
        self.processed = 0
        self.ok = 0
        self.no_segments_axis_missing = 0
        self.no_segments_single_segment = 0
        self.no_segments_other = 0
        self.search_fail = 0
        self.parse_error = 0
        self.download_error = 0
        self.other_error = 0
        self.segments_written = 0
        self.rows_flushed = 0
        self.overlap_count = 0
        self.cache_hits = 0
        self.downloads = 0
        self.skipped_resume = 0

        # batch tracking
        self._batch_filings = 0
        self._batch_rows = 0

    def record_no_segments(self, review_hint: str):
        """no_segments を細分類して記録。"""
        if review_hint == "no_segments_axis_missing":
            self.no_segments_axis_missing += 1
        elif review_hint in ("no_segments_single_segment", "facts_found_but_no_records_built"):
            self.no_segments_single_segment += 1
        else:
            self.no_segments_other += 1

    def record_error(self, review_hint: str):
        """error を細分類して記録。"""
        if "parse" in review_hint.lower():
            self.parse_error += 1
        elif "zip" in review_hint.lower() or "download" in review_hint.lower():
            self.download_error += 1
        else:
            self.other_error += 1

    def summary_dict(self) -> dict:
        return {
            "processed": self.processed,
            "ok": self.ok,
            "no_segments_axis_missing": self.no_segments_axis_missing,
            "no_segments_single_segment": self.no_segments_single_segment,
            "no_segments_other": self.no_segments_other,
            "search_fail": self.search_fail,
            "parse_error": self.parse_error,
            "download_error": self.download_error,
            "other_error": self.other_error,
            "segments_written": self.segments_written,
            "rows_flushed": self.rows_flushed,
            "overlap_count": self.overlap_count,
            "cache_hits": self.cache_hits,
            "downloads": self.downloads,
            "skipped_resume": self.skipped_resume,
        }

    def print_summary(self):
        s = self.summary_dict()
        print()
        print("=" * 60)
        print("  EDINET Segment Backfill Summary")
        print("=" * 60)
        for k, v in s.items():
            print(f"  {k:35s} {v}")
        print("=" * 60)


# ============================================================
# JSONL Logger
# ============================================================

class JsonlLogger:
    """filing ごとの結果を JSONL にログ。"""

    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "a", encoding="utf-8")

    def log(self, record: dict):
        self._f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._f.flush()

    def close(self):
        self._f.close()


# ============================================================
# Core: process one filing
# ============================================================

def process_one_filing(
    filing: dict,
    client: EdinetClient,
    *,
    retry: int = 3,
    dry_run: bool = False,
    config: dict | None = None,
    metrics: BackfillMetrics,
    jsonl: JsonlLogger | None = None,
    segment_buffer: list[dict],
    filing_buffer: list[dict],
) -> dict:
    """1 filing を処理して結果を返す。

    Returns:
        {
            "doc_id": str,
            "ticker": str,
            "status": str,  # ok / no_segments / error / download_error
            "review_hint": str,
            "segments_count": int,
            "rows_count": int,
        }
    """
    doc_id = filing["doc_id"]
    ticker = filing.get("ticker", "")
    quarter_raw = filing.get("quarter", "") or "FY"
    # 半期報告書 1H → ビューアー/TDnet 仕様の 2Q に正規化
    QUARTER_CANONICAL_MAP = {"1H": "2Q", "H1": "2Q"}
    quarter = QUARTER_CANONICAL_MAP.get(quarter_raw, quarter_raw)
    fiscal_end = filing.get("fiscal_end", "") or filing.get("fiscal_year", "")
    doc_type = filing.get("doc_type", "securities_report")

    result_record = {
        "doc_id": doc_id,
        "ticker": ticker,
        "company_name": filing.get("company_name", ""),
        "quarter": quarter,
        "fiscal_end": fiscal_end,
        "status": "error",
        "review_hint": "",
        "segments_count": 0,
        "rows_count": 0,
        "cache_hit": False,
        "timestamp": datetime.now(JST).isoformat(),
    }

    # ── Step 1: Download XBRL ZIP ──
    download_result = None
    for attempt in range(1, retry + 1):
        try:
            download_result = client.download_xbrl_zip(doc_id)
            if download_result.succeeded:
                break
            if download_result.skipped:
                break
        except Exception as e:
            logger.warning(f"[backfill] download retry {attempt}/{retry}: {doc_id}: {e}")
            if attempt < retry:
                time.sleep(1.0 * attempt)

    if download_result is None or not download_result.succeeded:
        reason = "download_failed"
        if download_result and download_result.skipped:
            reason = download_result.skipped_reason or "skipped"
        result_record["status"] = "error"
        result_record["review_hint"] = reason
        metrics.download_error += 1
        if jsonl:
            jsonl.log(result_record)
        return result_record

    zip_path = download_result.cache_path
    if download_result.cache_hit:
        metrics.cache_hits += 1
        result_record["cache_hit"] = True
    else:
        metrics.downloads += 1

    # ── Step 2: Extract segments ──
    extract_result = None
    for attempt in range(1, retry + 1):
        try:
            extract_result = extract_edinet_segments(
                zip_path,
                ticker=ticker,
                doc_type=doc_type,
                period=fiscal_end,
                quarter=quarter,
            )
            break
        except Exception as e:
            logger.warning(f"[backfill] extract retry {attempt}/{retry}: {doc_id}: {e}")
            if attempt < retry:
                time.sleep(0.5 * attempt)

    if extract_result is None:
        result_record["status"] = "error"
        result_record["review_hint"] = "extract_exception"
        metrics.parse_error += 1
        if jsonl:
            jsonl.log(result_record)
        return result_record

    result_record["status"] = extract_result.status
    result_record["review_hint"] = extract_result.review_hint
    result_record["instance_type"] = extract_result.debug_summary.get("instance_type", "")

    if extract_result.status == "no_segments":
        metrics.record_no_segments(extract_result.review_hint)
        if jsonl:
            jsonl.log(result_record)
        return result_record

    if extract_result.status != "ok":
        metrics.record_error(extract_result.review_hint)
        if jsonl:
            jsonl.log(result_record)
        return result_record

    # ── Step 3: Convert to canonical format ──
    canonical_segments = edinet_result_to_canonical_segments(
        extract_result, include_non_ordinary=False,
    )

    # source_system を追加
    for seg in canonical_segments:
        seg["source_system"] = "edinet"

    result_record["segments_count"] = len(canonical_segments)
    metrics.ok += 1

    if not canonical_segments:
        if jsonl:
            jsonl.log(result_record)
        return result_record

    # ── Step 4: Buffer for batch commit ──
    if not dry_run:
        for seg in canonical_segments:
            segment_buffer.append({
                "ticker": ticker,
                "period": fiscal_end,
                "quarter": quarter,
                "segment": seg,
                "source": "edinet_xbrl",
                "filing_id": doc_id,
            })
        filing_buffer.append(filing)
        metrics._batch_filings += 1
        metrics._batch_rows += len(canonical_segments) * 2  # sales + profit per segment

    result_record["rows_count"] = len(canonical_segments) * 2
    metrics.segments_written += len(canonical_segments)

    if jsonl:
        jsonl.log(result_record)
    return result_record


# ============================================================
# Batch flush
# ============================================================

def flush_batch(
    segment_buffer: list[dict],
    filing_buffer: list[dict],
    config: dict,
    metrics: BackfillMetrics,
    *,
    retry: int = 3,
) -> int:
    """バッファを Supabase に flush。

    Returns:
        flush した行数
    """
    if not segment_buffer:
        return 0

    # ticker × period × quarter でグループ化
    groups: dict[tuple, list[dict]] = {}
    for item in segment_buffer:
        key = (item["ticker"], item["period"], item["quarter"], item["source"])
        if key not in groups:
            groups[key] = {"segments": [], "filing_id": item["filing_id"]}
        groups[key]["segments"].append(item["segment"])

    total_flushed = 0

    for (ticker, period, quarter, source), group in groups.items():
        for attempt in range(1, retry + 1):
            try:
                result = write_segments_canonical(
                    ticker=ticker,
                    period=period,
                    quarter=quarter,
                    segments=group["segments"],
                    source=source,
                    filing_id=group["filing_id"],
                    config=config,
                )
                total_flushed += result["written"]
                if result["errors"] > 0:
                    logger.warning(
                        f"[flush] errors: ticker={ticker} period={period} "
                        f"quarter={quarter} errors={result['errors']}"
                    )
                break
            except Exception as e:
                logger.warning(
                    f"[flush] retry {attempt}/{retry}: "
                    f"ticker={ticker} period={period}: {e}"
                )
                if attempt < retry:
                    time.sleep(1.0 * attempt)

    metrics.rows_flushed += total_flushed
    metrics._batch_filings = 0
    metrics._batch_rows = 0

    segment_buffer.clear()
    filing_buffer.clear()

    return total_flushed


# ============================================================
# Main run
# ============================================================

def run_backfill(
    manifest_path: str,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    resume: bool = False,
    tickers: list[str] | None = None,
    status_filter: str | None = None,
    from_index: int | None = None,
    to_index: int | None = None,
    retry: int = 3,
    rate_limit: float = 0.2,
    batch_filings: int = 100,
    batch_rows: int = 5000,
    cache_dir: str | None = None,
    progress_path: str = PROGRESS_FILE,
    log_path: str | None = None,
    verification_tickers: list[str] | None = None,
) -> dict:
    """EDINET セグメントバックフィルを実行。"""

    load_env()
    config = None if dry_run else get_supabase_write_config()
    if not dry_run and not config:
        print("[ERROR] Supabase write config not available (service role key missing)")
        return {"error": "no_write_config"}

    # ── Load manifest ──
    filings = load_manifest(manifest_path)
    print(f"[backfill] loaded {len(filings)} filings from manifest")

    # ── Filters ──
    if tickers:
        tickers_set = set(tickers)
        filings = [f for f in filings if f.get("ticker", "") in tickers_set]
        print(f"[backfill] ticker filter: {len(filings)} filings")

    if from_index is not None or to_index is not None:
        start_idx = from_index or 0
        end_idx = to_index or len(filings)
        filings = filings[start_idx:end_idx]
        print(f"[backfill] index range [{start_idx}:{end_idx}]: {len(filings)} filings")

    if limit and limit > 0:
        filings = filings[:limit]
        print(f"[backfill] limit applied: {len(filings)} filings")

    # ── Resume ──
    progress = _load_progress(progress_path) if resume else {
        "processed_doc_ids": [], "status_counts": {}, "last_updated": ""
    }
    processed_set = set(progress.get("processed_doc_ids", []))

    # ── EDINET client ──
    client = EdinetClient(
        cache_dir=cache_dir or os.environ.get("EDINET_CACHE_DIR", "data/edinet_cache"),
        rate_limit=rate_limit,
    )

    # ── Logger ──
    if log_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = f"logs/backfill_segments_edinet_{ts}.jsonl"
    jsonl = JsonlLogger(log_path)

    # ── Metrics ──
    metrics = BackfillMetrics()
    segment_buffer: list[dict] = []
    filing_buffer: list[dict] = []

    mode = "DRY-RUN" if dry_run else "WRITE"
    print()
    print("=" * 60)
    print(f"  EDINET Segment Backfill — {mode}")
    print("=" * 60)
    print(f"  manifest:     {manifest_path}")
    print(f"  filings:      {len(filings)}")
    print(f"  resume:       {resume} (processed={len(processed_set)})")
    print(f"  retry:        {retry}")
    print(f"  rate_limit:   {rate_limit}s")
    print(f"  batch:        {batch_filings} filings or {batch_rows} rows")
    print(f"  log:          {log_path}")
    print(f"  dry_run:      {dry_run}")
    print()

    t0 = time.monotonic()

    # ── Verification tickers tracking ──
    verification_results: dict[str, list[dict]] = {}
    if verification_tickers:
        for t in verification_tickers:
            verification_results[t] = []

    # ── Process each filing ──
    for i, filing in enumerate(filings, 1):
        doc_id = filing["doc_id"]

        # Resume: skip already processed
        if doc_id in processed_set:
            metrics.skipped_resume += 1
            continue

        # Status filter
        if status_filter:
            # (future: filter by previous run status)
            pass

        metrics.processed += 1

        result = process_one_filing(
            filing, client,
            retry=retry,
            dry_run=dry_run,
            config=config,
            metrics=metrics,
            jsonl=jsonl,
            segment_buffer=segment_buffer,
            filing_buffer=filing_buffer,
        )

        # Track verification tickers
        ticker = filing.get("ticker", "")
        if ticker in verification_results:
            verification_results[ticker].append(result)

        # Update progress
        processed_set.add(doc_id)

        # ── Batch flush check ──
        should_flush = (
            not dry_run
            and config
            and (
                metrics._batch_filings >= batch_filings
                or metrics._batch_rows >= batch_rows
            )
        )

        if should_flush:
            flushed = flush_batch(segment_buffer, filing_buffer, config, metrics, retry=retry)
            logger.info(
                f"[backfill] batch flush: filings={metrics.processed} "
                f"rows_flushed={flushed}"
            )
            print(
                f"  [batch flush] processed={metrics.processed}/{len(filings)} "
                f"rows_flushed={flushed}"
            )

            # save progress after each batch
            progress["processed_doc_ids"] = list(processed_set)
            progress["status_counts"] = metrics.summary_dict()
            _save_progress(progress_path, progress)

        # Progress log
        if i % 50 == 0 or i == len(filings):
            elapsed = time.monotonic() - t0
            rate = metrics.processed / elapsed if elapsed > 0 else 0
            print(
                f"  [{i}/{len(filings)}] "
                f"ok={metrics.ok} "
                f"no_seg_axis={metrics.no_segments_axis_missing} "
                f"no_seg_single={metrics.no_segments_single_segment} "
                f"err={metrics.parse_error + metrics.download_error + metrics.other_error} "
                f"seg_written={metrics.segments_written} "
                f"({rate:.1f} filing/s)"
            )

    # ── Final flush ──
    if not dry_run and config and segment_buffer:
        flushed = flush_batch(segment_buffer, filing_buffer, config, metrics, retry=retry)
        print(f"  [final flush] rows_flushed={flushed}")

    elapsed = time.monotonic() - t0

    # ── Save final progress ──
    progress["processed_doc_ids"] = list(processed_set)
    progress["status_counts"] = metrics.summary_dict()
    _save_progress(progress_path, progress)

    jsonl.close()

    # ── Summary ──
    summary = metrics.summary_dict()
    summary["elapsed_sec"] = round(elapsed, 1)
    summary["elapsed_human"] = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"

    metrics.print_summary()
    print(f"  elapsed:  {summary['elapsed_human']}")
    print(f"  log:      {log_path}")
    print()

    # ── Verification tickers report ──
    if verification_tickers and verification_results:
        print("  Verification Tickers:")
        print("  " + "-" * 50)
        for ticker, results in verification_results.items():
            if results:
                for r in results:
                    status = r.get("status", "?")
                    hint = r.get("review_hint", "")
                    segs = r.get("segments_count", 0)
                    print(f"    {ticker}: status={status} hint={hint} segments={segs}")
            else:
                print(f"    {ticker}: not in manifest")
        print()

    # ── Save summary JSON ──
    summary_path = log_path.replace(".jsonl", "_summary.json")
    Path(summary_path).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  summary:  {summary_path}")

    return summary


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="EDINET セグメント全社バックフィル",
    )
    parser.add_argument("--manifest", required=True,
                        help="マニフェスト JSON パス")
    parser.add_argument("--limit", type=int, default=None,
                        help="処理件数上限")
    parser.add_argument("--dry-run", action="store_true",
                        help="Supabase write しない (download + extract のみ)")
    parser.add_argument("--resume", action="store_true",
                        help="前回の進捗から再開")
    parser.add_argument("--tickers", default=None,
                        help="カンマ区切り ticker (例: 7203,6758)")
    parser.add_argument("--status-filter", default=None,
                        help="前回 status でフィルタ (例: error)")
    parser.add_argument("--from-index", type=int, default=None,
                        help="manifest のスライス開始 index")
    parser.add_argument("--to-index", type=int, default=None,
                        help="manifest のスライス終了 index")
    parser.add_argument("--retry", type=int, default=3,
                        help="リトライ回数 (default: 3)")
    parser.add_argument("--rate-limit", type=float, default=0.2,
                        help="API rate limit sec (default: 0.2)")
    parser.add_argument("--batch-filings", type=int, default=100,
                        help="batch commit 間隔 filings (default: 100)")
    parser.add_argument("--batch-rows", type=int, default=5000,
                        help="batch commit 間隔 rows (default: 5000)")
    parser.add_argument("--cache-dir", default=None,
                        help="EDINET XBRL cache dir")
    parser.add_argument("--progress", default=PROGRESS_FILE,
                        help="進捗ファイルパス")
    parser.add_argument("--log", default=None,
                        help="JSONL ログファイルパス")
    parser.add_argument("--verify-tickers", default=None,
                        help="検証対象 ticker (カンマ区切り)")
    return parser


def main(args: list[str] | None = None) -> int:
    parser = build_parser()
    opts = parser.parse_args(args)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    tickers = opts.tickers.split(",") if opts.tickers else None
    verify_tickers = opts.verify_tickers.split(",") if opts.verify_tickers else None

    # デフォルトの検証 tickers
    if verify_tickers is None:
        verify_tickers = ["8058", "8001", "8031", "8053", "8015", "6367", "7011"]

    result = run_backfill(
        opts.manifest,
        limit=opts.limit,
        dry_run=opts.dry_run,
        resume=opts.resume,
        tickers=tickers,
        status_filter=opts.status_filter,
        from_index=opts.from_index,
        to_index=opts.to_index,
        retry=opts.retry,
        rate_limit=opts.rate_limit,
        batch_filings=opts.batch_filings,
        batch_rows=opts.batch_rows,
        cache_dir=opts.cache_dir,
        progress_path=opts.progress,
        log_path=opts.log,
        verification_tickers=verify_tickers,
    )

    if "error" in result:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
