"""
tools/export_audit_review_pack.py
===================================
segment_detection_v4 の成功18件について
監査パックを3ファイルに書き出す。

出力先:
    out/audit_segments_full.tsv
    out/audit_segments_spotcheck.tsv
    out/audit_review_report.md

使い方:
    cd "C:\\Users\\takuy\\OneDrive\\tdnet-excel-input"
    python tools/export_audit_review_pack.py

注意:
    - segment_detection_v4.py 本体は一切変更しない
    - fill1/fill2/fill3 補完ロジックは変更しない
"""

from __future__ import annotations

import csv
import re
import sys
import io
import logging
from pathlib import Path
from collections import defaultdict
from typing import Any

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

# ── 定数 ──────────────────────────────────────────────────────────
SAMPLE_DIR = PROJECT_ROOT / "data" / "セグメントサンプル20件"
OUT_DIR    = PROJECT_ROOT / "out"

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
    ("JX",  "JX金属"),
]

# fill 補完が入った企業（ログキャプチャで確定、起動時に更新）
FILL_TICKERS: dict[str, str] = {}   # ticker → fill_type (fill1/fill2/fill3)

# 既知の急変注意会社
KNOWN_ALERT: set[str] = {"7826"}

TSV_COLS = [
    "ticker", "company", "status", "fill_applied", "fill_type",
    "period", "segment_name", "sales", "profit", "unit",
    "source_pdf", "review_flag", "review_reason",
]


# ── 補助関数 ──────────────────────────────────────────────────────

def _fmt_num_tsv(value: float | None) -> str:
    if value is None:
        return "-"
    try:
        return str(int(value)) if value == int(value) else str(value)
    except (ValueError, OverflowError):
        return str(value)


def _fmt_num_md(value: float | None) -> str:
    if value is None:
        return "-"
    try:
        return f"{int(value):,}" if value == int(value) else f"{value:,.1f}"
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


def _detect_review_flag(
    ticker: str,
    groups: dict[str, list[PeriodResultV4]],
) -> tuple[bool, list[str]]:
    """(flag: bool, reasons: list[str])"""
    reasons: list[str] = []

    if ticker in FILL_TICKERS:
        reasons.append(f"period補完={FILL_TICKERS[ticker]}")

    if ticker in KNOWN_ALERT:
        reasons.append("既知急変注意会社")

    prev_segs = [s for pr in groups["previous"] for s in pr.segments]
    curr_segs = [s for pr in groups["current"] for s in pr.segments]

    if prev_segs and curr_segs and len(prev_segs) != len(curr_segs):
        reasons.append(f"seg件数不一致(prev={len(prev_segs)},curr={len(curr_segs)})")

    return bool(reasons), reasons


def _build_rows(
    ticker: str,
    company: str,
    groups: dict[str, list[PeriodResultV4]],
    pdf_path: Path,
    flag: bool,
    reasons: list[str],
) -> list[dict]:
    rows = []
    fill_applied = "yes" if ticker in FILL_TICKERS else "no"
    fill_type = FILL_TICKERS.get(ticker, "-")
    review_flag = "yes" if flag else "no"
    review_reason = "; ".join(reasons) if reasons else "-"
    src = pdf_path.name

    for period_label in ("previous", "current", "unknown"):
        for pr in groups[period_label]:
            for seg in pr.segments:
                rows.append({
                    "ticker": ticker,
                    "company": company,
                    "status": "SUCCESS",
                    "fill_applied": fill_applied,
                    "fill_type": fill_type,
                    "period": period_label,
                    "segment_name": seg.segment_name,
                    "sales": _fmt_num_tsv(seg.segment_sales),
                    "profit": _fmt_num_tsv(seg.segment_profit),
                    "unit": "百万円",
                    "source_pdf": src,
                    "review_flag": review_flag,
                    "review_reason": review_reason,
                })
    return rows


