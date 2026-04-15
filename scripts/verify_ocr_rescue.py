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
import re
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
# 診断情報収集
# ============================================================

# セグメントアンカーキーワード（segment_extractor_ocr.py と同等）
_SEGMENT_ANCHORS = [
    "報告セグメント", "事業セグメント", "セグメント情報",
    "セグメント別", "事業別", "セグメントの業績", "セグメント利益",
]

# 表ヘッダーキーワード
_HEADER_KEYWORDS = [
    "売上高", "売上収益", "営業収益", "営業利益",
    "セグメント利益", "セグメント損益",
    "外部顧客への売上高", "利益又は損失",
]

# セグメント名らしい日本語パターン（2文字以上の漢字/カタカナ/ひらがな）
_SEGMENT_NAME_RE = re.compile(
    r'^[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\u3000-\u303Fー・]{2,15}$'
)


@dataclass
class DiagnosticInfo:
    """PDFテキストに対する診断情報"""
    text_len: int = 0
    anchor_hits: int = 0
    header_detected: bool = False
    header_keyword_hits: int = 0
    segment_name_count: int = 0


def _collect_diagnostics(pdf_path: str) -> tuple[DiagnosticInfo, DiagnosticInfo]:
    """
    PDFからnative/OCRそれぞれの診断情報を収集する。
    extractor.pyを呼ばず、テキスト取得+キーワードスキャンのみ。

    Returns: (native_diag, ocr_diag)
    """
    import pdfplumber

    native_diag = DiagnosticInfo()
    ocr_diag = DiagnosticInfo()

    # --- native テキスト ---
    try:
        with pdfplumber.open(pdf_path) as pdf:
            texts = []
            for page in pdf.pages[:8]:
                page_text = page.extract_text()
                if page_text:
                    texts.append(page_text)
            native_text = "\n".join(texts)
    except Exception:
        native_text = ""

    native_diag = _scan_text(native_text)

    # --- OCR テキスト ---
    try:
        from src.extractor import _run_ocr_pipeline
        ocr_text = _run_ocr_pipeline(pdf_path) or ""
    except Exception:
        ocr_text = ""

    ocr_diag = _scan_text(ocr_text)

    return native_diag, ocr_diag


def _scan_text(text: str) -> DiagnosticInfo:
    """テキストからアンカー/ヘッダー/セグメント名候補をカウントする"""
    diag = DiagnosticInfo()
    diag.text_len = len(text)

    if not text.strip():
        return diag

    # アンカーヒット数
    for kw in _SEGMENT_ANCHORS:
        if kw in text:
            diag.anchor_hits += 1

    # ヘッダーキーワードヒット数
    for kw in _HEADER_KEYWORDS:
        if kw in text:
            diag.header_keyword_hits += 1
    diag.header_detected = diag.header_keyword_hits > 0

    # セグメント名候補数
    for line in text.split("\n"):
        tokens = line.strip().split()
        for token in tokens:
            if _SEGMENT_NAME_RE.match(token):
                diag.segment_name_count += 1
                break  # 行ごとに最大1カウント

    return diag


# ============================================================
# 失敗理由分類
# ============================================================

# failure_reason_final の優先順位（上ほど優先）
_FAILURE_PRIORITY = [
    "NO_TARGET_TABLE",
    "TABLE_DETECTION_FAIL",
    "HEADER_MAPPING_FAIL",
    "SEGMENT_NAME_FAIL",
    "VALUE_PARSE_FAIL",
    "OCR_TEXT_POOR",
    "TEXT_EXTRACTION_POOR",
    "OTHER",
]


