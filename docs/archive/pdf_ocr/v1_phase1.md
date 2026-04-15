PDF OCRアップグレード仕様（PHASE1）

目的
PDF抽出失敗時のみOCR実行

環境変数
- PDF_OCR_ENABLED=1
- PDF_OCR_PROVIDER=google_vision
- GHOSTSCRIPT_BIN=gswin64c.exe
- PDF_OCR_DPI=300
- PDF_OCR_MAX_PAGES=8

追加ファイル
- src/pdf/ocr/base.py
- src/pdf/ocr/google_vision_ocr.py
- src/pdf/ocr/ghostscript_render.py
- src/pdf/ocr/models.py

処理
1. native抽出
2. 失敗判定
3. OCR実行
4. 再投入

Ghostscript
- PDF→PNG
- 300dpi

OCR
- Google Vision

条件
- native上書き禁止
- OCR失敗でも継続
