# TDNET DATA PLATFORM SPEC

## Overview

本システムは TDNET / EDINET / J-Quants 等から財務データを取得し、Supabase(PostgreSQL)に格納・正規化する **データ基盤システム** である。

本システムは以下を目的とする：

* 財務データの統合・正規化
* 高速クエリ可能なDB構築
* AI要約・通知システムへのデータ供給

※ Excelは使用しない（完全排除）

---

# Architecture

## Data Flow

fetch → extract → normalize → canonicalize → store → serve → notify

---

## Components

### 1. fetcher.py

* TDNET / EDINET / J-Quants からデータ取得
* classify_disclosure により分類

---

### 2. extractor.py

* PDF / XBRL / HTML から数値抽出
* タイトル判定は柔軟に行う（決算短信限定禁止）

---

### 3. normalizer

* 単位統一（円 / 百万円）
* NaN / inf 除去

---

### 4. canonical layer

* ソース差異吸収
* canonical_financials に統合

---

### 5. pipeline

* earnings_production_pipeline.py による処理統合

---

### 6. serve layer（重要）

* API / Viewer / AIが参照する層
* Supabaseを直接利用

---

# Database Design

## canonical_financials

### Purpose

全ソース統合済みの正規化財務テーブル

---

### Primary Key

* source_row_key (BIGINT, monotonic)

---

### Columns

* ticker
* fiscal_year
* period_type (FY / Q1 / Q2 / Q3 / Q4)
* metric (sales / op / ordinary / net_income)
* value
* source (tdnet / jquants / edinet)

---

### Index（必須）

* source_row_key
* ticker + fiscal_year

---

# CRITICAL PERFORMANCE RULES（最重要）

## 1. Pagination Rule

### ✅ REQUIRED

keyset pagination ONLY

```sql
WHERE source_row_key > last_key
ORDER BY source_row_key
LIMIT N
```

---

### ❌ FORBIDDEN

```sql
OFFSET
```

理由:

* 全スキャン発生 → Disk IO枯渇 → Supabase不安定化

---

## 2. Batch Processing Rule

### ✅ REQUIRED

* batch size: 500〜2000
* bulk upsert使用

---

### ❌ FORBIDDEN

* row-by-row update
* 逐次insert

---

## 3. Transaction Rule

### ✅ REQUIRED

* batch単位でcommit

---

### ❌ FORBIDDEN

* loop内commit

---

## 4. Update Strategy

### ✅ REQUIRED

* 差分更新のみ

---

### ❌ FORBIDDEN

* 全件UPDATE

---

## 5. Concurrency Rule

### ❌ FORBIDDEN

* 重い処理の並列実行（複数ウィンドウ含む）

理由:

* Supabase Disk IO Budget枯渇
* インスタンスクラッシュ

---

# Known Bottlenecks

## 1. Supabase Disk IO

### 症状

* レスポンス遅延
* CPU上昇（IO待ち）
* 接続不安定

---

### 原因

* UPDATE連打
* OFFSET pagination
* 並列実行

---

### 対策

* keyset pagination
* batch upsert
* 処理直列化

---

## 2. 大量データ処理（J-Quants等）

* 50,000件以上
* canonical更新でIO最大負荷

---

### 必須対策

* keyset pagination
* batch処理

---

# Processing Standards

## fix_canonical

### REQUIRED

* keyset pagination
* bulk upsert

---

### Example

```python
last_key = 0

while True:
    rows = fetch(source_row_key > last_key LIMIT 1000)

    if not rows:
        break

    processed = process(rows)
    bulk_upsert(processed)

    last_key = rows[-1].source_row_key
```

---

# Data Consistency Rules

## Disclosure Classification

### RULE

fetcher と extractor の条件は必ず一致させる

---

### ❌ NG例

* fetcher: 四半期OK
* extractor: 決算短信のみ

---

## Unit Handling

* 円 / 百万円統一
* source差異吸収

---

# Error Handling

## Skip Conditions

* PDF解析不可
* XBRL未取得

---

## IMPORTANT

