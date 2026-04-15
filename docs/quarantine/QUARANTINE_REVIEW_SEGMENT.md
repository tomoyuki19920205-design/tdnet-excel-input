# Quarantine レビュー — セグメント抽出

## 既存 quarantine 理由

| reason | 対象 | 解消状況 |
|---|---|---|
| `segment_table_found_but_no_sales_profit_columns` | ヘッダーに売上/利益列が見つからない | v2 で改善見込 |
| `segment_table_found_but_no_rows_extracted` | 表はあるがデータ行が抽出できない | v2 で改善見込 |
| `no_segment_table` | セグメント表自体が見つからない | v2 で改善見込 (ページスコアリング) |

## V2 での失敗段階マッピング

| v1 quarantine reason | v2 failed_stage | review_hint |
|---|---|---|
| `no_segment_table` | `page_scoring` | セグメント表を含むページが見つかりません |
| `no_segment_table` | `table_scoring` | セグメント表候補のスコアが閾値未満 |
| `segment_table_found_but_no_sales_profit_columns` | `column_classification` | 売上/利益列のスコアが閾値未満 |
| `segment_table_found_but_no_rows_extracted` | `row_classification` | セグメント名行が検出されません |
| (新規) | `record_assembly` | 数値割当に失敗 |

## v2 による改善メカニズム

### 1. ページスコアリング (Phase A)
- v1: 全ページを結合して最初のKW一致を使用
- v2: 各ページに個別スコアを付けて最適ページを選択
- **効果**: 目次ページの誤検出を排除、後半ページのセグメント表を確実にキャッチ

### 2. テーブルスコアリング (Phase B)
- v1: 最初のKW一致テーブルのみ
- v2: 全候補テーブルにスコアを付けて最高スコアを採用
- **効果**: 参考表ではなく本表を正しく選択

### 3. ヘッダー正規化 (Phase C+D)
- v1: ヘッダー文字列結合でKW完全一致
- v2: グリッド再構築 → 列ごとスコアリング
- **効果**: 空白揺れ・複数行ヘッダー・結合セルに強い

### 4. 行分類 (Phase E)
- v1: 完全一致skipリスト
- v2: スコアベース + 部分一致 + 注記行検出
- **効果**: `消去又は全社`等の複合ラベル、`（注）`行を正しく除外

## 残る改善余地

1. **列位置ベースのヘッダー結合**: 現在はトークン分割ベースで位置の正確性に限界
2. **pdfplumber table座標の活用**: セル境界をより正確に把握
3. **confidence閾値の調整**: 実データフィードバックで最適化
4. **比率/前年比列の高度判定**: 数値パターンからの統計的判定

## 次回実データ回帰での確認項目

```
# 比較すべきメトリクス
files_total        # 処理ファイル数
succeeded          # 成功数
quarantined        # quarantine 数
failed             # 失敗数
segment_records    # セグメントレコード数
v2_adopted         # v2 採用件数
v1_fallback        # v1 フォールバック件数
```