def classify_failure_reason(
    error: str,
    diag: DiagnosticInfo,
    segment_count: int,
    sales_count: int,
    is_ocr: bool = False,
) -> str:
    """
    エラー文字列と診断情報から失敗理由を判定する。

    Args:
        error: native_error / ocr_error
        diag: 診断情報
        segment_count: 抽出されたセグメント数
        sales_count: 売上non-null数
        is_ocr: OCR側の判定か
    """
    # テキスト抽出不良
    if diag.text_len < 50:
        return "OCR_TEXT_POOR" if is_ocr else "TEXT_EXTRACTION_POOR"

    # NO_TARGET_TABLE: 保守的判定（全条件AND）
    _no_seg_error = any(kw in error for kw in [
        "no_segment_page", "no_segment_table",
    ])
    if (
        diag.text_len >= 50
        and _no_seg_error
        and diag.anchor_hits == 0
        and not diag.header_detected
        and diag.segment_name_count == 0
    ):
        return "NO_TARGET_TABLE"

    # TABLE_DETECTION_FAIL: テキスト十分だが表候補ゼロ
    if diag.anchor_hits == 0 and diag.text_len >= 200:
        return "TABLE_DETECTION_FAIL"

    # HEADER_MAPPING_FAIL: アンカーはあるが表ヘッダーなし
    if diag.anchor_hits > 0 and not diag.header_detected:
        return "HEADER_MAPPING_FAIL"

    # SEGMENT_NAME_FAIL: ヘッダーはあるがセグメント名が取れない
    if diag.header_detected and segment_count == 0 and diag.segment_name_count == 0:
        return "SEGMENT_NAME_FAIL"

    # VALUE_PARSE_FAIL: セグメント名はあるが売上が取れない
    if segment_count > 0 and sales_count == 0:
        return "VALUE_PARSE_FAIL"

    return "OTHER"