* state.db による永久スキップを防ぐ

---

# Serve Layer Rules（新規）

## API / Viewer

### 要件

* DB直接参照
* キャッシュ活用（必要に応じて）

---

### ❌ FORBIDDEN

* 大量データの都度計算

---

# Anti-Patterns（禁止事項）

* OFFSET pagination
* row-by-row update
* loop内commit
* 全件UPDATE
* 並列バッチ実行
* 非インデックス検索

---

# Performance Targets

* O(n)処理保証
* IO最小化
* 安定稼働（クラッシュ禁止）

---

# Future Improvements

* 非同期ジョブキュー導入
* マテリアライズドビュー
* 差分検知高度化

---

# Operating Rules

* 本specを唯一の仕様とする
* すべての実装は本spec準拠
* 仕様変更時は必ず更新

---

## Performance & I/O Safety Rules（必須遵守）

### 目的

本システムにおける処理遅延・Supabase負荷・タイムアウト・不安定化を防ぐため、
外部I/Oおよびデータ処理の実装ルールを定義する。

---

### 最上位原則（必須）

以下4点は最優先で遵守すること：

1. ループ内で外部I/Oを発行してはならない
2. 全件スキャンを前提とした実装をしてはならない
3. デフォルトは差分更新とする（フルリビルド禁止）
4. 同一資源への同時実行は禁止（排他制御必須）

---

### 禁止事項（Violation扱い）

#### 1. N+1 I/O

* ループ内でのHTTP/DB/API/ファイルアクセス
* 1件ごとの存在確認 / 衝突確認 / upsert / delete

#### 2. 全件スキャン

* Python側でfilterする前提の全件取得
* where/in/rangeで絞れるのに取得後に選別する実装

#### 3. 非効率ページング

* 大量件数処理でのoffset pagination使用

#### 4. 重複取得

* 同一run内で同一条件のデータ再取得

#### 5. 細粒度書き込み

* 1件ごとのDB書き込み・commit
* 1件ごとのstate保存・pipeline_runs更新

#### 6. 過剰ログ

* レコード単位のINFOログ
* 巨大データの常時出力

#### 7. フルリビルド濫用

* 差分で可能な処理の全件再処理

#### 8. 無排他実行

* 定期ジョブと手動実行の同時DB操作
* 同一stateファイルの並列更新

---

### 必須実装ルール

#### データ取得

* 必ずDB/API側で絞り込み（where / like / in / range）
* 大量処理はkeyset paginationを使用

#### 書き込み

* batch upsert / batch delete を必須とする
* バッチサイズは原則 100〜1000件

#### HTTPアクセス

* requests.Sessionの再利用を必須とする

#### 再試行

* 指数バックオフ付き retry を使用
* 対象は 429 / 502 / 503 / 504 のみ

#### ログ

* INFOは集計・開始・終了・警告・エラーのみ
* 詳細ログはDEBUG限定

#### メモリ

* 全件メモリ保持禁止
* chunk / generator 処理を優先

#### 更新戦略

* デフォルトは差分更新
* フルリビルドは明示フラグ時のみ許可

#### 排他制御

* DB/state/shared resource はロック必須
* 衝突時は skip または queue

---

### 実装前チェック（必須記載）

以下を必ず実装前に提示すること：

* 対象件数（概算）
* 想定read回数
* 想定write回数
* 外部I/O回数（1件あたり）
* バッチサイズ
* フルスキャンの有無（ある場合は理由）
* 差分更新不可の理由（ある場合のみ）
* 同時実行時の排他方法

---

### レビュー観点（レビュアー用）

* N+1 I/Oになっていないか
* where/inで絞れるのに全件取得していないか
* 書き込みがbatch化されているか
* ログ出力が過剰でないか
* 同時実行時に競合しないか
* 差分更新にできない理由は妥当か

---

### 例外規定（重要）

以下の場合のみ例外を許可する：

* データ量が100件未満
* 単発メンテナンススクリプト（本番定期実行されない）
* 検証・デバッグ用途（明示フラグ付き）

