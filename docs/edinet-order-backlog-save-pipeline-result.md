# edinet_order_data — 保存パイプライン実行結果

> **記録日**: 2026-06-15  
> **リポジトリ**: `tdnet-excel-input`  
> **DB変更**: あり（本記録作成時点では追加変更なし）  
> **デプロイ**: なし  
> **Viewer連携**: なし

---

## 1. 実施概要

`scratch/extract_edinet_orders.py` のロジックを正式モジュール化し、
`edinet_order_data` テーブルへの保存パイプラインを構築・実行した。

---

## 2. 作成ファイル

| ファイル | 役割 |
|---|---|
| `src/edinet_orders/__init__.py` | パッケージ exports |
| `src/edinet_orders/extractor.py` | 抽出ロジック（`extract_from_company` / `extract`） |
| `src/edinet_orders/transformer.py` | DB形式変換（単位変換・source_unit・null_reason付与） |
| `src/edinet_orders/saver.py` | UPSERT（GET+PATCH/POST via requests） |
| `run_edinet_orders.py` | エントリーポイント |

### モジュール設計

```
extract()        ← EDINET XBRLキャッシュから受注データを抽出
    ↓
transform_to_db_row()  ← 単位変換・カラムマッピング
    ↓
save_to_db()     ← PostgREST GET+PATCH/POST で UPSERT
```

### extract() / save_to_db() の分離

```bash
# 抽出 + DB保存（全社）
python run_edinet_orders.py

# DRY RUN（DB保存なし）
python run_edinet_orders.py --dry-run

# 既存JSONから保存のみ（再抽出しない）
python run_edinet_orders.py --from-json scratch/orders_extracted_30_v4.json

# 特定銘柄のみ
python run_edinet_orders.py --tickers 1812 6141
```

---

## 3. 実行結果（2026-06-15）

### 実行コマンド

```bash
python run_edinet_orders.py \
  --from-json C:\Users\takuy\OneDrive\tdnet-excel-input\scratch\orders_extracted_30_v4.json
```

### 結果サマリー

| 項目 | 結果 |
|---|---|
| 対象企業数 | 31社 |
| 総レコード数 | 31件 |
| INSERT件数 | 0件（5社テスト時に先行INSERT済み） |
| UPDATE件数 | **31件** |
| エラー件数 | 0件 |
| スキップ件数 | 0件（period=NULL 件数） |

---

## 4. DB保存確認（31件）

### confidence 内訳

| confidence | 件数 |
|---|---|
| `high` | 25件 |
| `medium` | 3件 |
| `low` | 3件（サンコール・ツガミ・SCREEN HD） |

### source_unit 内訳

| source_unit | 件数 |
|---|---|
| `million_yen` | 21件 |
| `thousand_yen` | 6件 |
| `billion_yen` | 1件（IHI） |
| `unknown` | 3件（low 銘柄） |

### period 内訳（YYYY-MM-DD）

| period | 件数 |
|---|---|
| `2025-03-31` | 25件 |
| `2025-12-31` | 2件（DMG森精機・タツモ） |
| `2025-09-30` | 2件（TVE・岡野バルブ） |
| `2025-02-28` | 1件（ローツェ） |
| `2024-12-31` | 1件（富士ソフト・fiscal_year=2024） |

---

## 5. AFTER SELECT サンプル5件（DB確認済み）

| ticker | company | period | fiscal_year | segment_name_key | source_unit | orders_received | raw_orders_received | confidence |
|---|---|---|---|---|---|---|---|---|
| 1762 | 高松コンストラクション | 2025-03-31 | 2025 | `__ALL__` | million_yen | 99,008 | 99,008 | medium |
| 1802 | 大林組 | 2025-03-31 | 2025 | `__ALL__` | million_yen | 2,044,406 | 2,044,406 | high |
| 1812 | 鹿島建設 | 2025-03-31 | 2025 | `__ALL__` | million_yen | 1,773,567 | 1,773,567 | high |
| 1952 | 新日本空調 | 2025-03-31 | 2025 | `__ALL__` | million_yen | 153,891 | 153,891 | high |
| 1969 | 高砂熱学工業 | 2025-03-31 | 2025 | `__ALL__` | million_yen | 307,974 | 307,974 | high |

