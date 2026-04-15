# tdnet-excel-input データ階層設計・責務整理仕様書

本文書はシステム全体のデータ階層・責務・保存方針・正本定義を定める中心文書である。

## 1. システム全体像

```
[TDnet / J-Quants API / XBRL / PDF / HTML]
         │ fetch / download
         ▼
┌───────────────────────┐
│     Raw Layer         │  data/docs/, data/xbrl_archive/
│  原本ファイル保管      │  jquants.db (raw responses)
└──────────┬────────────┘
           │ extract / parse / classify
           ▼
┌───────────────────────┐
│   Normalized Layer    │  decision_db.db (quarterly_results, segment_financials)
│  意味解釈済み・根拠付き │  quarantine.db, xbrl.db
└──────────┬────────────┘
           │ sync / push / rebuild
           ▼
┌───────────────────────┐
│    Serving Layer      │  Supabase (financials, segment_financials, company_memos)
│  viewer / API 向け     │  Excel export (data.xlsx, data_jquants.xlsx)
└──────────┬────────────┘
           │ (将来)
           ▼
┌───────────────────────┐
│  Search / Index Layer │  未実装。全文検索・embeddings
│  検索専用・本体DB非汚染 │  本体数値DBとは分離する
└───────────────────────┘
```

## 2. 4層構造

### 2.1 Raw Layer — 証拠保管

**責務**: 再取得・再解析の基準となる元データの保管。「証拠」の層。

| 対象 | 保管場所 |
|:---|:---|
| TDnet 取得 PDF / HTML | `data/docs/` |
| XBRL ZIP アーカイブ | `data/xbrl_archive/` |
| J-Quants API 生レスポンス | `jquants.db` → `jquants_financials_normalized` |
| 取得メタデータ (doc_id, disclosed_date) | `state.db` → `processing_log` |

**原則**:
- 加工済みの意味付けを混ぜない
- 何でも全部永遠に保存しない（保持期間を定める）
- Raw は「正本の1つ」だが、意味解釈済み数値の正本ではない

### 2.2 Normalized Layer — 意味解釈

**責務**: Raw から抽出した情報に意味づけを行い、根拠つきで保持。**最も重要な層**。

| 対象 | 保管場所 |
|:---|:---|
| PL 実績数値 (sales, gross_profit, operating_profit) | `decision_db.db` → `quarterly_results` (91K行) |
| セグメント数値 | `decision_db.db` → `segment_financials` (94K行) |
| quarantine (抽出失敗・要確認) | `quarantine.db` (769行) |
| XBRL fact 解析結果 | `xbrl.db` → `facts`, `guidance` (schema.sql) |
| buyback 抽出結果 | `data/decision_db.db` → `buyback_events` |
| Filing diff | `decision_db.db` → `filing_diff_summaries` (40行) |

**原則**:
- 「なぜその数値になったのか」を追跡可能にする
- field_sources 等で抽出ソース (summary_xbrl, attachment_xbrl, pdf, html) を記録
- 全文そのものを何重にも持たない
- canonical 化の根拠を残す

### 2.3 Serving Layer — 表示・利用

**責務**: viewer / API / Excel / alert が直接読む提供層。

| 対象 | 保管場所 |
|:---|:---|
| 表示用 financials | Supabase `financials` |
| 表示用 segment | Supabase `segment_financials` |
| メモ | Supabase `company_memos`、`decision_db.db` → `company_memos` |
| Excel export | `data/data.xlsx`, `data/data_jquants.xlsx` |
| viewer Parquet | `build_viewer_pq.py` 出力 |

**原則**:
- 使いやすさ優先で整形
- Raw や Normalized の細かい証跡を混ぜない
- 「派生正本」にはなるが、解釈の一次正本ではない

### 2.4 Search / Index Layer — 検索専用（未実装）

**責務**: 全文検索・embedding・ベクトル検索のための層。

**原則**:
- **本体数値DBに混ぜない**
- 必要になってから作る — 現時点では未実装
- `document_text`, `search_tokens`, `embeddings` 等は専用テーブル/専用DBに分離
- 「検索用に便利」を理由に Serving に全文を押し込まない

## 3. 現行 DB・テーブル一覧

| DB | サイズ | 層 | 主テーブル | 行数 |
|:---|---:|:---|:---|---:|
| `decision_db.db` | 30.9 MB | Normalized/Serving | `quarterly_results` | 91,652 |
| | | | `segment_financials` | 94,063 |
| | | | `company_memos` | 1,139 |
| | | | `quarterly_notes` | 19,572 |
| | | | `filing_diff_summaries` | 40 |
| | | | `audit_log` | 239 |
| `jquants.db` | 240.7 MB | Raw | `jquants_financials_normalized` | 87,157 |
| `quarantine.db` | 0.2 MB | Normalized | `quarantine` | 769 |
| `xbrl.db` | 0.1 MB | Normalized | schema.sql ベース (少量テスト) | ~50 |
| `state.db` | 0.05 MB | Raw (meta) | `processing_log` | 112 |
| `data/decision_db.db` | 0.02 MB | Normalized | `buyback_events` | 0 |