def _write_tsv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TSV_COLS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def _md_period_table(
    label: str,
    period_list: list[PeriodResultV4],
) -> list[str]:
    lines = [f"**[{label}]**"]
    if not period_list:
        lines.append("_(データなし)_")
        lines.append("")
        return lines

    lines.append("")
    lines.append("| セグメント名 | 売上 | 利益 |")
    lines.append("|---|---:|---:|")
    totals: float = 0.0
    totalp: float = 0.0
    has_s = has_p = False
    for pr in period_list:
        for seg in pr.segments:
            s = _fmt_num_md(seg.segment_sales)
            p = _fmt_num_md(seg.segment_profit)
            if seg.segment_sales is not None:
                totals += seg.segment_sales
                has_s = True
            if seg.segment_profit is not None:
                totalp += seg.segment_profit
                has_p = True
            lines.append(f"| {seg.segment_name} | {s} | {p} |")
    lines.append(f"| **合計** | **{_fmt_num_md(totals) if has_s else '-'}** "
                 f"| **{_fmt_num_md(totalp) if has_p else '-'}** |")
    lines.append("")
    return lines


def _write_markdown(
    path: Path,
    success_companies: list[dict],
    fail_companies: list[dict],
    summary: dict,
) -> None:
    lines: list[str] = []

    lines.append("# Audit Review Report")
    lines.append("")
    lines.append("自動生成 — segment_detection_v4 による監査パック")
    lines.append("")

    # ── Summary
    lines.append("## Final Summary")
    lines.append("")
    lines.append("| 項目 | 値 |")
    lines.append("|---|---:|")
    for k, v in summary.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")

    # ── Spot Check 一覧
    lines.append("## Spot Check Targets")
    lines.append("")
    spot_companies = [c for c in success_companies if c["flag"]]
    if spot_companies:
        lines.append("| ticker | company | fill_type | reasons |")
        lines.append("|---|---|---|---|")
        for c in spot_companies:
            ft = FILL_TICKERS.get(c["ticker"], "-")
            reasons_str = "; ".join(c["reasons"])
            lines.append(f"| {c['ticker']} | {c['company']} | {ft} | {reasons_str} |")
    else:
        lines.append("_(なし)_")
    lines.append("")

    # ── Success Companies (詳細)
    lines.append("## Success Companies")
    lines.append("")

    for c in success_companies:
        ticker   = c["ticker"]
        company  = c["company"]
        groups   = c["groups"]
        flag     = c["flag"]
        reasons  = c["reasons"]
        periods  = c["periods"]
        fill_tag = f"  ★ {FILL_TICKERS[ticker]}" if ticker in FILL_TICKERS else ""

        lines.append(f"### {ticker} {company}{fill_tag}")
        lines.append("")
        lines.append(f"- **periods**: {', '.join(periods)}")
        if flag:
            lines.append(f"- ⚠ **review**: {'; '.join(reasons)}")
        lines.append("")

        for period_label in ("previous", "current"):
            lines.extend(_md_period_table(period_label, groups[period_label]))

        if groups["unknown"]:
            lines.extend(_md_period_table("unknown", groups["unknown"]))

        lines.append("---")
        lines.append("")

    # ── Fail Cases
    lines.append("## Fail Cases")
    lines.append("")
    lines.append("| ticker | company | reason |")
    lines.append("|---|---|---|")
    for fc in fail_companies:
        lines.append(f"| {fc['ticker']} | {fc['company']} | {fc['reason']} |")
    lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ── メイン処理 ─────────────────────────────────────────────────────

