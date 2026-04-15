"""
tools/inspect_prev_current_segments.py
=======================================
前年・当年のセグメント業績が両方取得できた3社について、
期間別（previous / current / unknown）のセグメント名・売上・利益を
人間がすぐ目視確認できる形式で標準出力する。

対象:
  4099 四国化成
  7211 三菱自動車
  7826 フルヤ金属

使い方:
  python tools/inspect_prev_current_segments.py

注意:
  - 抽出ロジックは変更しない（segment_detection_v4 をそのまま呼ぶ）
  - 既存ファイルには一切変更を加えない
"""

from __future__ import annotations

import sys
import io
from pathlib import Path

# stdout を UTF-8 に固定（Windows PowerShell 対策）
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# プロジェクトルートを sys.path に追加
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.segment_detection_v4 import (
    PeriodResultV4,
    SegmentRecordV4,
    V4DetectionResult,
    run_segment_detection_v4,
)

# ── 対象テーブル ────────────────────────────────────────────────────
SAMPLE_DIR = PROJECT_ROOT / "data" / "セグメントサンプル20件"

TARGETS = [
    {"ticker": "4099", "name": "四国化成"},
    {"ticker": "7211", "name": "三菱自動車"},
    {"ticker": "7826", "name": "フルヤ金属"},
]


# ── 小補助関数 ─────────────────────────────────────────────────────

def _fmt_num(value: float | None) -> str:
    """None → '-'、整数化できる数値は int 表記、それ以外はそのまま。"""
    if value is None:
        return "-"
    try:
        if value == int(value):
            return f"{int(value):,}"
        return f"{value:,.1f}"
    except (ValueError, OverflowError):
        return str(value)


def _group_by_period(
    periods: list[PeriodResultV4],
) -> dict[str, list[PeriodResultV4]]:
    """period_type をキーに PeriodResultV4 をグルーピングする。"""
    groups: dict[str, list[PeriodResultV4]] = {
        "previous": [],
        "current": [],
        "unknown": [],
    }
    for pr in periods:
        groups.setdefault(pr.period_type, []).append(pr)
    return groups


def _print_period_table(label: str, period_results: list[PeriodResultV4]) -> None:
    """1期間ラベル（previous/current/unknown）の結果を整形して出力する。"""
    print(f"[{label}]")
    if not period_results:
        print("  (データなし)")
        return

    for pr in period_results:
        if pr.period_label:
            print(f"  period_label : {pr.period_label}")
        print(f"  page (1based): {pr.page_index_1based}")
        print(f"  sales_row    : {pr.sales_row_label!r}")
        print(f"  profit_row   : {pr.profit_row_label[:50]!r}")

        if not pr.segments:
            print("  (セグメントなし)")
            continue

        # ヘッダー
        col_name = 24
        col_val = 12
        header = (
            f"  {'セグメント名':<{col_name}} "
            f"{'売上':>{col_val}} "
            f"{'利益':>{col_val}}"
        )
        separator = "  " + "-" * (col_name + col_val * 2 + 3)
        print(header)
        print(separator)

        total_sales = 0.0
        total_profit = 0.0
        has_sales = False
        has_profit = False

        for seg in pr.segments:
            s_str = _fmt_num(seg.segment_sales)
            p_str = _fmt_num(seg.segment_profit)
            if seg.segment_sales is not None:
                total_sales += seg.segment_sales
                has_sales = True
            if seg.segment_profit is not None:
                total_profit += seg.segment_profit
                has_profit = True
            print(
                f"  {seg.segment_name:<{col_name}} "
                f"{s_str:>{col_val}} "
                f"{p_str:>{col_val}}"
            )

        # 合計行
        print(separator)
        total_s_str = _fmt_num(total_sales) if has_sales else "-"
        total_p_str = _fmt_num(total_profit) if has_profit else "-"
        print(
            f"  {'合計':<{col_name}} "
            f"{total_s_str:>{col_val}} "
            f"{total_p_str:>{col_val}}"
        )


def _find_pdf(ticker: str) -> Path | None:
    """SAMPLE_DIR から ticker で始まる PDF を探す。"""
    matches = list(SAMPLE_DIR.glob(f"{ticker}*.pdf"))
    return matches[0] if matches else None


# ── メイン処理 ────────────────────────────────────────────────────

def main() -> None:
    summary_rows: list[dict] = []
    W = 52

    for target in TARGETS:
        ticker = target["ticker"]
        name = target["name"]

        print("=" * W)
        print(f"{ticker} {name}")

        pdf_path = _find_pdf(ticker)
        if pdf_path is None:
            print(f"source_pdf  : NOT FOUND (ticker={ticker})")
            print(f"accepted    : False")
            summary_rows.append({
                "ticker": ticker,
                "name": name,
                "previous": 0,
                "current": 0,
                "unknown": 0,
            })
            print("=" * W)
            continue

        print(f"source_pdf  : {pdf_path.name}")

        result: V4DetectionResult = run_segment_detection_v4(
            str(pdf_path), ticker=ticker
        )
        print(f"accepted    : {result.success}")

        if not result.success:
            reason = result.log.get("reject_reason") or result.quarantine_reason
            print(f"reject      : {reason}")
            summary_rows.append({
                "ticker": ticker,
                "name": name,
                "previous": 0,
                "current": 0,
                "unknown": 0,
            })
            print("=" * W)
            continue

        groups = _group_by_period(result.extracted_periods)

        for label in ("previous", "current"):
            print("-" * W)
            _print_period_table(label, groups[label])

        # unknown は件数があるときだけ表示
        if groups["unknown"]:
            print("-" * W)
            _print_period_table("unknown", groups["unknown"])

        summary_rows.append({
            "ticker": ticker,
            "name": name,
            "previous": sum(len(pr.segments) for pr in groups["previous"]),
            "current":  sum(len(pr.segments) for pr in groups["current"]),
            "unknown":  sum(len(pr.segments) for pr in groups["unknown"]),
        })
        print("=" * W)

    # ── 総括 ─────────────────────────────────────────────────────────
    print()
    print("─" * W)
    print(f"{'企業':<14} {'previous':>10} {'current':>9} {'unknown':>8}")
    print("─" * W)
    for r in summary_rows:
        label = f"{r['ticker']} {r['name']}"
        print(
            f"{label:<14} "
            f"{r['previous']:>10} "
            f"{r['current']:>9} "
            f"{r['unknown']:>8}"
        )
    print("─" * W)


if __name__ == "__main__":
    main()
