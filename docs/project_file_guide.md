# Project Script Guide

プロジェクト内の主要スクリプト・ツール・バッチファイルの役割一覧。
カテゴリ別に整理し、各ファイルの **Purpose / Used when / Typical command / Notes** を記載する。

> **更新日**: 2026-03-11
> **対象外**: `tests/`, `__pycache__/`, `.venv/`, `.git/`, ビルド成果物

---

## 目次

1. [Pipeline — メイン実行](#pipeline--メイン実行)
2. [Extraction — 抽出エンジン](#extraction--抽出エンジン)
3. [Buyback Events — 自社株買いイベント](#buyback-events--自社株買いイベント)
4. [Database & Migration — DB 管理](#database--migration--db-管理)
5. [Cloud Sync — Supabase / J-Quants 同期](#cloud-sync--supabase--j-quants-同期)
6. [Backfill — 過去データ補完](#backfill--過去データ補完)
7. [Notification — 通知 / アラート](#notification--通知--アラート)
8. [Excel — Excel 連携](#excel--excel-連携)
9. [Maintenance — 保守 / クリーンアップ](#maintenance--保守--クリーンアップ)
10. [Debug / Peek — デバッグ / 確認](#debug--peek--デバッグ--確認)
11. [Batch / Shell — バッチスクリプト](#batch--shell--バッチスクリプト)
12. [src/ — コアモジュール (CLI 的役割)](#src--コアモジュール-cli-的役割)
13. [Legacy](#legacy)

---

## Pipeline — メイン実行

### tools/pipeline_run.py
- **Purpose**: パイプラインのメインエントリポイント。全ステップ実行またはサブコマンド
- **Used when**: 日次定期実行、手動一括、サブコマンド単体実行時
- **Steps**: ingest → process (Supabase push + J-Quants sync) → rebuild serving views → notify
- **Typical command**:
  ```
  python tools/pipeline_run.py              # 全ステップ
  python tools/pipeline_run.py ingest
  python tools/pipeline_run.py process
  python tools/pipeline_run.py rebuild
  python tools/pipeline_run.py notify
  python tools/pipeline_run.py reconcile
  python tools/pipeline_run.py retry-failed
  python tools/pipeline_run.py backfill --from YYYY-MM-DD --to YYYY-MM-DD
  python tools/pipeline_run.py --dry-run
  python tools/pipeline_run.py --trigger scheduler
  ```
- **Notes**:
  - サブコマンド省略時は全ステップ (legacy 互換)
  - `--trigger scheduler` で同時実行防止 (pipeline_runs チェック)
  - Exit: 0=OK, 1=ingest/process失敗, 2=rebuild失敗, 3=reconcile重大異常

### tools/tdnet_ingest.py
- **Purpose**: TDnet API から当日の開示を取得し、抽出 → SQLite 保存まで一気通貫で実行
- **Used when**: 日次のTDnetデータ取得時。pipeline_run.py から内部呼出しされる
- **Typical command**:
  ```
  python tools/tdnet_ingest.py
  python tools/tdnet_ingest.py --company-code 0812
  python tools/tdnet_ingest.py --dry-run
  python tools/tdnet_ingest.py --replay data/docs/test.zip
  ```
- **Notes**: PL抽出 + セグメント抽出 + 受注メトリクス抽出を同時実行。`--replay` でローカルZIPからのオフライン検証可能

### tools/filings_ingest.py
- **Purpose**: `tdnet_ingest.run_ingest()` の薄ラッパー。pipeline_run.py から import される
- **Used when**: pipeline_run.py 経由での呼び出し
- **Notes**: CLI ロジックと分離するためのモジュール

### tools/filings_process.py
- **Purpose**: SQLite → Supabase push + J-Quants financial sync
- **Used when**: pipeline_run.py の Step 2 として呼ばれる
- **Typical command**:
  ```
  python tools/filings_process.py --dry-run
  python tools/filings_process.py --skip-jquants
  ```

---

## Extraction — 抽出エンジン

### tools/extract_pdf.py
- **Purpose**: PDF 形式 IR 文書からテーブル数値を抽出（OCR 不要）
- **Used when**: 決算説明資料 / 補足資料 / セグメント / KPI の PDF 抽出
- **Typical command**:
  ```
  python tools/extract_pdf.py --db decision_db.db
  python tools/extract_pdf.py --doc-type presentation --limit 5
  ```

### tools/extract_html.py
- **Purpose**: HTML 形式の開示資料から表データを抽出
- **Used when**: 業績修正 / 月次速報 / KPI の HTML 抽出
- **Typical command**:
  ```
  python tools/extract_html.py --db decision_db.db
  python tools/extract_html.py --doc-type forecast_revision --limit 5
  ```

### tools/extract_to_json.py
- **Purpose**: 抽出結果を JSON 形式で出力
- **Used when**: 抽出結果の外部連携 / デバッグ時

### tools/classify_documents.py
- **Purpose**: 取得済み文書の分類（決算短信 / 修正 / 説明資料 etc.）
- **Used when**: 文書タイプの自動判定が必要な時

### tools/fetch_documents.py
- **Purpose**: TDnet から文書ファイルをダウンロード
- **Used when**: 手動での文書取得時

### tools/ixbrl_probe.py
- **Purpose**: iXBRL / XBRL ファイルの内部構造を調査
- **Used when**: 新しいXBRLタグの調査 / 抽出ルール策定時

### tools/normalize_extracted_facts.py
- **Purpose**: extracted_facts テーブルのデータ正規化
- **Used when**: 抽出済みファクトの一括正規化

### tools/reextract_all_zips.py
- **Purpose**: data/docs 配下のZIPを全件再抽出
- **Used when**: 抽出ロジック変更後の一括再適用

---

## Buyback Events — 自社株買いイベント

### tools/extract_buyback_events.py
- **Purpose**: TDnet 開示テキストから自社株買い関連イベントを抽出・構造化
- **Used when**: 自社株買い decision / status / result / cancel の抽出
- **Typical command**:
  ```
  python tools/extract_buyback_events.py --input data/docs/sample.pdf
  ```

### tools/find_buyback_candidate_docs.py
- **Purpose**: data/docs 配下の PDF から自社株買い候補文書をキーワード検索で高速抽出
- **Used when**: review 前段のスクリーニング。大量 PDF から buyback 候補を粗選別
- **Typical command**:
  ```
  python tools/find_buyback_candidate_docs.py --input-dir data/docs --recursive --output-dir artifacts/buyback_candidates
  ```
- **Outputs**: `buyback_candidates.csv`, `candidate_manifest.csv`, `candidate_summary.md`

### tools/review_buyback_extraction.py
- **Purpose**: buyback classifier / extractor の実データ一括検証ツール
- **Used when**: 抽出精度の検証、候補スキャン結果の batch review
- **Typical command**:
  ```
  python tools/review_buyback_extraction.py --input-dir data/docs --recursive
  python tools/review_buyback_extraction.py --manifest artifacts/buyback_candidates/candidate_manifest.csv --only-manifest-files
  ```
- **Outputs**: `review_buyback_results.csv`, `review_summary.md`, `review_low_confidence.csv`

---

## Database & Migration — DB 管理

### tools/migrate_db.py
- **Purpose**: SQLite スキーマのマイグレーション実行
- **Used when**: スキーマ更新時

### tools/load_results_to_db.py
- **Purpose**: JSON 抽出結果を SQLite に一括ロード
- **Used when**: 抽出結果の DB 保存

### tools/refresh_pl_view.py
- **Purpose**: PL ビューの再構築
- **Used when**: DB ビュー更新時

### schema.sql
- **Purpose**: SQLite スキーマ定義（DDL）
- **Notes**: decision_db.db のテーブル構造定義

### schema_pg.sql
- **Purpose**: PostgreSQL (Supabase) スキーマ定義
- **Notes**: Supabase 側のテーブル構造定義

---

## Cloud Sync — Supabase / J-Quants 同期

### sqlite_to_supabase.py (root)
- **Purpose**: SQLite quarterly_results / segment → Supabase financials / segment_financials に push
- **Used when**: pipeline_run.py 経由、または手動での Supabase push
- **Typical command**:
  ```
  python sqlite_to_supabase.py
  ```
- **Notes**: root にある（tools/ の sqlite_to_supabase.py とは異なる可能性あり）

### tools/sqlite_to_supabase.py
- **Purpose**: tools/ 版の SQLite → Supabase push
- **Used when**: filings_process.py から呼ばれる

### tools/supabase_loader.py
- **Purpose**: XBRL 抽出 JSON → Supabase PostgREST API で直接ロード
- **Used when**: JSON → Supabase の直接投入
- **Typical command**:
  ```
  python -m tools.supabase_loader --input results/
  ```

### tools/sync_financials.py
- **Purpose**: SQLite jquants_financials_normalized → Supabase public.financials 同期
- **Used when**: J-Quants データの Supabase 反映
- **Typical command**:
  ```
  python tools/sync_financials.py --apply
  python tools/sync_financials.py --apply --full
  ```

### tools/sync_segments.py
- **Purpose**: XBRL ZIP + SQLite(excel_legacy) -> Supabase segment_canonical 同期
- **Used when**: セグメントデータの Supabase 反映
- **Typical command**:
  ```
  python tools/sync_segments.py --apply
  python tools/sync_segments.py --dry-run
  python tools/sync_segments.py --apply --xbrl-only    # XBRL のみ (非推奨)
  ```
- **Notes**:
  - **デフォルトで XBRL + SQLite(excel_legacy) の両方を sync**
  - `--xbrl-only` を明示指定した場合のみ SQLite を除外
  - `--xbrl-only` 使用時に SQLite valid rows があれば警告が出る
  - 旧 `--include-sqlite` は後方互換で受け付けるが不要 (デフォルトで含まれる)
- **Key flags**:
  - `--apply`: Supabase 実書き込み
  - `--dry-run`: 書き込みなし (確認用)
  - `--xbrl-only`: XBRL のみ sync (excel_legacy を意図的に除外する場合)
  - `--db`: SQLite DB パス (default: `decision_db.db`)
  - `--source-dir`: XBRL ZIP ディレクトリ (default: `data/docs`)

### tools/audit_supabase_vs_sqlite.py
- **Purpose**: Supabase と SQLite のデータ不整合を検出する監査ツール
- **Used when**: データ整合性チェック

---

## Backfill — 過去データ補完

### tools/backfill_filings.py
- **Purpose**: 日付ループ方式で過去の開示を一括バックフィル
- **Used when**: 過去期間のデータ補完
- **Typical command**:
  ```
  python tools/backfill_filings.py --from 2025-01-01 --to 2025-12-31
  python tools/backfill_filings.py --from 2025-01-01 --to 2025-12-31 --dry-run
  ```
- **Notes**: 失敗日を記録しサマリ表示。再開可能

### tools/backfill_segments.py
- **Purpose**: 過去の決算短信 PDF からセグメントデータを再抽出
- **Used when**: セグメント抽出ロジック改善後の一括再適用
- **Typical command**:
  ```
  python tools/backfill_segments.py --max-items 50
  python tools/backfill_segments.py --tickers 1801,7203,9619
  ```
- **Notes**: 進捗 JSON で再開可能

### tools/backfill_source_doc_links.py
- **Purpose**: 既存レコードに source_doc_id / source_url を後付けで補完
- **Used when**: ソース文書リンクの一括補完

### tools/reprocess_ticker.py
- **Purpose**: 特定 ticker の既処理フラグクリア → 再 ingest → Supabase push
- **Used when**: 誤データ修正、抽出ロジック変更後の特定銘柄再処理
- **Typical command**:
  ```
  python tools/reprocess_ticker.py 2301
  python tools/reprocess_ticker.py 2301 --dry-run --skip-push
  ```

---

## Notification — 通知 / アラート

### tools/discord_alerts.py
- **Purpose**: TDNET 取込済み銘柄の YOY/QoQ をチェックし閾値超で Discord 通知
- **Used when**: 決算取込後のアラート通知
- **Typical command**:
  ```
  python tools/discord_alerts.py
  python tools/discord_alerts.py --tickers 7203,6758
  python tools/discord_alerts.py --test
  ```
- **Notes**: AI 差分要約 + 決算スコア付き通知。重複防止ログあり

### tools/notify_updates.py
- **Purpose**: パイプライン実行結果の通知（pipeline_run.py Step 4）
- **Used when**: pipeline_run.py 経由

### tools/generate_filing_diff_summaries.py
- **Purpose**: 前回決算との差分 AI 要約を生成し filing_diff_summaries テーブルに保存
- **Used when**: Discord アラートの AI コメント用ソース生成

---

## Excel — Excel 連携

### generate_data_excel.py (root)
- **Purpose**: SQLite データから data.xlsx を生成
- **Used when**: Excel レポート更新時
- **Notes**: root と tools/ の両方にある

### tools/generate_data_excel.py
- **Purpose**: tools/ 版の data.xlsx 生成

### tools/excel_sync.py
- **Purpose**: DB ↔ Excel 双方向同期
- **Used when**: Excel 編集結果の DB 反映、DB → Excel 更新

### build_viewer_pq.py (root)
- **Purpose**: Viewer 用 Parquet / Excel ファイル構築

### diagnose_excel.py (root)
- **Purpose**: Excel ファイルの構造診断（シート / リンク / 名前定義）
- **Used when**: Excel ファイル破損 / 不整合の調査

### inject_external_link_from_template.py (root)
- **Purpose**: Viewer Excel に外部リンク（data.xlsx 参照）を注入
- **Used when**: Viewer Excel の初期セットアップ / リンク修復
- **Notes**: OOXML レベルで安全にリンクを挿入

### verify_data_xlsx_and_copy.py (root)
- **Purpose**: data.xlsx の検証とバックアップコピー

---

## Maintenance — 保守 / クリーンアップ

### tools/cleanup_intermediate_data.py
- **Purpose**: SQLite 中間データ（migration_log / quarantine / extracted_facts）一括削除
- **Used when**: DB サイズ削減、中間データ整理
- **Typical command**:
  ```
  python tools/cleanup_intermediate_data.py                              # dry-run
  python tools/cleanup_intermediate_data.py --execute --vacuum           # 削除 + VACUUM
  python tools/cleanup_intermediate_data.py --execute --include-audit-log
  ```

### tools/quarantine_review.py
- **Purpose**: Quarantine 統合レビュー（SQLite quarantine + JSONL review）
- **Used when**: 抽出失敗 / quarantine 件数の監視
- **Typical command**:
  ```
  python tools/quarantine_review.py --limit 50
  python tools/quarantine_review.py --ticker 7203
  python tools/quarantine_review.py --fail-on-items   # CI 用
  ```

### tools/show_quarantine.py
- **Purpose**: quarantine テーブルの簡易表示
- **Used when**: quarantine 状況の確認

### tools/push_guard.py
- **Purpose**: Supabase push 前のデータ整合性チェック
- **Used when**: push 前のガードレール

### tools/fix_2301_supabase.py
- **Purpose**: ticker 2301 の Supabase データ修正（一時的修正スクリプト）
- **Notes**: 個別修正用。通常は reprocess_ticker.py を使用

---

## Debug / Peek — デバッグ / 確認

以下のスクリプトは root に配置されており、DB / Excel の状態確認に使用する。

| ファイル | Purpose |
|:---|:---|
| `check_2590_exists.py` | DB に ticker 2590 が存在するか確認 |
| `check_data_xlsx_2590.py` | data.xlsx に 2590 のデータがあるか確認 |
| `check_data_xlsx_contains_259.py` | data.xlsx に 259x 系コードが含まれるか確認 |
| `check_data_xlsx_updated_today.py` | data.xlsx が当日更新されたか確認 |
| `check_db_2590.py` | decision_db.db で 2590 のレコードを確認 |
| `check_db_codes_today.py` | 当日処理された企業コード一覧 |
| `check_pl_in_db.py` | PL データの存在確認 |
| `check_pl_nulls.py` | PL データの NULL チェック |
| `check_today_results.py` | 当日の処理結果確認 |
| `check_today_with_source.py` | 当日結果 + ソース情報 |
| `check_viewer_sheets.py` | Viewer のシート構造確認 |
| `delete_state_codes.py` | state.db から特定コードを削除 |
| `normalize_company_code_sqlite.py` | SQLite 内の企業コード正規化 |
| `peek_bad_259.py` | 259x 系の異常データ調査 |
| `peek_company_code_types.py` | 企業コードの型 (int/str) 調査 |
| `peek_decision_latest.py` | decision_db.db の最新レコード確認 |
| `peek_real_state_db.py` | state.db の実データ確認 |
| `peek_state_db.py` | state.db の概要表示 |
| `peek_state_today.py` | state.db の当日レコード |
| `peek_tdnet_raw_items.py` | TDnet API 生レスポンスの確認 |
| `peek_viewer__data_head.py` | Viewer Excel のデータ先頭行 |
| `print_paths.py` | パス情報表示 |
| `show_latest_pl.py` | 最新 PL データ表示 |
| `show_pl_by_code.py` | 企業コード指定の PL 表示 |
| `show_today_codes.py` | 当日処理コード一覧 |

### Excel デバッグ

| ファイル | Purpose |
|:---|:---|
| `dump_external_link_targets.py` | Excel 外部リンクのターゲット一覧 |
| `fast_scan_viewer_xlsx_links.py` | Viewer Excel のリンク高速スキャン |
| `inspect_viewer_links_and_names.py` | Viewer のリンクと名前定義の調査 |
| `link_viewer_to_data.py` | Viewer → data.xlsx のリンク設定 |
| `list_viewer_links.py` | Viewer のリンク一覧 |
| `make_viewer_linked.py` | Viewer にリンクを設定 |
| `scan_company_view_for_data_link.py` | Viewer のデータリンク走査 |
| `scan_tmp_links.py` | 一時リンクのスキャン |
| `scan_viewer_xlsx_mode.py` | Viewer の Excel モード確認 |
| `verify_tmp_zip.py` | 一時 ZIP の検証 |
| `verify_viewer_zip.py` | Viewer ZIP の検証 |

---

## Batch / Shell — バッチスクリプト

| ファイル | Purpose | Typical command |
|:---|:---|:---|
| `start_tdnet.bat` | TDnet パイプライン起動用バッチ | ダブルクリック or `start_tdnet.bat` |
| `run_ingest.ps1` | PowerShell 版 ingest 実行 | `.\run_ingest.ps1` |
| `run_extract_ir_docs.bat` | IR 文書抽出バッチ | `run_extract_ir_docs.bat` |
| `run_push_all.bat` | 全データ Supabase push | `run_push_all.bat` |
| `run_cleanup_dry_run.bat` | 中間データ cleanup (dry-run) | `run_cleanup_dry_run.bat` |
| `run_cleanup_execute_vacuum.bat` | 中間データ cleanup + VACUUM | `run_cleanup_execute_vacuum.bat` |
| `run_cleanup_menu.bat` | cleanup メニュー表示 | `run_cleanup_menu.bat` |
| `probe_ixbrl.bat` | iXBRL 調査バッチ | `probe_ixbrl.bat` |
| `データ取りそこないログ.bat` | データ取得失敗ログ確認 | ダブルクリック |

### legacy/batch_scripts/

| ファイル | Purpose |
|:---|:---|
| `run_generate_data.bat` | data.xlsx 生成（旧版） |
| `run_quick.bat` | クイック実行（旧版） |
| `run_update_all.bat` | 全更新実行（旧版） |

---

## src/ — コアモジュール (CLI 的役割)

以下は `src/` 内のモジュールだが、CLI エントリポイントとしても機能するもの。

| モジュール | Purpose |
|:---|:---|
| `src/cli.py` | メイン CLI エントリポイント |
| `src/main.py` | アプリケーションメインエントリ |
| `src/config.py` | config.yaml の読み込み・設定管理 |
| `src/db.py` | StateDB (state.db) 管理 |
| `src/downloader.py` | TDnet 文書ダウンローダー |
| `src/extractor.py` | PL / セグメント / 受注メトリクス抽出のメインロジック |
| `src/fetcher.py` | TDnet API からの開示一覧取得 |
| `src/excel_writer.py` | Excel 出力ライター |
| `src/persist_policy.py` | 中間データ永続化ポリシー管理 |
| `src/common_ticker.py` | ticker 正規化ユーティリティ |

### src/events/ — イベント抽出エンジン

| モジュール | Purpose |
|:---|:---|
| `src/events/buyback_classifier.py` | 自社株買い文書の分類器 |
| `src/events/buyback_extractor.py` | 自社株買いイベントの数値抽出 |
| `src/events/buyback_models.py` | 自社株買いイベントのデータモデル |
| `src/events/buyback_storage.py` | 自社株買いイベントの DB 保存 |

### src/migration/ — マイグレーション

| モジュール | Purpose |
|:---|:---|
| `src/migration/migration_db.py` | MigrationDB (decision_db.db) の全操作 |
| `src/migration/migrator.py` | スキーママイグレーション実行 |
| `src/migration/excel_parser.py` | Excel → DB 移行パーサー |

### src/normalization/ — データ正規化

| モジュール | Purpose |
|:---|:---|
| `src/normalization/normalize_field.py` | フィールド値正規化 |
| `src/normalization/merge_fields.py` | 複数ソースのフィールドマージ |
| `src/normalization/provenance_rules.py` | データ出自ルール |

### src/filing_diff/ — 開示差分解析

| モジュール | Purpose |
|:---|:---|
| `src/filing_diff/ai_summary.py` | AI による差分要約生成 |
| `src/filing_diff/section_diff.py` | セクション単位の差分抽出 |
| `src/filing_diff/text_extractor.py` | 開示テキスト抽出 |
| `src/filing_diff/previous_doc_resolver.py` | 前回開示文書の特定 |

---

## Legacy

`legacy/` 配下は旧バージョンのスクリプト群。

| ディレクトリ | Purpose |
|:---|:---|
| `legacy/batch_scripts/` | 旧バッチスクリプト群 |
| `legacy/diagnostics/` | 旧診断ツール (`check_clean_view.py`, `debug_quarantine.py`) |
| `legacy/excel_tools/` | 旧 Excel ツール (`xlsx_stats.py`) |

---

## サマリ統計

| カテゴリ | ファイル数 |
|:---|---:|
| Pipeline | 4 |
| Extraction | 8 |
| Buyback Events | 3 |
| Database & Migration | 5 |
| Cloud Sync | 6 |
| Backfill | 4 |
| Notification | 3 |
| Excel | 8 |
| Maintenance | 5 |
| Debug / Peek | 30+ |
| Batch / Shell | 12 |
| src/ (CLI 的) | 20+ |
| Legacy | 5 |
| **合計** | **100+** |

> **手動補完推奨**: Debug / Peek セクションのスクリプトの多くは単発調査用に作られたもので、不要なものは削除を検討してよい。
