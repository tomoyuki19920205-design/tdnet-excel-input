#!/usr/bin/env python3
"""tools/run_ab_test.py — V1/V2 A/B テスト実行ラッパー

使い方:
  python tools/run_ab_test.py --limit 20
  python tools/run_ab_test.py --limit 100 --reuse-filing-list data/ab_filings_20260315.json

手順:
  1. listing provider で filing list を取得し JSON に保存 (固定母集団)
  2. v1 worker で全件実行 (別 state DB, 別 JSONL)
  3. v2 worker で全件実行 (別 state DB, 別 JSONL)
  4. compare_ab_runs.py で A/B 比較レポートを生成
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _get_filing_list(*, start_date, end_date, tickers, limit, listing_provider_name,
                     only_earnings_summary, exclude_corrections) -> list[dict]:
    """listing provider から filing list を取得。"""
    from lib.backfill.listing_sources.tdnet_html import TdnetHtmlListingProvider
    from lib.backfill.filing_selector import should_process_for_segment_backfill

    provider = TdnetHtmlListingProvider()
    filings = provider.list_filings(
        start_date, end_date, tickers=tickers, doc_types=["financial_statement"],
    )

    # selector
    accepted = []
    for fi in filings:
        ok, _ = should_process_for_segment_backfill(
            fi.title,
            exclude_corrections=exclude_corrections,
            only_earnings_summary=only_earnings_summary,
        )
        if ok:
            accepted.append(fi)

    # limit
    if limit and limit > 0:
        accepted = accepted[:limit]

    # serialize
    return [
        {
            "filing_id": fi.filing_id,
            "ticker": fi.ticker,
            "title": fi.title,
            "disclosure_date": fi.disclosure_date,
            "doc_url": fi.doc_url,
            "xbrl_url": getattr(fi, "xbrl_url", ""),
            "has_xbrl": getattr(fi, "has_xbrl", False),
        }
        for fi in accepted
    ]


def _save_filing_list(filing_list: list[dict], path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(filing_list, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[ab_test] Filing list saved: {path} ({len(filing_list)} filings)")


def _load_filing_list(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    print(f"[ab_test] Filing list loaded: {path} ({len(data)} filings)")
    return data


def _run_version(
    version: str, filing_list: list[dict], *,
    cache_root: str, workers: int, ts: str,
    retry_download: int, retry_xbrl: int, retry_pdf: int,
    timeout_download: int, timeout_xbrl: int, timeout_pdf: int,
) -> str:
    """1バージョンを実行して JSONL パスを返す。

    state DB を独立させることで v1/v2 が互いに干渉しない。
    """
    from tools.backfill_segments_tdnet import run_backfill

    state_db = f"data/ab_state_{version}_{ts}.db"
    jsonl_path = f"logs/ab_{version}_{ts}.jsonl"

    print(f"\n{'=' * 60}")
    print(f"  A/B Test: Running {version}")
    print(f"  state_db: {state_db}")
    print(f"  jsonl:    {jsonl_path}")
    print(f"  filings:  {len(filing_list)}")
    print(f"{'=' * 60}\n")

    # filing_list → pending 相当のダミーデータを作る
    # 直接 run_backfill を呼ぶ (listing は内部で再取得される)
    # filing_list の filing_id を使って tickers を列挙
    tickers = list(set(f["ticker"] for f in filing_list))

    # filing_list の date range
    dates = [f["disclosure_date"] for f in filing_list if f.get("disclosure_date")]
    if dates:
        start_date = min(dates)
        end_date = max(dates)
    else:
        end = datetime.now()
        start_date = (end - timedelta(days=365)).strftime("%Y-%m-%d")
        end_date = end.strftime("%Y-%m-%d")

    run_backfill(
        start_date=start_date,
        end_date=end_date,
        tickers=tickers,
        limit=len(filing_list) + 100,  # 余裕を持たせる
        workers=workers,
        cache_root=cache_root,
        state_db=state_db,
        log_jsonl_path=jsonl_path,
        worker_version=version,
        retry_download=retry_download,
        retry_xbrl=retry_xbrl,
        retry_pdf=retry_pdf,
        timeout_download=timeout_download,
        timeout_xbrl=timeout_xbrl,
        timeout_pdf=timeout_pdf,
    )

    return jsonl_path


def _run_compare(before_jsonl: str, after_jsonl: str, output: str) -> None:
    """compare_ab_runs.py を呼び出す。"""
    from tools.compare_ab_runs import main as compare_main
    sys.argv = [
        "compare_ab_runs.py",
        "--before", before_jsonl,
        "--after", after_jsonl,
        "--output", output,
    ]
    compare_main()
    print(f"\n[ab_test] Compare report: {output}")


def main():
    parser = argparse.ArgumentParser(description="V1/V2 A/B テスト実行")
    parser.add_argument("--limit", type=int, default=20, help="Filing 件数 (default: 20)")
    parser.add_argument("--years", type=int, default=1)
    parser.add_argument("--date-from", type=str, default=None)
    parser.add_argument("--date-to", type=str, default=None)
    parser.add_argument("--tickers", type=str, default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--cache-root", type=str, default="data/tdnet_cache")
    parser.add_argument("--reuse-filing-list", type=str, default=None,
                        help="既存の filing list JSON を再利用")
    parser.add_argument("--retry-download", type=int, default=3)
    parser.add_argument("--retry-xbrl", type=int, default=2)
    parser.add_argument("--retry-pdf", type=int, default=1)
    parser.add_argument("--timeout-download", type=int, default=30)
    parser.add_argument("--timeout-xbrl", type=int, default=60)
    parser.add_argument("--timeout-pdf", type=int, default=120)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── 1. Filing list ──
    if args.reuse_filing_list:
        filing_list = _load_filing_list(args.reuse_filing_list)
    else:
        if args.date_from and args.date_to:
            start_date, end_date = args.date_from, args.date_to
        else:
            end = datetime.now()
            start = end - timedelta(days=365 * args.years)
            start_date = start.strftime("%Y-%m-%d")
            end_date = end.strftime("%Y-%m-%d")

        tickers = args.tickers.split(",") if args.tickers else None

        filing_list = _get_filing_list(
            start_date=start_date, end_date=end_date, tickers=tickers,
            limit=args.limit, listing_provider_name="tdnet_html",
            only_earnings_summary=True, exclude_corrections=True,
        )

        filing_list_path = f"data/ab_filings_{ts}.json"
        _save_filing_list(filing_list, filing_list_path)

    if not filing_list:
        print("[ab_test] ERROR: No filings found")
        sys.exit(1)

    print(f"\n[ab_test] Fixed population: {len(filing_list)} filings")
    print(f"[ab_test] Sample tickers: {', '.join(set(f['ticker'] for f in filing_list[:5]))}")

    # ── 2. Run v1 ──
    v1_jsonl = _run_version(
        "v1", filing_list,
        cache_root=args.cache_root, workers=args.workers, ts=ts,
        retry_download=args.retry_download, retry_xbrl=args.retry_xbrl, retry_pdf=args.retry_pdf,
        timeout_download=args.timeout_download, timeout_xbrl=args.timeout_xbrl, timeout_pdf=args.timeout_pdf,
    )

    # ── 3. Run v2 ──
    v2_jsonl = _run_version(
        "v2", filing_list,
        cache_root=args.cache_root, workers=args.workers, ts=ts,
        retry_download=args.retry_download, retry_xbrl=args.retry_xbrl, retry_pdf=args.retry_pdf,
        timeout_download=args.timeout_download, timeout_xbrl=args.timeout_xbrl, timeout_pdf=args.timeout_pdf,
    )

    # ── 4. Compare ──
    report_path = f"reports/ab_compare_{ts}.txt"
    Path("reports").mkdir(exist_ok=True)
    _run_compare(v1_jsonl, v2_jsonl, report_path)

    print(f"\n{'=' * 60}")
    print(f"  A/B Test Complete")
    print(f"{'=' * 60}")
    print(f"  Filing list:  data/ab_filings_{ts}.json")
    print(f"  V1 JSONL:     {v1_jsonl}")
    print(f"  V2 JSONL:     {v2_jsonl}")
    print(f"  Compare:      {report_path}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
