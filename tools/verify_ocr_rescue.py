"""
OCR救済率バッチ検証スクリプト

仕様: docs/specs/pdf_ocr/ocr_rescue_spec.md

native抽出とOCR fallback抽出を比較し、救済率を測定する。

使い方:
  cd C:\\Users\\takuy\\OneDrive\\tdnet-excel-input
  python scripts\\verify_ocr_rescue.py [--limit N]

環境変数:
  PDF_OCR_ENABLED=1     必須
  GHOSTSCRIPT_BIN       gswin64c.exe (デフォルト)
  GOOGLE_APPLICATION_CREDENTIALS  必須
"""
import os
import sys
import json
import glob
import time
import logging
import argparse
from dataclasses import dataclass, asdict

# ログ設定
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("verify_ocr")
logger.setLevel(logging.INFO)

# OCR有効化
os.environ["PDF_OCR_ENABLED"] = "1"
os.environ.setdefault("PDF_OCR_FORCE_TEST", "0")

# src をパスに追加
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================
# 成功判定（仕様準拠）
# ============================================================

def is_segment_success(segments) -> bool:
    """
    セグメント成功判定:
    - valid_segments >= 2
    - sales_non_null >= 1
    """
    if len(segments) < 2:
        return False
    sales_count = sum(1 for s in segments if s.segment_sales is not None)
    return sales_count >= 1


def is_order_success(result) -> bool:
    """
    受注成功判定:
    - 主要項目が1つ以上non-null
    """
    if result is None:
        return False
    return len(result.metrics) >= 1


# ============================================================
# データモデル
# ============================================================

@dataclass
class SegmentTestResult:
    pdf_path: str
    native_success: bool
    native_segment_count: int
    native_sales_count: int
    native_error: str
    ocr_success: bool
    ocr_segment_count: int
    ocr_sales_count: int
    ocr_error: str
    ocr_used: bool
    category: str  # rescued / regression / unchanged
    elapsed_native: float
    elapsed_ocr: float
    # 本番戦略フラグ
    native_keep: bool = False          # native成功→native結果を採用
    ocr_fallback_used: bool = False    # native失敗→OCR成功で救済
    ocr_regression_blocked: bool = False  # native成功→OCR失敗だが本番ではnative維持で防止
    production_outcome: str = "failed"    # native_keep / ocr_fallback_used / failed


@dataclass
class OrderTestResult:
    pdf_path: str
    native_success: bool
    native_metric_count: int
    native_error: str
    ocr_success: bool
    ocr_metric_count: int
    ocr_error: str
    ocr_used: bool
    category: str
    elapsed_native: float
    elapsed_ocr: float
    # 本番戦略フラグ
    native_keep: bool = False
    ocr_fallback_used: bool = False
    ocr_regression_blocked: bool = False
    production_outcome: str = "failed"


# ============================================================
# 抽出実行
# ============================================================

def run_segment_native(pdf_path: str, title: str):
    """native抽出のみ"""
    old = os.environ.get("PDF_OCR_ENABLED", "0")
    os.environ["PDF_OCR_ENABLED"] = "0"
    try:
        from src.extractor import extract_segment_financials
        t0 = time.time()
        segments, err = extract_segment_financials(pdf_path, title)
        elapsed = time.time() - t0
        return segments, err, elapsed
    except Exception as e:
        return [], str(e), 0.0
    finally:
        os.environ["PDF_OCR_ENABLED"] = old


def run_segment_ocr(pdf_path: str, title: str):
    """OCR fallback付き（FORCE_TEST）"""
    os.environ["PDF_OCR_ENABLED"] = "1"
    os.environ["PDF_OCR_FORCE_TEST"] = "1"
    try:
        from src.extractor import extract_segment_financials
        t0 = time.time()
        segments, err = extract_segment_financials(pdf_path, title)
        elapsed = time.time() - t0
        return segments, err, elapsed
    except Exception as e:
        return [], str(e), 0.0
    finally:
        os.environ["PDF_OCR_FORCE_TEST"] = "0"


def run_order_native(pdf_path: str, title: str):
    """native抽出のみ"""
    old = os.environ.get("PDF_OCR_ENABLED", "0")
    os.environ["PDF_OCR_ENABLED"] = "0"
    try:
        from src.extractor import extract_order_metrics
        t0 = time.time()
        result, err = extract_order_metrics(pdf_path, title)
        elapsed = time.time() - t0
        return result, err, elapsed
    except Exception as e:
        return None, str(e), 0.0
    finally:
        os.environ["PDF_OCR_ENABLED"] = old


def run_order_ocr(pdf_path: str, title: str):
    """OCR fallback付き"""
    os.environ["PDF_OCR_ENABLED"] = "1"
    os.environ["PDF_OCR_FORCE_TEST"] = "1"
    try:
        from src.extractor import extract_order_metrics
        t0 = time.time()
        result, err = extract_order_metrics(pdf_path, title)
        elapsed = time.time() - t0
        return result, err, elapsed
    except Exception as e:
        return None, str(e), 0.0
    finally:
        os.environ["PDF_OCR_FORCE_TEST"] = "0"


