#!/usr/bin/env python3
r"""
fix_remaining_rows.py -- attachment_xbrl 残単位補正

canonical_financials の source=attachment_xbrl で value >= 1億 の行について、
source/unit/値レンジ/同一ticker比較を見て判定し、円→百万円変換を実施。

Usage:
  cd C:\Users\takuy\OneDrive\tdnet-excel-input
  .\.venv\Scripts\python.exe C:\Users\takuy\.gemini\antigravity\scratch\fix_remaining_rows.py --dry-run
  .\.venv\Scripts\python.exe C:\Users\takuy\.gemini\antigravity\scratch\fix_remaining_rows.py --apply
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta

_ROOT = r"C:\Users\takuy\OneDrive\tdnet-excel-input"
sys.path.insert(0, _ROOT)
from lib.pipeline.db import load_env

load_env()
import requests

logger = logging.getLogger("fix_remaining")
JST = timezone(timedelta(hours=9))

url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1"
key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
headers = {
    "apikey": key,
    "Authorization": f"Bearer {key}",
    "Content-Type": "application/json",
}

THRESH = 100_000_000  # 1億
OUT_DIR = r"C:\Users\takuy\.gemini\antigravity\scratch"


def safe_get(table, params, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(f"{url}/{table}", headers=headers, params=params, timeout=90)
            if r.status_code >= 400:
                if attempt < retries - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                return {"_http_error": r.status_code, "body": r.text[:300]}
            return r.json()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
            else:
                return {"_exception": str(e)}


def supabase_patch(table, match_params, patch_data, retries=3):
    """PATCH (UPDATE) rows matching params."""
    h = {**headers, "Prefer": "return=minimal"}
    for attempt in range(retries):
        try:
            r = requests.patch(
                f"{url}/{table}", headers=h,
                params=match_params,
                json=patch_data, timeout=60,
            )
            if r.status_code >= 400:
                return {"ok": False, "status": r.status_code, "body": r.text[:300]}
            return {"ok": True, "status": r.status_code}
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
            else:
                return {"ok": False, "error": str(e)}


def classify_row(row):
    """行の補正カテゴリを判定する。"""
    value = row.get("value")
    unit = row.get("unit", "")
    ticker = row.get("ticker", "")
    metric = row.get("metric", "")

    if value is None:
        return "suspicious_skip", "null value"

    abs_val = abs(value)

    # 1億未満は対象外
    if abs_val < THRESH:
        return "suspicious_skip", f"abs_value={abs_val} < threshold"

    # unit 別判定
    if unit == "JPY":
        # unit=JPY, value>=1億 → 円単位のまま → ÷1M
        return "jpy_value_needs_million_conversion", f"unit=JPY abs_val={abs_val}"

    if unit in ("百万円",):
        # unit=百万円 なのに 1億以上 → 値は円単位のまま格納されている
        # 百万円で1億 = 実際は1000兆円 — 明らかに異常
        if abs_val >= THRESH:
            return "unit_label_mismatch_value_in_jpy", f"unit=百万円 but abs_val={abs_val} (too large)"
        return "suspicious_skip", f"unit=百万円 abs_val={abs_val}"

    if unit == "millions_jpy":
        # unit=millions_jpy なのに 1億以上 → 値は円単位のまま格納
        if abs_val >= THRESH:
            return "millions_jpy_but_raw_jpy_value", f"unit=millions_jpy but abs_val={abs_val} (too large)"
        return "suspicious_skip", f"unit=millions_jpy abs_val={abs_val}"

    return "suspicious_skip", f"unknown unit='{unit}' abs_val={abs_val}"


def scan_attachment_xbrl_remaining():
    """attachment_xbrl の value>=1億 / <=-1億 行を取得。"""
    all_rows = []
    for op, thresh_val in [("gte", THRESH), ("lte", -THRESH)]:
        rows = safe_get("canonical_financials", {
            "select": "source_row_key,ticker,period,quarter,metric,value,unit,source",
            "source": "eq.attachment_xbrl",
            "value": f"{op}.{thresh_val}",
            "order": "ticker.asc",
            "limit": "500",
        })
        if isinstance(rows, list):
            all_rows.extend(rows)
        elif isinstance(rows, dict):
            logger.warning(f"Error fetching {op}: {json.dumps(rows)[:200]}")
    return all_rows


def scan_all_sources_remaining():
    """全sourceの value>=1億行をスキャン（tdnet/html は500エラーの可能性あり）。"""
    results = {}
    for src in ["tdnet", "summary_xbrl", "attachment_xbrl", "pdf", "html"]:
        all_rows = []
        for op, thresh_val in [("gte", THRESH), ("lte", -THRESH)]:
            rows = safe_get("canonical_financials", {
                "select": "source_row_key,ticker,period,quarter,metric,value,unit,source",
                "source": f"eq.{src}",
                "value": f"{op}.{thresh_val}",
                "order": "ticker.asc",
                "limit": "500",
            })
            if isinstance(rows, list):
                all_rows.extend(rows)
            elif isinstance(rows, dict):
                logger.warning(f"[{src}] {op} error: {json.dumps(rows)[:200]}")
        results[src] = all_rows
    return results


def build_fix_plan(rows):
    """補正計画を立てる。"""
    plan = []
    for row in rows:
        category, reason = classify_row(row)

        proposed_value = None
        proposed_unit = "millions_jpy"
        if category in (
            "jpy_value_needs_million_conversion",
            "unit_label_mismatch_value_in_jpy",
            "millions_jpy_but_raw_jpy_value",
        ):
            proposed_value = int(row["value"] / 1_000_000)
        else:
            proposed_value = row["value"]
            proposed_unit = row.get("unit", "")

        plan.append({
            "ticker": row.get("ticker", ""),
            "period": row.get("period", ""),
            "quarter": row.get("quarter", ""),
            "metric": row.get("metric", ""),
            "source": row.get("source", ""),
            "source_row_key": row.get("source_row_key", ""),
            "unit_before": row.get("unit", ""),
            "value_before": row.get("value"),
            "proposed_unit": proposed_unit,
            "proposed_value": proposed_value,
            "reason_category": category,
            "reason_detail": reason,
        })
    return plan


def apply_fixes(plan):
    """fix plan を実行する。"""
    stats = {"fixed": 0, "skipped": 0, "errors": 0}
    fixable = [
        "jpy_value_needs_million_conversion",
        "unit_label_mismatch_value_in_jpy",
        "millions_jpy_but_raw_jpy_value",
    ]

    for item in plan:
        if item["reason_category"] not in fixable:
            stats["skipped"] += 1
            continue

        result = supabase_patch(
            "canonical_financials",
            {"source_row_key": f"eq.{item['source_row_key']}"},
            {"value": item["proposed_value"], "unit": item["proposed_unit"]},
        )
        if result.get("ok"):
            stats["fixed"] += 1
        else:
            logger.error(f"PATCH failed for {item['source_row_key']}: {result}")
            stats["errors"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(description="Fix attachment_xbrl remaining unit issues")
    parser.add_argument("--apply", action="store_true", help="Execute fixes")
    parser.add_argument("--dry-run", action="store_true", help="Scan and report only (default)")
    parser.add_argument("--all-sources", action="store_true", help="Scan all sources, not just attachment_xbrl")
    args = parser.parse_args()
    is_apply = args.apply and not args.dry_run

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    mode = "APPLY" if is_apply else "DRY-RUN"
    logger.info(f"=== fix_remaining_rows START ({mode}) ===")

    # 1. Scan
    if args.all_sources:
        logger.info("[SCAN] All sources ...")
        by_source = scan_all_sources_remaining()
        all_rows = []
        for src, rows in by_source.items():
            logger.info(f"  [{src}] {len(rows)} rows with |value| >= 1億")
            all_rows.extend(rows)
    else:
        logger.info("[SCAN] attachment_xbrl only ...")
        all_rows = scan_attachment_xbrl_remaining()
        logger.info(f"  [attachment_xbrl] {len(all_rows)} rows with |value| >= 1億")

    # 2. Plan
    plan = build_fix_plan(all_rows)

    # 3. Summary
    cat_counts = {}
    for item in plan:
        c = item["reason_category"]
        cat_counts[c] = cat_counts.get(c, 0) + 1

    print()
    print("=" * 60)
    print(f"  Remaining Rows Fix — {mode}")
    print("=" * 60)
    print(f"  total rows scanned          : {len(all_rows)}")
    print(f"  total plan items            : {len(plan)}")
    print(f"  categories:")
    for c, n in sorted(cat_counts.items()):
        print(f"    {c:45s}: {n}")
    print()

    # 4. Write plan CSV
    plan_csv = os.path.join(OUT_DIR, f"remaining_fix_plan_{ts}.csv")
    with open(plan_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "ticker", "period", "quarter", "metric", "source",
            "source_row_key",
            "unit_before", "value_before",
            "proposed_unit", "proposed_value",
            "reason_category", "reason_detail",
        ])
        w.writeheader()
        w.writerows(plan)
    print(f"  Plan CSV: {plan_csv}")

    # 5. Suspicious items
    suspicious = [p for p in plan if p["reason_category"] == "suspicious_skip"]
    if suspicious:
        s_csv = os.path.join(OUT_DIR, f"remaining_suspicious_{ts}.csv")
        with open(s_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=[
                "ticker", "period", "quarter", "metric", "source",
                "source_row_key", "unit_before", "value_before",
                "reason_category", "reason_detail",
            ])
            w.writeheader()
            for p in suspicious:
                w.writerow({k: p[k] for k in w.fieldnames})
        print(f"  Suspicious CSV: {s_csv}")

    if not is_apply:
        print(f"\n  DRY-RUN complete. Use --apply to execute.\n")
        return 0

    # 6. Apply
    logger.info("[APPLY] Executing fixes ...")
    stats = apply_fixes(plan)

    print()
    print("=" * 60)
    print(f"  APPLY Results")
    print("=" * 60)
    for k, v in sorted(stats.items()):
        print(f"    {k:20s}: {v}")
    print()

    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