※例外使用時はコメントで理由を明記すること

---

### 違反時の影響

本ルール違反は以下を引き起こす：

* Supabase timeout / rate limit
* 処理停止・長時間ハング
* 重複書き込み・データ不整合
* 開発環境の不安定化

重大違反として扱う

---

## ティッカー正規化（5桁→4桁）に関する安全ルール

### 目的

5桁の証券コード（主にJ-Quants由来）を正規の4桁コードへ統一し、データ整合性を保つ。

---

### 正規化ルール

* 5桁 → 4桁の変換は以下の条件を**すべて満たす場合のみ許可**する

  * 文字列長がちょうど5
  * 数字のみで構成されている
  * 末尾が「0」である

* 上記条件を満たさない場合は**一切変換しない（スキップ）**

  * 英字混在
  * 記号混入
  * 末尾が0でない5桁
  * 先頭ゼロなど不正フォーマット

---

### スコープ制限

* 原則として、**J-Quants由来データのみ対象**
* 他ソース（TDnet / EDINET / PDF / HTML 等）は対象外とする
  ※明示的な承認がある場合のみ例外的に適用可能

---

### dry-run 必須要件

apply 実行前に、必ず以下を出力すること：

* 対象ティッカー数（target tickers）
* 取得行数（fetched rows）
* 変換可能件数（convertible）
* スキップ件数（skipped_invalid）
* 衝突件数（collisions）
* 更新対象件数（updatable）
* 処理時間

さらに以下の内訳を必須とする：

* source別件数
* テーブル別件数
* 衝突の内訳

  * identical（完全一致）
  * conflicting（値不一致）
  * blocked（更新不可）

---

### 衝突（collision）の取り扱い

* **conflicting（値不一致）は絶対に自動上書き禁止**
* identical のみ自動処理許可
* conflicting はログ出力のみ（手動確認対象）

---

### 更新処理ルール

* 更新は必ずバッチ処理で実施する
* insert → delete の順序を厳守する
* 処理は冪等（idempotent）であること

---

### 安全制御

apply 実行には以下の制御を必須とする：

* 件数制限オプション

  * `--limit-updates`
* ソース制限

  * `--source`
* ティッカー指定

  * `--tickers`

---

### 適用前チェック

apply 実行前に以下を必ず確認する：

* 変換条件が厳密に実装されていること
* skipped_invalid が0である場合、その妥当性を確認する
* conflicting collision が存在しない、または内容を確認済みであること

---

### 適用後検証

apply 実行後は必ず以下を確認する：

* 5桁ティッカーの残存件数
* 4桁ティッカーの重複件数
* unique key の競合有無
* サンプルデータの整合性

---

### 禁止事項

* 全件スキャンによる処理
* 行単位での逐次HTTPリクエスト
* フィルタ未検証のまま apply 実行
* conflicting データの自動上書き

---

### 補足

* 本処理は「補正」ではなく「データ移行」に近いため、慎重に扱うこと
* 大量更新（10万行以上）の場合は段階的適用を推奨

---

# Gravity運用ルール（最適化版・事故防止バランス型）

## 目的
トークン消費を最小化しつつ、TDnet / OCR / canonical pipeline の実装成功率を維持する。
過剰な探索・読込・修正・出力を防ぎ、局所的かつ安全な変更のみを行う。

---

## 最上位原則
- 最小読込・最小変更・最小出力を徹底する
- 必要な範囲だけ理解する（過剰な全体理解は禁止）
- 推測で探索範囲を広げない
- まず局所修正で解決を試みる
- 不足時のみ段階的に参照範囲を拡張する

---

## 読込ルール
- 最初に読むファイルは1つを基本とする
- 必要な場合のみ追加参照を許可する
- 参照拡大は段階的に行う（いきなり広げない）
- 無関係なファイルは読まない
- docs/specs の全読込は禁止（必要箇所のみ）
- 過去履歴の全面再読は禁止（必要部分のみ）
- 「関連しそう」だけで読むことを禁止

---

