#!/usr/bin/env python3
r"""tools/generate_edinet_manifest.py — EDINET doc_id マニフェスト自動生成

EDINET 書類一覧 API をスキャンし、有報/半報の doc_id リストを生成する。

Usage:
  # 全上場企業 × 過去2年 (3月決算 + 12月決算)
  .\.venv\Scripts\python.exe tools\generate_edinet_manifest.py

  # テスト: 少数日のみ
  .\.venv\Scripts\python.exe tools\generate_edinet_manifest.py --limit-days 5

  # 特定 ticker のみ
  .\.venv\Scripts\python.exe tools\generate_edinet_manifest.py --tickers 7203,6758

  # Resume (途中から再開)
  .\.venv\Scripts\python.exe tools\generate_edinet_manifest.py --resume
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

import requests
from lib.pipeline.db import load_env

logger = logging.getLogger("edinet_manifest")

# ============================================================
# EDINET API
# ============================================================
EDINET_BASE = "https://api.edinet-fsa.go.jp/api/v2"
USER_AGENT = "EdinetBackfill/1.0"

# 有報/半報/四半期報
TARGET_DOC_CODES = {
    "120": "securities_report",       # 有報
    "130": "securities_report_amend", # 訂正有報
    "140": "quarterly_report",        # 四半期報
    "150": "quarterly_report_amend",  # 訂正四半期報
    "160": "semiannual_report",       # 半報
    "170": "semiannual_report_amend", # 訂正半報
}

# quarter マッピング — EDINET doc_type_code → canonical quarter
DOC_CODE_TO_QUARTER = {
    "120": "FY", "130": "FY",
    "140": None,  # 四半期 (要: 文書内容から判定)
    "150": None,
    "160": "2Q", "170": "2Q",  # 半期報告書 = 2Q累計 (ビューアー TDnet 仕様)
}


def _fetch_documents(date: str, api_key: str, rate_limit: float = 0.2) -> list[dict]:
    """EDINET 書類一覧 API で指定日の全書類を取得。"""
    url = f"{EDINET_BASE}/documents.json"
    params = {"date": date, "type": 2, "Subscription-Key": api_key}
    try:
        time.sleep(rate_limit)
        resp = requests.get(url, params=params,
                            headers={"User-Agent": USER_AGENT}, timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("statusCode", 200) != 200:
                logger.warning(f"[manifest] API body error: {data.get('message', '')}")
                return []
            return data.get("results", [])
        else:
            logger.warning(f"[manifest] HTTP {resp.status_code} for {date}")
    except Exception as e:
        logger.warning(f"[manifest] request error: {date}: {e}")
    return []


def _sec_code_to_ticker(sec_code: str) -> str:
    """5桁 secCode → 4桁 ticker。common_ticker に委譲。"""
    from src.common_ticker import normalize_ticker
    return normalize_ticker(sec_code)


def _infer_quarter_from_title(title: str) -> str:
    """タイトルから四半期を推定。"""
    if not title:
        return ""
    if "第1四半期" in title or "第１四半期" in title:
        return "1Q"
    if "第2四半期" in title or "第２四半期" in title:
        return "2Q"
    if "第3四半期" in title or "第３四半期" in title:
        return "3Q"
    return ""


def _infer_fiscal_end(doc: dict) -> str:
    """EDINET response から fiscal_end を推定。"""
    # periodEnd は EDINET の response に含まれている場合がある
    pe = doc.get("periodEnd", "")
    if pe and len(pe) >= 10:
        return pe[:10]
    # periodEnd が YYYYMMDD 形式
    if pe and len(pe) == 8 and pe.isdigit():
        return f"{pe[:4]}-{pe[4:6]}-{pe[6:8]}"
    return ""


def _infer_accounting_standard(doc: dict) -> str:
    """EDINET response から会計基準を推定 (取れれば)。"""
    title = doc.get("docDescription", "") or ""
    if "IFRS" in title or "国際会計基準" in title:
        return "IFRS"
    if "米国基準" in title or "US-GAAP" in title or "US GAAP" in title:
        return "US-GAAP"
    return "JP-GAAP"


def _infer_industry(doc: dict) -> str:
    """EDINET response から業種 (取れれば)。"""
    # EDINET v2 API に industryCode がある場合
    return doc.get("industryCode", "") or ""


def scan_date_range(
    api_key: str,
    start_date: str,
    end_date: str,
    *,
    rate_limit: float = 0.2,
    tickers_filter: set[str] | None = None,
    progress_path: str | None = None,
    resume: bool = False,
) -> list[dict]:
    """日付範囲をスキャンして doc_id マニフェストを構築する。"""
    s = datetime.strptime(start_date, "%Y-%m-%d")
    e = datetime.strptime(end_date, "%Y-%m-%d")

    # resume: 完了済み日付を読み込む
    scanned_dates: set[str] = set()
    filings: dict[str, dict] = {}  # doc_id → filing

    if resume and progress_path and os.path.exists(progress_path):
        with open(progress_path, "r", encoding="utf-8") as f:
            progress = json.load(f)
        scanned_dates = set(progress.get("scanned_dates", []))
        for fi in progress.get("filings", []):
            filings[fi["doc_id"]] = fi
        logger.info(f"[manifest] resume: {len(scanned_dates)} dates, {len(filings)} filings loaded")
        print(f"[manifest] resume: {len(scanned_dates)} dates, {len(filings)} filings")

    total_days = (e - s).days + 1
    scanned = 0

    d = s
    while d <= e:
        date_str = d.strftime("%Y-%m-%d")
        d += timedelta(days=1)

        if date_str in scanned_dates:
            scanned += 1
            continue

        results = _fetch_documents(date_str, api_key, rate_limit)
        new_count = 0

        for r in results:
            doc_type_code = r.get("docTypeCode", "")
            if doc_type_code not in TARGET_DOC_CODES:
                continue

            sec_code = (r.get("secCode") or "").strip()
            if not sec_code or len(sec_code) < 4:
                continue

            xbrl = r.get("xbrlFlag", "0") == "1"
            if not xbrl:
                continue

            ticker = _sec_code_to_ticker(sec_code)

            # ticker filter
            if tickers_filter and ticker not in tickers_filter:
                continue

            doc_id = r.get("docID", "")
            if doc_id in filings:
                continue

            # quarter
            quarter = DOC_CODE_TO_QUARTER.get(doc_type_code, "")
            if quarter is None:
                quarter = _infer_quarter_from_title(r.get("docDescription", ""))

            fiscal_end = _infer_fiscal_end(r)

            filings[doc_id] = {
                "doc_id": doc_id,
                "ticker": ticker,
                "company_name": r.get("filerName", ""),
                "fiscal_end": fiscal_end,
                "quarter": quarter,
                "doc_type": TARGET_DOC_CODES[doc_type_code],
                "doc_type_code": doc_type_code,
                "filing_date": date_str,
                "sec_code": sec_code,
                "edinet_code": r.get("edinetCode", "") or "",
                "search_basis": "date_scan",
                "accounting_standard": _infer_accounting_standard(r),
                "industry": _infer_industry(r),
                "instance_type": "",  # 抽出時に判明
                "xbrl_available": True,
            }
            new_count += 1

        scanned_dates.add(date_str)
        scanned += 1

        if scanned % 10 == 0 or scanned == total_days:
            logger.info(
                f"[manifest] {scanned}/{total_days} days scanned, "
                f"{len(filings)} filings, {new_count} new on {date_str}"
            )
            print(
                f"  [{scanned}/{total_days}] {date_str}: "
                f"+{new_count} → total {len(filings)}"
            )

        # 定期 save (10日ごと)
        if progress_path and scanned % 10 == 0:
            _save_progress(progress_path, filings, scanned_dates)

    # 最終 save
    if progress_path:
        _save_progress(progress_path, filings, scanned_dates)

    return list(filings.values())


def _save_progress(path: str, filings: dict, scanned_dates: set):
    """進捗を JSON に保存。"""
    progress = {
        "saved_at": datetime.now().isoformat(),
        "scanned_dates_count": len(scanned_dates),
        "filings_count": len(filings),
        "scanned_dates": sorted(scanned_dates),
        "filings": list(filings.values()),
    }
    Path(path).write_text(
        json.dumps(progress, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _resolve_corrections(filings: list[dict]) -> list[dict]:
    """訂正版がある場合、原本を除外して訂正版のみを採用。

    同一 ticker × fiscal_end × quarter で訂正版 (130/150/170) を優先。
    """
    correction_codes = {"130", "150", "170"}
    original_codes = {"120", "140", "160"}

    # group by (ticker, fiscal_end, quarter)
    groups: dict[tuple, list[dict]] = {}
    for fi in filings:
        key = (fi["ticker"], fi["fiscal_end"], fi["quarter"])
        if key not in groups:
            groups[key] = []
        groups[key].append(fi)

    resolved = []
    for key, group in groups.items():
        corrections = [f for f in group if f["doc_type_code"] in correction_codes]
        originals = [f for f in group if f["doc_type_code"] in original_codes]

        if corrections:
            # 訂正がある → 最新訂正だけ
            corrections.sort(key=lambda x: x["filing_date"], reverse=True)
            resolved.append(corrections[0])
        elif originals:
            # 原本のみ → 最新
            originals.sort(key=lambda x: x["filing_date"], reverse=True)
            resolved.append(originals[0])
        else:
            # fallback
            resolved.append(group[0])

    return resolved


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="EDINET doc_id マニフェスト生成",
    )
    parser.add_argument("--output", default="data/edinet_manifest.json",
                        help="出力先 (default: data/edinet_manifest.json)")
    parser.add_argument("--start-date", default=None,
                        help="スキャン開始日 (default: 2年前)")
    parser.add_argument("--end-date", default=None,
                        help="スキャン終了日 (default: 今日)")
    parser.add_argument("--limit-days", type=int, default=None,
                        help="スキャン日数上限 (テスト用)")
    parser.add_argument("--tickers", default=None,
                        help="カンマ区切り ticker (例: 7203,6758)")
    parser.add_argument("--rate-limit", type=float, default=0.2,
                        help="API rate limit (sec, default: 0.2)")
    parser.add_argument("--resume", action="store_true",
                        help="前回の進捗から再開")
    return parser


def main(args: list[str] | None = None) -> int:
    parser = build_parser()
    opts = parser.parse_args(args)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    load_env()
    api_key = os.environ.get("EDINET_API_KEY", "")
    if not api_key:
        print("[ERROR] EDINET_API_KEY not set")
        return 1

    # 日付範囲
    if opts.end_date:
        end_date = opts.end_date
    else:
        end_date = datetime.now().strftime("%Y-%m-%d")

    if opts.start_date:
        start_date = opts.start_date
    else:
        start_date = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")

    if opts.limit_days:
        end_dt = datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=opts.limit_days - 1)
        end_date = min(end_date, end_dt.strftime("%Y-%m-%d"))

    tickers_filter = None
    if opts.tickers:
        tickers_filter = set(opts.tickers.split(","))

    progress_path = opts.output.replace(".json", "_progress.json")

    print("=" * 60)
    print("  EDINET Manifest Generator")
    print("=" * 60)
    print(f"  range: {start_date} ~ {end_date}")
    print(f"  tickers: {tickers_filter or 'all'}")
    print(f"  rate_limit: {opts.rate_limit}s")
    print(f"  resume: {opts.resume}")
    print()

    t0 = time.monotonic()

    filings = scan_date_range(
        api_key, start_date, end_date,
        rate_limit=opts.rate_limit,
        tickers_filter=tickers_filter,
        progress_path=progress_path,
        resume=opts.resume,
    )

    # 訂正版解決
    resolved = _resolve_corrections(filings)

    elapsed = time.monotonic() - t0

    # Save
    Path(opts.output).parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "generated_at": datetime.now().isoformat(),
        "scan_range": f"{start_date} ~ {end_date}",
        "total_raw_filings": len(filings),
        "total_resolved_filings": len(resolved),
        "filings": resolved,
    }
    Path(opts.output).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Stats
    unique_tickers = set(f["ticker"] for f in resolved)
    quarter_dist = {}
    doc_type_dist = {}
    for f in resolved:
        q = f.get("quarter", "?")
        quarter_dist[q] = quarter_dist.get(q, 0) + 1
        dt = f.get("doc_type", "?")
        doc_type_dist[dt] = doc_type_dist.get(dt, 0) + 1

    print()
    print("=" * 60)
    print("  Manifest Generation Complete")
    print("=" * 60)
    print(f"  output:           {opts.output}")
    print(f"  raw filings:      {len(filings):,}")
    print(f"  resolved filings: {len(resolved):,}")
    print(f"  unique tickers:   {len(unique_tickers):,}")
    print(f"  quarter dist:     {json.dumps(quarter_dist)}")
    print(f"  doc_type dist:    {json.dumps(doc_type_dist)}")
    print(f"  elapsed:          {elapsed:.1f}s")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
