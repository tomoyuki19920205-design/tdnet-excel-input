# スキーマ棚卸し・要整理箇所 (Schema TODO)

現行スキーマと想定スキーマのギャップ、命名揺れ、責務曖昧テーブルの棚卸し。

## 1. 現在あるテーブル

### decision_db.db (30.9 MB) — Normalized / Serving 混在

| テーブル | 行数 | 層 | 責務 | 状態 |
|:---|---:|:---|:---|:---|
| `quarterly_results` | 91,652 | Normalized | PL 実績 (sales, gp, op, oi, ni 等) | ✅ 正常運用 |
| `segment_financials` | 94,063 | Normalized | セグメント別 (sales, profit) | ✅ 正常運用 |
| `company_memos` | 1,139 | Normalized | 会社別メモ | ✅ 正常運用 |
| `quarterly_notes` | 19,572 | Normalized | 四半期付帯テキスト | ✅ |
| `filing_diff_summaries` | 40 | Normalized | diff 検出サマリ | ✅ |
| `audit_log` | 239 | Meta | 操作ログ | ✅ |
| `documents` | 7 | Normalized | 文書メタ | ⚠️ 少量・用途不明瞭 |
| `extracted_facts` | 0 | Normalized | 抽出 fact 候補 | ⚠️ 空・未使用? |
| `migration_log` | 0 | Meta | DB マイグレーション | ⚠️ 空 |
| `order_metrics` | 0 | Normalized | 受注指標 | ⚠️ 空・未使用? |
| `quarantine` | 0 | Normalized | quarantine (decision_db内) | ⚠️ 空 (quarantine.db と重複?) |

### jquants.db (240.7 MB) — Raw

| テーブル | 行数 | 層 | 責務 |
|:---|---:|:---|:---|
| `jquants_financials_normalized` | 87,157 | Raw/Normalized | J-Quants 正規化済み決算 |

### quarantine.db (0.2 MB) — Normalized

| テーブル | 行数 | 層 | 責務 |
|:---|---:|:---|:---|
| `quarantine` | 769 | Normalized | 抽出失敗・要確認行 |

### xbrl.db (0.1 MB) — Normalized (テスト用)

| テーブル | 行数 | 層 | 責務 |
|:---|---:|:---|:---|
| `companies` | 10 | Master | 企業マスタ |
| `disclosures` | 10 | Meta | 開示メタ |
| `facts` | 20 | Normalized | XBRL fact |
| `periods` | 10 | Dimension | 会計期間 |
| (他 5テーブル) | 0 | — | schema.sql 定義のみ |

### data/decision_db.db (0.02 MB) — Normalized

| テーブル | 行数 | 層 | 責務 |
|:---|---:|:---|:---|
| `buyback_events` | 0 | Normalized | 自社株買いイベント |

### state.db (0.05 MB) — Raw (meta)

| テーブル | 行数 | 層 | 責務 |
|:---|---:|:---|:---|
| `processing_log` | 112 | Meta | TDnet 取得処理ログ |

---

## 2. 役割が曖昧なテーブル

> [!WARNING]
> 以下のテーブルは責務が不明瞭。整理が必要。

| テーブル | DB | 問題点 | 推奨 |
|:---|:---|:---|:---|
| `documents` | decision_db | 7行のみ。filing_artifacts の代替? | 用途確認 → 不要なら廃止 |
| `extracted_facts` | decision_db | 空。schema.sql の facts と重複? | 用途確認 → 不要なら廃止 |
| `order_metrics` | decision_db | 空。KPI 的だが未使用 | 将来要件なら残す、なければ廃止 |
| `quarantine` (decision_db内) | decision_db | quarantine.db と重複 | quarantine.db に一本化 |
| `migration_log` | decision_db | 空。マイグレーション管理 | migrate_db.py と整合確認 |

---

## 3. Raw なのに Serving 的に使っているもの

| 対象 | 現状 | リスク | 推奨 |
|:---|:---|:---|:---|
| `jquants_financials_normalized` | `sync_financials.py` で直接 Supabase へ push | jquants.db が Raw と Normalized の混在 | 許容範囲だが認識しておく |

---

## 4. Serving なのに正本っぽく扱われているもの

| 対象 | 現状 | リスク | 推奨 |
|:---|:---|:---|:---|
| Supabase `financials` | viewer が直接クエリ | viewer 手修正が正本を汚染しうる | Supabase は配信先。正本は SQLite |
| `data.xlsx` / `data_jquants.xlsx` | Excel export | 手修正されたら逆流の恐れ | 派生物扱いを徹底 |

---

## 5. 一時データなのに長期保存しているもの