## 参照拡張ルール（重要）
- 初回は対象ファイルのみで解決を試みる
- 解決不能な場合のみ依存先を追加参照する
- 通常タスクでは最大3〜5ファイルまでを目安とする
- それ以上必要な場合は「なぜ必要か」を簡潔に整理してから最小範囲で実施
- OCR / canonical / ETL 系は依存関係が多いため、必要に応じて例外的に拡張を許可
- リポジトリ全体スキャンは禁止

---

## 修正ルール
- 修正は局所差分を基本とする
- 無関係な変更は禁止
- リファクタリングは禁止（指示がある場合のみ許可）
- 命名変更・import整理は禁止
- 「ついで修正」禁止
- 「将来のための修正」禁止
- まず最小差分で対応し、不足時のみ追加修正

---

## 出力ルール
- 差分中心で出力する
- fullコードの再出力は禁止（必要時のみ例外）
- 長文解説は禁止
- 出力は簡潔にする
- 以下の形式を基本とする：

読んだファイル:
追加参照:
変更ファイル:
変更概要:
差分:

---

## 仕様書ルール
- 仕様書は必要箇所のみ参照
- indexを理由に全章読まない
- 一度確定した仕様は再読しない
- 差分仕様があればそれを優先
- 仕様書が存在しても全読込しない

---

## ログ・エラールール
- ログは問題箇所のみ扱う
- 全ログ読込は禁止
- エラー行・スタックトレース周辺のみ確認
- 必要に応じて段階的に範囲拡張
- 原因候補の無制限列挙は禁止

---

## 実装計画ルール
- 大規模な計画は作らない
- まず最小修正案を提示
- 複数案は最大3案まで
- 軽微な修正で設計議論を始めない

---

## 禁止事項
- リポジトリ全体の探索
- specsの全読込
- 無関係な横断検索
- 無断のファイル追加
- 無断の設計変更
- 無断の最適化
- 無断のリファクタリング
- 無断の命名変更
- 無断の広範囲修正
- 同一情報の再読込
- 不要な長文出力

---

## 推奨手順
1. 指示文だけで解決可能か確認
2. 必要なら対象ファイルを1つ読む
3. そのファイル内で解決を試みる
4. 不足時のみ依存ファイルを1つ追加
5. 最小差分で修正
6. 差分のみ出力

---

## 優先順位
- トークン節約よりも「正しく動くこと」を優先する
- ただし過剰な探索は禁止
- 迷った場合は「小さく直す」を優先

---

## 例外ルール（重要）
以下の場合は参照拡張を許可する：

- OCR抽出ロジック修正
- canonical統合処理
- データ整合性バグ
- 複数ファイル連携バグ

ただし最小範囲で実施すること

---

## 最終原則
- 読みすぎない
- 直しすぎない
- 書きすぎない
- ただし壊さない

以上をGravityの常設ルールとする。

---

# Gravity運用モード切替ルール

## 基本方針
通常は「トークン節約モード」で動作する。
ただし、局所修正で解決できない場合のみ、一時的に「フル探索モード」へ切り替える。
フル探索モードは常用しない。

---

## 1. 通常モード：トークン節約モード
### 目的
- トークン消費を抑える
- 不要な探索を防ぐ
- 最小差分で安全に直す

### ルール
- 最初に読むファイルは1つを基本とする
- 必要な場合のみ追加参照する
- 追加参照は段階的に行う
- 無関係なファイルは読まない
- specs/docs の全読込は禁止
- リポジトリ全体探索は禁止
- 修正は局所差分のみ
- 無関係な修正、ついで修正、リファクタリングは禁止
- 出力は差分中心、簡潔にする

### 向いているタスク
- 単純なバグ修正
- 条件分岐修正
- OCRの小規模改善
- ログ1件ベースの修正
- 軽微な表示・判定修正

---

## 2. 切替条件
以下のいずれかに該当した場合のみ、フル探索モードへの切替を許可する。

