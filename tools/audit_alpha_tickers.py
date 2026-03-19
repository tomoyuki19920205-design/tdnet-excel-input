#!/usr/bin/env python3
# ============================================================
# audit_alpha_tickers.py — alpha ticker 整合性チェック
# ============================================================
"""
Supabase / SQLite 上の alpha ticker (418A, 421A, 429A ...) が
全パイプラインで正しく処理されていること、5桁残存がないことを検証する。

Usage:
  .\.venv\Scripts\python.exe tools\audit_alpha_tickers.py
  .\.venv\Scripts\python.exe tools\audit_alpha_tickers.py --fix
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import requests
from lib.pipeline.db import load_env
from src.common_ticker import normalize_ticker, is_valid_ticker

JST = timezone(timedelta(hours=9))
DB_PATH = os.path.join(_PROJECT_ROOT, "data", "jquants.db")


def _supa_headers():
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    return (
        f"{url}/rest/v1",
        {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )


def audit_sqlite(conn: sqlite3.Connection) -> dict:
    """SQLite 上の alpha ticker / 5桁残存チェック"""
    conn.row_factory = sqlite3.Row
    all_codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT local_code FROM jquants_financials_normalized ORDER BY local_code"
    ).fetchall()]

    alpha_codes = [c for c in all_codes if any(ch.isalpha() for ch in c)]
    five_digit = [c for c in all_codes if len(c) == 5]

    # 41800 パターン検出: 5桁の純数値で末尾00かつ対応するalphaコードが存在する可能性
    suspicious_numeric = []
    alpha_prefix_set = set()
    for c in alpha_codes:
        normalized = normalize_ticker(c)
        if len(normalized) >= 3:
            alpha_prefix_set.add(normalized[:3])

    for c in all_codes:
        if len(c) == 5 and c.isdigit() and c.endswith("00"):
            prefix = c[:3]
            if prefix in alpha_prefix_set:
                suspicious_numeric.append(c)

    return {
        "total_codes": len(all_codes),
        "alpha_codes": len(alpha_codes),
        "alpha_list": sorted(alpha_codes),
        "five_digit_codes": len(five_digit),
        "suspicious_numeric_alpha": suspicious_numeric,
    }


def audit_supabase(rest_url: str, headers: dict) -> dict:
    """Supabase 上の alpha ticker 整合性チェック"""
    results = {}

    for table in ["financials", "canonical_financials", "api_latest_financials", "companies"]:
        ticker_col = "ticker_code" if table == "companies" else "ticker"
        try:
            r = requests.get(
                f"{rest_url}/{table}?select={ticker_col}&limit=5000",
                headers={k: v for k, v in headers.items() if k != "Content-Type"},
                timeout=30,
            )
            if r.status_code == 200:
                all_tickers = [row[ticker_col] for row in r.json()]
                alpha = [t for t in all_tickers if any(c.isalpha() for c in str(t))]
                five_digit = [t for t in all_tickers if len(str(t)) == 5]
                invalid = [t for t in all_tickers if not is_valid_ticker(str(t))]
                results[table] = {
                    "total": len(set(all_tickers)),
                    "alpha": sorted(set(alpha)),
                    "five_digit_remaining": sorted(set(five_digit)),
                    "invalid_format": sorted(set(invalid))[:20],
                }
            else:
                results[table] = {"error": f"HTTP {r.status_code}"}
        except Exception as e:
            results[table] = {"error": str(e)}

    return results


def audit_specific_tickers(rest_url: str, headers: dict) -> dict:
    """代表 alpha ticker の全経路チェック"""
    tickers = ["418A", "421A", "429A", "130A", "135A", "137A"]
    results = {}

    for t in tickers:
        status = {}
        for table in ["financials", "canonical_financials", "api_latest_financials"]:
            r = requests.get(
                f"{rest_url}/{table}?ticker=eq.{t}&select=ticker&limit=1",
                headers={k: v for k, v in headers.items() if k != "Content-Type"},
                timeout=15,
            )
            d = r.json() if r.status_code == 200 else []
            status[table] = "✅" if d else "❌"

        r = requests.get(
            f"{rest_url}/companies?ticker_code=eq.{t}&select=ticker_code&limit=1",
            headers={k: v for k, v in headers.items() if k != "Content-Type"},
            timeout=15,
        )
        d = r.json() if r.status_code == 200 else []
        status["companies"] = "✅" if d else "❌"

        status["all_ok"] = all(v == "✅" for v in status.values())
        results[t] = status

    return results


def main():
    parser = argparse.ArgumentParser(description="Alpha ticker 整合性チェック")
    parser.add_argument("--fix", action="store_true", help="問題発見時に自動修正 (未実装)")
    parser.add_argument("--json", action="store_true", help="JSON 出力")
    opts = parser.parse_args()

    load_env()
    rest_url, headers = _supa_headers()

    print("=" * 60)
    print("  Alpha Ticker 整合性監査")
    print("=" * 60)

    # 1. SQLite
    print("\n--- SQLite ---")
    conn = sqlite3.connect(DB_PATH)
    sqlite_result = audit_sqlite(conn)
    conn.close()
    print(f"  Total codes: {sqlite_result['total_codes']}")
    print(f"  Alpha codes: {sqlite_result['alpha_codes']}")
    print(f"  Suspicious numeric (should be alpha): {sqlite_result['suspicious_numeric_alpha']}")

    # 2. Supabase
    print("\n--- Supabase ---")
    supa_result = audit_supabase(rest_url, headers)
    for table, info in supa_result.items():
        if "error" in info:
            print(f"  {table}: ERROR {info['error']}")
        else:
            print(f"  {table}: {info['total']} unique tickers, "
                  f"{len(info['alpha'])} alpha, "
                  f"{len(info['five_digit_remaining'])} five-digit remaining, "
                  f"{len(info['invalid_format'])} invalid")
            if info['five_digit_remaining']:
                print(f"    ⚠️  5桁残存: {info['five_digit_remaining'][:10]}")
            if info['alpha']:
                print(f"    Alpha: {info['alpha'][:15]}")

    # 3. Specific tickers
    print("\n--- Alpha Ticker 全経路チェック ---")
    specific = audit_specific_tickers(rest_url, headers)
    all_ok = True
    for t, status in sorted(specific.items()):
        ok = "✅ ALL OK" if status["all_ok"] else "❌ ISSUES"
        print(f"  {t}: {ok} | "
              f"fin={status['financials']} canon={status['canonical_financials']} "
              f"view={status['api_latest_financials']} comp={status['companies']}")
        if not status["all_ok"]:
            all_ok = False

    # Summary
    print()
    print("=" * 60)
    if all_ok and not sqlite_result['suspicious_numeric_alpha']:
        print("  ✅ PASS: 全チェック合格")
    else:
        print("  ❌ FAIL: 問題あり")
        if sqlite_result['suspicious_numeric_alpha']:
            print(f"    SQLite suspicious: {sqlite_result['suspicious_numeric_alpha']}")
    print("=" * 60)

    if opts.json:
        print(json.dumps({
            "sqlite": sqlite_result,
            "supabase": supa_result,
            "specific_tickers": specific,
        }, indent=2, default=str, ensure_ascii=False))

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
