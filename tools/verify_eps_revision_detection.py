"""
EPS修正前→修正後 検出ロジック 実開示検証スクリプト

仕様: docs/specs/eps_revision_detection/

旧ロジック (backup/eps_merge_20260326_102117) vs 新ロジック (src/events) の
EPS 検出差分を定量化する Phase 1 相互比較ツール。

Phase 2: --input-manifest で expected 付き JSONL/CSV を指定すると
exact_match / false_positive / wrong_value 評価も実行。

使い方:
  cd C:\\Users\\takuy\\OneDrive\\tdnet-excel-input
  python tools/verify_eps_revision_detection.py [--limit N] [--debug]
"""
from __future__ import annotations

import argparse
import csv
import glob
import importlib
import importlib.util
import json
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

# ============================================================
# ログ設定
# ============================================================
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("verify_eps")
logger.setLevel(logging.INFO)

# 本番ロジックの [forecast_ocr] ログを表示するためにレベル設定
logging.getLogger("forecast_extractor").setLevel(logging.INFO)

# プロジェクトルートをパスに追加
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


# ============================================================
# 旧ロジック動的ロード
# ============================================================
_OLD_EXTRACT_FROM_TEXT = None
_OLD_LOGIC_SOURCE = "unknown"


def _load_old_logic():
    """backup/eps_merge_20260326_102117/forecast_extractor.py を動的ロード。

    相対インポート (from .xxx) を絶対インポート (from src.events.xxx) に
    書き換えてからロードする。
    """
    global _OLD_EXTRACT_FROM_TEXT, _OLD_LOGIC_SOURCE

    backup_path = _PROJECT_ROOT / "backup" / "eps_merge_20260326_102117" / "forecast_extractor.py"
    if not backup_path.exists():
        logger.warning(f"旧ロジックファイルが見つかりません: {backup_path}")
        _OLD_LOGIC_SOURCE = "approximate (backup not found)"
        return False

    try:
        source_code = backup_path.read_text(encoding="utf-8")

        # 相対インポートを絶対インポートに書き換え
        source_code = source_code.replace(
            "from .forecast_models", "from src.events.forecast_models"
        )
        source_code = source_code.replace(
            "from .common_normalizers", "from src.events.common_normalizers"
        )
        source_code = source_code.replace(
            "from .pdf_ocr", "from src.events.pdf_ocr"
        )

        # 一時ファイルに書き出してロード
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8",
            dir=str(_PROJECT_ROOT / "tmp"),
        ) as tf:
            tf.write(source_code)
            tf_path = tf.name

        spec = importlib.util.spec_from_file_location(
            "old_forecast_extractor", tf_path
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        _OLD_EXTRACT_FROM_TEXT = mod._extract_from_text
        _OLD_LOGIC_SOURCE = "backup/eps_merge_20260326_102117"
        logger.info(f"旧ロジック読み込み成功: {_OLD_LOGIC_SOURCE}")

        # 一時ファイル削除
        try:
            os.unlink(tf_path)
        except OSError:
            pass

        return True

    except Exception as e:
        logger.warning(f"旧ロジック読み込み失敗: {e}")
        _OLD_LOGIC_SOURCE = f"approximate (import error: {e})"
        return False


def _extract_old_eps(text: str, title: str, is_difference: bool = False):
    """旧ロジックで EPS を抽出する。

    Returns: (prev_eps, rev_eps)
    """
    if _OLD_EXTRACT_FROM_TEXT is not None:
        try:
            ev = _OLD_EXTRACT_FROM_TEXT(text, title, is_difference, source="pdf_text")
            return ev.previous_eps, ev.revised_eps
        except Exception as e:
            logger.debug(f"旧ロジック抽出エラー: {e}")
            return None, None

    # フォールバック: 現行ロジックの横型テーブルのみで近似
    try:
        from src.events.forecast_extractor import (
            _normalize_text,
            _detect_unit,
            _find_consolidated_section,
            _extract_from_horizontal_table,
        )
        full_text = _normalize_text(text)
        lines = full_text.split("\n")
        unit = _detect_unit(full_text) or "百万円"
        c_start, c_end = _find_consolidated_section(lines)
        target_lines = lines[c_start:c_end] if c_start > 0 else lines
        table_result = _extract_from_horizontal_table(target_lines, is_difference, unit)
        return table_result.get("previous_eps"), table_result.get("revised_eps")
    except Exception as e:
        logger.debug(f"近似旧ロジック抽出エラー: {e}")
        return None, None


def _extract_new_eps(text: str, title: str, is_difference: bool = False, pdf_path: str = ""):
    """新ロジック（本番の extract_forecast_revision）で EPS を抽出する。

    OCR フォールバックを含む本番の全経路を検証する。
    """
    try:
        from src.events.forecast_extractor import extract_forecast_revision
        # extract_forecast_revision を呼ぶことで OCR 判定と OCR ログ出力が行われる
        ev = extract_forecast_revision(
            text, title=title, is_difference=is_difference,
            pdf_path=pdf_path, doc_id=os.path.basename(pdf_path)
        )
        return ev.previous_eps, ev.revised_eps
    except Exception as e:
        logger.debug(f"新ロジック抽出エラー: {e}")
        return None, None


# ============================================================
# EPS 1.0 誤検出ガード
# ============================================================
def validate_eps_pair(
    new_prev: Optional[float], new_rev: Optional[float],
    old_prev: Optional[float], old_rev: Optional[float],
    pdf_path: str,
) -> tuple[Optional[float], Optional[float]]:
    """新ロジックの EPS 誤検出を高度なガード（位置・ラベル・文脈）でフィルタリングする。"""
    if new_prev is None and new_rev is None:
        return None, None

    # PDF コンテキスト（座標付きテキスト行）を取得
    context_lines = extract_text_lines_with_positions(pdf_path)

    new_prev = _enhanced_guard(new_prev, old_prev, context_lines, is_revised=False)
    new_rev = _enhanced_guard(new_rev, old_rev, context_lines, is_revised=True)
    return new_prev, new_rev


def extract_text_lines_with_positions(pdf_path: str) -> list[dict]:
    """PDF から行ごとのテキストと Y 座標比率、ページ番号を取得する。"""
    results = []
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            for p_idx, page in enumerate(pdf.pages[:10]):
                h = page.height
                words = page.extract_words()
                if not words:
                    continue
                # Y座標でグループ化
                words.sort(key=lambda x: (x['top'], x['x0']))
                current_top = words[0]['top']
                line_words = []
                for w in words:
                    if abs(w['top'] - current_top) > 3: # 3pt threshold
                        line_text = "".join(lw['text'] for lw in line_words)
                        results.append({
                            'text': line_text,
                            'y_ratio': (current_top / h),
                            'page': p_idx + 1
                        })
                        line_words = [w]
                        current_top = w['top']
                    else:
                        line_words.append(w)
                if line_words:
                    line_text = "".join(lw['text'] for lw in line_words)
                    results.append({'text': line_text, 'y_ratio': (current_top/h), 'page': p_idx + 1})
    except Exception as e:
        logger.debug(f"座標付きテキスト抽出失敗: {e}")
    return results


def _enhanced_guard(
    val: Optional[float],
    old_val: Optional[float],
    context_lines: list[dict],
    is_revised: bool = False
) -> Optional[float]:
    """位置・ラベル・文脈に基づく高度な数値棄却ロジック。"""
    if val is None:
        return None

    # 旧ロジックでも同値を検出 → 信頼度高（保持）
    if old_val is not None and abs(old_val - val) < 0.01:
        return val

    # 数値文字列のバリエーション
    val_abs = abs(val)
    val_str_int = f"{val_abs:.0f}"
    val_str_f1 = f"{val_abs:.1f}"
    val_str_f2 = f"{val_abs:.2f}"

    # 棄却対象ワード (前後3行含めて検索)
    REJECT_WORDS = ["配当", "利益率", "％", "%", "増減", "前年", "進捗", "達成", "実績", "前期", "修正率", "騰落"]
    # 強い EPS アンカー (1.0 や不審な数値の場合に推奨)
    STRONG_EPS_ANCHORS = ["1株当たり", "１株当たり", "一株当たり", "EPS", "eps", "純利益"]
    # 弱い EPS アンカー
    WEAK_EPS_ANCHORS = ["円", "銭"]

    # 重点監視対象（これらはエビデンスが弱い場合、積極的に棄却する）
    SUSPICIOUS_VALUES = [1.0, 0.0] # 0.0もたまに誤爆する
    # よくある配当値（これらも配当ラベルが近くにあれば棄却）
    COMMON_DIV_VALUES = [10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0, 60.0, 70.0, 75.0, 80.0, 100.0]

    is_suspicious = (val_abs in SUSPICIOUS_VALUES) or (val_abs in COMMON_DIV_VALUES)

    # PDF内での出現箇所を全てチェック
    found_occurrences = []
    # ページ内に棄却ワードがあるか事前チェック
    page_has_reject = {}
    for line in context_lines:
        p = line['page']
        if any(rw in line['text'] for rw in REJECT_WORDS):
            page_has_reject[p] = True

    for i, line in enumerate(context_lines):
        txt_for_match = line['text']
        # ラベル内の数字をマスク
        for anchor in STRONG_EPS_ANCHORS:
            txt_for_match = txt_for_match.replace(anchor, "____")
        
        is_match = False
        tokens = re.findall(r"[\d,.]+", txt_for_match)
        for t in tokens:
            t_clean = t.replace(",", "")
            if t_clean.strip() in (".", "") or t_clean.count('.') > 1: continue
            try:
                if abs(float(t_clean) - val_abs) < 0.01:
                    is_match = True
                    break
            except: continue

        if is_match:
            # 1.0 の場合は、同一行内に強いアンカーがあるかチェック（超厳格）
            has_strong_same_line = any(a in line['text'] for a in STRONG_EPS_ANCHORS)
            
            # 周辺テキスト（前後3行程度に縮小して精度を上げる）
            start_idx = max(0, i - 2)
            end_idx = min(len(context_lines), i + 3)
            nearby_text = "".join(l['text'] for l in context_lines[start_idx:end_idx])

            is_hf = (line['y_ratio'] < 0.07 or line['y_ratio'] > 0.93)
            has_reject_word = any(rw in nearby_text for rw in REJECT_WORDS)
            page_wide_reject = page_has_reject.get(line['page'], False) and val_abs in COMMON_DIV_VALUES
            
            has_strong_nearby = any(a in nearby_text for a in STRONG_EPS_ANCHORS)
            has_weak_nearby = any(a in nearby_text for a in WEAK_EPS_ANCHORS)

            # 判定ロジック
            if is_hf:
                found_occurrences.append({'is_bad': True, 'reason': 'header_footer'})
                continue
            
            if has_reject_word or page_wide_reject:
                # 強力な EPS アンカーが同一行にない限り棄却
                if not any(a in line['text'] for a in STRONG_EPS_ANCHORS):
                    found_occurrences.append({'is_bad': True, 'reason': 'reject_word_or_page_div'})
                    continue

            # 3. アンカーチェック
            if val_abs < 1.1: # 1.0 等
                # 1.0 の場合は同一行内への強いアンカーを必須とする
                bad_anchor = not has_strong_same_line
            else:
                # 1.0 以外は周辺にアンカーがあれば許容
                bad_anchor = not (has_strong_nearby or has_weak_nearby)
            
            found_occurrences.append({
                'is_bad': bad_anchor,
                'reason': 'bad_anchor'
            })

    # 一つでも確実に「善玉」と判定された箇所があれば保持
    if any(not occ['is_bad'] for occ in found_occurrences):
        return val

    # 見つからなかった場合：不審な値なら棄却、そうでなければ保持
    if not found_occurrences:
        if is_suspicious:
            logger.debug(f"Enhanced Guard 棄却: {val} (PDF内に見つからず不審なため)")
            return None
        return val

    # 全ての出現箇所が「悪玉」と判定された
    logger.info(f"Enhanced Guard 棄却: {val} (理由: {found_occurrences[0]['reason']})")
    return None

    # 一つでも「悪くない」出現箇所があれば保持、全て「悪い」なら棄却
    if not found_occurrences:
        # 見つからない場合は念のため保持
        return val

    if any(not occ['is_bad'] for occ in found_occurrences):
        return val

    logger.info(f"Enhanced Guard 棄却: {val} (理由: {found_occurrences[0]['reason']})")
    return None


# ============================================================
# データモデル
# ============================================================
@dataclass
class EPSCompareResult:
    source_path: str = ""
    ticker: str = ""
    company_name: str = ""
    title: str = ""

    old_prev_eps: Optional[float] = None
    old_new_eps: Optional[float] = None
    new_prev_eps: Optional[float] = None
    new_new_eps: Optional[float] = None

    # Phase 2: expected
    expected_prev_eps: Optional[float] = None
    expected_new_eps: Optional[float] = None

    diff_status: str = ""
    failure_category: str = ""
    error: str = ""

    # Phase 2 status
    old_status: str = ""
    new_status: str = ""


# ============================================================
# diff_status 判定（EPSペア単位）
# ============================================================
def _has_any(prev, rev) -> bool:
    return prev is not None or rev is not None


def _has_both(prev, rev) -> bool:
    return prev is not None and rev is not None


def _eps_approx_equal(a: Optional[float], b: Optional[float]) -> bool:
    """EPS 値の近似一致判定（小数点以下の丸め差を許容）"""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) < 0.01


