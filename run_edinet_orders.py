#!/usr/bin/env python3
# run_edinet_orders.py
"""
EDINET受注データ 抽出→DB保存 エントリーポイント

使用例:
    # 抽出 + DB保存（31社）
    python run_edinet_orders.py

    # DRY RUN（DB保存なし）
    python run_edinet_orders.py --dry-run

    # 特定企業のみ
    python run_edinet_orders.py --tickers 1812 6141 6834

    # 既存JSONから保存のみ（再抽出しない）
    python run_edinet_orders.py --from-json scratch/orders_extracted_30_v4.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# .env 読み込み
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).parent))

from src.edinet_orders.extractor import extract
from src.edinet_orders.transformer import transform_to_db_row
from src.edinet_orders.saver import save_to_db

SURVEY_JSON = Path(r"C:\Users\takuy\.gemini\antigravity\brain\8ceab1ef-6c13-410f-9a78-5f3b53e47b74\scratch\survey_detail.json")
SCRATCH_DIR = Path(__file__).parent / "scratch"


def _load_survey() -> list[dict]:
    with open(SURVEY_JSON, encoding="utf-8") as f:
        return json.load(f)


def _build_fiscal_end_map(survey_data: list[dict]) -> dict[str, str]:
    return {
        d["ticker"]: d["fiscal_end"]
        for d in survey_data
        if d.get("ticker") and d.get("fiscal_end")
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="EDINET受注データ 抽出→DB保存")
    parser.add_argument("--dry-run", action="store_true", help="DB保存をスキップ")
    parser.add_argument("--tickers", nargs="+", help="対象銘柄コードを指定（省略時は全社）")
    parser.add_argument("--from-json", type=Path, help="既存JSONから保存（再抽出しない）")
    parser.add_argument("--save-json", type=Path, help="抽出結果JSONの保存先（省略時は scratch/edinet_orders_YYYYMMDD.json）")
    args = parser.parse_args()

    print("=" * 60)
    print("EDINET受注データ保存パイプライン")
    print(f"  mode    : {'DRY RUN' if args.dry_run else 'LIVE'}")
    print(f"  tickers : {args.tickers or 'ALL'}")
    print("=" * 60)

    survey_data = _load_survey()
    fiscal_end_map = _build_fiscal_end_map(survey_data)
    print(f"survey_detail: {len(survey_data)} entries, fiscal_end mapped: {len(fiscal_end_map)}")

    # ── 1. 抽出 ──
    if args.from_json:
        print(f"\n[SKIP EXTRACT] Loading from: {args.from_json}")
        with open(args.from_json, encoding="utf-8") as f:
            extracted_list = json.load(f)
        # from-json はリスト形式
        if isinstance(extracted_list, list) and extracted_list and "rows" in extracted_list[0]:
            extracted_list = extracted_list[0]["rows"]  # DRY RUN JSON形式への対応
    else:
        # survey_data をフィルタ（tickers指定があれば絞り込み）
        target_survey = survey_data
        if args.tickers:
            target_survey = [d for d in survey_data if d.get("ticker") in args.tickers]
            print(f"\n[FILTER] {len(target_survey)} companies selected")

        print(f"\n[EXTRACT] Start extracting {len([d for d in target_survey if d.get('doc_id')])} companies...")
        extracted_list = extract(target_survey)

    # ── 2. JSON 保存 ──
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = args.save_json or (SCRATCH_DIR / f"edinet_orders_{ts}.json")
    SCRATCH_DIR.mkdir(exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(extracted_list, f, ensure_ascii=False, indent=2)
    print(f"\n[JSON] Saved {len(extracted_list)} records → {json_path}")

    # ── 3. DB形式へ変換 ──
    print("\n[TRANSFORM] Converting to DB format...")
    db_rows = []
    for item in extracted_list:
        ticker = item.get("ticker")
        fiscal_end = fiscal_end_map.get(ticker)
        if not fiscal_end:
            print(f"  WARNING: fiscal_end not found for {ticker}")
        row = transform_to_db_row(item, fiscal_end=fiscal_end)
        db_rows.append(row)

    # ── 4. 統計表示 ──
    conf_stats: dict[str, int] = {}
    unit_stats: dict[str, int] = {}
    nr_stats: dict[str, int] = {}
    period_stats: dict[str, int] = {}
    or_cnt = ob_cnt = cc_cnt = comp_cnt = rpo_cnt = 0

    for r in db_rows:
        c = r.get("confidence", "low")
        conf_stats[c] = conf_stats.get(c, 0) + 1
        u = r.get("source_unit", "unknown")
        unit_stats[u] = unit_stats.get(u, 0) + 1
        nr = r.get("null_reason")
        if nr:
            nr_stats[nr] = nr_stats.get(nr, 0) + 1
        p = r.get("period")
        if p:
            period_stats[p] = period_stats.get(p, 0) + 1
        if r.get("orders_received") is not None:
            or_cnt += 1
        if r.get("order_backlog") is not None:
            ob_cnt += 1
        if r.get("construction_carryover") is not None:
            cc_cnt += 1
        if r.get("completed_construction") is not None:
            comp_cnt += 1
        if r.get("rpo") is not None:
            rpo_cnt += 1

    print(f"\n[STATS]")
    print(f"  Total rows      : {len(db_rows)}")
    print(f"  confidence      : {conf_stats}")
    print(f"  source_unit     : {unit_stats}")
    print(f"  null_reason     : {nr_stats}")
    print(f"  period values   : {period_stats}")
    print(f"  orders_received : {or_cnt}")
    print(f"  order_backlog   : {ob_cnt}")
    print(f"  construction_carryover : {cc_cnt}")
    print(f"  completed_construction : {comp_cnt}")
    print(f"  rpo             : {rpo_cnt}")

    # ── 5. BEFORE サンプル5件 ──
    print("\n[BEFORE INSERT - sample 5]") 
    for r in db_rows[:5]:
        print(
            f"  {r['ticker']} {r['company_name']}"
            f" period={r['period']} fiscal_year={r['fiscal_year']}"
            f" su={r['source_unit']} orders_received={r['orders_received']}"
            f" raw_or={r['raw_orders_received']}"
            f" rpo={r['rpo']} conf={r['confidence']}"
        )

    # ── 6. DB 保存 ──
    print(f"\n[SAVE] dry_run={args.dry_run}")
    stats = save_to_db(db_rows, dry_run=args.dry_run)

    # ── 7. AFTER サンプル5件（LIVE時のみ確認） ──
    if not args.dry_run and not stats["errors"]:
        try:
            from src.edinet_orders.saver import _get_client
            sb = _get_client()
            resp = (
                sb.table("edinet_order_data")
                .select(
                    "ticker,company_name,period,fiscal_year,"
                    "orders_received,order_backlog,rpo,"
                    "raw_orders_received,raw_order_backlog,raw_rpo,"
                    "source_unit,segment_name,segment_name_key,"
                    "confidence,null_reason"
                )
                .order("ticker")
                .limit(5)
                .execute()
            )
            print("\n[AFTER INSERT - sample 5 from DB]")
            for row in (resp.data or []):
                print(
                    f"  {row.get('ticker')} {row.get('company_name')}"
                    f" period={row.get('period')} fiscal_year={row.get('fiscal_year')}"
                    f" segment_name_key={row.get('segment_name_key')}"
                    f" su={row.get('source_unit')}"
                    f" orders_received={row.get('orders_received')}"
                    f" raw_or={row.get('raw_orders_received')}"
                    f" rpo={row.get('rpo')} conf={row.get('confidence')}"
                )
        except Exception as e:
            print(f"  [WARNING] Post-insert SELECT failed: {e}")

    print("\n[DONE]")
    print(f"  upserted : {stats.get('upserted', 0)}")
    print(f"  skipped  : {stats.get('skipped', 0)}")
    print(f"  errors   : {len(stats.get('errors', []))}")
    if stats.get("errors"):
        for err in stats["errors"]:
            print(f"    - {err}")


if __name__ == "__main__":
    main()