- 対象ファイル単体では原因が説明できない
- 依存関係の不整合が疑われる
- 修正しても同じエラーが再発する
- データフローや呼び出し元の確認が必須
- OCR / canonical / ETL の複数ファイル連携が原因候補
- DBスキーマ、API、ビュー、同期処理の整合確認が必要
- 局所修正を2回試しても解決しない

---

## 3. 一時モード：フル探索モード
### 目的
- 根本原因の特定
- 複数ファイル連携の確認
- 設計や依存関係の把握

### ルール
- 必要な範囲に限定して探索する
- 全リポジトリ無差別探索は禁止
- 読む理由を説明できるファイルだけ読む
- 原因特定後は通常モードへ戻る
- フル探索モードのまま実装を広げない
- 原因調査と無関係な修正は禁止
- 出力では「なぜ拡張したか」を簡潔に示す

### 向いているタスク
- OCR抽出不良の根本調査
- canonical不整合
- ETL連携バグ
- period/ticker/sourceの食い違い
- DB/view/APIの接続不良
- 複数段パイプラインの整合確認

---

## 4. フル探索モードの終了条件
以下を満たしたら、必ず通常モードへ戻る。

- 原因候補が絞れた
- 修正対象ファイルが特定できた
- 追加参照なしで実装に入れる
- 横断調査が不要になった

---

## 5. 回答フォーマット
通常モードでもフル探索モードでも、以下を簡潔に出力する。

- 動作モード:
- 読んだファイル:
- 追加参照した理由:
- 変更ファイル:
- 変更概要:
- 差分:

---

## 6. 重要な制約
- 迷ったら通常モードを優先する
- フル探索モードは調査専用の例外措置とする
- フル探索モードをデフォルト動作にしない
- 原因特定後は必ず最小差分修正に戻る

---

## 7. 最終原則
- 普段は狭く読む
- 詰まった時だけ広げる
- 原因が分かったらまた狭く戻す

---

# Known Gotchas / Monitoring Pitfalls

## segment_financials_clean ビューの対象範囲

### 事実

`segment_financials_clean` ビューは以下の定義を持つ:

```sql
CREATE VIEW segment_financials_clean AS
    SELECT * FROM segment_financials
    WHERE data_source = 'tdnet'
      AND segment_name IS NOT NULL
      AND segment_name != ''
      AND (segment_sales IS NOT NULL OR segment_profit IS NOT NULL)
```

### 問題

V4 backfill（`backfill_segments_tdnet.py`）が XBRL 経由で抽出したセグメントは
`data_source = 'backfill_xbrl'` として書き込まれる。

このため、V4 backfill が正常完了しても `segment_financials_clean` には **一切反映されない**。

### 影響範囲

| 処理 | 参照元 | 影響 |
|------|--------|------|
| `process_batch` の canonical sync | `segment_financials`（全件） | **影響なし** — backfill_xbrl も対象に含まれる |
| Supabase `canonical_segments` 反映 | `segment_financials`（全件） | **影響なし** |
| 監視・検証 SQL で `segment_financials_clean` を使う場合 | `segment_financials_clean` | **⚠️ 要注意** — backfill_xbrl 成功分が0件に見える |

### ✅ 正しい監視 SQL

```sql
-- backfill_xbrl を含む全セグメントの確認
SELECT company_code, fiscal_year_end, quarter, data_source, COUNT(*) as cnt
FROM segment_financials
WHERE company_code IN ('4674', '7987')
GROUP BY company_code, fiscal_year_end, quarter, data_source
ORDER BY fiscal_year_end DESC;
```

### ❌ 誤った監視 SQL（backfill_xbrl を見落とす）

```sql
-- segment_financials_clean は data_source='tdnet' のみ → backfill_xbrl が見えない
SELECT * FROM segment_financials_clean WHERE company_code IN ('4674', '7987');
```

### 発生日時・経緯

* 2026-05-08: `run_backfill_segments_v4_recent.bat` が `--date-to` 未指定により1年分 listing を試みてクラッシュ
* 4674（クレスコ）/ 7987（ナカバヤシ）の FY2026-03-31 セグメントが未抽出
* 翌日に手動再処理（`--date-from 2026-05-08 --date-to 2026-05-08 --tickers 4674,7987`）で復旧
* `segment_financials_clean` が 0件にもかかわらず Supabase への反映は成功していたため、ビューの定義が実際の push 経路と一致していないことが判明