def classify_diff_status(
    old_prev: Optional[float], old_rev: Optional[float],
    new_prev: Optional[float], new_rev: Optional[float],
) -> str:
    """EPSペア単位で diff_status を判定する。"""
    old_any = _has_any(old_prev, old_rev)
    new_any = _has_any(new_prev, new_rev)
    old_both = _has_both(old_prev, old_rev)
    new_both = _has_both(new_prev, new_rev)

    # 両方 None
    if not old_any and not new_any:
        return "both_missing"

    # 新のみ検出
    if not old_any and new_any:
        return "new_only_detected"

    # 旧のみ検出
    if old_any and not new_any:
        return "old_only_detected"

    # 両方検出あり → ペア完備度を比較
    # new_completed_partial: 旧=片側, 新=両側
    if not old_both and new_both:
        return "new_completed_partial"

    # old_completed_partial: 旧=両側, 新=片側
    if old_both and not new_both:
        return "old_completed_partial"

    # 両方同じ完備度 → 値一致チェック
    prev_same = _eps_approx_equal(old_prev, new_prev)
    rev_same = _eps_approx_equal(old_rev, new_rev)
    if prev_same and rev_same:
        return "both_detected_same"
    else:
        return "both_detected_different"


# ============================================================
# 失敗カテゴリ分類（新ロジックが片側以上 None の場合）
# ============================================================
def classify_failure(
    text: str,
    new_prev: Optional[float], new_rev: Optional[float],
) -> str:
    """新ロジックの検出失敗原因を推定する。"""
    if new_prev is not None and new_rev is not None:
        return ""  # 成功

    from src.events.forecast_extractor import _normalize_text

    normalized = _normalize_text(text)
    lines = normalized.split("\n")

    # EPS ラベルがあるか
    eps_labels = [
        "1株当たり", "１株当たり", "一株当たり",
        "eps", "EPS",
    ]
    has_eps_label = any(
        any(lbl.lower() in line.lower() for lbl in eps_labels)
        for line in lines
    )

    # 前回/修正ラベルがあるか
    prev_labels = ["前回発表予想", "前回予想"]
    rev_labels = ["今回修正予想", "今回予想", "修正予想"]
    has_prev_label = any(
        any(lbl in line for lbl in prev_labels)
        for line in lines
    )
    has_rev_label = any(
        any(lbl in line for lbl in rev_labels)
        for line in lines
    )

    # 文字化け指標: 制御文字・CID文字が多い
    cid_count = len(re.findall(r"[\x00-\x08\x0e-\x1f]|cid:|\\u[0-9a-f]{4}", normalized.lower()))
    has_ocr_corruption = cid_count > 5

    if has_ocr_corruption:
        return "OCR崩れ"
    if not has_eps_label:
        return "ラベル未検出"
    if has_eps_label and not has_prev_label and not has_rev_label:
        return "縦ブロック失敗"
    if has_eps_label and (has_prev_label or has_rev_label):
        return "数値抽出失敗"

    return "その他"


