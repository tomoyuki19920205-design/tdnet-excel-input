"""
tools/audit_all_success_segments.py
=====================================
segment_detection_v4 の成功18件について、
前年/当年のセグメント名・売上・利益を全件一覧出力する監査用スクリプト。

使い方:
    cd "C:\\Users\\takuy\\OneDrive\\tdnet-excel-input"
    python tools/audit_all_success_segments.py

注意:
    - 抽出ロジックは一切変更しない
    - segment_detection_v4.py をそのまま呼ぶ
"""

from __future__ import annotations

import re
import sys
import io
import logging
from pathlib import Path
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# v4 の INFO ログは stderr へ（stdout を汚さない）
logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(message)s")

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.segment_detection_v4 import (
    V4DetectionResult,
    PeriodResultV4,
    run_segment_detection_v4,
)

# ── 対象PDF／会社名定義 ────────────────────────────────────────────
SAMPLE_DIR = PROJECT_ROOT / "data" / "セグメントサンプル20件"

TARGETS = [
    ("1515", "日鉄鉱業"),
    ("1736", "オートバックス"),
    ("4055", "住友化学"),
    ("4078", "堺化学"),
    ("4099", "四国化成"),
    ("5401", "日本製鉄"),
    ("5713", "住友金属鉱山"),
    ("5803", "フジクラ"),
    ("5805", "SWCC"),
    ("5985", "サンコール"),
    ("6264", "マルマエ"),
    ("6674", "GSユアサ"),
    ("6701", "日本電気"),
    ("6857", "アドバンテスト"),
    ("6946", "アビオニクス"),
    ("7211", "三菱自動車"),
    ("7826", "フルヤ金属"),
    ("8244", "近鉄百貨店"),
    ("9983", "ファーストリテイリング"),
    ("JX",   "JX金属"),
]

# period 補完で救われた企業（監査フラグ用）
FILL_TICKERS: set[str] = set()


# ── 補助関数 ──────────────────────────────────────────────────────

def _fmt_num(value: float | None) -> str:
    if value is None:
        return "-"
    try:
        if value == int(value):
            return f"{int(value):,}"
        return f"{value:,.1f}"
    except (ValueError, OverflowError):
        return str(value)


def _find_pdf(ticker: str) -> Path | None:
    matches = list(SAMPLE_DIR.glob(f"{ticker}*.pdf"))
    return matches[0] if matches else None


def _group_by_period(
    periods: list[PeriodResultV4],
) -> dict[str, list[PeriodResultV4]]:
    g: dict[str, list[PeriodResultV4]] = {"previous": [], "current": [], "unknown": []}
    for pr in periods:
        g.setdefault(pr.period_type, []).append(pr)
    return g


def _print_period_rows(label: str, period_list: list[PeriodResultV4], W: int) -> None:
    print(f"  [{label}]")
    if not period_list:
        print("    (データなし)")
        return
    col_n, col_s, col_p = 22, 12, 12
    header = (
        f"    {'セグメント名':<{col_n}} "
        f"{'売上':>{col_s}} "
        f"{'利益':>{col_p}}"
    )
    sep = "    " + "-" * (col_n + col_s + col_p + 3)
    print(header)
    print(sep)
    for pr in period_list:
        if pr.period_label:
            print(f"    period_label: {pr.period_label}")
        print(f"    page(1b)={pr.page_index_1based}  "
              f"sales_row={pr.sales_row_label!r}  "
              f"profit_row={pr.profit_row_label[:35]!r}")
        for seg in pr.segments:
            s = _fmt_num(seg.segment_sales)
            p = _fmt_num(seg.segment_profit)
            print(f"    {seg.segment_name:<{col_n}} {s:>{col_s}} {p:>{col_p}}")

    # 期間内合計
    total_s = sum(s.segment_sales for pr in period_list
                  for s in pr.segments if s.segment_sales is not None)
    total_p = sum(s.segment_profit for pr in period_list
                  for s in pr.segments if s.segment_profit is not None)
    has_s = any(s.segment_sales is not None
                for pr in period_list for s in pr.segments)
    has_p = any(s.segment_profit is not None
                for pr in period_list for s in pr.segments)
    print(sep)
    ts = _fmt_num(total_s) if has_s else "-"
    tp = _fmt_num(total_p) if has_p else "-"
    print(f"    {'合計':<{col_n}} {ts:>{col_s}} {tp:>{col_p}}")


def _needs_spot_check(
    ticker: str,
    groups: dict[str, list[PeriodResultV4]],
) -> list[str]:
    flags = []

    if ticker in FILL_TICKERS:
        flags.append("period補完あり")

    prev_segs = [s for pr in groups["previous"] for s in pr.segments]
    curr_segs = [s for pr in groups["current"] for s in pr.segments]

    if prev_segs and curr_segs:
        if len(prev_segs) != len(curr_segs):
            flags.append(f"prev/curr セグ数不一致({len(prev_segs)}/{len(curr_segs)})")

        prev_map = {s.segment_name: s.segment_sales for s in prev_segs}
        for cs in curr_segs:
            ps = prev_map.get(cs.segment_name)
            if ps and ps != 0 and cs.segment_sales is not None:
                ratio = cs.segment_sales / ps
                if ratio > 3.0 or ratio < 0.33:
                    flags.append(f"売上急変: {cs.segment_name} "
                                 f"({_fmt_num(ps)}→{_fmt_num(cs.segment_sales)})")

    if groups["unknown"]:
        flags.append("unknown 残あり")

    return flags