def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # fill ログをキャプチャして FILL_TICKERS を更新
    class _FillCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            msg = self.format(record)
            if "[v4-period-fill" not in msg or "skip" in msg:
                return
            m_tick = re.search(r"ticker=(\S+)", msg)
            if not m_tick:
                return
            t = m_tick.group(1)
            if "[v4-period-fill3]" in msg:
                FILL_TICKERS[t] = "fill3"
            elif "[v4-period-fill2]" in msg:
                FILL_TICKERS.setdefault(t, "fill2")
            elif "[v4-period-fill]" in msg:
                FILL_TICKERS.setdefault(t, "fill1")

    _cap = _FillCapture()
    _cap.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger().addHandler(_cap)

    all_tsv_rows: list[dict] = []
    success_companies: list[dict] = []
    fail_companies: list[dict] = []

    for ticker, company in TARGETS:
        pdf_path = _find_pdf(ticker)
        if not pdf_path:
            fail_companies.append({"ticker": ticker, "company": company,
                                   "reason": "PDF not found"})
            continue

        result: V4DetectionResult = run_segment_detection_v4(
            str(pdf_path), ticker=ticker
        )

        if not result.success:
            fail_companies.append({
                "ticker": ticker, "company": company,
                "reason": result.log.get("reject_reason") or result.quarantine_reason,
            })
            continue

        groups  = _group_by_period(result.extracted_periods)
        flag, reasons = _detect_review_flag(ticker, groups)
        periods = [pr.period_type for pr in result.extracted_periods]

        rows = _build_rows(ticker, company, groups, pdf_path, flag, reasons)
        all_tsv_rows.extend(rows)

        success_companies.append({
            "ticker": ticker, "company": company,
            "groups": groups, "flag": flag, "reasons": reasons, "periods": periods,
        })

    # spot-check
    spot_rows = [r for r in all_tsv_rows if r["review_flag"] == "yes"]

    # 集計
    n_both     = sum(1 for c in success_companies
                     if "previous" in c["periods"] and "current" in c["periods"])
    n_curr_only = sum(1 for c in success_companies
                      if "current" in c["periods"] and "previous" not in c["periods"])
    n_prev_only = sum(1 for c in success_companies
                      if "previous" in c["periods"] and "current" not in c["periods"])
    n_unk_only  = sum(1 for c in success_companies if "unknown" in c["periods"])
    n_fill      = len(FILL_TICKERS)
    n_spot      = sum(1 for c in success_companies if c["flag"])
    summary = {
        "SUCCESS":          len(success_companies),
        "FAIL":             len(fail_companies),
        "PREV+CURR_BOTH":  n_both,
        "CURRENT_ONLY":    n_curr_only,
        "PREVIOUS_ONLY":   n_prev_only,
        "UNKNOWN_ONLY":    n_unk_only,
        "FILL_APPLIED":    n_fill,
        "NEEDS_SPOT_CHECK": n_spot,
    }

    # ── 書き出し
    path_full  = OUT_DIR / "audit_segments_full.tsv"
    path_spot  = OUT_DIR / "audit_segments_spotcheck.tsv"
    path_md    = OUT_DIR / "audit_review_report.md"

    _write_tsv(path_full, all_tsv_rows)
    _write_tsv(path_spot, spot_rows)
    _write_markdown(path_md, success_companies, fail_companies, summary)

    # ── 6264 確認
    rows_6264 = [r for r in all_tsv_rows if r["ticker"] == "6264"]

    # ── 標準出力（サマリのみ）
    print("=" * 55)
    print("  export_audit_review_pack 完了")
    print("=" * 55)
    print(f"  out/audit_segments_full.tsv      ({len(all_tsv_rows)} rows)")
    print(f"  out/audit_segments_spotcheck.tsv ({len(spot_rows)} rows)")
    print(f"  out/audit_review_report.md")
    print()
    for k, v in summary.items():
        print(f"  {k:<20}: {v}")
    print()
    print(f"  FILL_TICKERS: {dict(sorted(FILL_TICKERS.items()))}")
    print()
    print(f"  Spot-check 対象社数: {n_spot}")
    spot_names = [f"{c['ticker']} {c['company']}"
                  for c in success_companies if c["flag"]]
    for sn in spot_names:
        print(f"    - {sn}")
    print()
    print(f"  6264 マルマエ — 出力行数: {len(rows_6264)}")
    for r in rows_6264:
        print(f"    period={r['period']:8}  seg={r['segment_name']:<22}"
              f"  sales={r['sales']:>10}  profit={r['profit']:>10}")
    print("=" * 55)


if __name__ == "__main__":
    main()