### 対処方針

* 監視・検証スクリプトでは `segment_financials_clean` ではなく `segment_financials` を直接参照する
* `data_source` 列でフィルタする場合は `IN ('tdnet', 'backfill_xbrl')` を明示する
* `segment_financials_clean` ビューは legacy 互換のため維持するが、新規スクリプトでの使用を禁止する

---

# Segment Source Priority Rules

## canonical_segments source_priority

| source | priority |
|--------|----------|
| backfill_v4_pdf | 0 |
| edinet_xbrl | 0（既存ロジックに準拠） |
| xbrl / backfill_xbrl | 1 |
| excel_legacy | 5 以上 |

### 重要

`backfill_v4_pdf` は viewer 側で `xbrl:Other only` に負けてはならない。

### 禁止

* `backfill_v4_pdf` を default priority（5以上）に落とすこと
* `sync_segments.py` で `source="excel_legacy"` を固定ハードコードすること

---

## sync_segments.py source passthrough

SQLite `segment_financials.data_source` を `canonical_segments.source` へそのまま引き継ぐこと。

`data_source='backfill_v4_pdf'` の行は Supabase 側でも `source='backfill_v4_pdf'` になること。

---

# Partial XBRL Other Cleanup

## 条件

同一 `ticker / period / quarter` に `backfill_v4_pdf` の有効セグメントが **2件以上** 存在する場合、以下は cleanup 対象とする。

* `source IN ('xbrl', 'backfill_xbrl')`
* `segment_key = 'other'` または `segment_name = 'Other'`

## 目的

partial XBRL の Other only が viewer で優先表示される事故を防ぐ。

### 禁止

* `backfill_v4_pdf` が1件以下のケースへの適用
* `edinet_xbrl` の削除

---

# VertRescue Safety Rules

`_try_vertical_format_rescue()` は **Phase2 PASSED page のみ** を対象とする。

Phase2 で reject されたページは VertRescue の再スキャン対象から除外すること（`passed_pages` 引数で管理）。

## 理由

株主資本等変動計算書ページが「利益」「株主資本等」キーワードに反応してセグメント表として誤採用される事故を防ぐ。

## 禁止

Phase2 reject 済みページの VertRescue 再スキャン。

---

# Equity Statement Exclude Rules

以下を含むページは `_is_equity_statement_page()` によりセグメント候補から除外する。

| 判定種別 | キーワード |
|--------|----------|
| 単独一致で即除外 | 株主資本等変動計算書 |
| 複数（3語以上）で除外 | 当期首残高、当期末残高、当期変動額、剰余金の配当、自己株式、株主資本、資本剰余金、利益剰余金、新株予約権、非支配株主持分、その他の包括利益累計額 |

## 目的

連結株主資本等変動計算書ページを縦型セグメント表として誤抽出することを防ぐ。

---

# Profit Row Keyword Matching

`_phase6_find_profit_row()` でのキーワード照合は、**改行除去後の文字列**に対して行うこと。

```python
left_cell_norm = left_cell.replace("\n", "")
if kw in left_cell_norm:  # 改行除去後で判定
```

## 理由

pdfplumber が `'セグメント利\n益又は損失\n（△）'` のように改行入りセルを返すため、`'利益'` 等のキーワードが部分一致しない。

---

# Cleanup Tool: cleanup_bad_segment_names.py

## ファイル

`tools/cleanup_bad_segment_names.py`

## 用途

過去に保存済みの株主資本等変動計算書由来ゴミセグメントを `canonical_segments` から一括削除する。

## 対象 source

* `backfill_v4_pdf`
* `excel_legacy`

## 削除対象キーワード

`segment_name` に以下を含む行が削除対象。

