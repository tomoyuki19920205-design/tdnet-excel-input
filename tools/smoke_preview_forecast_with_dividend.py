#!/usr/bin/env python3
"""smoke_preview_forecast_with_dividend.py

format_forecast_msg() の通知文プレビュー（Discord送信なし）。
check_forecast_fitz_only.py と同一の呼び方で forecast を抽出し、
配当付記が通知文に出るかを確認する。

使い方:
    Set-Location "C:\\Users\\takuy\\OneDrive\\tdnet-excel-input"

    # FITZ モードで実行（推奨・本番と同じ）
    .venv\\Scripts\\python.exe tools\\smoke_preview_forecast_with_dividend.py --fitz-only data\\docs\\XXXXX.pdf

    # 環境変数で指定しても可
    $env:FORECAST_FITZ_ONLY = "1"
    .venv\\Scripts\\python.exe tools\\smoke_preview_forecast_with_dividend.py data\\docs\\XXXXX.pdf
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# ── FORECAST_FITZ_ONLY を src.events インポート前に設定 ───────────────────────
# forecast_extractor がモジュールロード時に env を参照するため、
# インポートより前に argparse で --fitz-only を先読みして設定する。
_pre = argparse.ArgumentParser(add_help=False)
_pre.add_argument("--fitz-only", action="store_true", default=False)
_pre_args, _ = _pre.parse_known_args()
if _pre_args.fitz_only:
    os.environ["FORECAST_FITZ_ONLY"] = "1"

# ── src.events インポート（env 設定後） ───────────────────────────────────────
from src.events.forecast_extractor import extract_forecast_revision              # noqa: E402
from src.events.dividend_extractor import _extract_dividend_annual_total_via_fitz  # noqa: E402
from src.events.common_notify import format_forecast_msg                         # noqa: E402
from src.events.common_models import EventRecord, EventType                      # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
def _build_mock_record(forecast_ev, div_result: dict | None) -> EventRecord:
    """forecast_ev と div_result から通知プレビュー用の EventRecord を組み立てる。"""
    payload = forecast_ev.to_dict()

    # 配当フィールドを payload に付加（event_pipeline.py と同じロジック）
    if div_result and div_result.get("annual_total_revised") is not None:
        _d_prev = div_result.get("annual_total_previous")
        _d_rev  = div_result["annual_total_revised"]
        payload["dividend_annual_total_previous"] = _d_prev
        payload["dividend_annual_total_revised"]  = _d_rev
        if _d_prev is not None:
            payload["dividend_delta"] = round(_d_rev - _d_prev, 2)
            payload["dividend_change_pct"] = (
                round((_d_rev - _d_prev) / abs(_d_prev) * 100, 1)
                if _d_prev != 0 else None
            )

    return EventRecord(
        source_doc_id="preview",
        ticker="0000",
        company_name="プレビュー株式会社",
        disclosure_datetime="2026-04-30T09:00:00",
        title="（プレビュー）業績予想修正",
        event_type=EventType.FORECAST_REVISION,
        subtype=forecast_ev.subtype,
        importance=forecast_ev.importance,
        summary_text="preview",
        raw_payload_json="{}",
        extracted_payload_json=json.dumps(payload, ensure_ascii=False, default=str),
        fingerprint="preview",
        doc_url="",
    )


def preview_one(pdf_path: str) -> None:
    path = Path(pdf_path)
    if not path.exists():
        print(f"[ERROR] not found: {pdf_path}")
        return

    abs_pdf = str(path.resolve())

    print()
    print("=" * 70)
    print(f"  PDF : {path.name}")
    print(f"  FORECAST_FITZ_ONLY={os.environ.get('FORECAST_FITZ_ONLY', '0')}")
    print("=" * 70)

    # ── 業績予想修正抽出（check_forecast_fitz_only.py と完全に同一の呼び方） ──
    # text="" = pdfplumber_table ルートをスキップして FITZ 抽出を使わせる
    print("\n--- forecast 抽出 ---")
    forecast_ev = extract_forecast_revision(
        text="",
        title="業績予想及び配当予想の修正に関するお知らせ",
        is_difference=False,
        pdf_path=abs_pdf,
        doc_url="",
        doc_id=path.stem,
    )
    print(f"  subtype            : {forecast_ev.subtype}")
    print(f"  extraction_source  : {getattr(forecast_ev, 'extraction_source', 'N/A')}")
    print(f"  revised_sales      : {forecast_ev.revised_sales}")
    print(f"  revised_op         : {forecast_ev.revised_op}")
    print(f"  revised_ordinary   : {forecast_ev.revised_ordinary}")
    print(f"  revised_net_income : {forecast_ev.revised_net_income}")
    print(f"  revised_eps        : {forecast_ev.revised_eps}")
    print(f"  previous_net_income: {forecast_ev.previous_net_income}")
    print(f"  previous_eps       : {forecast_ev.previous_eps}")

    # ── 配当年間合計抽出 ──────────────────────────────────────────────────────
    print("\n--- 配当 FITZ 抽出 ---")
    div_result = _extract_dividend_annual_total_via_fitz(abs_pdf)
    if div_result:
        print(f"  annual_total_previous: {div_result.get('annual_total_previous')}")
        print(f"  annual_total_revised : {div_result.get('annual_total_revised')}")
    else:
        print("  (配当テーブル未検出)")

    # ── 通知文プレビュー ──────────────────────────────────────────────────────
    print("\n--- Discord 通知文プレビュー ---")
    record = _build_mock_record(forecast_ev, div_result)
    msg = format_forecast_msg(record)
    print(msg)
    print("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="forecast + 配当付記 通知文プレビュー（Discord送信なし）"
    )
    parser.add_argument("pdfs", nargs="+", metavar="PDF_PATH")
    parser.add_argument(
        "--fitz-only", action="store_true", default=False,
        help="FORECAST_FITZ_ONLY=1 を強制（インポート前の先読みと二重設定）",
    )
    args = parser.parse_args()

    # インポート後にも念のため設定（通常は先読みで設定済み）
    if args.fitz_only:
        os.environ["FORECAST_FITZ_ONLY"] = "1"

    mode = os.environ.get("FORECAST_FITZ_ONLY", "0")
    print(f"\n[smoke_preview] FORECAST_FITZ_ONLY={mode}")
    if mode != "1":
        print("  ※ --fitz-only を付けるか $env:FORECAST_FITZ_ONLY='1' を設定してください。")

    for pdf in args.pdfs:
        preview_one(pdf)

    print("[done]")


if __name__ == "__main__":
    main()