# ============================================================
# Phase 2: manifest 読み込み
# ============================================================
def load_manifest(path: str) -> list[dict]:
    """expected付きマニフェストを読み込む（JSONL / CSV 自動判定）"""
    ext = os.path.splitext(path)[1].lower()
    records = []

    if ext == ".jsonl":
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    elif ext == ".csv":
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # float 変換
                for key in ["expected_prev_eps", "expected_new_eps"]:
                    if key in row and row[key]:
                        try:
                            row[key] = float(row[key])
                        except (ValueError, TypeError):
                            row[key] = None
                    else:
                        row[key] = None
                records.append(row)
    else:
        raise ValueError(f"未対応のmanifest形式: {ext} (JSONL/CSVのみ)")

    # バリデーション
    for r in records:
        if "expected_prev_eps" not in r or "expected_new_eps" not in r:
            raise ValueError("manifest に expected_prev_eps / expected_new_eps が必要です")
        ident_keys = {"ticker", "title", "doc_id", "source_path"}
        if not any(r.get(k) for k in ident_keys):
            raise ValueError("manifest に ticker/title/doc_id/source_path のいずれかが必要です")

    return records


def evaluate_against_expected(
    actual_prev: Optional[float], actual_rev: Optional[float],
    expected_prev: Optional[float], expected_rev: Optional[float],
) -> str:
    """expected と比較して status を判定する。"""
    prev_match = _eps_approx_equal(actual_prev, expected_prev)
    rev_match = _eps_approx_equal(actual_rev, expected_rev)

    if expected_prev is None and expected_rev is None:
        # expected が両方 None = EPS なしのドキュメント
        if actual_prev is None and actual_rev is None:
            return "exact_match"
        else:
            return "false_positive"

    if prev_match and rev_match:
        return "exact_match"

    if actual_prev is None and actual_rev is None:
        return "both_missing"

    if prev_match or rev_match:
        return "partial_match"

    if actual_prev is not None or actual_rev is not None:
        return "wrong_value"

    return "both_missing"