| 対象 | 場所 | 問題 | 推奨 |
|:---|:---|:---|:---|
| `artifacts/` 配下の CSV/JSONL | artifacts/ | 累積する | 定期削除 (30日) |
| `data/backfill_review.csv` | data/ | backfill 一時出力 | 完了後削除 |
| `check_pl_output.txt` | root/ | debug 出力 | 削除 |
| `decision_db.db.bak_*` | root/ | バックアップ | 整理 (最新1本のみ保持) |

---

## 6. source 列の値揺れ

| 場所 | 現状の値例 | 問題 |
|:---|:---|:---|
| `quarterly_results.field_sources` | `summary_xbrl`, `attachment_xbrl`, `html`, `pdf_ocr` | 概ね統一 |
| `buyback_events.source_type` | `pdf`, `html`, `unknown` | OK |
| schema.sql `facts.quality` | `XBRL`, `IXBRL`, `PDF`, `MANUAL` | ALL CAPS |
| schema.sql `disclosures.source` | `TDNET`, `EDINET`, `MANUAL`, `OTHER` | ALL CAPS |

→ **SQLite 実運用テーブル (小文字) と schema.sql (大文字) で case が不統一**

---

## 7. quarter / period の持ち方揺れ

| 場所 | 表現 | 例 |
|:---|:---|:---|
| `quarterly_results` | `period`, `quarter` | `"2024-10-31"`, `"2Q"` |
| `jquants_financials_normalized` | `current_fiscal_year_end_date`, `type_of_current_period` | `"2024-10-31"`, `"2Q"` |
| schema.sql `periods` | `fiscal_year_end`, `quarter` (INTEGER 1-4) | `"2025-12-31"`, `2` |
| viewer | R 期 / Q 表記 | `"R8/3 2Q"` |

→ **schema.sql は quarter=INTEGER(1-4) だが実運用は TEXT("1Q","2Q","3Q","FY")**
→ TEXT 表現を標準とし、FY (通期) を含む点で INTEGER 不適合

---

## 8. 「あとから検索したいかも」で膨らみそうな箇所

| 箇所 | リスク | 対策 |
|:---|:---|:---|
| PDF 本文テキスト | 1社あたり数10KB × 数千社 | Search 層へ分離 |
| XBRL tag 全量 | xbrl.db に全 fact 保存 | 必要 tag のみ抽出 |
| Discord 通知ログ | 通知本文蓄積 | 再生成可能なので破棄 |
| buyback scanner 候補全量 | candidate CSV 累積 | artifacts/ で短期保持 |

---

## 9. 同じ意味の数値を複数場所で持っている箇所

| 数値 | 場所1 | 場所2 | 場所3 |
|:---|:---|:---|:---|
| PL (sales, op 等) | `quarterly_results` | Supabase `financials` | `data.xlsx` |
| セグメント | `segment_financials` | Supabase `segment_financials` | `data.xlsx` |
| メモ | `company_memos` | Supabase `company_memos` | — |

→ 正本 = SQLite (`quarterly_results`, `segment_financials`, `company_memos`)
→ Supabase / Excel は派生 (sync で上書き)

---

## 10. 既存ドキュメントと実装のズレ

| ドキュメント | 内容 | 実装との差異 |
|:---|:---|:---|
| `schema.sql` | 理想的な XBRL ETL スキーマ | `xbrl.db` でのみ使用 (10行テスト)。実運用は `decision_db.db` |
| `schema_pg.sql` | Supabase 用変換 | 実際の Supabase テーブルとの整合未確認 |
| `ARCHITECTURE_FINAL.md` | アーキテクチャ概要 | 実装の進展が反映されていない可能性 |
| `ARCHITECTURE_GAP.md` | ギャップ分析 | 一部解消済み? |

---

## 11. 未実装だが将来必要なテーブル

| テーブル | 層 | 用途 |
|:---|:---|:---|
| `document_text` | Search | 全文検索用 |
| `search_tokens` / `keywords` | Search | キーワード検索用 |
| `embeddings` | Search | ベクトル検索用 |
| `extraction_audit` | Normalized | 抽出品質ログ |
| `dividend_events` | Normalized | 配当イベント |
| `split_events` | Normalized | 株式分割イベント |

---

## 12. 命名揺れ整理候補

| 現状 | 統一案 | 補足 |
|:---|:---|:---|
| `ticker_code` / `ticker` / `local_code` | `ticker` (4桁) | `local_code` は J-Quants 5桁用 |
| `disclosure_id` / `doc_id` / `filing_id` | `doc_id` (TDnet) | DB 内部は `disclosure_id` |
| `period` / `fiscal_year_end` / `current_fiscal_year_end_date` | `fiscal_year_end` | period は曖昧 |
| `quarter` (INT) / `quarter` (TEXT) / `type_of_current_period` | `quarter` (TEXT: 1Q/2Q/3Q/FY) | FY 含むため TEXT |
| `source` (ALL CAPS) / `source_type` (lowercase) | `source_type` (lowercase) | |
| `created_at` / `fetched_at` / `parsed_at` | 役割別に使い分け | |
