#!/usr/bin/env python3
"""check_forecast_fitz_only.py

既存PDFを直接 forecast_extractor.extract_forecast_revision() に渡し、
FORECAST_FITZ_ONLY=1 の抽出結果を確認するデバッグスクリプト。

Discord 送信なし / DB 保存なし / event_pipeline は通さない。

使い方:
    # 環境変数を設定して単体実行
    $env:FORECAST_FITZ_ONLY = "1"
    python tools/check_forecast_fitz_only.py data/docs/140120260428513729.pdf

    # 複数PDF を一括確認
    python tools/check_forecast_fitz_only.py `
        data/docs/140120260428513729.pdf `
        data/docs/140120260427512131.pdf `
        data/docs/140120260428512577.pdf `
        data/docs/140120260420506965.pdf

    # スクリプト内から FORECAST_FITZ_ONLY=1 を強制する場合
    python tools/check_forecast_fitz_only.py --fitz-only data/docs/xxx.pdf
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ── プロジェクトルートを sys.path に追加 ──────────────────────────────────────
_HERE = Path(__file__).resolve().parent          # tools/
_ROOT = _HERE.parent                              # tdnet-excel-input/
sys.path.insert(0, str(_ROOT))

# ── 抽出器をインポート ────────────────────────────────────────────────────────
from src.events.forecast_extractor import extract_forecast_revision  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────────
def _fmt(v: float | None, unit: str = "") -> str:
    """数値を読みやすい文字列にフォーマットする。"""
    if v is None:
        return "None"
    if unit == "pct":
        return f"{v:+.2f}%"
    if unit == "eps":
        return f"{v:.2f} 円"
    # 百万円単位（sales / op / ordinary / net_income）
    return f"{v:,.0f} 百万円"


def _check_one(pdf_path: str) -> None:
    """1 PDFを抽出して結果を表示する。"""
    path = Path(pdf_path)
    if not path.exists():
        print(f"[ERROR] PDF not found: {pdf_path}")
        return

    print()
    print("=" * 70)
    print(f"  PDF : {path.name}")
    print(f"  Mode: FORECAST_FITZ_ONLY={os.environ.get('FORECAST_FITZ_ONLY', '0')}")
    print("=" * 70)

    # テキストは渡さず pdf_path のみ。fitz_only は pdf_path から全部取る。
    event = extract_forecast_revision(
        text="",
        title="",
        is_difference=False,
        pdf_path=str(path.resolve()),
        doc_url="",
        doc_id=path.stem,
    )

    # ── 主要フィールドを表示 ──────────────────────────────────────────────────
    rows = [
        ("period_label",     event.period_label or "(未取得)"),
        ("basis",            event.basis        or "(未取得)"),
        ("subtype",          event.subtype),
        ("importance",       event.importance),
        ("confidence",       f"{event.confidence:.2f}"),
        ("extraction_source", event.extraction_source),
        ("extracted_metrics_count", event.extracted_metrics_count),
        ("─── 売上高",       ""),
        ("  previous_sales", _fmt(event.previous_sales)),
        ("  revised_sales",  _fmt(event.revised_sales)),
        ("  delta_sales",    _fmt(event.delta_sales)),
        ("  change_sales%",  _fmt(event.change_sales_pct, "pct")),
        ("─── 営業利益",     ""),
        ("  previous_op",    _fmt(event.previous_op)),
        ("  revised_op",     _fmt(event.revised_op)),
        ("  delta_op",       _fmt(event.delta_op)),
        ("  change_op%",     _fmt(event.change_op_pct, "pct")),
        ("─── 経常利益",     ""),
        ("  previous_ordinary", _fmt(event.previous_ordinary)),
        ("  revised_ordinary",  _fmt(event.revised_ordinary)),
        ("  delta_ordinary",    _fmt(event.delta_ordinary)),
        ("  change_ordinary%",  _fmt(event.change_ordinary_pct, "pct")),
        ("─── 当期純利益",   ""),
        ("  previous_net_income", _fmt(event.previous_net_income)),
        ("  revised_net_income",  _fmt(event.revised_net_income)),
        ("  delta_net_income",    _fmt(event.delta_net_income)),
        ("  change_net_income%",  _fmt(event.change_net_income_pct, "pct")),
        ("─── EPS",          ""),
        ("  previous_eps",   _fmt(event.previous_eps, "eps")),
        ("  revised_eps",    _fmt(event.revised_eps,  "eps")),
        ("  delta_eps",      _fmt(event.delta_eps,    "eps")),
        ("  change_eps%",    _fmt(event.change_eps_pct, "pct")),
        ("  latest_full_year_eps", _fmt(event.latest_full_year_eps, "eps")),
    ]

    for label, value in rows:
        if label.startswith("───"):
            print(f"\n  {label}")
        elif value == "":
            pass
        else:
            print(f"  {label:<28} {value}")

    # ── has_change 判定（修正1: eps の変化も含む）────────────────────────────
    has_change = any(
        v is not None
        for v in [
            event.delta_sales, event.delta_op,
            event.delta_ordinary, event.delta_net_income,
            event.delta_eps,
        ]
    )
    print()
    print(f"  has_change                   {'YES  ← 修正あり' if has_change else 'NO   ← 変化なし'}")
    print()


# ──────────────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="FORECAST_FITZ_ONLY=1 モードの抽出結果を直接確認する"
    )
    parser.add_argument(
        "pdfs",
        nargs="+",
        metavar="PDF_PATH",
        help="確認するPDFファイルのパス（複数指定可）",
    )
    parser.add_argument(
        "--fitz-only",
        action="store_true",
        default=False,
        help="スクリプト内から FORECAST_FITZ_ONLY=1 を強制する",
    )
    args = parser.parse_args()

    # FORECAST_FITZ_ONLY の設定
    if args.fitz_only:
        os.environ["FORECAST_FITZ_ONLY"] = "1"

    mode = os.environ.get("FORECAST_FITZ_ONLY", "0")
    print(f"\n[check_forecast_fitz_only] FORECAST_FITZ_ONLY={mode}")
    if mode != "1":
        print("  ※ 通常モードで実行中。--fitz-only を付けるか $env:FORECAST_FITZ_ONLY='1' を設定してください。")

    for pdf in args.pdfs:
        _check_one(pdf)

    print("=" * 70)
    print("[done]")


if __name__ == "__main__":
    main()
