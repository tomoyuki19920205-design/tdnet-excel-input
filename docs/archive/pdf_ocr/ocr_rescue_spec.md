OCR/GS導入後の救済率検証 仕様書

目的
Google OCR + Ghostscript による救済効果を定量評価する

検証対象
- native失敗PDF（rescue候補）
- native成功PDF（回帰監視）

成功判定（簡易）
[セグメント]
- valid_segments >= 2
- sales_non_null >= 1

[受注]
- 主要項目が1つ以上non-null

判定カテゴリ
- rescued: native失敗 → OCR成功
- regression: native成功 → OCR失敗
- unchanged: 変化なし

必須ログ
- pdf_path
- success判定
- segment数 / sales件数
- ocr使用有無
- 処理時間

出力
tmp/ocr_rescue_results.json

サマリ出力
- 検証件数
- native成功率
- OCR成功率
- 救済率
- 回帰率
- 平均処理時間

# OCR実行前提（固定）

- Google Vision OCRはJSONキー方式を使用しない
- 認証はApplication Default Credentials（gcloud auth application-default login）を使用する
- quota project は固定: ocr-test-491310
- OCR実行は必ず .venv\Scripts\python.exe を使用する

# OCR検証時の必須確認

OCR結果が不正な場合、以下を必ず先に確認すること:

1. gcloud config list で project が ocr-test-491310 になっているか
2. gcloud auth list で想定アカウントになっているか
3. google-cloud-vision が import可能か
4. 403 (quota project / SERVICE_DISABLED) エラーが出ていないか
5. 実行Pythonが .venv かどうか

これらを確認せずにOCRロジックの問題と判断しないこと

# 禁止

- 複数のOCR実装を並立させること
- 正式実装以外を参照すること

