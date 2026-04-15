# ============================================================
# models.py — OCR結果データモデル
# ============================================================
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OcrWord:
    """OCRで検出された1単語（座標付き）"""
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    confidence: float = 0.0


@dataclass
class OcrPageResult:
    """1ページのOCR結果"""
    page_number: int
    words: list[OcrWord] = field(default_factory=list)
    full_text: str = ""
    width: int = 0
    height: int = 0


@dataclass
class OcrResult:
    """PDF全体のOCR結果"""
    pages: list[OcrPageResult] = field(default_factory=list)
    provider: str = ""
    success: bool = False
    error: str = ""

    @property
    def full_text(self) -> str:
        """全ページのテキストを結合"""
        return "\n".join(p.full_text for p in self.pages if p.full_text)
