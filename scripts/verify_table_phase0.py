#!/usr/bin/env python3
"""verify_table_phase0.py — Phase 0 テーブル優先版 before/after 検証

before (tables=None) と after (tables=実データ) の previous 取得率を比較する。
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# UTF-8 出力
if sys.stdout and hasattr(sys.stdout, "encoding"):
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from src.events.env_loader import load_project_env
load_project_env()

from src.fetcher import fetch_new_disclosures
from src.events.forecast_extractor import extract_forecast_revision
from src.events.forecast_classifier import classify_forecast
from src.events.common_notify import build_event_parts, _fmt_amount_billion, _fmt_pct
from src.events.common_models import EventRecord, EventType

logging.basicConfig(level=logging.WARNING, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")

# ============================================================
# PDF テキスト＋テーブル抽出
# ============================================================
def _extract_text_and_tables(doc_url: str):
    """PDFからテキストとテーブル構造を抽出"""
    if not doc_url:
        return "", []
    try:
        import io as _io
        import pdfplumber
        import requests

        if not any(h in doc_url for h in ["tdnet.info", "disclosure.edinet"]):
            return "", []

        resp = requests.get(doc_url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; TDNETEventBot/1.0)"
        })
        if resp.status_code != 200:
            return "", []

        is_pdf = "pdf" in resp.headers.get("Content-Type", "").lower() or resp.content[:5] == b"%PDF-"
        if not is_pdf:
            return "", []

        with pdfplumber.open(_io.BytesIO(resp.content)) as pdf:
            texts = []
            all_tables = []
            for page in pdf.pages[:10]:
                texts.append(page.extract_text() or "")
                page_tables = page.extract_tables()
                if page_tables:
                    all_tables.extend(page_tables)
            return "\n".join(texts), all_tables
    except Exception as e:
        return "", []


# ============================================================
# メッセージフォーマット（簡易版）
# ============================================================
def _format_metric(label, prev, rev, pct):
    if rev is not None and prev is not None:
        return f"{label}: {_fmt_amount_billion(prev)}→{_fmt_amount_billion(rev)}({_fmt_pct(pct)})"
    elif rev is not None:
        return f"{label}: {_fmt_amount_billion(rev)}"
    return None


def _format_ev_short(ev):
    parts = []
    for label, key_prev, key_rev, key_pct in [
        ("純利益", "previous_net_income", "revised_net_income", "change_net_income_pct"),
        ("営業利益", "previous_op", "revised_op", "change_op_pct"),
        ("売上高", "previous_sales", "revised_sales", "change_sales_pct"),
    ]:
        prev = getattr(ev, key_prev, None)
        rev = getattr(ev, key_rev, None)
        pct = getattr(ev, key_pct, None)
        s = _format_metric(label, prev, rev, pct)
        if s:
            parts.append(s)
    return " / ".join(parts) if parts else "(抽出なし)"


# ============================================================
# メイン
# ============================================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Phase 0 テーブル優先版 dry-run 検証")
    parser.add_argument("--date", type=str, default=None, help="対象日 (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, default=3, help="何日分遡るか (default: 3)")
    args = parser.parse_args()

    from datetime import date, timedelta
    dates = []
    if args.date:
        dates = [args.date]
    else:
        today = date.today()
        for i in range(args.days):
            d = today - timedelta(days=i)
            dates.append(d.strftime("%Y-%m-%d"))

    print(f"=== Phase 0 before/after 検証 ===")
    print(f"対象日: {dates}")
    print()

    # 結果集計
    stats_before = {"revised": 0, "previous": 0, "pair": 0, "no_prev": 0, "total": 0}
    stats_after  = {"revised": 0, "previous": 0, "pair": 0, "no_prev": 0, "total": 0}
    samples = []  # (ticker, company, before_msg, after_msg, improved)
    errors = []   # 誤抽出候補

    for target_date in dates:
        print(f"--- {target_date} ---")
        items = fetch_new_disclosures(target_date=target_date)
        print(f"  開示件数: {len(items)}")

        for item in items:
            title = item.title
            cls_result = classify_forecast(title, "")
            if not cls_result.is_target:
                continue

            is_diff = cls_result.subtype_hint == "difference"
            doc_url = item.doc_url

            text, tables = _extract_text_and_tables(doc_url)
            if not text:
                continue

            # ---- BEFORE: tables=None ----
            ev_before = extract_forecast_revision(text, title, is_difference=is_diff, tables=None)

            # ---- AFTER: tables=実データ ----
            ev_after = extract_forecast_revision(text, title, is_difference=is_diff, tables=tables)

            # 集計
            for ev, stats in [(ev_before, stats_before), (ev_after, stats_after)]:
                has_rev = any(getattr(ev, f"revised_{m}", None) is not None
                             for m in ["sales", "op", "ordinary", "net_income"])
                has_prev = any(getattr(ev, f"previous_{m}", None) is not None
                              for m in ["sales", "op", "ordinary", "net_income"])
                if has_rev:
                    stats["revised"] += 1
                if has_prev:
                    stats["previous"] += 1
                if has_rev and has_prev:
                    stats["pair"] += 1
                if has_rev and not has_prev:
                    stats["no_prev"] += 1
                stats["total"] += 1

            # サンプル記録（before で prev なし → after で prev あり）
            before_has_prev = any(getattr(ev_before, f"previous_{m}", None) is not None
                                  for m in ["sales", "op", "ordinary", "net_income"])
            after_has_prev = any(getattr(ev_after, f"previous_{m}", None) is not None
                                for m in ["sales", "op", "ordinary", "net_income"])
            improved = not before_has_prev and after_has_prev

            before_msg = _format_ev_short(ev_before)
            after_msg = _format_ev_short(ev_after)

            if improved or len(samples) < 3:
                samples.append({
                    "ticker": item.ticker,
                    "company": item.company_name,
                    "title": title[:40],
                    "before": before_msg,
                    "after": after_msg,
                    "improved": improved,
                    "before_source": ev_before.extraction_source,
                    "after_source": ev_after.extraction_source,
                })

            # 誤抽出チェック
            for m in ["sales", "op", "ordinary", "net_income"]:
                prev_a = getattr(ev_after, f"previous_{m}", None)
                rev_a = getattr(ev_after, f"revised_{m}", None)
                if prev_a is not None and rev_a is not None:
                    # previous > revised なのに upward → 逆転疑い
                    if ev_after.subtype == "upward" and prev_a > rev_a:
                        errors.append(f"{item.ticker} {m}: prev={prev_a} > rev={rev_a} but subtype=upward")
                    if ev_after.subtype == "downward" and prev_a < rev_a:
                        errors.append(f"{item.ticker} {m}: prev={prev_a} < rev={rev_a} but subtype=downward")
                    # 単位ずれ（10倍以上の差）
                    if rev_a != 0 and abs(prev_a / rev_a) > 10:
                        errors.append(f"{item.ticker} {m}: 単位ずれ疑い prev={prev_a} rev={rev_a}")

        print(f"  forecast対象: {stats_after['total'] - sum(1 for _ in []) if True else 0}")

    # ============================================================
    # レポート出力
    # ============================================================
    print()
    print("=" * 70)
    print("  PHASE 0 BEFORE/AFTER 比較レポート")
    print("=" * 70)
    print()
    print(f"{'指標':<25s} {'BEFORE':>10s} {'AFTER':>10s} {'差分':>10s}")
    print("-" * 55)
    print(f"{'対象件数':<25s} {stats_before['total']:>10d} {stats_after['total']:>10d}")
    print(f"{'revised有件数':<25s} {stats_before['revised']:>10d} {stats_after['revised']:>10d} {stats_after['revised'] - stats_before['revised']:>+10d}")
    print(f"{'previous有件数':<25s} {stats_before['previous']:>10d} {stats_after['previous']:>10d} {stats_after['previous'] - stats_before['previous']:>+10d}")
    print(f"{'pair成立件数':<25s} {stats_before['pair']:>10d} {stats_after['pair']:>10d} {stats_after['pair'] - stats_before['pair']:>+10d}")
    print(f"{'previous未取得件数':<25s} {stats_before['no_prev']:>10d} {stats_after['no_prev']:>10d} {stats_after['no_prev'] - stats_before['no_prev']:>+10d}")

    if stats_before['no_prev'] > 0:
        improvement = (stats_before['no_prev'] - stats_after['no_prev']) / stats_before['no_prev'] * 100
        print(f"{'改善率':<25s} {'':>10s} {'':>10s} {improvement:>+9.1f}%")

    print()
    print("=" * 70)
    print("  サンプル比較")
    print("=" * 70)
    improved_samples = [s for s in samples if s["improved"]]
    other_samples = [s for s in samples if not s["improved"]]

    print(f"\n--- 改善されたケース ({len(improved_samples)}件) ---")
    for i, s in enumerate(improved_samples[:8], 1):
        print(f"\n  [{i}] {s['ticker']} {s['company']}")
        print(f"      タイトル: {s['title']}")
        print(f"      BEFORE ({s['before_source']}): {s['before']}")
        print(f"      AFTER  ({s['after_source']}): {s['after']}")

    if not improved_samples:
        print("  (改善されたケースなし)")

    print(f"\n--- その他サンプル ({len(other_samples)}件中最大3件) ---")
    for i, s in enumerate(other_samples[:3], 1):
        print(f"\n  [{i}] {s['ticker']} {s['company']}")
        print(f"      タイトル: {s['title']}")
        print(f"      BEFORE ({s['before_source']}): {s['before']}")
        print(f"      AFTER  ({s['after_source']}): {s['after']}")

    print()
    print("=" * 70)
    print("  誤抽出チェック")
    print("=" * 70)
    if errors:
        print(f"  検出数: {len(errors)}")
        for e in errors[:10]:
            print(f"  ⚠️ {e}")
    else:
        print("  検出数: 0 ✅")

    print()
    print("=" * 70)


if __name__ == "__main__":
    main()
