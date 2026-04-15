# Quarantine Review JSONL フォーマット

## 出力先
```
review/quarantine_review_segment.jsonl
```
環境変数 `TDNET_REVIEW_DIR` で変更可能。

## 出力タイミング
- v2 で quarantine された時 (全 failed_stage)
- v2 失敗→v1 fallback は review hint 付きで出力

## フィールド定義

| フィールド | 型 | 説明 |
|---|---|---|
| doc_id | string | 開示ドキュメントID |
| ticker | string | 証券コード |
| source_file | string | PDFファイルパス |
| failed_stage | string | 失敗した段階 |
| quarantine_reason | string | quarantine 理由 |
| review_hint | string | レビュー用ヒント |
| candidate_tables | int | テーブル候補数 |
| best_table_score | float? | 最高テーブルスコア |
| sales_col_role | string? | 売上列 role |
| profit_col_role | string? | 利益列 role |
| unit_raw | string? | 検出単位 (raw) |
| unit_multiplier | int? | 単位乗数 |
| header_snapshot | list[str] | ヘッダー先頭3行 |
| row_labels_sample | list[str] | 行ラベルサンプル (最大10件) |
| extraction_engine | string | "v2" |
| rule_trace | list[str] | ルールトレース (最新5件) |
| timestamp | string | ISO 8601 (JST) |

## 1レコード例
```json
{
  "doc_id": "abc123",
  "ticker": "1736",
  "source_file": "path/to/file.pdf",
  "failed_stage": "column_classification",
  "quarantine_reason": "segment_table_found_but_no_sales_profit_columns",
  "review_hint": "売上/利益列のスコアが閾値未満です",
  "candidate_tables": 3,
  "best_table_score": 0.82,
  "sales_col_role": null,
  "profit_col_role": null,
  "unit_raw": "百万円",
  "unit_multiplier": 1000000,
  "header_snapshot": ["報告セグメント", "売上高", "利益又は損失"],
  "row_labels_sample": ["自動車", "産業機器", "調整額", "合計"],
  "extraction_engine": "v2",
  "rule_trace": ["Phase A: 候補ページ 3件", "Phase B: best_table score=0.82"],
  "timestamp": "2026-03-09T19:30:00+09:00"
}
```

## 改善ループでの使い方
1. `quarantine_review_segment.jsonl` を定期レビュー
2. `failed_stage` + `review_hint` で改善ポイントを特定
3. `header_snapshot` / `row_labels_sample` で実データパターンを確認
4. KW辞書・閾値を調整してテスト追加
5. 再実行して改善効果を確認