# ============================================================
# PDF テキスト抽出
# ============================================================
def extract_text_from_pdf(pdf_path: str) -> str:
    """pdfplumber で PDF からテキスト抽出"""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            texts = [page.extract_text() or "" for page in pdf.pages[:10]]
            return "\n".join(texts)
    except Exception as e:
        logger.debug(f"PDF抽出失敗: {pdf_path}: {e}")
        return ""


def is_forecast_revision_pdf(text: str) -> bool:
    """業績予想修正関連の PDF かどうかを判定"""
    keywords = ["業績予想", "修正", "予想の修正", "差異"]
    text_head = text[:2000].lower()
    return any(kw in text_head for kw in keywords)


# ============================================================
# サマリ出力
# ============================================================
def _count_hit(results: list[EPSCompareResult], side: str) -> dict:
    """旧 or 新 の detected_count 詳細を集計"""
    prefix = "old_" if side == "old" else "new_"
    counts = {"any_hit": 0, "both_hit": 0, "prev_only": 0, "new_only": 0}
    for r in results:
        prev = getattr(r, f"{prefix}prev_eps")
        rev = getattr(r, f"{prefix}new_eps")
        if prev is not None or rev is not None:
            counts["any_hit"] += 1
        if prev is not None and rev is not None:
            counts["both_hit"] += 1
        elif prev is not None:
            counts["prev_only"] += 1
        elif rev is not None:
            counts["new_only"] += 1
    return counts