---

## 6. 各確認項目

### created_at / updated_at

- `created_at`: 2026-06-15 付近（5社テスト INSERT 時 or 本実行 UPDATE 時）
- `updated_at`: 本実行（2026-06-15）で全31件が `now()` に更新されていることを確認
- trigger `update_updated_at()` が正常動作

### generated column `segment_name_key`

- AFTER SELECT で `segment_name_key = '__ALL__'` が全31件で確認済み
- `segment_name = NULL` のとき `COALESCE(segment_name, '__ALL__') = '__ALL__'` が正常に動作
- INSERT 時に `segment_name_key` を含めなかったことで generated column が自動生成された

### RPO の分離

- SWCC（5805）: `rpo = 1,997` / `order_backlog = NULL` — RPO が `order_backlog` に混入していないことを確認
- 東京エレクトロン（8035）: `rpo = 225,019` / `order_backlog = NULL` — 同上
- `rpo` カラムと `order_backlog` カラムが明確に分離されていることを確認

### raw_* 保存

- 精工技研（6834）: `raw_orders_received = 21,380,102`（千円）/ `orders_received = 21,380`（百万円変換後）
- TOWA（6315）: `raw_orders_received = 47,429,464`（千円）/ `orders_received = 47,429`
- IHI（7013）: `raw_orders_received = 17,511`（億円）/ `orders_received = 1,751,100`
- 原単位の値が `raw_*` カラムに保持されていることを確認

### UPSERT 方式の詳細

supabase-py v2 の `.upsert()` は generated column (`segment_name_key`) を含む制約
`edinet_order_data_uniq` では動作しない（`42P10: no unique constraint matching`）。

解決策として **GET + PATCH/POST** 方式を採用：

```python
# 存在確認
GET /rest/v1/edinet_order_data?ticker=eq.1812&period=eq.2025-03-31&...
# 存在すれば UPDATE
PATCH /rest/v1/edinet_order_data?ticker=eq.1812&...
# なければ INSERT
POST /rest/v1/edinet_order_data
```

---

## 7. commit hash

| リポジトリ | commit | 内容 |
|---|---|---|
| `tdnet-excel-input` | `01969ad` | feat: add edinet_orders module + run_edinet_orders.py pipeline |

---

## 8. 次に実施すべき作業

| # | 作業 | 状態 |
|---|---|---|
| ✅ | 009 migration 実行（edinet_order_data 作成） | 完了 |
| ✅ | RLS SELECT policy 設定 | 完了 |
| ✅ | INSERT DRY RUN v2（period=YYYY-MM-DD 修正） | 完了 |
| ✅ | 5社 INSERT テスト | 完了 |
| ✅ | 正式モジュール化（src/edinet_orders/） | 完了 |
| ✅ | 31社 DB 保存（UPDATE 31件確認） | 完了 |
| 🔲 | **API設計**（Viewer連携前の設計レビュー） | 未着手 |
| 🔲 | Viewer 連携（edinet_order_data 表示） | 未着手 |
| 🔲 | 抽出精度改善（extractor.py リファイン） | 未着手 |
| 🔲 | キーローテーション | 未着手 |

> [!IMPORTANT]
> **次はViewer連携前にAPI設計を行うこと。**  
> Viewer から `edinet_order_data` を参照するためのエンドポイント設計・RLS 動作確認・
> クエリ設計（銘柄別・年度別フィルタ等）を先に決定してから実装に進む。

> [!NOTE]
> **抽出精度について**  
> 正式モジュール（`src/edinet_orders/extractor.py`）は scratch 版より汎化されているため、
> 再抽出すると一部銘柄の confidence や unit が異なる場合がある。  
> 現在は `--from-json scratch/orders_extracted_30_v4.json` で検証済み v4 データを使用。  
> 抽出精度の改善は extractor.py を別途リファインして対応する。