# ============================================================
# 判定カテゴリ
# ============================================================

def classify(native_ok: bool, ocr_ok: bool) -> str:
    """
    rescued:   native失敗 → OCR成功
    regression: native成功 → OCR失敗
    unchanged: 変化なし（両方成功 or 両方失敗）
    """
    if not native_ok and ocr_ok:
        return "rescued"
    if native_ok and not ocr_ok:
        return "regression"
    return "unchanged"


# ============================================================
# メイン
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="OCR救済率バッチ検証")
    parser.add_argument("--limit", type=int, default=10, help="検証PDF数")
    parser.add_argument("--pdf-dir", default="data/docs", help="PDF格納ディレクトリ")
    parser.add_argument("--segment-only", action="store_true")
    parser.add_argument("--order-only", action="store_true")
    args = parser.parse_args()

    pdf_files = sorted(glob.glob(os.path.join(args.pdf_dir, "*.pdf")))
    if not pdf_files:
        print(f"ERROR: PDF not found in {args.pdf_dir}")
        return

    target_pdfs = pdf_files[:args.limit]
    print(f"=== OCR救済率検証 ===")
    print(f"PDF候補: {len(pdf_files)}件, 検証対象: {len(target_pdfs)}件")
    print()

    seg_results: list[SegmentTestResult] = []
    ord_results: list[OrderTestResult] = []

    for i, pdf_path in enumerate(target_pdfs):
        pdf_name = os.path.basename(pdf_path)
        title = "決算短信"
        print(f"[{i+1}/{len(target_pdfs)}] {pdf_name}")

        # --- セグメント ---
        if not args.order_only:
            n_segs, n_err, n_time = run_segment_native(pdf_path, title)
            o_segs, o_err, o_time = run_segment_ocr(pdf_path, title)

            n_ok = is_segment_success(n_segs)
            o_ok = is_segment_success(o_segs)
            n_sales = sum(1 for s in n_segs if s.segment_sales is not None)
            o_sales = sum(1 for s in o_segs if s.segment_sales is not None)
            cat = classify(n_ok, o_ok)

            # 本番戦略フラグ算出
            _native_keep = n_ok
            _ocr_fallback_used = (not n_ok) and o_ok
            _ocr_regression_blocked = n_ok and (not o_ok)
            # final_ok = native_ok or ((not native_ok) and ocr_ok)
            if _native_keep:
                _prod_outcome = "native_keep"
            elif _ocr_fallback_used:
                _prod_outcome = "ocr_fallback_used"
            else:
                _prod_outcome = "failed"

            seg_results.append(SegmentTestResult(
                pdf_path=pdf_name,
                native_success=n_ok,
                native_segment_count=len(n_segs),
                native_sales_count=n_sales,
                native_error=n_err,
                ocr_success=o_ok,
                ocr_segment_count=len(o_segs),
                ocr_sales_count=o_sales,
                ocr_error=o_err,
                ocr_used=True,
                category=cat,
                elapsed_native=round(n_time, 2),
                elapsed_ocr=round(o_time, 2),
                native_keep=_native_keep,
                ocr_fallback_used=_ocr_fallback_used,
                ocr_regression_blocked=_ocr_regression_blocked,
                production_outcome=_prod_outcome,
            ))

            mark = {"rescued": "✅RESCUED", "regression": "⚠️REGRESSION", "unchanged": "—"}[cat]
            print(f"  SEG: native={len(n_segs)}segs/{n_sales}sales({'OK' if n_ok else 'FAIL'}) "
                  f"ocr={len(o_segs)}segs/{o_sales}sales({'OK' if o_ok else 'FAIL'}) "
                  f"[{mark}] {n_time:.1f}s/{o_time:.1f}s")

        # --- 受注 ---
        if not args.segment_only:
            n_res, n_err, n_time = run_order_native(pdf_path, title)
            o_res, o_err, o_time = run_order_ocr(pdf_path, title)

            n_ok = is_order_success(n_res)
            o_ok = is_order_success(o_res)
            n_cnt = len(n_res.metrics) if n_res else 0
            o_cnt = len(o_res.metrics) if o_res else 0
            cat = classify(n_ok, o_ok)

            # 本番戦略フラグ算出
            _native_keep = n_ok
            _ocr_fallback_used = (not n_ok) and o_ok
            _ocr_regression_blocked = n_ok and (not o_ok)
            if _native_keep:
                _prod_outcome = "native_keep"
            elif _ocr_fallback_used:
                _prod_outcome = "ocr_fallback_used"
            else:
                _prod_outcome = "failed"

            ord_results.append(OrderTestResult(
                pdf_path=pdf_name,
                native_success=n_ok,
                native_metric_count=n_cnt,
                native_error=n_err,
                ocr_success=o_ok,
                ocr_metric_count=o_cnt,
                ocr_error=o_err,
                ocr_used=True,
                category=cat,
                elapsed_native=round(n_time, 2),
                elapsed_ocr=round(o_time, 2),
                native_keep=_native_keep,
                ocr_fallback_used=_ocr_fallback_used,
                ocr_regression_blocked=_ocr_regression_blocked,
                production_outcome=_prod_outcome,
            ))

            mark = {"rescued": "✅RESCUED", "regression": "⚠️REGRESSION", "unchanged": "—"}[cat]
            print(f"  ORD: native={n_cnt}m({'OK' if n_ok else 'FAIL'}) "
                  f"ocr={o_cnt}m({'OK' if o_ok else 'FAIL'}) "
                  f"[{mark}] {n_time:.1f}s/{o_time:.1f}s")

        print()

    # ============================================================
    # サマリ出力
    # ============================================================
    print("=" * 60)

    if seg_results:
        _print_summary("セグメント", seg_results)

    if ord_results:
        _print_summary("受注", ord_results)

    # 改善例・失敗例
    if seg_results:
        print("=== セグメント改善例 ===")
        for r in seg_results:
            if r.category == "rescued":
                print(f"  {r.pdf_path}: native=0 -> ocr={r.ocr_segment_count}segs/{r.ocr_sales_count}sales")
        print("=== セグメント回帰 ===")
        for r in seg_results:
            if r.category == "regression":
                print(f"  ⚠️ {r.pdf_path}: native={r.native_segment_count} -> ocr=FAIL")
        if not any(r.category == "regression" for r in seg_results):
            print("  なし ✅")
        print()

    if ord_results:
        print("=== 受注改善例 ===")
        for r in ord_results:
            if r.category == "rescued":
                print(f"  {r.pdf_path}: native=0 -> ocr={r.ocr_metric_count}metrics")
        print("=== 受注回帰 ===")
        for r in ord_results:
            if r.category == "regression":
                print(f"  ⚠️ {r.pdf_path}: native={r.native_metric_count} -> ocr=FAIL")
        if not any(r.category == "regression" for r in ord_results):
            print("  なし ✅")

    # JSON出力
    output = {
        "segments": [asdict(r) for r in seg_results],
        "orders": [asdict(r) for r in ord_results],
    }
    os.makedirs("tmp", exist_ok=True)
    out_path = "tmp/ocr_rescue_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n詳細結果: {out_path}")


