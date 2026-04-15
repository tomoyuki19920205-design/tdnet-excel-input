# PDF セグメント表自動検出 v2 — Phase 2

## 目的

Phase 1 (「表を見つける」) から Phase 2 (「意味を正しく正規化してDB投入できる」) に進化。

## 追加機能一覧

| 機能 | ファイル | 目的 |
|---|---|---|
| 単位検出 | `unit_detection.py` | 百万円/千円/億円/USD等の自動判定 |
| セグメント名正規化 | `segment_name_normalizer.py` | raw/normalized 分離、表記ゆれ吸収 |
| 利益 taxonomy | `column_analysis.py` 強化 | 12種 column role の詳細判定 |
| 行 role 詳細化 | `row_analysis.py` 強化 | subtotal/elimination 追加、is_reportable |
| quarantine review | `segment_detection_v2.py` | jsonl 出力で弱教師データ蓄積 |

## 1. unit_detection

### 優先順位
1. ヘッダーセル内 (confidence +0.1)
2. テーブル直上テキスト
3. ページ上部 (先頭20行, confidence -0.1)
4. ページ全体 (confidence -0.2)
5. unknown

### 対応パターン
- 百万円 / 千円 / 億円 / 円
- 千米ドル / 百万米ドル
- millions of yen / thousands of yen
- 「（単位：百万円）」等 wrapper 形式

## 2. segment_name_normalizer

### Phase 1: 基本文字整形
- NFKC正規化、改行→空白、連続空白圧縮
- 注記記号除去 (※1, (注))
- 末尾接尾辞除去: 「事業」「部門」「関連」「セグメント」

### Phase 2: 同義語辞書 (14エントリ)
- automotive → 自動車, electronics → 電子, housing → 住宅, etc.

### Phase 3 (将来hook)
- ticker別 company-specific alias

## 3. Column Taxonomy

| Role | 例 | 採用可能? |
|---|---|---|
| sales | 売上高, Revenue | ○ |
| operating_profit_like | 営業利益, Core OP | ○ (強) |
| segment_profit_like | セグメント利益 | ○ (強) |
| ordinary_profit_like | 経常利益 | ○ (弱め) |
| pretax_like | 税引前利益 | △ (低スコア) |
| net_income_like | 当期純利益 | △ (低スコア) |
| margin_like | 営業利益率 | × 利益判定から除外 |
| assets_like | セグメント資産 | × |
| depreciation_like | 減価償却費 | × |
| capex_like | 設備投資額 | × |

## 4. Row Role

| Role | is_reportable | 例 |
|---|---|---|
| segment | True | 自動車, 電子 |
| subtotal | False | 小計, 報告セグメント計 |
| total | False | 合計, 総計 |
| adjustment | False | 調整額, 連結調整 |
| corporate | False | 全社, 配賦不能 |
| elimination | False | 消去又は全社, セグメント間消去 |
| other | False | その他 |
| note | False | （注）... |

**方針**: 「その他」は今回 False。docs/QUARANTINE_REVIEW_FORMAT.md に明記。

## 5. v1 fallback の扱い

- v1 fallback 時は Phase 2 フィールドを None 埋め
- extraction_engine フィールドで "v2" / "v1_fallback" を区別
- `SEGMENT_V2_DISABLE=1` で v2 全体を無効化可能

## 今後の拡張ポイント
1. company profile cache (ticker → 期待セグメント名辞書)
2. OCR fallback
3. column-level unit override
4. quarantine reviewの再学習ループ
5. pdfplumber table座標活用
