#!/usr/bin/env python3
import os
import sys
import logging
import subprocess
import tempfile
from pathlib import Path

# プロジェクトルートをパスに追加 (src.events.pdf_ocr をインポート可能にするため)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# 案件のロギング設定に合わせる
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
logger = logging.getLogger("ocr_smoke_test")

def smoke_test(pdf_path: str):
    logger.info("=== Google OCR Smoke Test Start ===")
    logger.info(f"Target file: {pdf_path}")
    
    # 1. 認証情報の確認
    env_creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    enable_ocr = os.environ.get("ENABLE_GOOGLE_OCR")
    
    logger.info(f"ENABLE_GOOGLE_OCR: {enable_ocr}")
    logger.info(f"GOOGLE_APPLICATION_CREDENTIALS: {env_creds}")
    
    if env_creds:
        if os.path.exists(env_creds):
            logger.info(f"Credentials file exists at: {env_creds}")
        else:
            logger.error(f"Credentials file NOT FOUND at: {env_creds}")
    else:
        logger.info("GOOGLE_APPLICATION_CREDENTIALS is not set. Using Application Default Credentials (ADC) or system default.")

    # 2. Ghostscript の確認
    from src.events.pdf_ocr import _get_ghostscript_exe, rasterize_pdf_with_ghostscript
    gs_exe = _get_ghostscript_exe()
    logger.info(f"Ghostscript EXE: {gs_exe}")

    # 3. PDF ラスタライズ
    logger.info("Rasterizing PDF...")
    image_paths = rasterize_pdf_with_ghostscript(pdf_path, max_pages=1)
    if not image_paths:
        logger.error("Failed to rasterize PDF.")
        return
    logger.info(f"Rasterized to: {image_paths}")

    # 4. Google OCR API クライアント初期化
    try:
        from google.cloud import vision
        logger.info("Initializing Vision API Client...")
        client = vision.ImageAnnotatorClient()
        logger.info("Vision API Client initialized successfully.")
    except Exception as e:
        logger.error(f"Vision API Client init failed: {type(e).__name__}: {e}")
        return

    # 5. API 呼び出し
    logger.info(f"Calling Google Cloud Vision API (document_text_detection) for {image_paths[0]}...")
    try:
        with open(image_paths[0], "rb") as f:
            content = f.read()

        image = vision.Image(content=content)
        # 実際にAPIを呼ぶ
        response = client.document_text_detection(image=image)
        
        if response.error.message:
            logger.error(f"OCR API returned ERROR: {response.error.message}")
            return

        # 6. 結果表示
        full_text = response.full_text_annotation.text if response.full_text_annotation else ""
        text_len = len(full_text)
        
        if text_len > 0:
            logger.info("OCR API Success!")
            logger.info(f"Total Character Count: {text_len}")
            logger.info("--- OCR Text Preview (First 500 chars) ---")
            print(full_text[:500])
            logger.info("--- End of Preview ---")
        else:
            logger.warning("OCR API success but returned EMPTY text.")
            if response.full_text_annotation:
                 logger.info("full_text_annotation exists but text is empty.")
            else:
                 logger.info("full_text_annotation is None.")

    except Exception as e:
        logger.error(f"OCR API Call Exception: {type(e).__name__} - {e}")
        import traceback
        logger.error(traceback.format_exc())

    finally:
        # クリーンアップ
        for img in image_paths:
            if os.path.exists(img):
                os.remove(img)
        parent = os.path.dirname(image_paths[0]) if image_paths else None
        if parent and os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)

if __name__ == "__main__":
    test_pdf = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\takuy\OneDrive\tdnet-excel-input\data\docs\140120260303574773.pdf"
    if not os.path.exists(test_pdf):
        print(f"Error: File not found {test_pdf}")
        sys.exit(1)
    smoke_test(test_pdf)
