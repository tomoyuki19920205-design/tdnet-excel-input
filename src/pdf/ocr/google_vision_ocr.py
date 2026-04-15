# ============================================================
# google_vision_ocr.py — Google Cloud Vision OCRプロバイダ
# ============================================================
from __future__ import annotations

import logging
from pathlib import Path

from .base import OcrError, OcrProvider
from .models import OcrPageResult, OcrWord

logger = logging.getLogger("tdnet")


class GoogleVisionOcr(OcrProvider):
    """Google Cloud Vision APIを使ったOCRプロバイダ"""

    def __init__(self) -> None:
        try:
            from google.cloud import vision  # type: ignore[import-untyped]
            self._client = vision.ImageAnnotatorClient()
            self._vision = vision
        except ImportError:
            raise OcrError(
                "google-cloud-vision is not installed. "
                "Run: pip install google-cloud-vision"
            )
        except Exception as e:
            raise OcrError(f"Vision API client init failed: {e}")

    def ocr_image(self, image_path: Path) -> OcrPageResult:
        """
        PNG画像1枚をGoogle Vision APIでOCRする。

        Args:
            image_path: PNG画像パス

        Returns:
            OcrPageResult（full_text + 座標付きwords）
        """
        vision = self._vision

        content = image_path.read_bytes()
        image = vision.Image(content=content)

        try:
            response = self._client.document_text_detection(image=image)
        except Exception as e:
            raise OcrError(f"Vision API call failed: {e}")

        if response.error.message:
            raise OcrError(f"Vision API error: {response.error.message}")

        full_text = ""
        words: list[OcrWord] = []

        if response.full_text_annotation:
            full_text = response.full_text_annotation.text

            # 単語レベルの座標を抽出
            for page in response.full_text_annotation.pages:
                for block in page.blocks:
                    for paragraph in block.paragraphs:
                        for word in paragraph.words:
                            word_text = "".join(
                                symbol.text for symbol in word.symbols
                            )
                            # バウンディングボックス
                            vertices = word.bounding_box.vertices
                            if len(vertices) >= 4:
                                words.append(OcrWord(
                                    text=word_text,
                                    x0=vertices[0].x,
                                    y0=vertices[0].y,
                                    x1=vertices[2].x,
                                    y1=vertices[2].y,
                                    confidence=word.confidence,
                                ))

        logger.info(
            f"[OCR] Vision: {len(words)} words, "
            f"text_len={len(full_text)}"
        )

        return OcrPageResult(
            page_number=0,  # 呼び出し側で設定
            words=words,
            full_text=full_text,
        )
