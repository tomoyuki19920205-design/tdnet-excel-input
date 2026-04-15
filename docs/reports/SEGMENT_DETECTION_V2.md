# PDF セグメント表自動検出 v2

## 目的

quarantine 上位原因を削減し、PDF セグメント抽出成功率を引き上げる:
- `segment_table_found_but_no_sales_profit_columns`
- `segment_table_found_but_no_rows_extracted`

## v1 との違い

| 項目 | v1 | v2 |
|---|---|---|
| ページ選択 | 先頭8p全結合 | **ページ別スコアリング → 上位N件** |
| 表候補 | 最初の1件のみ | **全候補にスコア → 最高スコア採用** |
| ヘッダー | 結合文字列でKWマッチ | **グリッド再構築 + 列ごとrole判定** |
| 列判定 | 最初のKW一致列 | **全列スコア → 最高スコア列採用** |
| 行判定 | 完全一致skipリスト | **スコアベース分類 (合計/調整/全社/注記)** |
| 数値割当 | 位置ベース (1st=sales) | **列roleベース** |
| quarantine | reason のみ | **failed_stage + review_hint** |
| provenance | なし | **page/table/col/row スコア付き** |

## 7フェーズ処理

### Phase A: Candidate Page Scoring
各PDFページにスコアを付けて候補ページを絞り込む。

加点: セグメント系KW, 財務KW, 調整額/全社, 表あり
減点: 目次KW, ドットリーダー行, 低数値密度

### Phase B: Candidate Table Scoring
候補ページ内の各テーブル領域にスコアを付ける。

加点: 売上系/利益系ヘッダー, セグメント名行, 数値密度, 周辺テキスト
減点: 目次行, 比率中心

### Phase C: Header Grid Reconstruction
`detect_header_band()` で数値行開始位置を推定し、
`reconstruct_header_grid()` で複数行ヘッダーを列ごとに縦結合。
単位情報を `extract_header_units()` で分離。

### Phase D: Column Role Classification
各列に `score_header_role()` + データ特性 (数値率/比率率/文字列率) で
スコアを付けて最高スコア列を sales/profit として採用。

roles: `segment_label`, `sales`, `operating_profit`, `segment_profit`,
       `ordinary_profit`, `ratio`, `yoy`, `assets`, `unknown`

### Phase E: Row Role Classification
各行にスコアを付けてロールを分類。

roles: `segment_item`(抽出対象), `total`, `adjustment`, `corporate`,
       `other`, `note`, `blank`, `header`, `unknown`

### Phase F: Segment Record Assembly
列role + 行role からセグメントレコードを組み立て。confidence を算出。

### Phase G: Stage-aware Quarantine
各段階の失敗に対して `failed_stage` + `review_hint` を記録。

## Feature Flag

```python
# v2 有効 (デフォルト)
extract_segment_financials(pdf_path, title, use_v2=True)

# v2 無効 (v1 のみ)
extract_segment_financials(pdf_path, title, use_v2=False)

# 環境変数で無効化
SEGMENT_V2_DISABLE=1
```

## Fallback 戦略

1. v2 実行
2. v2 成功 (segments > 0 && quarantine_reason == "") → 採用
3. v2 失敗 → 自動的に v1 にフォールバック
4. v2 例外 → v1 にフォールバック

## 今後の改善余地

1. 列位置ベースの複数行ヘッダー結合 (現在はトークン分割ベース)
2. pdfplumber の table 座標情報を活用した列位置推定
3. 行内の数値位置からの列自動推定
4. 比率列/前年比列の高度な判定
5. confidence 閾値の調整 (実データフィードバック)
