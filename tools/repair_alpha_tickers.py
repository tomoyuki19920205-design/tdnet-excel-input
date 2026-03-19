#!/usr/bin/env python3
# ============================================================
# repair_alpha_tickers.py — Alpha ticker 恒久修正スクリプト
# ============================================================
"""
Supabase 上の 5桁 ticker / 誤正規化 ticker を
canonical_financials / financials で修正する。

Usage:
  # Dry-run (変更なし、レポートのみ)
  .\.venv\Scripts\python.exe tools\repair_alpha_tickers.py

  # 修正適用
  .\.venv\Scripts\python.exe tools\repair_alpha_tickers.py --apply

  # 特定 ticker のみ
  .\.venv\Scripts\python.exe tools\repair_alpha_tickers.py --apply --tickers 4180,1380
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import requests
from lib.pipeline.db import load_env
from src.common_ticker import normalize_ticker, is_valid_ticker, JQUANTS_ALPHA_MAP

JST = timezone(timedelta(hours=9))


def _supa_api():
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    return (
        f"{url}/rest/v1",
        {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )


def classify_ticker(raw: str) -> tuple[str, str]:
    """
    5桁 ticker を分類する。

    Returns:
        (category, correct_ticker)
        category: "alpha_should_convert", "normal_5digit", "invalid"
    """
    s = raw.strip()
    normalized = normalize_ticker(s)

    # JQUANTS_ALPHA_MAP に含まれる → alpha 化すべき
    if s in JQUANTS_ALPHA_MAP:
        return "alpha_should_convert", JQUANTS_ALPHA_MAP[s]

    # 5桁末尾0で正規化後が4桁 valid → 正常な5桁コード (末尾0除去で valid)
    if len(s) == 5 and s.endswith("0"):
        candidate = s[:4]
        if is_valid_ticker(candidate):
            # alpha version check
            if any(c.isalpha() for c in candidate):
                return "alpha_should_convert", candidate
            return "normal_5digit", candidate
        return "invalid", s

    # 5桁で末尾0以外 → 不正
    if len(s) == 5:
        return "invalid", s

    return "unknown", s


def audit_canonical_financials(rest_url: str, headers: dict) -> dict:
    """canonical_financials の全 ticker を監査。"""
    print("  Fetching all tickers from canonical_financials...")
    all_tickers = set()
    offset = 0
    while True:
        r = requests.get(
            f"{rest_url}/canonical_financials?select=ticker&limit=5000&offset={offset}",
            headers={k: v for k, v in headers.items() if k != "Content-Type"},
            timeout=30,
        )
        d = r.json() if r.status_code == 200 else []
        for row in d:
            all_tickers.add(row["ticker"])
        if len(d) < 5000:
            break
        offset += 5000
    print(f"  Total unique tickers: {len(all_tickers)}")

    # Classify 5-digit tickers
    five_digit = sorted(t for t in all_tickers if len(t) == 5)
    print(f"  5-digit tickers: {len(five_digit)}")

    alpha_convert = []
    normal_5digit = []
    invalid = []

    for t in five_digit:
        cat, correct = classify_ticker(t)
        entry = {"raw": t, "correct": correct, "category": cat}
        if cat == "alpha_should_convert":
            alpha_convert.append(entry)
        elif cat == "normal_5digit":
            normal_5digit.append(entry)
        else:
            invalid.append(entry)

    return {
        "total_tickers": len(all_tickers),
        "five_digit_count": len(five_digit),
        "alpha_should_convert": alpha_convert,
        "normal_5digit": normal_5digit,
        "invalid": invalid,
    }


def check_viewer_impact(rest_url: str, headers: dict, tickers: list[str]) -> list[dict]:
    """api_latest_financials に出ている5桁 ticker を確認。"""
    results = []
    for t in tickers:
        r = requests.get(
            f"{rest_url}/api_latest_financials?ticker=eq.{t}&select=ticker,period&limit=3",
            headers={k: v for k, v in headers.items() if k != "Content-Type"},
            timeout=15,
        )
        d = r.json() if r.status_code == 200 else []
        results.append({"ticker": t, "in_viewer": len(d) > 0, "row_count": len(d)})
    return results


def repair_ticker(rest_url: str, headers: dict, old_ticker: str, new_ticker: str,
                  table: str = "canonical_financials", dry_run: bool = True) -> dict:
    """1 ticker を修正する。冪等。"""
    # Fetch old rows
    r = requests.get(
        f"{rest_url}/{table}?ticker=eq.{old_ticker}&select=*&limit=1000",
        headers={k: v for k, v in headers.items() if k != "Content-Type"},
        timeout=30,
    )
    old_rows = r.json() if r.status_code == 200 else []

    if not old_rows:
        return {"old": old_ticker, "new": new_ticker, "action": "skip", "reason": "no_rows"}

    if dry_run:
        return {"old": old_ticker, "new": new_ticker, "action": "dry_run", "rows": len(old_rows)}

    # Prepare new rows
    for row in old_rows:
        row["ticker"] = new_ticker
        row.pop("id", None)
        # Update source_row_key if present
        srk = row.get("source_row_key", "")
        if srk and old_ticker in srk:
            row["source_row_key"] = srk.replace(old_ticker, new_ticker)

    # Delete old
    r = requests.delete(
        f"{rest_url}/{table}?ticker=eq.{old_ticker}",
        headers={**headers, "Prefer": "return=headers-only"},
        timeout=30,
    )
    delete_status = r.status_code

    # Upsert new
    r = requests.post(
        f"{rest_url}/{table}",
        headers={**headers, "Prefer": "return=headers-only,resolution=merge-duplicates"},
        json=old_rows,
        timeout=60,
    )
    upsert_status = r.status_code

    return {
        "old": old_ticker, "new": new_ticker,
        "action": "applied",
        "rows": len(old_rows),
        "delete_status": delete_status,
        "upsert_status": upsert_status,
        "success": upsert_status in (200, 201),
    }


def main():
    parser = argparse.ArgumentParser(description="Alpha ticker 恒久修正")
    parser.add_argument("--apply", action="store_true", help="修正を適用する (デフォルト: dry-run)")
    parser.add_argument("--tickers", default=None, help="指定 ticker のみ修正 (カンマ区切り)")
    parser.add_argument("--table", default="canonical_financials", help="対象テーブル")
    parser.add_argument("--json-output", default=None, help="結果を JSON ファイルに出力")
    opts = parser.parse_args()

    load_env()
    rest_url, headers = _supa_api()
    dry_run = not opts.apply

    print("=" * 60)
    print(f"  Alpha Ticker Repair {'(DRY-RUN)' if dry_run else '(APPLY)'}")
    print("=" * 60)

    # Phase 1: Audit
    print("\n--- Phase 1: Audit ---")
    audit = audit_canonical_financials(rest_url, headers)

    print(f"\n  分類結果:")
    print(f"    alpha 化すべき: {len(audit['alpha_should_convert'])}")
    print(f"    正常 5桁 (末尾0除去で OK): {len(audit['normal_5digit'])}")
    print(f"    不正データ: {len(audit['invalid'])}")

    if audit["alpha_should_convert"]:
        print(f"\n  === Alpha 化すべき ticker (上位20) ===")
        for e in audit["alpha_should_convert"][:20]:
            print(f"    {e['raw']} → {e['correct']}")

    if audit["normal_5digit"]:
        print(f"\n  === 正常 5桁 (上位20) ===")
        for e in audit["normal_5digit"][:20]:
            print(f"    {e['raw']} → {e['correct']}")

    if audit["invalid"]:
        print(f"\n  === 不正データ ===")
        for e in audit["invalid"]:
            print(f"    {e['raw']}")

    # Phase 2: Viewer impact
    print("\n--- Phase 2: Viewer Impact ---")
    five_digit_tickers = [e["raw"] for e in audit["alpha_should_convert"] + audit["normal_5digit"] + audit["invalid"]]
    if five_digit_tickers:
        viewer_impact = check_viewer_impact(rest_url, headers, five_digit_tickers[:50])
        viewer_visible = [v for v in viewer_impact if v["in_viewer"]]
        print(f"  api_latest_financials に影響: {len(viewer_visible)}/{len(viewer_impact)} tickers")
        for v in viewer_visible:
            print(f"    {v['ticker']}: {v['row_count']} rows")
    else:
        viewer_impact = []
        print("  5桁 ticker なし")

    # Phase 3: Repair
    if opts.tickers:
        targets = opts.tickers.split(",")
        to_repair = [(t, normalize_ticker(t)) for t in targets if normalize_ticker(t) != t]
    else:
        to_repair = [
            (e["raw"], e["correct"])
            for e in audit["alpha_should_convert"]
        ]
        # Also include normal 5-digit that just need trailing-0 removal
        to_repair += [
            (e["raw"], e["correct"])
            for e in audit["normal_5digit"]
        ]

    print(f"\n--- Phase 3: Repair ({len(to_repair)} tickers) ---")
    results = []
    for old, new in to_repair:
        if old == new:
            continue
        res = repair_ticker(rest_url, headers, old, new, table=opts.table, dry_run=dry_run)
        results.append(res)
        status = "✅" if res.get("success", True) else "❌"
        print(f"  {status} {old} → {new}: {res['action']} ({res.get('rows', 0)} rows)")
        if not dry_run:
            time.sleep(0.1)

    # Summary
    print()
    print("=" * 60)
    print(f"  Summary: {len(results)} tickers processed")
    applied = sum(1 for r in results if r["action"] == "applied")
    skipped = sum(1 for r in results if r["action"] == "skip")
    dry_runs = sum(1 for r in results if r["action"] == "dry_run")
    print(f"    Applied: {applied}, Skipped: {skipped}, Dry-run: {dry_runs}")
    print("=" * 60)

    # JSON output
    if opts.json_output:
        output = {
            "timestamp": datetime.now(JST).isoformat(),
            "mode": "apply" if opts.apply else "dry_run",
            "audit": {
                "total_tickers": audit["total_tickers"],
                "five_digit_count": audit["five_digit_count"],
                "alpha_should_convert_count": len(audit["alpha_should_convert"]),
                "normal_5digit_count": len(audit["normal_5digit"]),
                "invalid_count": len(audit["invalid"]),
                "alpha_should_convert": audit["alpha_should_convert"],
                "normal_5digit": audit["normal_5digit"],
                "invalid": audit["invalid"],
            },
            "viewer_impact": viewer_impact,
            "repairs": results,
        }
        Path(opts.json_output).write_text(
            json.dumps(output, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n  JSON output: {opts.json_output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
