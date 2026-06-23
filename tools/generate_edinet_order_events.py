#!/usr/bin/env python3
# tools/generate_edinet_order_events.py
"""
EDINET受注データから Viewer 用の通知イベントを作成するスクリプト。

使用例:
    # DRY RUN (デフォルト)
    python tools/generate_edinet_order_events.py --today
    python tools/generate_edinet_order_events.py --date 2026-06-22

    # 本番実行 (DB保存)
    python tools/generate_edinet_order_events.py --date 2026-06-22 --apply
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.edinet_orders.saver import _get_client


def _get_edinet_targets_by_date(target_date: str) -> list[dict]:
    """EDINET API から指定日に提出された有価証券報告書の情報を取得する"""
    import requests
    api_key = os.environ.get("EDINET_API_KEY", "")
    if not api_key:
        print("[ERROR] EDINET_API_KEY is not set.")
        return []
    
    url = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
    params = {"date": target_date, "type": 2, "Subscription-Key": api_key}
    
    print(f"[API] Fetching EDINET documents for {target_date}...")
    try:
        resp = requests.get(url, params=params, timeout=60)
        if resp.status_code != 200:
            print(f"  [ERROR] HTTP {resp.status_code}")
            return []
        data = resp.json()
        if data.get("statusCode", 200) != 200:
            print(f"  [ERROR] API error: {data.get('message', '')}")
            return []
    except Exception as e:
        print(f"  [ERROR] request error: {e}")
        return []
        
    results = data.get("results", [])
    targets = []
    seen = set()
    
    def normalize_edinet_sec_code(sec_code: str | None) -> str | None:
        if not sec_code:
            return None
        code = str(sec_code).strip()
        if len(code) == 5 and code.endswith("0"):
            return code[:4]
        if len(code) == 4:
            return code
        print(f"[WARNING] Unexpected secCode format: {code}")
        return code
    
    for r in results:
        if r.get("docTypeCode", "") != "120":
            continue
        if r.get("xbrlFlag", "0") != "1":
            continue
        sec_code = (r.get("secCode") or "").strip()
        if not sec_code or len(sec_code) < 4:
            continue
            
        ticker = normalize_edinet_sec_code(sec_code)
        period_end = r.get("periodEnd")
        if not period_end:
            continue
            
        if ticker not in seen:
            seen.add(ticker)
            targets.append({
                "ticker": ticker,
                "company_name": r.get("filerName", ""),
                "doc_id": r.get("docID", ""),
                "submitted_at": r.get("submitDateTime", ""),
                "period_end": period_end
            })
            
    return targets


def _calculate_yoy(current: int | None, previous: int | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / abs(previous)


def generate_events(targets: list[dict], target_date_str: str, dry_run: bool = True):
    if not targets:
        print("[INFO] No targets provided.")
        return

    client = _get_client()
    tickers = [t["ticker"] for t in targets]
    
    # 対象銘柄の全保存データを取得（YoY計算や期末日照合のため）
    resp = client.table("edinet_order_data").select("*").in_("ticker", tickers).order("fiscal_year", desc=True).execute()
    records = resp.data or []
    
    company_data = {}
    for r in records:
        t = r["ticker"]
        if t not in company_data:
            company_data[t] = []
        company_data[t].append(r)
        
    events_to_insert = []
    results_report = []
    
    jst_now = datetime.now(timezone(timedelta(hours=9)))
    
    for target in targets:
        t = target["ticker"]
        filing_period_end = target["period_end"]
        company_name = target.get("company_name", "")
        
        data_list = company_data.get(t, [])
        if not data_list:
            results_report.append({
                "ticker": t, "company_name": company_name, "doc_id": target.get("doc_id"), 
                "submitted_at": target.get("submitted_at"), "filing_period_end": filing_period_end, 
                "matched_edinet_order_data_period_end": None, "orders_received": None, "order_backlog": None, "source_unit": None,
                "event_title": None, "dedupe_key": None, "action": "SKIP", "skip_reason": "SKIP_NO_ORDER_DATA"
            })
            continue
            
        # 提出有報のperiod_endと一致する保存データを探す
        matched_data = None
        for data in data_list:
            if data.get("period") == filing_period_end:
                matched_data = data
                break
                
        if not matched_data:
            # 提出されたperiod_endと一致するデータがDBにない
            latest_period = data_list[0].get("period") if data_list else None
            results_report.append({
                "ticker": t, "company_name": company_name, "doc_id": target.get("doc_id"), 
                "submitted_at": target.get("submitted_at"), "filing_period_end": filing_period_end, 
                "matched_edinet_order_data_period_end": latest_period, "orders_received": None, "order_backlog": None, "source_unit": None,
                "event_title": None, "dedupe_key": None, "action": "SKIP", "skip_reason": "SKIP_PERIOD_MISMATCH"
            })
            continue
            
        # 前年データを探す（YoY計算用）
        prev_data = None
        for data in data_list:
            if data.get("fiscal_year", 0) < matched_data.get("fiscal_year", 0):
                prev_data = data
                break
                
        # YoY計算
        or_yoy = _calculate_yoy(matched_data.get("orders_received"), prev_data.get("orders_received") if prev_data else None)
        ob_yoy = _calculate_yoy(matched_data.get("order_backlog"), prev_data.get("order_backlog") if prev_data else None)
        
        c_name = matched_data.get("company_name", company_name)
        period = matched_data.get("period")
        
        raw_payload = {
            "source": "edinet",
            "original_event_type": "edinet_order",
            "extracted": {
                "doc_id": matched_data.get("doc_id") or target.get("doc_id"),
                "ticker": t,
                "company_name": c_name,
                "period": period,
                "fiscal_year": matched_data.get("fiscal_year"),
                "quarter": matched_data.get("quarter") if matched_data.get("quarter") else "FY",
                "orders_received": matched_data.get("orders_received"),
                "orders_received_yoy": or_yoy,
                "order_backlog": matched_data.get("order_backlog"),
                "order_backlog_yoy": ob_yoy,
                "rpo": matched_data.get("rpo"),
                "confidence": matched_data.get("confidence"),
                "null_reason": matched_data.get("null_reason"),
                "source_unit": matched_data.get("source_unit")
            }
        }
        
        # dedupe_key に period_end を含める
        dedupe_key = f"edinet_order_{t}_{period}"
        event_title = f"{c_name} 受注/有報 FY"
        
        # 重複チェック (tdnet_events)
        check_resp = client.table("tdnet_events").select("id").eq("dedupe_key", dedupe_key).execute()
        if check_resp.data:
            results_report.append({
                "ticker": t, "company_name": c_name, "doc_id": target.get("doc_id"), 
                "submitted_at": target.get("submitted_at"), "filing_period_end": filing_period_end, 
                "matched_edinet_order_data_period_end": period, "orders_received": matched_data.get("orders_received"), 
                "order_backlog": matched_data.get("order_backlog"), "source_unit": matched_data.get("source_unit"),
                "event_title": event_title, "dedupe_key": dedupe_key, "action": "SKIP", "skip_reason": "SKIP_ALREADY_EXISTS"
            })
            continue
            
        event_record = {
            "id": str(uuid.uuid4()),
            "created_at": jst_now.isoformat(),
            "detected_at": jst_now.isoformat(),
            "disclosed_at": target.get("submitted_at") or jst_now.isoformat(),
            "ticker": t,
            "company_name": c_name,
            "event_type": "edinet_order",
            "headline": event_title,
            "summary": "EDINET受注データ抽出",
            "raw_payload": raw_payload,
            "priority_rank": 5,
            "display_title": event_title,
            "display_summary": "EDINET受注データ",
            "formatted_message": "",
            "dedupe_key": dedupe_key,
            "notify_to_discord": False,
            "status": "active",
            "schema_version": 1
        }
        events_to_insert.append(event_record)
        
        results_report.append({
            "ticker": t, "company_name": c_name, "doc_id": target.get("doc_id"), 
            "submitted_at": target.get("submitted_at"), "filing_period_end": filing_period_end, 
            "matched_edinet_order_data_period_end": period, "orders_received": matched_data.get("orders_received"), 
            "order_backlog": matched_data.get("order_backlog"), "source_unit": matched_data.get("source_unit"),
            "event_title": event_title, "dedupe_key": dedupe_key, "action": "INSERT", "skip_reason": "EVENT_CANDIDATE"
        })

    # ドライランのレポート出力
    scratch_dir = Path(__file__).parent.parent / "scratch"
    scratch_dir.mkdir(exist_ok=True)
    
    base_name = f"edinet_order_event_dryrun_{target_date_str.replace('-', '')}"
    csv_path = scratch_dir / f"{base_name}.csv"
    md_path = scratch_dir / f"{base_name}.md"
    
    if results_report:
        keys = results_report[0].keys()
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(results_report)
            
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# EDINET Order Event Dry-run Report: {target_date_str}\n\n")
        
        cand = [r for r in results_report if r["action"] == "INSERT"]
        f.write(f"## EVENT_CANDIDATE: {len(cand)}\n\n")
        f.write("| ticker | company_name | filing_period_end | orders_received | order_backlog | source_unit |\n")
        f.write("|---|---|---|---|---|---|\n")
        for r in cand:
            f.write(f"| {r['ticker']} | {r['company_name']} | {r['filing_period_end']} | {r['orders_received']} | {r['order_backlog']} | {r['source_unit']} |\n")
        f.write("\n")
        
        skips = [r for r in results_report if r["action"] == "SKIP"]
        f.write(f"## SKIP: {len(skips)}\n\n")
        f.write("| ticker | company_name | skip_reason | filing_period | matched_period |\n")
        f.write("|---|---|---|---|---|\n")
        for r in skips:
            f.write(f"| {r['ticker']} | {r['company_name']} | {r['skip_reason']} | {r['filing_period_end']} | {r['matched_edinet_order_data_period_end']} |\n")
            
    print(f"\n[SUMMARY] {len(events_to_insert)} events prepared (EVENT_CANDIDATE).")
    print(f"[SUMMARY] Report saved to {csv_path} and {md_path}")
    
    if dry_run:
        print("\n[DRY RUN] データベースへの書き込み(INSERT)をスキップしました。")
        print("          ※ 本番書き込みを行うには `--apply` を付与して実行してください。")
    else:
        if events_to_insert:
            print("\n[APPLY] Inserting events into tdnet_events...")
            try:
                client.table("tdnet_events").insert(events_to_insert).execute()
                print("  Success!")
            except Exception as e:
                print(f"  [ERROR] Failed to insert events: {e}")
        else:
            print("\n[APPLY] No events to insert.")


def main():
    parser = argparse.ArgumentParser(description="EDINET受注データから通知イベントを生成")
    # デフォルトで dry-run となり、--apply のみでDB書き込み
    parser.add_argument("--apply", action="store_true", help="DBへイベントを書き込む (指定がなければdry-run)")
    parser.add_argument("--dry-run", action="store_true", help="明示的なdry-run指定 (デフォルトと同じ動作)")
    parser.add_argument("--today", action="store_true", help="今日提出された有報を対象とする")
    parser.add_argument("--date", type=str, help="指定日(YYYY-MM-DD)に提出された有報を対象とする")
    parser.add_argument("--tickers", nargs="+", help="※現在非対応 (period_endが特定できないため禁止)")
    args = parser.parse_args()

    if args.dry_run and args.apply:
        print("[ERROR] --dry-run と --apply は同時に指定できません")
        sys.exit(1)
        
    if args.tickers:
        print("[ERROR] --tickers は現在非対応です。提出日の period_end と照合できないため禁止されています。")
        sys.exit(1)
        
    if args.today and args.date:
        print("[ERROR] --today と --date は同時に指定できません")
        sys.exit(1)

    is_dry_run = not args.apply
    
    print("=" * 60)
    print("EDINET通知イベント生成 (安全化版)")
    print(f"  mode    : {'DRY RUN (DB保存なし)' if is_dry_run else 'APPLY (DB保存実行)'}")
    if args.today:
        print("  target  : TODAY")
    elif args.date:
        print(f"  target  : DATE ({args.date})")
    else:
        print("  [ERROR] --today または --date のいずれかを指定してください")
        sys.exit(1)
    print("=" * 60)

    jst = timezone(timedelta(hours=9))
    target_date = args.date if args.date else datetime.now(jst).strftime("%Y-%m-%d")
    
    # APIから対象リストとそれぞれの期末日を取得
    target_dicts = _get_edinet_targets_by_date(target_date)

    if not target_dicts:
        print("[INFO] Target tickers is empty. Exiting.")
        return

    generate_events(target_dicts, target_date, dry_run=is_dry_run)


if __name__ == "__main__":
    main()
