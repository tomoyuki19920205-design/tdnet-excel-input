# ============================================================
# ghostscript_render.py — Ghostscript PDF→PNG レンダラー
# ============================================================
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("tdnet")

# デフォルト設定（環境変数で上書き可能）
_GS_BIN = os.environ.get("GHOSTSCRIPT_BIN", "gswin64c.exe")
_DPI = int(os.environ.get("PDF_OCR_DPI", "300"))
_MAX_PAGES = int(os.environ.get("PDF_OCR_MAX_PAGES", "8"))


def render_pdf_to_images(
    pdf_path: str | Path,
    output_dir: str | Path | None = None,
    dpi: int | None = None,
    max_pages: int | None = None,
) -> list[Path]:
    """
    GhostscriptでPDFをPNG画像に変換する。

    Args:
        pdf_path: 入力PDFファイルパス
        output_dir: 出力ディレクトリ（Noneならtempdir）
        dpi: 解像度（デフォルト: 300）
        max_pages: 最大ページ数（デフォルト: 8）

    Returns:
        生成されたPNG画像のパスリスト

    Raises:
        FileNotFoundError: Ghostscriptが見つからない場合
        RuntimeError: Ghostscript実行エラー
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    dpi = dpi or _DPI
    max_pages = max_pages or _MAX_PAGES

    # 出力ディレクトリ
    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="ocr_render_"))
    else:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

    output_pattern = str(output_dir / "page_%03d.png")

    cmd = [
        _GS_BIN,
        "-dNOPAUSE",
        "-dBATCH",
        "-dSAFER",
        "-sDEVICE=png16m",
        f"-r{dpi}",
        f"-dFirstPage=1",
        f"-dLastPage={max_pages}",
        f"-sOutputFile={output_pattern}",
        str(pdf_path),
    ]

    logger.info(f"[OCR] Ghostscript render: dpi={dpi}, max_pages={max_pages}")
    logger.debug(f"[OCR] cmd: {' '.join(cmd)}")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Ghostscript not found: {_GS_BIN}. "
            "Set GHOSTSCRIPT_BIN environment variable."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Ghostscript timed out (120s)")

    if proc.returncode != 0:
        logger.warning(f"[OCR] Ghostscript stderr: {proc.stderr[:500]}")
        raise RuntimeError(f"Ghostscript failed (rc={proc.returncode})")

    # 生成されたPNG一覧を取得
    images = sorted(output_dir.glob("page_*.png"))
    logger.info(f"[OCR] Rendered {len(images)} page(s)")

    return images
