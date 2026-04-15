# Rules

## 禁止
- docs/archive 配下を現行仕様として扱うこと
- recovered系ファイルを参照すること
- 無関係なファイルまで広く修正すること
- 複数のOCR実装を並立させること

## 必須
- docs/specs/pdf_ocr/00_index.md を起点に参照すること
- OCRの正式実装は src/pdf/ocr/segment_extractor_ocr.py として扱うこと
- 差分修正のみ行うこと
- 既存の Native抽出優先方針を維持すること

## 方針
- Native抽出を優先
- OCRは fallback のみ