当期首残高、当期末残高、当期変動額、当期変動額合計、剰余金の配当、自己株式、株主資本、資本剰余金、利益剰余金、新株予約権、非支配株主持分、その他の包括利益累計額、親会社株主に帰属する当期純利益

## 運用ルール

必ず `--dry-run` → `--apply` の順で実行すること。

```bash
python tools/cleanup_bad_segment_names.py --dry-run
python tools/cleanup_bad_segment_names.py --apply --yes
- 普段は狭く読む
- 詰まった時だけ広げる
- 原因が分かったらまた狭く戻す

---

# Known Gotchas / Monitoring Pitfalls

## segment_financials_clean ビューの対象範囲

### 事実

`segment_financials_clean` ビューは以下の定義を持つ:

```sql
CREATE VIEW segment_financials_clean AS
    SELECT * FROM segment_financials
    WHERE data_source = 'tdnet'
      AND segment_name IS NOT NULL
      AND segment_name != ''
      AND (segment_sales IS NOT NULL OR segment_profit IS NOT NULL)
```

### 問題

V4 backfill（`backfill_segments_tdnet.py`）が XBRL 経由で抽出したセグメントは
`data_source = 'backfill_xbrl'` として書き込まれる。

このため、V4 backfill が正常完了しても `segment_financials_clean` には **一切反映されない**。

### 影響範囲

| 処理 | 参照元 | 影響 |
|------|--------|------|
| `process_batch` の canonical sync | `segment_financials`（全件） | **影響なし** — backfill_xbrl も対象に含まれる |
| Supabase `canonical_segments` 反映 | `segment_financials`（全件） | **影響なし** |
| 監視・検証 SQL で `segment_financials_clean` を使う場合 | `segment_financials_clean` | **⚠️ 要注意** — backfill_xbrl 成功分が0件に見える |

### ✅ 正しい監視 SQL

```sql
-- backfill_xbrl を含む全セグメントの確認
SELECT company_code, fiscal_year_end, quarter, data_source, COUNT(*) as cnt
FROM segment_financials
WHERE company_code IN ('4674', '7987')
GROUP BY company_code, fiscal_year_end, quarter, data_source
ORDER BY fiscal_year_end DESC;
```

### ❌ 誤った監視 SQL（backfill_xbrl を見落とす）

```sql
-- segment_financials_clean は data_source='tdnet' のみ → backfill_xbrl が見えない
SELECT * FROM segment_financials_clean WHERE company_code IN ('4674', '7987');
```

### 発生日時・経緯

* 2026-05-08: `run_backfill_segments_v4_recent.bat` が `--date-to` 未指定により1年分 listing を試みてクラッシュ
* 4674（クレスコ）/ 7987（ナカバヤシ）の FY2026-03-31 セグメントが未抽出
* 翌日に手動再処理（`--date-from 2026-05-08 --date-to 2026-05-08 --tickers 4674,7987`）で復旧
* `segment_financials_clean` が 0件にもかかわらず Supabase への反映は成功していたため、ビューの定義が実際の push 経路と一致していないことが判明

### 対処方針

* 監視・検証スクリプトでは `segment_financials_clean` ではなく `segment_financials` を直接参照する
* `data_source` 列でフィルタする場合は `IN ('tdnet', 'backfill_xbrl')` を明示する
* `segment_financials_clean` ビューは legacy 互換のため維持するが、新規スクリプトでの使用を禁止する

---

# Segment Source Priority Rules

## canonical_segments source_priority

| source | priority |
|--------|----------|
| backfill_v4_pdf | 0 |
| edinet_xbrl | 0（既存ロジックに準拠） |
| xbrl / backfill_xbrl | 1 |
| excel_legacy | 5 以上 |

### 重要

`backfill_v4_pdf` は viewer 側で `xbrl:Other only` に負けてはならない。

### 禁止

* `backfill_v4_pdf` を default priority（5以上）に落とすこと
* `sync_segments.py` で `source="excel_legacy"` を固定ハードコードすること

---

## sync_segments.py source passthrough

SQLite `segment_financials.data_source` を `canonical_segments.source` へそのまま引き継ぐこと。

`data_source='backfill_v4_pdf'` の行は Supabase 側でも `source='backfill_v4_pdf'` になること。

---

# Partial XBRL Other Cleanup

## 条件

同一 `ticker / period / quarter` に `backfill_v4_pdf` の有効セグメントが **2件以上** 存在する場合、以下は cleanup 対象とする。

* `source IN ('xbrl', 'backfill_xbrl')`
* `segment_key = 'other'` または `segment_name = 'Other'`

## 目的

partial XBRL の Other only が viewer で優先表示される事故を防ぐ。

### 禁止

* `backfill_v4_pdf` が1件以下のケースへの適用
* `edinet_xbrl` の削除

---

# VertRescue Safety Rules

`_try_vertical_format_rescue()` は **Phase2 PASSED page のみ** を対象とする。

Phase2 で reject されたページは VertRescue の再スキャン対象から除外すること（`passed_pages` 引数で管理）。

## 理由

株主資本等変動計算書ページが「利益」「株主資本等」キーワードに反応してセグメント表として誤採用される事故を防ぐ。

## 禁止

Phase2 reject 済みページの VertRescue 再スキャン。

---

# Equity Statement Exclude Rules

以下を含むページは `_is_equity_statement_page()` によりセグメント候補から除外する。

| 判定種別 | キーワード |
|--------|----------|
| 単独一致で即除外 | 株主資本等変動計算書 |
| 複数（3語以上）で除外 | 当期首残高、当期末残高、当期変動額、剰余金の配当、自己株式、株主資本、資本剰余金、利益剰余金、新株予約権、非支配株主持分、その他の包括利益累計額 |

## 目的

連結株主資本等変動計算書ページを縦型セグメント表として誤抽出することを防ぐ。

---

# Profit Row Keyword Matching

`_phase6_find_profit_row()` でのキーワード照合は、**改行除去後の文字列**に対して行うこと。

```python
left_cell_norm = left_cell.replace("\n", "")
if kw in left_cell_norm:  # 改行除去後で判定
```

## 理由

pdfplumber が `'セグメント利\n益又は損失\n（△）'` のように改行入りセルを返すため、`'利益'` 等のキーワードが部分一致しない。

---

# Cleanup Tool: cleanup_bad_segment_names.py

## ファイル

`tools/cleanup_bad_segment_names.py`

## 用途

過去に保存済みの株主資本等変動計算書由来ゴミセグメントを `canonical_segments` から一括削除する。

## 対象 source

* `backfill_v4_pdf`
* `excel_legacy`

## 削除対象キーワード

`segment_name` に以下を含む行が削除対象。

当期首残高、当期末残高、当期変動額、当期変動額合計、剰余金の配当、自己株式、株主資本、資本剰余金、利益剰余金、新株予約権、非支配株主持分、その他の包括利益累計額、親会社株主に帰属する当期純利益

## 運用ルール

必ず `--dry-run` → `--apply` の順で実行すること。

```bash
python tools/cleanup_bad_segment_names.py --dry-run
python tools/cleanup_bad_segment_names.py --apply --yes
```

## 禁止

* `--apply` を dry-run なしで実行すること
* `edinet_xbrl` / `xbrl` / `backfill_xbrl` の削除
* 正規セグメント（事業セグメント名）の削除

---

# UI / Frontend Modification Rules

【重要】

AlertsPage.tsx を含む通知サイトの修正を行う場合、
作業開始前に必ず以下を報告すること。

1. 修正対象GitHubリポジトリ名
2. 修正対象Vercelプロジェクト名
3. Production URL
4. 修正対象画面のURL
5. 修正対象ファイルのフルパス

特に company viewer と notification site を混同しないこと。

修正完了報告時は必ず以下を添付すること。

- Commit hash
- GitHub push完了確認
- Vercel Production Ready確認
- 修正対象URL
- 修正対象画面のスクリーンショットまたは確認方法

「ローカルで修正した」「tscが通った」は完了条件にならない。

完了条件は
『ユーザーが実際に見ている本番画面で変更が確認できること』
である。
