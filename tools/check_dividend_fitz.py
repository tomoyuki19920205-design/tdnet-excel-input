#!/usr/bin/env python3
"""check_dividend_fitz.py

Phase 1 確認用: _extract_dividend_annual_total_via_fitz() が動作しているか検証する。

使い方:
    Set-Location "C:\\Users\\takuy\\OneDrive\\tdnet-excel-input"
    .venv\\Scripts\\python.exe tools\\check_dividend_fitz.py data\\docs\\XXXXXXX.pdf
    .venv\\Scripts\\python.exe tools\\check_dividend_fitz.py data\\docs\\A.pdf data\\docs\\B.pdf
"""
from __future__ import annotations

import sys
from pathlib import Path

# ── プロジェクトルートを sys.path に追加 ──────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.events.dividend_extractor import extract_dividend_revision  # noqa: E402


def _get_text(pdf_path: str) -> str:
    """pdfplumber でテキストを取得する（event_pipeline と同等の簡易版）。"""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages[:10])
    except Exception as e:
        print(f"  [warn] pdfplumber text extract failed: {e}")
        return ""


def check_one(pdf_path: str) -> None:
    path = Path(pdf_path)
    if not path.exists():
        print(f"[ERROR] not found: {pdf_path}")
        return

    print()
    print("=" * 70)
    print(f"  PDF : {path.name}")
    print("=" * 70)

    text = _get_text(str(path))

    # FITZ ログは print() で出るのでここに混ざって表示される
    event = extract_dividend_revision(
        text=text,
        title=path.stem,        # タイトル代わりにファイル名
        pdf_path=str(path.resolve()),
    )

    rows = [
        ("─── 年間合計",              ""),
        ("  annual_total_previous",   _fmt(event.annual_total_previous)),
        ("  annual_total_revised",    _fmt(event.annual_total_revised)),
        ("─── 1株当たり配当",          ""),
        ("  previous_dividend_per_share", _fmt(event.previous_dividend_per_share)),
        ("  revised_dividend_per_share",  _fmt(event.revised_dividend_per_share)),
        ("  delta_dividend_per_share",    _fmt(event.delta_dividend_per_share)),
        ("─── 判定",                  ""),
        ("  subtype",                 event.subtype),
        ("  importance",              str(event.importance)),
        ("  confidence",              f"{event.confidence:.2f}"),
        ("  fiscal_period",           event.fiscal_period or "(未検出)"),
        ("  dividend_basis",          event.dividend_basis or "(未検出)"),
    ]

    for label, value in rows:
        if label.startswith("───"):
            print(f"\n  {label}")
        elif value:
            print(f"  {label:<34} {value}")

    has_annual = event.annual_total_revised is not None
    has_pair   = (
        event.annual_total_previous is not None
        and event.annual_total_revised is not None
    )
    print()
    print(f"  annual_total_revised 取得   : {'YES' if has_annual else 'NO'}")
    print(f"  前回/今回 両方取得          : {'YES' if has_pair else 'NO'}")
    print()


def _fmt(v: float | None) -> str:
    if v is None:
        return "None"
    return f"{v:g} 円"


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("使い方: python tools/check_dividend_fitz.py <PDF> [<PDF> ...]")
        sys.exit(1)

    for pdf in args:
        check_one(pdf)

    print("=" * 70)
    print("[done]")


if __name__ == "__main__":
    main()
