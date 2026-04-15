# ルール

## 最重要
- 本番ロジックは変更しない
- forecast_extractor.pyを壊さない
- 1件失敗で止めない

## 実装方針
- 比較ロジックは verify 側に寄せる
- ブラックボックス禁止
- 判定基準は明示

## 禁止
- 印象評価
- successの過大判定
- 大規模リファクタ

## 最終報告必須
- exact_match_rate
- false_positive_rate
- improved/regressed
- 失敗カテゴリ
- 改善案3つ