def print_summary(results: list[EPSCompareResult], has_expected: bool):
    """stdoutサマリ出力"""
    total = len(results)
    if total == 0:
        print("検証対象が0件です。")
        return

    print()
    print("=" * 65)
    print(f"  EPS修正検出 検証サマリ  (旧ロジック: {_OLD_LOGIC_SOURCE})")
    print("=" * 65)
    print(f"  total_documents           : {total}")
    print()

    # detected_count 詳細
    old_c = _count_hit(results, "old")
    new_c = _count_hit(results, "new")
    print("  --- 旧ロジック ---")
    print(f"    old_any_hit_count       : {old_c['any_hit']}")
    print(f"    old_both_hit_count      : {old_c['both_hit']}")
    print(f"    old_prev_only_count     : {old_c['prev_only']}")
    print(f"    old_new_only_count      : {old_c['new_only']}")
    print()
    print("  --- 新ロジック ---")
    print(f"    new_any_hit_count       : {new_c['any_hit']}")
    print(f"    new_both_hit_count      : {new_c['both_hit']}")
    print(f"    new_prev_only_count     : {new_c['prev_only']}")
    print(f"    new_new_only_count      : {new_c['new_only']}")
    print()

    # diff_status 集計
    status_counts: dict[str, int] = {}
    for r in results:
        status_counts[r.diff_status] = status_counts.get(r.diff_status, 0) + 1

    print("  --- diff_status 集計 ---")
    for st in [
        "both_detected_same", "both_detected_different",
        "new_only_detected", "old_only_detected",
        "new_completed_partial", "old_completed_partial",
        "both_missing",
    ]:
        cnt = status_counts.get(st, 0)
        label = ""
        if st == "new_only_detected":
            label = "  (改善候補)"
        elif st == "new_completed_partial":
            label = "  (改善候補)"
        elif st == "old_only_detected":
            label = "  (回帰候補)"
        elif st == "old_completed_partial":
            label = "  (回帰候補)"
        print(f"    {st:30s}: {cnt}{label}")
    print()

    # 失敗カテゴリ集計
    fail_cats: dict[str, int] = {}
    for r in results:
        if r.failure_category:
            fail_cats[r.failure_category] = fail_cats.get(r.failure_category, 0) + 1
    if fail_cats:
        print("  --- 失敗カテゴリ（新ロジック片側以上None） ---")
        for cat, cnt in sorted(fail_cats.items(), key=lambda x: -x[1]):
            print(f"    {cat:20s}: {cnt}")
        print()

    # Phase 2: expected 付き評価
    if has_expected:
        # expected が設定されているレコードのみで集計
        expected_results = [r for r in results if r.expected_prev_eps is not None or r.expected_new_eps is not None]
        n_expected = len(expected_results)

        if n_expected > 0:
            print(f"  --- Phase 2: expected 付き評価 (監査件数: {n_expected}) ---")

            for side_label, side_prefix in [("旧ロジック", "old"), ("新ロジック", "new")]:
                status_attr = f"{side_prefix}_status"
                statuses: dict[str, int] = {}
                for r in expected_results:
                    s = getattr(r, status_attr, "")
                    if s:
                        statuses[s] = statuses.get(s, 0) + 1

                exact = statuses.get("exact_match", 0)
                partial = statuses.get("partial_match", 0)
                fp = statuses.get("false_positive", 0)
                wrong = statuses.get("wrong_value", 0)
                missing = statuses.get("both_missing", 0)
                # prev_only / new_only を追加算出
                prev_only_cnt = sum(
                    1 for r in expected_results
                    if getattr(r, f"{side_prefix}_prev_eps") is not None
                    and getattr(r, f"{side_prefix}_new_eps") is None
                )
                new_only_cnt = sum(
                    1 for r in expected_results
                    if getattr(r, f"{side_prefix}_prev_eps") is None
                    and getattr(r, f"{side_prefix}_new_eps") is not None
                )

                print(f"    {side_label}:")
                print(f"      exact_match_count     : {exact}  ({100*exact/n_expected:.1f}%)")
                print(f"      partial_match_count   : {partial}  ({100*partial/n_expected:.1f}%)")
                print(f"      false_positive_count  : {fp}  ({100*fp/n_expected:.1f}%)")
                print(f"      wrong_value_count     : {wrong}")
                print(f"      prev_only_count       : {prev_only_cnt}")
                print(f"      new_only_count        : {new_only_cnt}")
                print(f"      both_missing_count    : {missing}")
            print()

    # manual review 候補上位例
    review_candidates = [
        r for r in results
        if r.diff_status in (
            "new_only_detected", "old_only_detected",
            "new_completed_partial", "old_completed_partial",
            "both_detected_different",
        )
    ]
    if review_candidates:
        print("  --- manual review 候補 (上位10件) ---")
        for r in review_candidates[:10]:
            label = "改善候補" if "new" in r.diff_status else "回帰候補" if "old" in r.diff_status else "差異"
            print(
                f"    [{label}] {r.diff_status}"
            )
            print(
                f"      file : {os.path.basename(r.source_path)}"
            )
            print(
                f"      旧EPS: prev={r.old_prev_eps} rev={r.old_new_eps}"
            )
            print(
                f"      新EPS: prev={r.new_prev_eps} rev={r.new_new_eps}"
            )
        print()

    # new_eps=1.0 棄却統計
    eps_1_0_raw = sum(
        1 for r in results
        if getattr(r, '_raw_new_prev_eps', None) == 1.0 or getattr(r, '_raw_new_new_eps', None) == 1.0
    )
    eps_1_0_rejected = getattr(print_summary, '_eps_1_0_rejected', 0)
    if eps_1_0_rejected > 0:
        print(f"  --- EPS 1.0 ガード ---")
        print(f"    棄却件数              : {eps_1_0_rejected}")
        print()

    print("=" * 65)