def _determine_failure_reason_final(
    reason_native: str | None,
    reason_ocr: str | None,
) -> str:
    """
    native/OCR両方の理由から、優先順位が高い方をfinalに採用する。
    """
    candidates = [r for r in [reason_native, reason_ocr] if r]
    if not candidates:
        return "OTHER"
    if len(candidates) == 1:
        return candidates[0]
    # 優先順位で比較
    idx_n = _FAILURE_PRIORITY.index(reason_native) if reason_native in _FAILURE_PRIORITY else 999
    idx_o = _FAILURE_PRIORITY.index(reason_ocr) if reason_ocr in _FAILURE_PRIORITY else 999
    return reason_native if idx_n <= idx_o else reason_ocr



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
    # 失敗理由分類
    failure_reason_native: str | None = None
    failure_reason_ocr: str | None = None
    failure_reason_final: str | None = None
    # 診断情報
    native_text_len: int = 0
    ocr_text_len: int = 0
    native_anchor_hits: int = 0
    ocr_anchor_hits: int = 0
    native_header_detected: bool = False
    ocr_header_detected: bool = False
    native_header_keyword_hits: int = 0
    ocr_header_keyword_hits: int = 0
    native_segment_name_count: int = 0
    ocr_segment_name_count: int = 0


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
    parser.add_argument("--manifest", type=str, default=None,
                        help="manifest ファイルから PDF リストを読み込む")
    parser.add_argument("--save-manifest", type=str, default=None,
                        help="現在の PDF リストを manifest ファイルに保存して終了")
    args = parser.parse_args()

    pdf_files = sorted(glob.glob(os.path.join(args.pdf_dir, "*.pdf")))
    if not pdf_files:
        print(f"ERROR: PDF not found in {args.pdf_dir}")
        return

    # manifest 保存モード
    if args.save_manifest:
        target_pdfs = pdf_files[:args.limit]
        manifest_data = [os.path.basename(p) for p in target_pdfs]
        with open(args.save_manifest, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, ensure_ascii=False, indent=2)
        print(f"manifest 保存: {args.save_manifest} ({len(manifest_data)}件)")
        return

    # manifest 読み込みモード
    if args.manifest:
        with open(args.manifest, "r", encoding="utf-8") as f:
            manifest_names = json.load(f)
        pdf_dir = args.pdf_dir
        target_pdfs = [os.path.join(pdf_dir, name) for name in manifest_names
                       if os.path.exists(os.path.join(pdf_dir, name))]
        if not target_pdfs:
            print(f"ERROR: manifest のPDFが見つかりません")
            return
        print(f"=== OCR救済率検証 (manifest: {args.manifest}) ===")
        print(f"PDF候補: {len(pdf_files)}件, manifest対象: {len(target_pdfs)}件")
    else:
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

            # 失敗理由分類（failed の場合のみ診断実行）
            _fr_native = None
            _fr_ocr = None
            _fr_final = None
            _n_diag = DiagnosticInfo()
            _o_diag = DiagnosticInfo()

            if _prod_outcome == "failed":
                _n_diag, _o_diag = _collect_diagnostics(pdf_path)
                _fr_native = classify_failure_reason(
                    n_err, _n_diag, len(n_segs), n_sales, is_ocr=False,
                )
                _fr_ocr = classify_failure_reason(
                    o_err, _o_diag, len(o_segs), o_sales, is_ocr=True,
                )
                _fr_final = _determine_failure_reason_final(_fr_native, _fr_ocr)
                print(f"  DIAG: native_text={_n_diag.text_len} anchor={_n_diag.anchor_hits} "
                      f"hdr={_n_diag.header_keyword_hits} segname={_n_diag.segment_name_count}")
                print(f"        ocr_text={_o_diag.text_len} anchor={_o_diag.anchor_hits} "
                      f"hdr={_o_diag.header_keyword_hits} segname={_o_diag.segment_name_count}")
                print(f"  FAIL: native={_fr_native} ocr={_fr_ocr} final={_fr_final}")

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
                failure_reason_native=_fr_native,
                failure_reason_ocr=_fr_ocr,
                failure_reason_final=_fr_final,
                native_text_len=_n_diag.text_len,
                ocr_text_len=_o_diag.text_len,
                native_anchor_hits=_n_diag.anchor_hits,
                ocr_anchor_hits=_o_diag.anchor_hits,
                native_header_detected=_n_diag.header_detected,
                ocr_header_detected=_o_diag.header_detected,
                native_header_keyword_hits=_n_diag.header_keyword_hits,
                ocr_header_keyword_hits=_o_diag.header_keyword_hits,
                native_segment_name_count=_n_diag.segment_name_count,
                ocr_segment_name_count=_o_diag.segment_name_count,
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

    # Failed Reason Breakdown
    failed_results = [r for r in results if r.production_outcome == "failed"]
    if failed_results and hasattr(failed_results[0], "failure_reason_final"):
        print(f"=== {label} Failed Reason Breakdown ===")
        reason_counts: dict[str, int] = {r: 0 for r in _FAILURE_PRIORITY}
        for r in failed_results:
            reason = getattr(r, "failure_reason_final", None) or "OTHER"
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        for reason in _FAILURE_PRIORITY:
            count = reason_counts.get(reason, 0)
            bar = "█" * count
            print(f"  {reason:<25s}: {count}  {bar}")
        print()

        # 最多カテゴリをハイライト
        if failed_results:
            top_reason = max(reason_counts, key=lambda k: reason_counts[k])
            top_count = reason_counts[top_reason]
            if top_count > 0:
                print(f"  → 最多失敗原因: {top_reason} ({top_count}件)")
                print()

        # 個別failed詳細
        print(f"=== {label} Failed 詳細 ===")
        for r in failed_results:
            print(f"  {r.pdf_path}:")
            print(f"    native_err: {getattr(r, 'native_error', '')}")
            print(f"    ocr_err  : {getattr(r, 'ocr_error', '')}")
            print(f"    reason   : native={getattr(r, 'failure_reason_native', '')} "
                  f"ocr={getattr(r, 'failure_reason_ocr', '')} "
                  f"→ final={getattr(r, 'failure_reason_final', '')}")
            print(f"    diag     : n_text={getattr(r, 'native_text_len', 0)} "
                  f"o_text={getattr(r, 'ocr_text_len', 0)} "
                  f"n_anchor={getattr(r, 'native_anchor_hits', 0)} "
                  f"o_anchor={getattr(r, 'ocr_anchor_hits', 0)} "
                  f"n_hdr={getattr(r, 'native_header_keyword_hits', 0)} "
                  f"o_hdr={getattr(r, 'ocr_header_keyword_hits', 0)}")
        print()


if __name__ == "__main__":
    main()
