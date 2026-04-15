PDF OCRアップグレード仕様（SUMMARY）

目的
既存PDF抽出で失敗するケースをOCR fallbackで救済する

基本方針
- native優先
- 失敗時のみOCR
- Ghostscriptで画像化
- Google Vision使用
- 座標付きデータ取得

スコープ
- OCR基盤
- fallback
- logging

やらない
- Document AI
- LLM
- DB保存

フロー
1. native抽出
2. 失敗時OCR
3. 再抽出

成功条件
- nativeに影響なし
- 一部改善

指示
- 実装計画のみ
- 実装しない
- 簡潔に