# ============================================================
# 出力
# ============================================================
def save_jsonl(results: list[EPSCompareResult], path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(asdict(r), ensure_ascii=False, default=str) + "\n")
    print(f"JSONL 出力: {path} ({len(results)}件)")


def save_csv(results: list[EPSCompareResult], path: str):
    if not results:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fieldnames = list(asdict(results[0]).keys())
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))
    print(f"CSV 出力: {path} ({len(results)}件)")


def save_review_csv(results: list[EPSCompareResult], path: str):
    """manual review 用 manifest CSV を出力。

    対象: new_only_detected の疑義ケース + both_missing の代表例
    Phase 2 用の expected 列を空で含む。
    """
    review_targets = []

    # new_only_detected: 全件（疑義確認用）
    for r in results:
        if r.diff_status in ("new_only_detected", "new_completed_partial"):
            review_targets.append(r)

    # both_missing: 上位20件
    both_missing = [r for r in results if r.diff_status == "both_missing"]
    review_targets.extend(both_missing[:20])

    # both_detected_different: 全件
    for r in results:
        if r.diff_status == "both_detected_different":
            review_targets.append(r)

    if not review_targets:
        print("review対象が0件です。")
        return

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fieldnames = [
        "source_path", "title",
        "old_prev_eps", "old_new_eps",
        "new_prev_eps", "new_new_eps",
        "diff_status", "failure_category",
        "expected_prev_eps", "expected_new_eps",
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in review_targets:
            row = {
                "source_path": r.source_path,
                "title": r.title,
                "old_prev_eps": r.old_prev_eps,
                "old_new_eps": r.old_new_eps,
                "new_prev_eps": r.new_prev_eps,
                "new_new_eps": r.new_new_eps,
                "diff_status": r.diff_status,
                "failure_category": r.failure_category,
                "expected_prev_eps": r.expected_prev_eps or "",
                "expected_new_eps": r.expected_new_eps or "",
            }
            writer.writerow(row)
    print(f"Review CSV 出力: {path} ({len(review_targets)}件)")


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="EPS修正検出 検証ツール (Phase 1: 相互比較 / Phase 2: expected付き評価)"
    )
    parser.add_argument("--limit", type=int, default=50, help="検証PDF数 (default: 50)")
    parser.add_argument("--pdf-dir", default="data/docs", help="PDF格納ディレクトリ")
    parser.add_argument("--input-manifest", help="expected付きマニフェスト (JSONL/CSV)")
    parser.add_argument("--only-failures", action="store_true",
                        help="both_detected_same 以外のみ表示")
    parser.add_argument("--save-jsonl", help="JSONL出力パス")
    parser.add_argument("--save-csv", help="CSV出力パス")
    parser.add_argument("--save-review-csv", help="manual review用manifest CSV出力パス")
    parser.add_argument("--debug", action="store_true", help="DEBUGログ有効")
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.getLogger("forecast_extractor").setLevel(logging.DEBUG)

    # 旧ロジック読み込み
    os.makedirs(str(_PROJECT_ROOT / "tmp"), exist_ok=True)
    _load_old_logic()

    # Phase 2: manifest 読み込み
    manifest_map: dict[str, dict] = {}
    has_expected = False
    if args.input_manifest:
        manifest_records = load_manifest(args.input_manifest)
        has_expected = True
        for rec in manifest_records:
            # 識別キーを構築
            key = rec.get("source_path") or rec.get("doc_id") or rec.get("ticker", "") + "|" + rec.get("title", "")
            manifest_map[key] = rec
        logger.info(f"manifest 読み込み: {len(manifest_records)}件")

    # PDF走査
    pdf_files = sorted(glob.glob(os.path.join(args.pdf_dir, "*.pdf")))
    if not pdf_files:
        print(f"ERROR: PDF が見つかりません: {args.pdf_dir}")
        return

    print(f"=== EPS修正検出 検証 ===")
    print(f"旧ロジック: {_OLD_LOGIC_SOURCE}")
    print(f"PDF候補: {len(pdf_files)}件")
    print()

    results: list[EPSCompareResult] = []
    processed = 0
    _eps_1_0_rejected_count = [0]  # mutable counter for closure

    for pdf_path in pdf_files:
        if args.limit and processed >= args.limit:
            break

        pdf_name = os.path.basename(pdf_path)

        # テキスト抽出
        text = extract_text_from_pdf(pdf_path)
        if not text.strip():
            continue

        # 業績予想修正関連のみ対象
        if not is_forecast_revision_pdf(text):
            continue

        processed += 1
        title = "業績予想の修正に関するお知らせ"  # PDF内タイトルの代替

        # タイトル推定: 先頭200文字から抽出
        text_head = text[:500]
        for kw in ["業績予想", "予想の修正", "予想と実績値との差異"]:
            if kw in text_head:
                # タイトル行を抽出
                for line in text_head.split("\n"):
                    if kw in line and len(line.strip()) < 80:
                        title = line.strip()
                        break
                break

        is_diff = "差異" in title

        # 旧/新 EPS 抽出
        t0 = time.time()
        old_prev, old_rev = _extract_old_eps(text, title, is_diff)
        t_old = time.time() - t0

        t0 = time.time()
        # Phase 5: pdf_path を渡し、本番のガードロジックを直接利用する
        new_prev, new_rev = _extract_new_eps(text, title, is_diff, pdf_path=pdf_path)
        t_new = time.time() - t0

        # 検証ツール側のガードは、Production 側で同等のものが動くため省略（二重掛け防止）
        # new_prev, new_rev = validate_eps_pair(new_prev, new_rev, old_prev, old_rev, pdf_path)

        # diff_status 判定
        ds = classify_diff_status(old_prev, old_rev, new_prev, new_rev)

        # 失敗カテゴリ
        fail_cat = classify_failure(text, new_prev, new_rev)

        r = EPSCompareResult(
            source_path=pdf_name,
            title=title,
            old_prev_eps=old_prev,
            old_new_eps=old_rev,
            new_prev_eps=new_prev,
            new_new_eps=new_rev,
            diff_status=ds,
            failure_category=fail_cat,
        )

        # Phase 2: expected 照合
        if has_expected:
            manifest_key = pdf_name
            rec = manifest_map.get(manifest_key, {})
            if rec:
                r.expected_prev_eps = rec.get("expected_prev_eps")
                r.expected_new_eps = rec.get("expected_new_eps")
                r.old_status = evaluate_against_expected(
                    old_prev, old_rev,
                    r.expected_prev_eps, r.expected_new_eps,
                )
                r.new_status = evaluate_against_expected(
                    new_prev, new_rev,
                    r.expected_prev_eps, r.expected_new_eps,
                )

        results.append(r)

        # 件ごとの出力
        if args.only_failures and ds == "both_detected_same":
            continue

        mark = {
            "new_only_detected": "✅改善候補",
            "new_completed_partial": "✅改善候補",
            "old_only_detected": "⚠️回帰候補",
            "old_completed_partial": "⚠️回帰候補",
            "both_detected_same": "—",
            "both_detected_different": "🔀差異",
            "both_missing": "❌両方None",
        }.get(ds, ds)

        print(
            f"[{processed:3d}] {pdf_name[:40]:40s} "
            f"旧=({old_prev},{old_rev}) "
            f"新=({new_prev},{new_rev}) "
            f"[{mark}]"
        )
        if fail_cat:
            print(f"      失敗: {fail_cat}")

    # サマリ
    print_summary._eps_1_0_rejected = _eps_1_0_rejected_count[0]
    print_summary(results, has_expected)

    # 出力
    if args.save_jsonl:
        save_jsonl(results, args.save_jsonl)
    if args.save_csv:
        save_csv(results, args.save_csv)
    if args.save_review_csv:
        save_review_csv(results, args.save_review_csv)


if __name__ == "__main__":
    main()
