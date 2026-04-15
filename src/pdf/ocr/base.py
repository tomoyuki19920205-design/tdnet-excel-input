# ============================================================
# base.py — OCRプロバイダ基底クラス
# ============================================================
from __future__ import annotations

import abc
from pathlib import Path

from .models import OcrPageResult


class OcrProvider(abc.ABC):
    """OCRプロバイダの基底インターフェース"""

    @abc.abstractmethod
    def ocr_image(self, image_path: Path) -> OcrPageResult:
        """
        画像ファイル1枚をOCRしてページ結果を返す。

        Args:
            image_path: PNG画像のパス

        Returns:
            OcrPageResult

        Raises:
            OcrError: OCR失敗時
        """
        ...


class OcrError(Exception):
    """OCR処理で発生するエラー"""
    pass
