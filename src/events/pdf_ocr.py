#!/usr/bin/env python3
"""pdf_ocr.py — PDF OCR フォールバックモジュール

forecast_revision 限定で使用。
既存のPDFテキスト抽出が失敗・低品質の場合のみ、
Ghostscript + Google Cloud Vision OCR で救済する。

環境変数:
- ENABLE_GOOGLE_OCR=1  : OCR を有効化
- GOOGLE_APPLICATION_CREDENTIALS : Google Cloud 認証
- GHOSTSCRIPT_EXE : Ghostscript 実行ファイルパス（省略時は gswin64c / gs）
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Optional

logger = logging.getLogger("forecast_ocr")


# ============================================================
# 設定
# ============================================================
def _is_ocr_enabled() -> bool:
    """ENABLE_GOOGLE_OCR=1 が設定されているか"""
    return os.environ.get("ENABLE_GOOGLE_OCR", "").strip() in ("1", "true", "yes")


def _get_ghostscript_exe() -> str:
    """Ghostscript 実行ファイルパスを取得"""
    env = os.environ.get("GHOSTSCRIPT_EXE", "").strip()
    if env:
        return env
    # PATH 上で検索
    for candidate in ["gswin64c", "gswin32c", "gs"]:
        found = shutil.which(candidate)
        if found:
            return found
    # Windows: Program Files 配下のGSインストール先を探索
    if os.name == "nt":
        for pf in [os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")]:
            if not pf:
                continue
            gs_dir = os.path.join(pf, "gs")
            if os.path.isdir(gs_dir):
                for exe_name in ["gswin64c.exe", "gswin32c.exe"]:
                    matches = list(Path(gs_dir).rglob(exe_name))
                    if matches:
                        return str(matches[0])
    return "gs"


# ============================================================
# Ghostscript PDF → 画像
# ============================================================
def rasterize_pdf_with_ghostscript(
    pdf_path: str,
    out_dir: str | None = None,
    dpi: int = 300,
    max_pages: int = 5,
) -> list[str]:
    """PDF を Ghostscript で画像にラスタライズする。

    Parameters
    ----------
    pdf_path : str
        入力PDFのパス
    out_dir : str | None
        出力ディレクトリ。None なら一時ディレクトリを作成
    dpi : int
        解像度（デフォルト 300）
    max_pages : int
        最大ページ数（デフォルト 5）

    Returns
    -------
    list[str]
        生成された画像ファイルパスのリスト。失敗時は空リスト。
    """
    if not os.path.isfile(pdf_path):
        logger.warning(f"[forecast_ocr] PDF not found: {pdf_path}")
        return []

    if out_dir is None:
        out_dir = tempfile.mkdtemp(prefix="forecast_ocr_")

    gs_exe = _get_ghostscript_exe()
    output_pattern = os.path.join(out_dir, "page_%03d.png")

    cmd = [
        gs_exe,
        "-dNOPAUSE",
        "-dBATCH",
        "-dSAFER",
        "-sDEVICE=png16m",
        f"-r{dpi}",
        f"-dFirstPage=1",
        f"-dLastPage={max_pages}",
        f"-sOutputFile={output_pattern}",
        pdf_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            logger.warning(
                f"[forecast_ocr] Ghostscript failed (rc={result.returncode}): "
                f"{result.stderr[:200]}"
            )
            return []

    except FileNotFoundError:
        logger.warning(f"[forecast_ocr] Ghostscript not found: {gs_exe}")
        return []
    except subprocess.TimeoutExpired:
        logger.warning(f"[forecast_ocr] Ghostscript timeout (120s)")
        return []
    except Exception as e:
        logger.warning(f"[forecast_ocr] Ghostscript error: {e}")
        return []

    # 生成された画像を収集
    images = sorted(
        str(p) for p in Path(out_dir).glob("page_*.png")
    )
    logger.info(f"[forecast_ocr] rasterized pages={len(images)}")
    return images


# ============================================================
# Google Cloud Vision OCR
# ============================================================
def extract_text_via_google_ocr(image_paths: list[str]) -> str:
    """画像リストから Google Cloud Vision OCR でテキストを抽出する。

    Returns
    -------
    str
        全ページのテキストを結合した文字列。失敗時は空文字。
    """
    if not image_paths:
        return ""

    try:
        from google.cloud import vision
    except ImportError:
        logger.warning("[forecast_ocr] google-cloud-vision not installed, OCR skipped")
        return ""

    try:
        client = vision.ImageAnnotatorClient()
    except Exception as e:
        logger.warning(f"[forecast_ocr] Vision client init failed: {e}")
        return ""

    all_texts = []
    for img_path in image_paths:
        try:
            with open(img_path, "rb") as f:
                content = f.read()

            image = vision.Image(content=content)
            response = client.document_text_detection(image=image)

            if response.error.message:
                logger.warning(
                    f"[forecast_ocr] OCR error for {img_path}: "
                    f"{response.error.message}"
                )
                continue

            if response.full_text_annotation:
                all_texts.append(response.full_text_annotation.text)

        except Exception as e:
            logger.warning(f"[forecast_ocr] OCR failed for {img_path}: {e}")
            continue

    text = "\n".join(all_texts)
    logger.info(f"[forecast_ocr] ocr_text_len={len(text)}")
    return text


# ============================================================
# OCR 発火条件判定
# ============================================================
def _count_garbled_ratio(text: str) -> float:
    """文字化け率を推定する。

    制御文字、置換文字（U+FFFD）、連続の ? や . を文字化け指標とする。
    """
    if not text:
        return 1.0
    total = len(text)
    garbled = 0
    for ch in text:
        cat = unicodedata.category(ch)
        if cat.startswith("C") and ch not in ("\n", "\r", "\t"):
            garbled += 1
        elif ch == "\ufffd":
            garbled += 1
    # 連続 ? パターン
    garbled += len(re.findall(r"\?{3,}", text)) * 3
    return garbled / total if total > 0 else 0.0


def should_run_ocr_fallback(
    raw_text: str,
    event,
    diagnostics: dict | None = None,
) -> bool:
    """OCR フォールバックを実行すべきかを判定する。

    OCR が有効かどうかは呼び出し側でチェックする。
    この関数は「抽出品質が不十分か」だけを判定する。

    発火条件:
    1. raw_text が空 or 200文字未満
    2. event が None
    3. score_forecast_result(event) == 0（数値ゼロ = 完全抽出失敗）
    4. subtype が undecided
    """
    reasons = []

    # 1. テキストが空または短い
    if not raw_text or len(raw_text.strip()) < 200:
        reasons.append("text_empty_or_short")
        if diagnostics is not None:
            diagnostics["reason"] = "|".join(reasons)
        logger.info(f"[forecast_ocr] should_run=True reasons={reasons}")
        return True

    # 2. event が None
    if event is None:
        reasons.append("event_is_none")
        if diagnostics is not None:
            diagnostics["reason"] = "|".join(reasons)
        logger.info(f"[forecast_ocr] should_run=True reasons={reasons}")
        return True

    # 3. スコアが 0（数値抽出が完全に失敗）
    if score_forecast_result(event) == 0:
        reasons.append("score_zero")
        if diagnostics is not None:
            diagnostics["reason"] = "|".join(reasons)
        logger.info(f"[forecast_ocr] should_run=True reasons={reasons}")
        return True

    # 4. subtype が undecided
    if getattr(event, "subtype", None) == "undecided":
        reasons.append("subtype_undecided")
        if diagnostics is not None:
            diagnostics["reason"] = "|".join(reasons)
        logger.info(f"[forecast_ocr] should_run=True reasons={reasons}")
        return True

    # 十分な抽出結果がある
    if diagnostics is not None:
        diagnostics["reason"] = "not_needed"
    logger.info(f"[forecast_ocr] skipped reason=not_needed")
    return False


# ============================================================
# 抽出結果スコアリング
# ============================================================
def score_forecast_result(event) -> int:
    """ForecastRevisionEvent のスコアを算出する。

    スコアが高いほど抽出品質が良い。
    """
    if event is None:
        return 0

    score = 0

    # 修正値（主要指標）
    if event.revised_net_income is not None:
        score += 3
    if event.revised_op is not None:
        score += 3
    if event.revised_ordinary is not None:
        score += 2
    if event.revised_sales is not None:
        score += 2

    # 前回予想値
    if event.previous_net_income is not None:
        score += 2
    if event.previous_op is not None:
        score += 2

    # EPS
    if event.revised_eps is not None:
        score += 2
    if event.previous_eps is not None:
        score += 2

    # subtype 判定成功
    if event.subtype != "undecided":
        score += 2

    return score


# ============================================================
# 結果選択
# ============================================================
def select_better_result(base, ocr):
    """base と ocr の結果を比較し、良い方を返す。

    同スコアなら base を優先（ネイティブ抽出の方が信頼性が高い）。
    """
    base_score = score_forecast_result(base)
    ocr_score = score_forecast_result(ocr)

    logger.info(
        f"[forecast_ocr] base_score={base_score} ocr_score={ocr_score} "
        f"selected={'ocr' if ocr_score > base_score else 'base'}"
    )

    if ocr_score > base_score:
        # OCR 結果を採用する場合、extraction_source を更新
        if hasattr(ocr, "extraction_source"):
            ocr.extraction_source = "ocr_fallback"
        return ocr

    return base


# ============================================================
# 一時ファイルクリーンアップ
# ============================================================
def cleanup_temp_images(image_paths: list[str]) -> None:
    """OCR 用一時画像を削除する"""
    for img_path in image_paths:
        try:
            if os.path.isfile(img_path):
                os.remove(img_path)
        except OSError:
            pass

    # 親ディレクトリも削除を試みる
    if image_paths:
        parent = os.path.dirname(image_paths[0])
        try:
            if parent and os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
        except OSError:
            pass
