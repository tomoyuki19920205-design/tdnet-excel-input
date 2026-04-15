# OCR Logic

## 正式実装
OCRのセグメント抽出は以下のみを正とする:

- src/pdf/ocr/segment_extractor_ocr.py

## 参照対象外
以下は正式実装として扱わない:

- docs/archive 配下
- recovered系ファイル
- テスト用スクリプト
- __pycache__ 配下

## 原則
- OCRは fallback のみ
- Native抽出を優先する