def _print_summary(label: str, results):
    """サマリ出力（仕様準拠）+ 本番戦略シミュレーション"""
    total = len(results)
    native_ok = sum(1 for r in results if r.native_success)
    ocr_ok = sum(1 for r in results if r.ocr_success)
    rescued = sum(1 for r in results if r.category == "rescued")
    regression = sum(1 for r in results if r.category == "regression")

    avg_native = sum(r.elapsed_native for r in results) / total if total else 0
    avg_ocr = sum(r.elapsed_ocr for r in results) / total if total else 0

    print(f"=== {label}救済サマリ ===")
    print(f"  検証件数        : {total}")
    print(f"  native成功率    : {native_ok}/{total} ({100*native_ok/total:.0f}%)")
    print(f"  OCR成功率       : {ocr_ok}/{total} ({100*ocr_ok/total:.0f}%)")
    print(f"  救済率          : {rescued}/{total} ({100*rescued/total:.0f}%)")
    print(f"  回帰率          : {regression}/{total} ({100*regression/total:.0f}%)")
    print(f"  平均処理時間    : native={avg_native:.1f}s, ocr={avg_ocr:.1f}s")
    print()

    # 本番戦略シミュレーション
    # final_ok = native_ok or ((not native_ok) and ocr_ok)
    n_native_keep = sum(1 for r in results if r.native_keep)
    n_ocr_fallback = sum(1 for r in results if r.ocr_fallback_used)
    n_blocked = sum(1 for r in results if r.ocr_regression_blocked)
    n_failed = sum(1 for r in results if r.production_outcome == "failed")
    final_ok = n_native_keep + n_ocr_fallback

    print(f"=== {label}本番戦略シミュレーション ===")
    print(f"  最終成功率      : {final_ok}/{total} ({100*final_ok/total:.0f}%)")
    print(f"    native_keep       : {n_native_keep}  (native成功→native結果を採用)")
    print(f"    ocr_fallback_used : {n_ocr_fallback}  (native失敗→OCR救済)")
    print(f"    failed            : {n_failed}  (native/OCRともに失敗)")
    print(f"  ──")
    print(f"  回帰防止件数    : {n_blocked}  (OCR悪化だが本番ではnative維持で防止)")
    print()


if __name__ == "__main__":
    main()