# ── メイン処理 ────────────────────────────────────────────────────

def main() -> None:

    # ── period補完ログをキャプチャして FILL_TICKERS を更新
    class _FillCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            msg = self.format(record)
            if "[v4-period-fill" in msg and "skip" not in msg:
                m = re.search(r"ticker=(\S+)", msg)
                if m:
                    FILL_TICKERS.add(m.group(1))

    _cap = _FillCapture()
    _cap.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger().addHandler(_cap)

    W = 60
    success_rows: list[dict] = []
    fail_rows: list[dict] = []

    print("=" * W)
    print("  segment_detection_v4 全件監査レポート")
    print("=" * W)

    for ticker, company in TARGETS:
        pdf_path = _find_pdf(ticker)
        if not pdf_path:
            fail_rows.append({
                "ticker": ticker, "company": company,
                "reason": "PDF not found",
            })
            continue

        result: V4DetectionResult = run_segment_detection_v4(
            str(pdf_path), ticker=ticker
        )

        if not result.success:
            fail_rows.append({
                "ticker": ticker, "company": company,
                "reason": result.log.get("reject_reason") or result.quarantine_reason,
            })
            continue

        groups = _group_by_period(result.extracted_periods)
        spot = _needs_spot_check(ticker, groups)
        fill_mark = " ★補完" if ticker in FILL_TICKERS else ""

        n_prev = sum(len(pr.segments) for pr in groups["previous"])
        n_curr = sum(len(pr.segments) for pr in groups["current"])
        n_unk  = sum(len(pr.segments) for pr in groups["unknown"])

        print()
        print("=" * W)
        print(f"  ticker:  {ticker}  {company}{fill_mark}")
        print(f"  status:  SUCCESS")
        print(f"  summary: previous={n_prev}行, current={n_curr}行, unknown={n_unk}行")
        if spot:
            print(f"  ⚠ SPOT:  {' / '.join(spot)}")
        print("-" * W)

        for label in ("previous", "current"):
            _print_period_rows(label, groups[label], W)
            print()

        if groups["unknown"]:
            print("-" * W)
            _print_period_rows("unknown", groups["unknown"], W)
            print()

        success_rows.append({
            "ticker": ticker, "company": company,
            "n_prev": n_prev, "n_curr": n_curr, "n_unk": n_unk,
            "spot": spot,
            "fill": ticker in FILL_TICKERS,
            "periods": [pr.period_type for pr in result.extracted_periods],
        })

    # ── FAIL ────────────────────────────────────────────────────────
    print()
    print("=" * W)
    print("  FAIL CASES")
    print("=" * W)
    for fr in fail_rows:
        print(f"  ticker: {fr['ticker']}  company: {fr['company']}")
        print(f"  reason: {fr['reason']}")
        print()

    # ── FINAL SUMMARY ───────────────────────────────────────────────
    n_both = sum(
        1 for r in success_rows
        if "previous" in r["periods"] and "current" in r["periods"]
    )
    n_curr_only = sum(
        1 for r in success_rows
        if "current" in r["periods"] and "previous" not in r["periods"]
    )
    n_prev_only = sum(
        1 for r in success_rows
        if "previous" in r["periods"] and "current" not in r["periods"]
    )
    n_unk_only = sum(1 for r in success_rows if r["n_unk"] > 0)
    n_fill = sum(1 for r in success_rows if r["fill"])
    n_spot = sum(1 for r in success_rows if r["spot"])

    print("=" * W)
    print("  FINAL SUMMARY")
    print("=" * W)
    print(f"  SUCCESS         : {len(success_rows)}")
    print(f"  FAIL            : {len(fail_rows)}")
    print(f"  PREV+CURR_BOTH  : {n_both}")
    print(f"  CURRENT_ONLY    : {n_curr_only}")
    print(f"  PREVIOUS_ONLY   : {n_prev_only}")
    print(f"  UNKNOWN_ONLY    : {n_unk_only}")
    print(f"  FILL_APPLIED    : {n_fill}  (fill1/fill2/fill3 いずれか)")
    print(f"  NEEDS_SPOT_CHECK: {n_spot}")
    print()
    print("  Companies requiring manual spot-check:")
    for r in success_rows:
        if r["spot"]:
            fill_tag = " [補完]" if r["fill"] else ""
            print(f"    - {r['ticker']} {r['company']}{fill_tag}")
            for f in r["spot"]:
                print(f"        • {f}")
    print("=" * W)


if __name__ == "__main__":
    main()
