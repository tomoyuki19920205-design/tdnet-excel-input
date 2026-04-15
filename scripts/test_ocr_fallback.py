"""
OCR Fallback 検証スクリプト（一時テスト用）

使い方:
  cd C:\\Users\\takuy\\OneDrive\\tdnet-excel-input
  $env:PDF_OCR_ENABLED="1"; $env:PDF_OCR_FORCE_TEST="1"; python -m scripts.test_ocr_fallback <pdf_path>

必要:
  - Ghostscript (gswin64c.exe) がPATHに存在
  - GOOGLE_APPLICATION_CREDENTIALS が設定済み
"""
import sys
import os
import logging

# ログ設定
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("tdnet")

# 環境変数確認
print("=== 環境変数 ===")
print(f"  PDF_OCR_ENABLED     = {os.environ.get('PDF_OCR_ENABLED', '(未設定)')}")
print(f"  PDF_OCR_FORCE_TEST  = {os.environ.get('PDF_OCR_FORCE_TEST', '(未設定)')}")
print(f"  GHOSTSCRIPT_BIN     = {os.environ.get('GHOSTSCRIPT_BIN', 'gswin64c.exe')}")
print(f"  GOOGLE_APPLICATION_CREDENTIALS = {os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', '(未設定)')}")
print()

if len(sys.argv) < 2:
    print("Usage: python -m scripts.test_ocr_fallback <pdf_path> [title]")
    sys.exit(1)

pdf_path = sys.argv[1]
title = sys.argv[2] if len(sys.argv) > 2 else "決算短信テスト"

if not os.path.exists(pdf_path):
    print(f"ERROR: PDF not found: {pdf_path}")
    sys.exit(1)

print(f"=== テスト対象: {pdf_path} ===")
print(f"    タイトル: {title}")
print()

# --- 1. Financials ---
print("=" * 50)
print("1. Financials (売上/営業利益)")
print("=" * 50)
try:
    from src.extractor import _extract_from_pdf
    result, err = _extract_from_pdf(pdf_path)
    if result:
        print(f"  sales={result.sales}, gp={result.gross_profit}, op={result.operating_profit}")
        print(f"  confidence={result.confidence}, sources={result.field_sources}")
    else:
        print(f"  FAILED: {err}")
except Exception as e:
    print(f"  ERROR: {e}")
print()

# --- 2. Order Metrics ---
print("=" * 50)
print("2. Order Metrics (受注/受注残)")
print("=" * 50)
try:
    from src.extractor import extract_order_metrics
    result, err = extract_order_metrics(pdf_path, title)
    if result:
        for m in result.metrics:
            print(f"  {m.metric_name}: value={m.value}, raw={m.raw_value}, unit={m.unit}")
    else:
        print(f"  FAILED: {err}")
except Exception as e:
    print(f"  ERROR: {e}")
print()

# --- 3. Segment Financials ---
print("=" * 50)
print("3. Segment Financials (セグメント)")
print("=" * 50)
try:
    from src.extractor import extract_segment_financials
    segments, err = extract_segment_financials(pdf_path, title)
    if segments:
        for s in segments:
            print(f"  [{s.segment_order}] {s.segment_name}: sales={s.segment_sales}, profit={s.segment_profit}")
    else:
        print(f"  FAILED: {err}")
except Exception as e:
    print(f"  ERROR: {e}")

print()
print("=== 検証完了 ===")