## 4. 永続保存するもの / しないもの

### 永続保存

- 取得済み Raw ファイル (PDF, XBRL ZIP)
- canonical 数値 (quarterly_results, segment_financials)
- company_memos
- quarantine 理由
- buyback_events (確定済み)
- processing_log (取得履歴)
- J-Quants 正規化済みデータ

### 破棄または短期保持

- 展開途中の一時 XML
- 一時 CSV / JSONL (artifacts/ 配下)
- OCR 中間画像
- パース途中の冗長な JSON
- ad hoc debug dump
- viewer cache
- 再生成可能な候補一覧 (buyback candidate 中間)

## 5. Source of Truth 定義

詳細は [source_of_truth.md](source_of_truth.md) を参照。

| 対象 | 正本 | 派生物 |
|:---|:---|:---|
| 生ファイル | Raw archive | — |
| PL 数値 | `quarterly_results` + field_sources | Supabase financials, Excel |
| セグメント | `segment_financials` | Supabase segment_financials |
| メモ | `company_memos` | viewer 表示 |
| 通知 | — | Discord alert (派生) |
| Excel | — | export (派生) |

## 6. 新機能追加時の判断ルール

新しい要件が出たら以下のフローで判断する:

```
1. 最終的に何を出したい？
   │
2. それは Raw / Normalized / Serving / Search のどの層？
   │
3. 正本か派生物か？
   │
4. 再生成可能か？ 永続保存が必要か？
   │
5. 既存テーブルに足すべきか、別テーブルに分けるべきか？
   │
6. 全文や大量テキストが絡む → Search 層へ分離
   │
7. 同じ意味の列やテーブルが二重化しないか？
   │
8. source / period / quarter の表現揺れが発生しないか？
```

### 判断例

| 要件 | NG | OK |
|:---|:---|:---|
| PDF本文を後で検索したい | financials に本文列追加 | Search 層の document_text へ分離 |
| buyback 候補のスコア根拠 | events に巨大ログ追加 | Normalized で score breakdown |
| viewer 向け四半期PL | 直接手修正 | Serving に整形, 根拠は Normalized |

## 7. 命名・期間・分類の統一ルール

### ticker
- 4桁文字列 `"2301"` を標準とする
- `local_code` は5桁 `"23010"` (J-Quants)、必要時に変換

### period / quarter
- `fiscal_year_end`: `"2024-10-31"` (日付文字列)
- `quarter`: `"1Q"`, `"2Q"`, `"3Q"`, `"FY"` (文字列)
- 累計 (cumulative) vs 単Q (discrete) は明示的に区別する
- `type_of_current_period` (J-Quants) → `quarter` に正規化

### source
- `summary_xbrl`, `attachment_xbrl`, `pdf`, `html`, `manual`, `j_quants`
- 表現を増やす場合は data_architecture.md を更新すること

### タイムスタンプ
- `disclosed_date`: 開示日
- `fetched_at`: 取得日時
- `created_at` / `updated_at`: レコード管理用

## 8. 将来分離候補

以下は現時点では本体に含まれてよいが、規模拡大時に別モジュール / 別 DB / 別スキーマへ分離しうる。

| 候補 | 分離先 | 基準 |
|:---|:---|:---|
| 全文検索 | Search 層 DB | 全文保存量が 1GB 超 |
| embedding / vector search | vector DB (pgvector等) | 検索要件確定後 |
| OCR / PDF 解析補助 | 専用テーブル | OCR 本格導入時 |
| Discord 通知生成 | alert microservice | 通知頻度 100件/日超 |
| AI 差分要約 | analysis DB | 要約結果の永続化要件時 |
| company memo app | 専用 memo DB | memo 10K件超 |
| Excel 連携アップロード | ETL 専用 pipeline | 双方向 sync 要件時 |

## 9. アンチパターン集

> [!CAUTION]
> 以下は禁止事項。いずれも過去に発生した問題に基づく。

1. **「とりあえず全部保存」** — 保存コスト・管理コストが見えなくなる
2. **viewer 表示都合で normalized 根拠を消す** — 数値の追跡性を失う
3. **Serving テーブルに全文本文を載せる** — DB肥大化の主因
4. **派生物を正本扱いする** — Excel export や Discord 通知を修正元にしない
5. **source の意味が曖昧なまま列追加** — summary_xbrl と xbrl が混在するとデバッグ不能
6. **period 表現を場当たりで増やす** — "1Q", "Q1", "第1四半期" の混在
7. **手修正した export を正本へ戻す** — 手動→自動の逆流は事故の元
8. **似たテーブルを用途不明で増殖させる** — 同じPLが3箇所に存在等
9. **debug 用 dump を長期保存する** — artifacts/ の一時出力が永遠に残る
10. **"検索したいかも" だけで全文常時保存する** — Search 層分離で対応
